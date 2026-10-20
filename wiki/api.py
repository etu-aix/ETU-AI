"""Local-administrator-only API; long operations always return durable job IDs."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from integration.api import admin
from wiki.external import location, launch
from wiki.store import Conflict


class DocumentEdit(BaseModel):
    path: str = Field(max_length=220)
    content: str = Field(default="", max_length=256000)
    expected_hash: str | None = None


class Restore(BaseModel):
    path: str
    revision: int
    expected_hash: str | None = None


def router_for(service):
    def authorize(request: Request):
        admin(request)

    router = APIRouter(prefix="/api/wiki", dependencies=[Depends(authorize)])

    def handle(operation):
        try:
            return operation()
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, OSError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/settings")
    def settings():
        return service.settings()

    @router.put("/settings")
    def configure(patch: dict):
        return handle(lambda: service.configure(patch))

    @router.get("/documents")
    def documents(q: str = Query(default="", max_length=200)):
        service.sync()
        return {"documents": service.store.documents(q)}

    @router.get("/location")
    def wiki_location():
        return location(service.store.root)

    @router.post("/open-folder")
    def open_folder():
        return handle(lambda: (launch(str(service.store.root)), {"opened": True})[1])

    @router.post("/open-obsidian")
    def open_obsidian():
        def operation():
            info = location(service.store.root)
            if not info["obsidian_available"]:
                raise ValueError("Obsidian n’est pas installé ou son protocole n’est pas enregistré")
            if not info["obsidian_ready"]:
                raise ValueError("Ouvrez d’abord ce dossier comme coffre dans Obsidian")
            launch(info["obsidian_uri"])
            return {"opened": True}
        return handle(operation)

    @router.get("/deleted")
    def deleted():
        return {"documents": service.store.deleted()}

    @router.get("/document")
    def read(path: str):
        service.sync()
        return handle(lambda: service.store.read(path))

    @router.put("/document")
    def write(body: DocumentEdit):
        result = handle(lambda: service.store.write(body.path, body.content, body.expected_hash))
        if service.settings()["embedding_model"]:
            service.queue.enqueue("embed", {}, 5, "embed:" + service.settings()["embedding_model"])
        return result

    @router.delete("/document")
    def delete(body: DocumentEdit):
        return handle(lambda: service.store.write(body.path, "", body.expected_hash, delete=True))

    @router.get("/history")
    def history(path: str):
        return {"revisions": handle(lambda: service.store.history(path))}

    @router.post("/restore")
    def restore(body: Restore):
        result = handle(lambda: service.store.restore(body.path, body.revision, body.expected_hash))
        if service.settings()["embedding_model"]:
            service.queue.enqueue("embed", {}, 5, "embed:" + service.settings()["embedding_model"])
        return result

    @router.get("/search")
    def search(q: str = Query(max_length=2000)):
        # Manual inspection is always lexical and never starts an inference.
        service.sync()
        return {"sources": service.retriever().search(q, service.settings()["documents"])}

    @router.get("/graph")
    def graph(q: str = Query(default="", max_length=200)):
        service.sync()
        return service.store.graph(q)

    @router.get("/events/{conversation}")
    def events(conversation: str):
        return {"events": service.store.events(conversation)}

    @router.get("/jobs")
    def jobs():
        return service.queue.jobs()

    @router.post("/jobs/run", status_code=202)
    def run():
        service.queue.force = True
        return {"status": "requested", "message": "Lancement demandé ; priorité au chat et aux ressources"}

    @router.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return service.queue.cancel(job_id)

    @router.post("/jobs/{job_id}/approve", status_code=202)
    def approve(job_id: str):
        handle(lambda: service.approve(job_id))
        return {"job_id": job_id, "status": "pending"}

    @router.post("/jobs/{kind}", status_code=202)
    def schedule(kind: str):
        if kind not in {"index", "embed"}:
            raise HTTPException(422, "Traitement inconnu")
        return {"job_id": service.queue.enqueue(kind, {}, priority=3, dedupe="explicit:" + kind), "status": "pending"}

    return router
