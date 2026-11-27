"""Replaceable, bounded lexical/hybrid retrieval without ML runtime dependencies."""
import json
from beacon_address import beacon_url
import math
import re
from typing import Protocol

import httpx


STOP = set("avec dans pour une des les est que qui aux sur du de la le et en un ce ma mon mes nous vous notre cette comment pourquoi peux peut moi bonjour merci salut".split())


def terms(query):
    return list(dict.fromkeys(t for t in re.findall(r"[^\W_]{3,}", query.casefold()) if t not in STOP))[:20]


class Retriever(Protocol):
    def search(self, query: str, limit: int = 4) -> list[dict]: ...


class LocalEmbeddings:
    """Use only the model already resident in Beacon; never load/download a model."""
    def __init__(self, model, base_url=None):
        self.model = model
        self.base_url = base_url or beacon_url()

    def embed(self, text):
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            status = client.get(self.base_url + "/api/status")
            status.raise_for_status()
            state = status.json()
            if state.get("status") != "running" or state.get("active_model") != self.model:
                raise RuntimeError("Le modèle d’embeddings doit déjà être chargé dans Beacon")
            response = client.post(self.base_url + "/v1/embeddings", json={"model": self.model, "input": text[:1800]})
            response.raise_for_status()
            vector = response.json()["data"][0]["embedding"]
        return self.normalize(vector)

    async def aembed(self, text):
        # Closing the async request on cancellation releases the model promptly.
        async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            response = await client.get(self.base_url + "/api/status")
            response.raise_for_status()
            state = response.json()
            if state.get("status") != "running" or state.get("active_model") != self.model:
                raise RuntimeError("Le modèle d’embeddings doit déjà être chargé dans Beacon")
            response = await client.post(self.base_url + "/v1/embeddings", json={"model": self.model, "input": text[:1800]})
            response.raise_for_status()
            return self.normalize(response.json()["data"][0]["embedding"])

    @staticmethod
    def normalize(vector):
        if not isinstance(vector, list) or not 1 <= len(vector) <= 4096 or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector):
            raise ValueError("Vecteur invalide")
        norm = math.sqrt(sum(v * v for v in vector))
        if not norm:
            raise ValueError("Vecteur nul")
        return [v / norm for v in vector]


class SQLiteRetriever:
    def __init__(self, store, embeddings=None, vector_limit=3000):
        self.store = store
        self.embeddings = embeddings
        self.vector_limit = min(10000, vector_limit)

    def search(self, query, limit=4):
        tokens = terms(query)
        if not tokens:
            return []
        limit = max(1, min(20, limit))
        with self.store.connect() as db:
            if self.store.fts:
                rows = db.execute("SELECT c.*, bm25(search) AS rank FROM search JOIN chunks c ON c.id=search.rowid WHERE search MATCH ? ORDER BY rank LIMIT ?", (" OR ".join('"' + t + '"' for t in tokens), limit * 4)).fetchall()
            else:
                conditions = " OR ".join("lower(text) LIKE ?" for _ in tokens)
                rows = db.execute(f"SELECT * FROM chunks WHERE {conditions} LIMIT ?", (*[f"%{t}%" for t in tokens], limit * 4)).fetchall()
            ranked = {r["id"]: {**dict(r), "score": 1 / (60 + i)} for i, r in enumerate(rows)}
            if self.embeddings:
                try:
                    query_vector = self.embeddings.embed(query)
                    # Stream vectors from SQLite; do not load the corpus into RAM.
                    best = []
                    for row in db.execute("SELECT * FROM chunks WHERE model=? AND vector IS NOT NULL ORDER BY id LIMIT ?", (self.embeddings.model, self.vector_limit)):
                        vector = json.loads(row["vector"])
                        if len(vector) != len(query_vector):
                            continue
                        score = sum(a * b for a, b in zip(vector, query_vector))
                        if score > 0.25:
                            best.append((score, dict(row)))
                            best.sort(key=lambda item: item[0], reverse=True)
                            del best[limit * 4:]
                    for i, (_, row) in enumerate(best):
                        result = ranked.setdefault(row["id"], {**row, "score": 0})
                        result["score"] += 1 / (60 + i)
                except (httpx.HTTPError, RuntimeError, ValueError, KeyError, IndexError, TypeError):
                    pass  # Lexical retrieval remains available offline.
        selected, paths = [], set()
        for row in sorted(ranked.values(), key=lambda r: r["score"], reverse=True):
            if row["path"] in paths:
                continue
            # A filesystem edit must not inject an obsolete indexed excerpt.
            try:
                current = self.store.read(row["path"])
            except (OSError, ValueError):
                continue
            if current["content"][row["offset"]:row["offset"] + len(row["text"])] != row["text"]:
                continue
            paths.add(row["path"])
            selected.append({"path": row["path"], "text": row["text"], "offset": row["offset"], "hash": current["hash"], "score": row["score"]})
            if len(selected) >= limit:
                break
        return selected


def context(results, token_budget):
    # UTF-8 byte count is a conservative token upper bound for byte tokenizers.
    budget = max(0, token_budget)
    passages = []
    used = []
    for result in results:
        header = f"\nSource [[{result['path']}]] (position {result['offset']}):\n"
        available = budget - len(header.encode("utf-8"))
        if available < 40:
            break
        text = result["text"].encode("utf-8")[:available].decode("utf-8", errors="ignore")
        passages.append(header + text)
        used.append({**result, "text": text})
        budget -= len((header + text).encode("utf-8"))
    return "".join(passages), used
