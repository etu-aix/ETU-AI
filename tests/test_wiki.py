"""Wiki storage, retrieval, policy, queue recovery and resource gates."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from storage.db import StorageManager
from wiki.store import WikiStore, Conflict
from wiki.retrieval import SQLiteRetriever, context
from wiki.service import WikiService, safe_fact
from wiki.frontmatter import properties
from wiki.external import location


class WikiCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WikiStore(self.root / "wiki")

    def tearDown(self):
        self.temp.cleanup()

    def test_markdown_edits_conflicts_history_and_restore_delete(self):
        first = self.store.write("Projets/ETU.md", "# ETU\nArchitecture SQLite.", None)
        with self.assertRaises(Conflict):
            self.store.write("Projets/ETU.md", "écrasement", None)
        second = self.store.write("Projets/ETU.md", "# ETU\nArchitecture Markdown.", first["hash"])
        history = self.store.history("Projets/ETU.md")
        self.assertEqual(len(history), 2)
        restored = self.store.restore("Projets/ETU.md", history[-1]["id"], second["hash"])
        self.assertEqual(restored["hash"], first["hash"])
        self.store.write("Projets/ETU.md", "", first["hash"], delete=True)
        self.assertFalse((self.store.root / "Projets/ETU.md").exists())
        self.store.restore("Projets/ETU.md", history[-1]["id"], None)
        self.assertEqual(self.store.read("Projets/ETU.md")["hash"], first["hash"])

    def test_path_traversal_and_invalid_names(self):
        for path in ("../outside.md", "/outside.md", "a/../../outside.md", "C:/outside.md", "a\\evil.md", ".hidden/test.md", "test.txt", "x.md:stream", "a?/b.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.store.write(path, "text", None)

    def test_symlink_escape(self):
        target = self.root / "external"
        target.mkdir()
        try:
            (self.store.root / "link").symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Création de liens indisponible sur cette machine")
        with self.assertRaises(ValueError):
            self.store.write("link/test.md", "text", None)

    def test_search_without_embeddings_and_stale_file(self):
        self.store.write("Architecture.md", "# Architecture\nLa mémoire utilise SQLite avec des fichiers Markdown.", None)
        retriever = SQLiteRetriever(self.store)
        results = retriever.search('mémoire SQLite " OR *')
        self.assertEqual(results[0]["path"], "Architecture.md")
        self.assertIn("mémoire", results[0]["text"])
        (self.store.root / "Architecture.md").write_text("Remplacé hors ETU", encoding="utf-8")
        self.assertEqual(retriever.search("SQLite"), [])
        self.store.reconcile()
        self.assertEqual(retriever.search("SQLite"), [])

    def test_no_fts_fallback(self):
        self.store.fts = False
        self.store.write("Test.md", "Architecture SQLite locale", None)
        self.assertEqual(SQLiteRetriever(self.store).search("SQLite")[0]["path"], "Test.md")

    def test_budget_is_bounded_for_unicode(self):
        text, used = context([{"path": "École.md", "offset": 0, "text": "école 日本語 " * 1000}], 300)
        self.assertLessEqual(len(text.encode("utf-8")), 300)
        self.assertTrue(used)

    def test_links_resolve_paths_titles_and_ambiguity(self):
        self.store.write("Projets/ETU.md", "# ETU\n[[Stockage]] [[Tech/DB|base]] [[Fantôme]]", None)
        self.store.write("Tech/DB.md", "# Stockage\nSQLite", None)
        graph = self.store.graph()
        self.assertEqual(graph["edges"], [{"source": "Projets/ETU.md", "target": "Tech/DB.md"}])
        self.store.write("Autre/DB.md", "# Stockage\nAutre base", None)
        self.assertEqual(len(self.store.graph()["edges"]), 1)

    def test_obsidian_frontmatter_aliases_and_nested_markdown(self):
        content = ('---\n'
                   'title: "Études"\n'
                   'aliases:\n  - "BUT Info"\n  - Informatique\n'
                   'tags: [formation, local]\n'
                   'custom:\n  nested: kept\n'
                   '---\n# BUT informatique\nLa formation a lieu à [[Villes/Aix-en-Provence|Aix]].\n')
        self.store.write("Études/BUT.md", content, None)
        self.store.write("Villes/Aix-en-Provence.md", "# Aix-en-Provence\n", None)
        self.store.write("Notes/Parcours.md", "# Parcours\n[[BUT Info]] et [[Études/BUT|formation]]\n", None)
        doc = self.store.read("Études/BUT.md")
        self.assertEqual(doc["content"], content)
        self.assertEqual(doc["frontmatter"]["aliases"], ["BUT Info", "Informatique"])
        self.assertEqual(doc["frontmatter"]["tags"], ["formation", "local"])
        self.assertEqual(len(self.store.graph()["edges"]), 2)
        self.assertEqual(self.store.documents("BUT Info")[0]["path"], "Études/BUT.md")
        self.assertIn("Études/BUT.md", [result["path"] for result in SQLiteRetriever(self.store).search("formation")])
        self.assertEqual(properties("# No frontmatter\n---\n")[0], {})

    def test_external_edit_reindexes_and_stale_save_conflicts(self):
        first = self.store.write("Notes/Projet.md", "# Projet\n[[Ancien]]\n", None)
        (self.store.root / "Notes/Projet.md").write_text("---\naliases:\n  - Nouveau projet\n---\n# Projet\n[[Nouveau]]\n", encoding="utf-8")
        self.store.sync_if_due(0)
        self.assertEqual(self.store.read("Notes/Projet.md")["frontmatter"]["aliases"], ["Nouveau projet"])
        self.assertEqual(self.store.documents("Nouveau projet")[0]["path"], "Notes/Projet.md")
        self.assertEqual(SQLiteRetriever(self.store).search("Nouveau")[0]["path"], "Notes/Projet.md")
        self.assertEqual(self.store.history("Notes/Projet.md")[0]["origin"], '{"type": "filesystem"}')
        with self.assertRaises(Conflict):
            self.store.write("Notes/Projet.md", "# Écrasement", first["hash"])
        (self.store.root / "Notes/Projet.md").unlink()
        self.store.sync_if_due(0)
        self.assertEqual(self.store.documents(), [])

    def test_obsidian_handoff_uses_existing_vault_and_absolute_note_path(self):
        self.store.write("Cours/Aix et BUT.md", "# Cours\n", None)
        with patch("wiki.external.obsidian_available", return_value=True):
            self.assertFalse(location(self.store.root)["obsidian_ready"])
            (self.store.root / ".obsidian").mkdir()
            info = location(self.store.root)
        self.assertTrue(info["obsidian_ready"])
        self.assertTrue(info["obsidian_uri"].startswith("obsidian://open?path="))
        self.assertIn("Aix%20et%20BUT.md", info["obsidian_uri"])

    def test_memory_reuses_an_imported_obsidian_alias(self):
        self.store.write("Notions/aix en provence.md", "---\naliases:\n  - Aix\n---\n# Aix-en-Provence\n", None)
        service = WikiService(self.store.root, StorageManager(self.root / "chat.db"))
        self.assertEqual(service.notions_for("Je connais un café à Aix.", ["Aix"])[0]["path"],
                         "Notions/aix en provence.md")

    def test_incremental_index_preserves_vectors_and_history(self):
        self.store.write("Test.md", "# Architecture\nSQLite " * 500, None)
        with self.store.connect() as db:
            db.execute("UPDATE chunks SET vector='[1,0]',model='tiny'")
        self.store.reconcile()
        self.store.reconcile()
        self.assertEqual(len(self.store.history("Test.md")), 1)
        with self.store.connect() as db:
            self.assertGreater(db.execute("SELECT count(*) FROM chunks WHERE model='tiny'").fetchone()[0], 0)

    def test_hybrid_and_embedding_failure_fallback(self):
        self.store.write("Chat.md", "# Félins\nLes chats ronronnent.", None)
        self.store.write("SQL.md", "# SQL\nSQLite stocke la mémoire.", None)
        with self.store.connect() as db:
            db.execute("UPDATE chunks SET vector='[1,0]',model='resident' WHERE path='Chat.md'")
            db.execute("UPDATE chunks SET vector='[0,1]',model='resident' WHERE path='SQL.md'")
        class Embeddings:
            model = "resident"
            def embed(self, text): return [1, 0]
        results = SQLiteRetriever(self.store, Embeddings()).search("ronronnement")
        self.assertEqual(results[0]["path"], "Chat.md")
        with patch.object(Embeddings, "embed", side_effect=RuntimeError("unavailable")):
            self.assertEqual(SQLiteRetriever(self.store, Embeddings()).search("SQLite")[0]["path"], "SQL.md")


class QueueCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.storage = StorageManager(self.root / "chat.db")
        self.service = WikiService(self.root / "wiki", self.storage)
        self.service.configure({"min_free_mb": 0, "idle_seconds": 0, "auto_memory": True})
        self.conv = self.storage.create_conversation("Mon projet")

    async def asyncTearDown(self):
        await self.service.stop()
        self.temp.cleanup()

    def add(self, text):
        message = self.storage.add_message(self.conv, "user", text)
        return message, self.service.remember(self.conv, message, text)

    async def drain(self, count=10):
        for _ in range(count):
            if not await self.service.queue.tick(): break

    async def test_extraction_writes_provenance_and_deduplicates(self):
        _, job = self.add("Nous utilisons SQLite pour notre projet.")
        self.assertIsNotNone(job)
        await self.drain()
        docs = self.service.store.documents()
        self.assertEqual(len(docs), 1)
        self.assertIn("conversation", docs[0]["origin"])
        self.add("Nous utilisons SQLite pour notre projet.")
        await self.drain()
        self.assertEqual(len(self.service.store.history(docs[0]["path"])), 1)
        self.assertTrue(any(e["state"] == "saved" for e in self.service.store.events(self.conv)))

    async def test_explicit_note_request_saves_previous_safe_statement_when_auto_memory_is_off(self):
        self.service.configure({"auto_memory": False})
        fact = "Je m'appelle Maxence et je suis en BUT Informatique à Aix-en-Provence."
        self.assertIsNone(self.add(fact)[1])
        request, job = self.add("Tu peux l'enregistrer si tu veux comme ça tu t'en souviendras.")
        self.assertIsNotNone(job)
        await self.drain()
        docs = [doc for doc in self.service.store.documents() if doc["path"].startswith("Conversations/")]
        self.assertEqual(len(docs), 1)
        self.assertIn(fact, self.service.store.read(docs[0]["path"])["content"])
        saved = next(event for event in self.service.store.events(self.conv) if event["state"] == "saved")
        self.assertEqual(saved["message_id"], request)
        self.assertFalse(self.service.settings()["auto_memory"])

    async def test_direct_explicit_memory_request_saves_only_the_fact(self):
        self.service.configure({"auto_memory": False})
        _, job = self.add("Mémorise que je préfère Python pour les exemples de code.")
        self.assertIsNotNone(job)
        await self.drain()
        docs = [doc for doc in self.service.store.documents() if doc["path"].startswith("Conversations/")]
        self.assertEqual(len(docs), 1)
        content = self.service.store.read(docs[0]["path"])["content"]
        self.assertIn("je préfère Python pour les exemples de code", content)
        self.assertNotIn("Mémorise que", content)

    async def test_active_api_model_runs_memory_job_immediately_without_local_ram_gate(self):
        import httpx
        self.service.configure({"auto_memory": False, "queue_mode": "manual", "idle_seconds": 3600, "min_free_mb": 65536})
        self.storage.set_setting("api_providers", {"active_provider": "openai", "configs": {"openai": {
            "type": "openai", "base_url": "https://api.example/v1", "model": "currently-selected-model",
            "models": ["currently-selected-model", "wiki-task-model"]
        }}})
        self.service.configure({"task_model": "api:openai:wiki-task-model"})
        text = "Mémorise que je préfère Python pour les exemples de code."
        _, job_id = self.add(text)
        job = next(item for item in self.service.queue.jobs()["jobs"] if item["id"] == job_id)
        self.assertEqual(job["priority"], 3)
        self.assertTrue(job["payload"]["use_api"])
        requests = []

        async def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"facts": [{
                "title": "Préférences de code", "quote": "je préfère Python pour les exemples de code"
            }]})}}]})

        client_type = httpx.AsyncClient
        with patch("wiki.service.get_api_key", return_value="test-key"), patch(
            "wiki.service.httpx.AsyncClient",
            side_effect=lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs)
        ), patch.object(self.service, "resources", return_value=False):
            self.assertTrue(await self.service.queue.tick())
            self.assertTrue(await self.service.queue.tick())

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url, "https://api.example/v1/chat/completions")
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], "wiki-task-model")
        self.assertEqual(body["messages"][1]["content"], text)
        jobs = self.service.queue.jobs()["jobs"]
        self.assertEqual(next(item for item in jobs if item["id"] == job_id)["status"], "completed")
        self.assertTrue(any(item["kind"] == "write" and item["status"] == "completed" for item in jobs))

    async def test_notions_are_created_and_reused_across_conversations_without_embeddings(self):
        from unittest.mock import AsyncMock
        self.storage.set_setting("api_providers", {"active_provider": "example", "configs": {"example": {
            "type": "openai", "base_url": "https://api.example/v1", "model": "chat",
            "models": ["chat"]
        }}})
        self.service.configure({"task_model": "api:example:chat", "embedding_model": ""})
        facts = [
            [{"title": "Études", "quote": "je suis en BUT informatique", "notions": ["BUT informatique"]},
             {"title": "Lieu", "quote": "à aix en provence", "notions": ["aix en provence"]}],
            [{"title": "Café des Arts", "quote": "j'aime le Café des Arts à Aix-en-Provence",
              "notions": ["Café des Arts", "Aix-en-Provence"]}],
        ]
        with patch.object(self.service, "extract_with_api", new=AsyncMock(side_effect=facts)):
            self.add("Mémorise que je suis en BUT informatique à aix en provence.")
            await self.drain()
            first = self.service.store.graph()
            self.assertEqual(len(first["nodes"]), 3)
            self.assertEqual(len(first["edges"]), 2)
            self.add("Mémorise que j'aime le Café des Arts à Aix-en-Provence.")
            await self.drain()
        graph = self.service.store.graph()
        self.assertEqual(len(graph["nodes"]), 5)
        self.assertEqual(len(graph["edges"]), 4)
        city = [node for node in graph["nodes"] if node["id"] == "Notions/aix en provence.md"]
        self.assertEqual(len(city), 1)
        self.assertEqual(sum(edge["target"] == city[0]["id"] for edge in graph["edges"]), 2)
        self.assertIn("[[Notions/aix en provence|Aix-en-Provence]]",
                      self.service.store.read("Conversations/Café des Arts.md")["content"])
        self.assertFalse(any(job["status"] == "review" for job in self.service.queue.jobs()["jobs"]))

    async def test_model_proposal_creates_missing_link_targets_after_approval(self):
        fact = "BUT informatique à aix en provence"
        message = self.storage.add_message(self.conv, "user", fact)
        content = "# Études\n\n" + fact + "\n\n[[BUT informatique]] à [[aix en provence]]\n"
        proposal = self.service.propose("Conversations/Études.md", content,
                                        {"type": "model_proposal", "conversation": self.conv,
                                         "message_id": message, "quote": fact})
        self.assertEqual(proposal["status"], "review")
        self.assertEqual(self.service.store.documents(), [])
        self.service.approve(proposal["job_id"])
        await self.drain()
        graph = self.service.store.graph()
        self.assertEqual(len(graph["nodes"]), 3)
        self.assertEqual(len(graph["edges"]), 2)

    async def test_changed_fact_requires_review_and_stale_approval_refused(self):
        self.add("Nous utilisons SQLite pour notre projet.")
        await self.drain()
        original = self.service.store.read("Conversations/Mon projet.md")
        self.service.store.write(original["path"], "---\naliases:\n  - Décision base\n---\n" + original["content"], original["hash"])
        self.add("Nous utilisons PostgreSQL pour notre projet.")
        await self.drain()
        jobs = self.service.queue.jobs()["jobs"]
        review = next(j for j in jobs if j["status"] == "review")
        self.assertTrue(review["payload"]["content"].startswith("---\naliases:\n  - Décision base\n---\n"))
        doc = self.service.store.read(review["payload"]["path"])
        self.assertIn("SQLite", doc["content"])
        self.service.store.write(doc["path"], "# Modifié manuellement", doc["hash"])
        with self.assertRaises(Conflict):
            self.service.approve(review["id"])

    async def test_no_secret_uncertain_or_assistant_inference(self):
        for text in ("Retiens mon mot de passe : abcdef", "Je préfère peut-être Python pour ce projet.", "Retiens mon identifiant utilisateur max14", "Retiens ceci : sk-1234567890123456789012345678901234", "Je préfère écrire à moi@example.com"):
            self.assertIsNone(self.add(text)[1])
            self.assertFalse(safe_fact(text))
        message, _ = self.add("Une explication temporaire sans décision explicite.")
        with self.assertRaises(ValueError):
            self.service.propose("Fake.md", "# Faux\nCette architecture utilise Rust.", {"conversation": self.conv, "message_id": message, "quote": "Cette architecture utilise Rust."})
        await self.drain()
        self.assertEqual(self.service.store.documents(), [])

    async def test_queue_recovery_cancel_pause_and_manual_priority(self):
        queue = self.service.queue
        job = queue.enqueue("index", {}, 5)
        with self.service.store.connect() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
        other = WikiService(self.root / "wiki", self.storage)
        other.queue.recover()
        self.assertEqual(other.queue.jobs()["jobs"][0]["status"], "pending")
        self.service.configure({"paused": True})
        self.assertFalse(await queue.tick())
        self.service.configure({"paused": False, "queue_mode": "manual"})
        self.assertFalse(await queue.tick())
        queue.enqueue("index", {}, 3)
        self.assertTrue(await queue.tick())
        queue.cancel(job)
        self.assertEqual(next(j for j in queue.jobs()["jobs"] if j["id"] == job)["status"], "cancelled")

    async def test_chat_and_low_resources_defer_even_explicit_jobs(self):
        self.service.queue.enqueue("index", {}, 3)
        self.service.begin_chat()
        self.assertFalse(await self.service.queue.tick())
        self.service.end_chat()
        with patch.object(self.service, "resources", return_value=False):
            # The queue has a bound callback: patch its resource probe directly.
            with patch.object(self.service.queue, "resources", return_value=False):
                self.assertFalse(await self.service.queue.tick())
        self.assertTrue(await self.service.queue.tick())

    async def test_interrupt_resume_and_shutdown(self):
        entered = asyncio.Event()
        async def execute(job):
            entered.set()
            await asyncio.sleep(60)
        self.service.queue.execute = execute
        job = self.service.queue.enqueue("index", {}, 3)
        task = asyncio.create_task(self.service.queue.tick())
        await asyncio.wait_for(entered.wait(), 1)
        self.service.begin_chat()
        await asyncio.wait_for(task, 1)
        self.service.end_chat()
        self.assertEqual(self.service.queue.jobs()["jobs"][0]["status"], "pending")
        self.service.queue.execute = self.service.execute
        await self.service.queue.tick()
        self.assertEqual(next(j for j in self.service.queue.jobs()["jobs"] if j["id"] == job)["status"], "completed")
        await self.service.start()
        await asyncio.wait_for(self.service.stop(), 1)

    async def test_remote_consent_and_budget_and_disable(self):
        self.service.store.write("Architecture.md", "# Architecture\nSQLite est local. " * 80, None)
        self.assertEqual(await self.service.retrieve("Architecture SQLite", remote=True), ("", []))
        self.service.configure({"allow_remote": True, "rag_tokens": 300})
        text, sources = await self.service.retrieve("Architecture SQLite", remote=True)
        self.assertLessEqual(len(text.encode("utf-8")), 300)
        self.assertEqual(sources[0]["path"], "Architecture.md")
        self.service.configure({"enabled": False})
        self.assertEqual(await self.service.retrieve("Architecture SQLite"), ("", []))
        self.assertIsNone(self.add("Nous utilisons SQLite pour notre projet.")[1])

    async def test_loaded_model_guard_never_loads_models(self):
        from wiki.retrieval import LocalEmbeddings
        with patch("wiki.retrieval.httpx.Client") as client:
            client.return_value.__enter__.return_value.get.return_value.json.return_value = {"status": "running", "active_model": "chat"}
            with self.assertRaises(RuntimeError):
                LocalEmbeddings("different-model").embed("un document")
            client.return_value.__enter__.return_value.post.assert_not_called()

    async def test_pending_write_rechecks_memory_setting_and_source(self):
        self.add("Nous utilisons SQLite pour notre projet.")
        await self.service.queue.tick()  # Extraction only; the write remains queued.
        self.service.configure({"auto_memory": False})
        await self.drain()
        self.assertEqual(self.service.store.documents(), [])
        self.service.configure({"auto_memory": True})
        message, _ = self.add("Nous utilisons Python pour notre projet.")
        await self.service.queue.tick()
        self.storage.edit_user_message(self.conv, message, "Message corrigé sans cette décision.")
        await self.drain()
        self.assertEqual(self.service.store.documents(), [])

    async def test_write_recovery_is_idempotent_after_commit(self):
        self.add("Nous utilisons SQLite pour notre projet.")
        await self.service.queue.tick()
        job = next(j for j in self.service.queue.jobs()["jobs"] if j["kind"] == "write")
        payload = job["payload"]
        self.service.store.write(payload["path"], payload["content"], None, payload["origin"])
        await self.service.queue.tick()
        self.assertEqual(len(self.service.store.history(payload["path"])), 1)
        self.assertEqual(next(j for j in self.service.queue.jobs()["jobs"] if j["id"] == job["id"])["status"], "completed")

    async def test_cancel_during_atomic_commit_reports_actual_result(self):
        import threading
        self.add("Nous utilisons SQLite pour notre projet.")
        await self.service.queue.tick()
        job = next(j for j in self.service.queue.jobs()["jobs"] if j["kind"] == "write")
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original_write = self.service.store.write
        def delayed_write(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise RuntimeError("Test commit gate timed out")
            return original_write(*args, **kwargs)
        with patch.object(self.service.store, "write", side_effect=delayed_write):
            task = asyncio.create_task(self.service.queue.tick())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                result = await asyncio.to_thread(self.service.queue.cancel, job["id"])
                self.assertEqual(result["status"], "running")
                self.service.begin_chat()
            finally:
                release.set()
            await asyncio.wait_for(task, 2)
            self.service.end_chat()
        self.assertEqual(len(self.service.store.documents()), 1)
        self.assertEqual(next(j for j in self.service.queue.jobs()["jobs"] if j["id"] == job["id"])["status"], "completed")

    async def test_local_llm_extraction_requires_exact_quote(self):
        import httpx
        self.service.configure({"extraction": "local_llm", "extraction_model": "resident"})
        self.add("Nous utilisons SQLite pour notre projet.")
        requests = []
        async def respond(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"status": "running", "active_model": "resident"})
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"facts": [{"title": "Architecture locale", "quote": "Nous utilisons SQLite pour notre projet."}, {"title": "Inventé", "quote": "Nous utilisons Rust pour notre projet."}]})}}]})
        client_type = httpx.AsyncClient
        with patch("wiki.service.httpx.AsyncClient", side_effect=lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs)):
            await self.drain()
        self.assertEqual(len(requests), 2)
        self.assertFalse(any(j["status"] == "review" for j in self.service.queue.jobs()["jobs"]))
        self.assertEqual(len(self.service.store.documents()), 1)


if __name__ == "__main__":
    unittest.main()
