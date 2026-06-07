"""Persistence for the frozen snapshot + a lazily-grown runtime cache.

The *snapshot* (built offline by `snapshot.build_snapshot`) drives prompt
generation and BFS difficulty. The *runtime cache* lets the live prototype serve
and validate any article a player clicks into, even if it was not part of the
bounded snapshot - fetching it once from Wikipedia and caching it. Move
validation always uses the link set of the page the player is actually on, so it
stays correct regardless of which store the page came from.
"""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path

import requests

from .wiki import Article, fetch_article

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshot"
PAGES_DIR = SNAPSHOT_DIR / "pages"
GRAPH_PATH = SNAPSHOT_DIR / "graph.json"
META_PATH = SNAPSHOT_DIR / "meta.json"
PROMPTS_PATH = DATA_DIR / "prompts.json"
RUNTIME_CACHE_DIR = DATA_DIR / "runtime_cache"


def _safe_name(title: str) -> str:
    return base64.urlsafe_b64encode(title.encode("utf-8")).decode("ascii") + ".json"


def save_article(article: Article, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _safe_name(article.title)
    payload = json.dumps(
        {"title": article.title, "html": article.html, "links": article.links},
        ensure_ascii=False,
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _load_article(directory: Path, title: str) -> Article | None:
    path = directory / _safe_name(title)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Article(title=data["title"], html=data["html"], links=data["links"])


class SnapshotStore:
    def __init__(self) -> None:
        self.adjacency: dict[str, list[str]] = {}
        self.titles: list[str] = []
        self._lock = threading.Lock()
        RUNTIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ---- snapshot loading -------------------------------------------------
    def load_graph(self) -> bool:
        if not GRAPH_PATH.exists():
            return False
        data = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        self.adjacency = data["adjacency"]
        self.titles = data["titles"]
        return True

    @property
    def loaded(self) -> bool:
        return bool(self.titles)

    # ---- article access (snapshot -> runtime cache -> live fetch) ---------
    def get_article(self, title: str) -> Article | None:
        art = _load_article(PAGES_DIR, title)
        if art is not None:
            return art
        with self._lock:
            art = _load_article(RUNTIME_CACHE_DIR, title)
            if art is not None:
                return art
            try:
                art = fetch_article(title)
            except requests.RequestException:
                return None
            save_article(art, RUNTIME_CACHE_DIR)
            return art

    def links_of(self, title: str) -> list[str]:
        """Valid outgoing links for move validation.

        Prefer the page's actual rendered link set (authoritative for what the
        player can click); fall back to the snapshot adjacency.
        """
        art = self.get_article(title)
        if art is not None:
            return art.links
        return self.adjacency.get(title, [])

    def is_valid_move(self, frm: str, to: str) -> bool:
        return to in set(self.links_of(frm))
