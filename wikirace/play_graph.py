"""Adjacency graph built incrementally from real play, backed by SQLite.

Instead of crawling Wikipedia's API up front, we accumulate each page's link set
as players actually visit pages: the content script reads the live DOM and reports
the article links it finds. Over time this becomes a real link graph we can run
BFS on (par, shortest path, route analysis) without ever hitting Wikipedia's API.
First players seed it; everyone after benefits.

"Latest observation wins": each visit *overwrites* that page's link set with what
is currently on the page, so links edited out of an article are pruned from the
graph (the cache self-heals to match live Wikipedia). Two guards keep that safe:
  * we only overwrite on a non-empty observation, so a transient empty/failed
    parse can't wipe a node's edges;
  * we stamp `seen_at` per node so we know how fresh each node's edges are.

SQLite (not a JSON blob) so a write is O(#links on the page) instead of
re-serializing the whole graph, it's durable across restarts, and it stays fast
as the graph grows to tens/hundreds of thousands of nodes.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PLAY_GRAPH_PATH = DATA_DIR / "play_graph.sqlite3"


class PlayGraph:
    def __init__(self, path: Path | str = PLAY_GRAPH_PATH, flush_interval: float = 0.0) -> None:
        # flush_interval is accepted for API compatibility; SQLite persists on
        # commit so there's no separate flush throttle.
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # One shared connection guarded by a lock (FastAPI runs sync handlers in a
        # threadpool). WAL keeps reads/writes from blocking each other.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    title   TEXT PRIMARY KEY,
                    seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS edges (
                    src TEXT NOT NULL,
                    dst TEXT NOT NULL,
                    PRIMARY KEY (src, dst)
                );
                CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
                """
            )
            self._conn.commit()

    def record(self, title: str, links: list[str]) -> None:
        """Overwrite `title`'s outgoing links with the latest observed set.

        No-op on an empty observation so a failed/empty parse never wipes a node.
        Removed links are pruned (latest observation wins).
        """
        if not title or not links:
            return
        unique = sorted(set(links))
        now = time.time()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM edges WHERE src = ?", (title,))
            cur.executemany(
                "INSERT OR IGNORE INTO edges (src, dst) VALUES (?, ?)",
                [(title, d) for d in unique],
            )
            cur.execute(
                "INSERT INTO nodes (title, seen_at) VALUES (?, ?) "
                "ON CONFLICT(title) DO UPDATE SET seen_at = excluded.seen_at",
                (title, now),
            )
            self._conn.commit()

    def links_of(self, title: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT dst FROM edges WHERE src = ? ORDER BY dst", (title,)
            ).fetchall()
        return [r[0] for r in rows]

    def has(self, title: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM nodes WHERE title = ? LIMIT 1", (title,)
            ).fetchone()
        return row is not None

    def flush(self) -> None:
        # Commits happen per write; nothing buffered. Kept for API compatibility.
        return None

    @property
    def stats(self) -> dict:
        with self._lock:
            nodes = self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edges = self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return {"nodes": nodes, "edges": edges}
