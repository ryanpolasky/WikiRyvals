"""Build a bounded, frozen Wikipedia snapshot via BFS over the MediaWiki API.

Crawls outward from a set of seed articles, caching sanitized HTML + the link
adjacency for each page. The result drives prompt generation (BFS difficulty)
and gives the prototype a consistent set of pages to serve.

Usage:
    python -m snapshot.build_snapshot --max-articles 300
"""

from __future__ import annotations

import argparse
import json
import time
from collections import deque

import requests

from snapshot.seeds import SEEDS
from wikirace.snapshot_store import (
    GRAPH_PATH,
    META_PATH,
    PAGES_DIR,
    SNAPSHOT_DIR,
    save_article,
)
from wikirace.wiki import USER_AGENT, fetch_article


def build(max_articles: int, enqueue_per_page: int, delay: float) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    PAGES_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    adjacency: dict[str, list[str]] = {}
    queued: set[str] = set(SEEDS)
    frontier: deque[str] = deque(SEEDS)
    fetched = 0
    failures = 0

    while frontier and fetched < max_articles:
        title = frontier.popleft()
        try:
            article = fetch_article(title, session=session)
        except requests.RequestException as exc:
            failures += 1
            print(f"  ! skip {title!r}: {exc}")
            continue

        if article.title in adjacency:
            continue

        save_article(article, PAGES_DIR)
        adjacency[article.title] = article.links
        fetched += 1
        if fetched % 25 == 0 or fetched == 1:
            print(f"  [{fetched}/{max_articles}] {article.title} "
                  f"({len(article.links)} links, frontier={len(frontier)})")

        for linked in article.links[:enqueue_per_page]:
            if linked not in queued:
                queued.add(linked)
                frontier.append(linked)

        if delay:
            time.sleep(delay)

    titles = sorted(adjacency)
    GRAPH_PATH.write_text(
        json.dumps({"titles": titles, "adjacency": adjacency}, ensure_ascii=False),
        encoding="utf-8",
    )
    META_PATH.write_text(json.dumps({
        "articles": fetched,
        "failures": failures,
        "seeds": SEEDS,
        "max_articles": max_articles,
        "enqueue_per_page": enqueue_per_page,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nDone. {fetched} articles, {failures} failures.")
    print(f"  graph -> {GRAPH_PATH}")
    print(f"  pages -> {PAGES_DIR}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-articles", type=int, default=300,
                        help="number of articles to fetch into the snapshot")
    parser.add_argument("--enqueue-per-page", type=int, default=40,
                        help="max links followed from each page (keeps the crawl bounded)")
    parser.add_argument("--delay", type=float, default=0.1,
                        help="seconds to sleep between requests (be polite to the API)")
    args = parser.parse_args()
    build(args.max_articles, args.enqueue_per_page, args.delay)


if __name__ == "__main__":
    main()
