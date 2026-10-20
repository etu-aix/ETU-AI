"""
Module de persistance locale ultra-léger basé sur SQLite3.
Gère l'historique des conversations, les paramètres (tokens etu-aix) et le cache des modèles.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Optional
import uuid

DB_PATH = Path(__file__).resolve().parent / "chats.db"


class StorageManager:
    """Gestionnaire de persistance SQLite pour ETU-AI."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    from contextlib import contextmanager

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_tables(self) -> None:
        """Initialise le schéma de base de données si nécessaire."""
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    model_used TEXT
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_calls TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS models_cache (
                    filename TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    size_bytes INTEGER DEFAULT 0,
                    sha256 TEXT,
                    backend TEXT,
                    downloaded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
                CREATE TABLE IF NOT EXISTS assistant_profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    avatar TEXT NOT NULL,
                    instructions TEXT NOT NULL DEFAULT '',
                    model TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
            if "profile_id" not in columns:
                conn.execute("ALTER TABLE conversations ADD COLUMN profile_id TEXT")
            if not conn.execute("SELECT 1 FROM assistant_profiles LIMIT 1").fetchone():
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO assistant_profiles VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("default", "ETU-AI", "✦", "", None, now, now),
                )

    # -------------------------------------------------------------------------
    # Conversations
    # -------------------------------------------------------------------------

    def create_conversation(self, title: str = "Nouvelle discussion", model_used: Optional[str] = None, profile_id: Optional[str] = None) -> str:
        """Crée une nouvelle discussion et renvoie son ID."""
        conv_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at, model_used, profile_id) VALUES (?, ?, ?, ?, ?, ?)",
                (conv_id, title, now, now, model_used or "default", profile_id or "default"),
            )
        return conv_id

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        """Liste les discussions récentes."""
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT id, title, created_at, updated_at, model_used, profile_id FROM conversations ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_conversation(self, conv_id: str) -> Optional[dict[str, Any]]:
        """Récupère une conversation par son identifiant."""
        with self._connection() as conn:
            cursor = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def set_conversation_profile(self, conv_id: str, profile_id: str) -> bool:
        with self._connection() as conn:
            result = conn.execute("UPDATE conversations SET profile_id = ? WHERE id = ?", (profile_id, conv_id))
            return result.rowcount > 0

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM assistant_profiles ORDER BY created_at, id"
            )]

    def get_profile(self, profile_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM assistant_profiles WHERE id = ?", (profile_id,)).fetchone()
            return dict(row) if row else None

    def save_profile(self, name: str, avatar: str, instructions: str, model: Optional[str] = None, profile_id: Optional[str] = None) -> dict[str, Any]:
        profile_id = profile_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO assistant_profiles (id, name, avatar, instructions, model, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, avatar=excluded.avatar,
                   instructions=excluded.instructions, model=excluded.model, updated_at=excluded.updated_at""",
                (profile_id, name, avatar, instructions, model, now, now),
            )
        return self.get_profile(profile_id)

    def delete_profile(self, profile_id: str) -> bool:
        if profile_id == "default":
            return False
        with self._connection() as conn:
            conn.execute("UPDATE conversations SET profile_id = 'default' WHERE profile_id = ?", (profile_id,))
            result = conn.execute("DELETE FROM assistant_profiles WHERE id = ?", (profile_id,))
            return result.rowcount > 0

    def update_conversation_title(self, conv_id: str, new_title: str) -> bool:
        """Met à jour le titre d'une discussion."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            res = conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (new_title, now, conv_id),
            )
            return res.rowcount > 0

    def delete_conversation(self, conv_id: str) -> bool:
        """Supprime une discussion et tous ses messages associés."""
        with self._connection() as conn:
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
            res = conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            return res.rowcount > 0

    def delete_all_conversations(self) -> int:
        """Supprime tout l'historique et renvoie le nombre de discussions supprimées."""
        with self._connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM conversations")
            return int(count)

    # -------------------------------------------------------------------------
    # Messages
    # -------------------------------------------------------------------------

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        tool_calls: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        """Ajoute un message à une conversation et met à jour le timestamp de la conversation."""
        msg_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO messages (id, conversation_id, role, content, tool_calls, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (msg_id, conversation_id, role, content, tool_calls_json, now),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
        return msg_id

    def get_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        """Récupère tous les messages chronologiques d'une conversation."""
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT id, role, content, tool_calls, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC",
                (conversation_id,),
            )
            results = []
            for row in cursor.fetchall():
                item = dict(row)
                if item.get("tool_calls"):
                    try:
                        item["tool_calls"] = json.loads(item["tool_calls"])
                    except json.JSONDecodeError:
                        pass
                results.append(item)
            return results

    def get_message(self, conversation_id: str, message_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT id, role, content, tool_calls, created_at FROM messages WHERE conversation_id = ? AND id = ?",
                (conversation_id, message_id),
            ).fetchone()
        return dict(row) if row else None

    def edit_user_message(self, conversation_id: str, message_id: str, content: str) -> int:
        """Edit a user turn and discard every later message in that conversation."""
        if not content.strip():
            raise ValueError("Le message ne peut pas être vide.")
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, role FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC",
                (conversation_id,),
            ).fetchall()
            index = next((i for i, row in enumerate(rows) if row["id"] == message_id), None)
            if index is None:
                raise LookupError("Message introuvable dans cette discussion.")
            if rows[index]["role"] != "user":
                raise ValueError("Seuls les messages utilisateur peuvent être modifiés.")
            later_ids = [row["id"] for row in rows[index + 1:]]
            conn.execute("UPDATE messages SET content = ? WHERE id = ?", (content, message_id))
            if later_ids:
                conn.execute("CREATE TABLE IF NOT EXISTS message_attachments (message_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                placeholders = ",".join("?" for _ in later_ids)
                conn.execute(f"DELETE FROM message_attachments WHERE message_id IN ({placeholders})", later_ids)
                conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", later_ids)
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), conversation_id),
            )
        return len(later_ids)

    # -------------------------------------------------------------------------
    # Paramètres (Settings)
    # -------------------------------------------------------------------------

    def set_setting(self, key: str, value: Any) -> None:
        """Enregistre un paramètre sous forme sérialisée."""
        val_str = json.dumps(value) if not isinstance(value, str) else value
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (key, val_str, now),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Récupère la valeur d'un paramètre."""
        with self._connection() as conn:
            cursor = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            if not row:
                return default
            val_str = row["value"]
            try:
                return json.loads(val_str)
            except Exception:
                return val_str

    # -------------------------------------------------------------------------
    # Cache Modèles
    # -------------------------------------------------------------------------

    def register_model(
        self,
        filename: str,
        repo: str,
        size_bytes: int = 0,
        sha256: Optional[str] = None,
        backend: Optional[str] = None,
    ) -> None:
        """Enregistre un modèle téléchargé dans le cache local."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO models_cache (filename, repo, size_bytes, sha256, backend, downloaded_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(filename) DO UPDATE SET
                   repo=excluded.repo, size_bytes=excluded.size_bytes, sha256=excluded.sha256,
                   backend=excluded.backend, downloaded_at=excluded.downloaded_at""",
                (filename, repo, size_bytes, sha256, backend, now),
            )

    def list_cached_models(self) -> list[dict[str, Any]]:
        """Liste les modèles enregistrés en cache."""
        with self._connection() as conn:
            cursor = conn.execute("SELECT * FROM models_cache ORDER BY downloaded_at DESC")
            return [dict(row) for row in cursor.fetchall()]
