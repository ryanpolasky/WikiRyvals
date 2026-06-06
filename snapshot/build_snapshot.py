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

from snapshot.seeds import EXTRA_SEEDS, SEEDS
from wikirace.snapshot_store import (
    GRAPH_PATH,
    META_PATH,
    PAGES_DIR,
    SNAPSHOT_DIR,
    save_article,
)
from wikirace.wiki import USER_AGENT, fetch_article


def build(max_articles: int, enqueue_per_page: int, delay: float,
          *, seeds: list[str] | None = None, extend: bool = False) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    PAGES_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    # --extend grafts NEW seeds onto the existing snapshot instead of rebuilding
    # it: load the current graph, treat its articles as already fetched (so we
    # never recrawl them), and only explore outward from seeds not yet present.
    # `max_articles` then caps how many *new* articles we add this run.
    adjacency: dict[str, list[str]] = {}
    pre_existing: set[str] = set()
    if extend and GRAPH_PATH.exists():
        existing = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        adjacency = existing["adjacency"]
        pre_existing = set(adjacency)
        print(f"Extending snapshot: {len(pre_existing)} articles already present.")
    elif extend:
        print("No existing snapshot to extend - building a fresh one.")

    seeds = list(seeds) if seeds else list(SEEDS)
    queued: set[str] = set(adjacency)
    frontier: deque[str] = deque()
    for s in seeds:
        if s not in queued:
            queued.add(s)
            frontier.append(s)
    print(f"Seeding crawl with {len(frontier)} new seed(s); target +{max_articles} article(s).")

    fetched = 0  # NEW articles fetched this run (existing ones aren't recrawled)
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
    added = sorted(set(adjacency) - pre_existing)
    GRAPH_PATH.write_text(
        json.dumps({"titles": titles, "adjacency": adjacency}, ensure_ascii=False),
        encoding="utf-8",
    )
    # When extending, preserve prior meta (esp. the original seed set) and record
    # exactly what this run added so prompt generation can focus new races on it.
    if extend:
        meta: dict = {}
        if META_PATH.exists():
            try:
                meta = json.loads(META_PATH.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        meta.update({
            "articles": len(adjacency),
            "failures": failures,
            "enqueue_per_page": enqueue_per_page,
            "added_this_run": len(added),
            "last_added": added,
            "last_seeds": seeds,
        })
        meta.setdefault("seeds", SEEDS)
    else:
        meta = {
            "articles": len(adjacency),
            "failures": failures,
            "seeds": seeds,
            "max_articles": max_articles,
            "enqueue_per_page": enqueue_per_page,
        }
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    print(f"\nDone. +{fetched} new article(s) ({len(adjacency)} total), {failures} failures.")
    print(f"  graph -> {GRAPH_PATH}")
    print(f"  pages -> {PAGES_DIR}")
    if added:
        preview = ", ".join(added[:12]) + ("..." if len(added) > 12 else "")
        print(f"  added: {preview}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-articles", type=int, default=300,
                        help="number of NEW articles to fetch (in --extend, added on top)")
    parser.add_argument("--enqueue-per-page", type=int, default=40,
                        help="max links followed from each page (keeps the crawl bounded)")
    parser.add_argument("--delay", type=float, default=0.1,
                        help="seconds to sleep between requests (be polite to the API)")
    parser.add_argument("--extend", action="store_true",
                        help="add to the existing snapshot instead of rebuilding it")
    parser.add_argument("--seeds", nargs="+", metavar="TITLE",
                        help="seed titles to crawl from "
                             "(default: EXTRA_SEEDS with --extend, else SEEDS)")
    args = parser.parse_args()
    seeds = args.seeds
    if seeds is None and args.extend:
        seeds = EXTRA_SEEDS
    build(args.max_articles, args.enqueue_per_page, args.delay,
          seeds=seeds, extend=args.extend)


if __name__ == "__main__":
    main()
