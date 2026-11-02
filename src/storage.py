from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.json_path = db_path.parent / "index.json"
        self.init_db()
        self._sync_from_json()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_hashes (
                    hash TEXT PRIMARY KEY,
                    source_id INTEGER NOT NULL,
                    creation_time INTEGER,
                    first_seen_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_seen_hashes_source
                ON seen_hashes (source_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    post_id TEXT NOT NULL,
                    post_url TEXT,
                    creation_time INTEGER,
                    message_snippet TEXT,
                    first_seen_at TEXT NOT NULL,
                    notified INTEGER DEFAULT 0,
                    UNIQUE(source_id, post_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_seen_posts_source_post
                ON seen_posts (source_id, post_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_seen_posts_source_url
                ON seen_posts (source_id, post_url)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    @staticmethod
    def _hash_key(prefix: int, val: str) -> str:
        raw = f"{prefix}:{str(val).strip()}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _sync_from_json(self) -> None:
        """Load seen hashes from index.json if available."""
        if not self.json_path.is_file():
            return
        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            with self._get_connection() as conn:
                for key, item in data.items():
                    if key == "_metadata" and isinstance(item, dict):
                        for m_k, m_v in item.items():
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO app_metadata (key, value)
                                VALUES (?, ?)
                                """,
                                (m_k, str(m_v)),
                            )
                        continue
                    if key.startswith("_"):
                        continue

                    now_iso = datetime.now(timezone.utc).isoformat()
                    if isinstance(item, dict) and "s" in item and len(key) == 64:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO seen_hashes (hash, source_id, creation_time, first_seen_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (key, item.get("s"), item.get("t"), now_iso),
                        )
                    elif isinstance(item, dict) and "source_id" in item:
                        sid = item.get("source_id")
                        pid = str(item.get("post_id", ""))
                        url = item.get("post_url")
                        ctime = item.get("creation_time")
                        fseen = item.get("first_seen_at") or now_iso
                        if pid:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO seen_hashes (hash, source_id, creation_time, first_seen_at)
                                VALUES (?, ?, ?, ?)
                                """,
                                (self._hash_key(sid, pid), sid, ctime, fseen),
                            )
                        if url:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO seen_hashes (hash, source_id, creation_time, first_seen_at)
                                VALUES (?, ?, ?, ?)
                                """,
                                (self._hash_key(sid, url), sid, ctime, fseen),
                            )
                conn.commit()
        except Exception as e:
            logger.warning(f"Could not load state from {self.json_path}: {e}")

    def _sync_to_json(self) -> None:
        """Export all seen hashes atomically to index.json without sensitive strings."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT hash, source_id, creation_time
                    FROM seen_hashes ORDER BY first_seen_at ASC
                    """
                )
                rows = cursor.fetchall()

                cursor.execute("SELECT key, value FROM app_metadata")
                meta_rows = cursor.fetchall()

            export_data = {}
            if meta_rows:
                export_data["_metadata"] = {r["key"]: r["value"] for r in meta_rows}

            for r in rows:
                export_data[r["hash"]] = {
                    "s": r["source_id"],
                    "t": r["creation_time"],
                }

            tmp_file = self.json_path.with_suffix(".json.tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_file, self.json_path)
        except Exception as e:
            logger.warning(f"Could not export state to {self.json_path}: {e}")

    def sync_to_json(self) -> None:
        """Public alias to force export to JSON."""
        self._sync_to_json()

    def get_metadata(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT value FROM app_metadata WHERE key = ? LIMIT 1",
                (key,),
            )
            row = cursor.fetchone()
            if row is not None:
                return row["value"]
            return default

    def set_metadata(self, key: str, value: str, sync_json: bool = True) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO app_metadata (key, value)
                VALUES (?, ?)
                """,
                (key, str(value)),
            )
            conn.commit()
        if sync_json:
            self._sync_to_json()

    def is_seen(
        self,
        source_id: int,
        post_id: str,
        post_url: Optional[str] = None,
    ) -> bool:
        pid_str = str(post_id).strip()
        pid_hash = self._hash_key(source_id, pid_str)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM seen_hashes WHERE hash = ? LIMIT 1",
                (pid_hash,),
            )
            if cursor.fetchone() is not None:
                return True

            if post_url:
                url_hash = self._hash_key(source_id, post_url.strip())
                cursor.execute(
                    "SELECT 1 FROM seen_hashes WHERE hash = ? LIMIT 1",
                    (url_hash,),
                )
                if cursor.fetchone() is not None:
                    return True

                for emb in re.findall(r"\d{7,}", post_url):
                    emb_hash = self._hash_key(source_id, emb)
                    cursor.execute(
                        "SELECT 1 FROM seen_hashes WHERE hash = ? LIMIT 1",
                        (emb_hash,),
                    )
                    if cursor.fetchone() is not None:
                        return True

            cursor.execute(
                "SELECT 1 FROM seen_posts WHERE source_id = ? AND post_id = ? LIMIT 1",
                (source_id, pid_str),
            )
            if cursor.fetchone() is not None:
                return True

            return False

    def has_source_history(self, source_id: int) -> bool:
        """Check if any posts have ever been recorded for this source."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM seen_hashes WHERE source_id = ? LIMIT 1",
                (source_id,),
            )
            if cursor.fetchone() is not None:
                return True
            cursor.execute(
                "SELECT 1 FROM seen_posts WHERE source_id = ? LIMIT 1",
                (source_id,),
            )
            return cursor.fetchone() is not None

    def mark_seen(
        self,
        source_id: int,
        post_id: str,
        post_url: Optional[str] = None,
        creation_time: Optional[int] = None,
        message_snippet: Optional[str] = None,
        notified: bool = True,
        sync_json: bool = True,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        pid_str = str(post_id).strip()
        pid_hash = self._hash_key(source_id, pid_str)

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO seen_hashes (hash, source_id, creation_time, first_seen_at)
                VALUES (?, ?, ?, ?)
                """,
                (pid_hash, source_id, creation_time, now_iso),
            )
            if post_url:
                url_hash = self._hash_key(source_id, post_url.strip())
                conn.execute(
                    """
                    INSERT OR REPLACE INTO seen_hashes (hash, source_id, creation_time, first_seen_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (url_hash, source_id, creation_time, now_iso),
                )
                for emb in re.findall(r"\d{7,}", post_url):
                    emb_hash = self._hash_key(source_id, emb)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO seen_hashes (hash, source_id, creation_time, first_seen_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (emb_hash, source_id, creation_time, now_iso),
                    )

            conn.execute(
                """
                INSERT OR REPLACE INTO seen_posts 
                (source_id, post_id, post_url, creation_time, message_snippet, first_seen_at, notified)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    pid_str,
                    post_url,
                    creation_time,
                    message_snippet[:200] if message_snippet else None,
                    now_iso,
                    1 if notified else 0,
                ),
            )
            conn.commit()

        if sync_json:
            self._sync_to_json()

    def get_stats(self) -> Dict[str, int]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM seen_hashes")
            total_posts = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(DISTINCT source_id) FROM seen_hashes")
            tracked_sources = cursor.fetchone()[0]

            return {
                "total_posts": total_posts,
                "tracked_sources": tracked_sources,
                "total_notified": total_posts,
            }
