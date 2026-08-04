"""Generate difficulty-bucketed race prompts from the snapshot graph.

Samples (start, target) pairs, computes BFS shortest hops over the induced
snapshot graph, and buckets them by how recognizable the endpoints are (see
`bucket_difficulty`). The output feeds the prototype's "new race" endpoint.

Requires the fame signal from `snapshot.fetch_fame`: difficulty is keyed on
notability rather than hop count, and interlanguage-link counts are the only
measure of that which survives contact with a topically skewed crawl.

Usage:
    python -m snapshot.fetch_fame
    python -m snapshot.generate_prompts --per-bucket 40
"""

from __future__ import annotations

import argparse
import json
import random
import statistics

from wikirace.graph import (
    bucket_difficulty,
    in_degrees,
    induced_adjacency,
    shortest_hops,
)
from wikirace.snapshot_store import FAME_PATH, GRAPH_PATH, META_PATH, PROMPTS_PATH
from wikirace.wiki import _is_article_title

# Endpoints nobody could place. Below this many interlanguage links an article is
# jargon or a list page ("One liner schedule", "Lists of bodies of water") - a
# fine thing to pass *through*, a miserable thing to be handed as a destination.
FAME_FLOOR = 8
# Corpus percentiles that separate the tiers.
EASY_PCT, MEDIUM_PCT = 0.75, 0.30


def _usable_endpoint(title: str, fame: int) -> bool:
    """Whether an article can be handed to a player as a start or target.

    List pages clear the fame bar on interlanguage links alone but make dismal
    endpoints - they're indexes, not places, and "get to Lists of mathematics
    topics" is a scavenger hunt rather than a race.
    """
    if fame < FAME_FLOOR:
        return False
    # Snapshots built before the namespace fix still hold "Template talk:" pages;
    # they currently fall below the fame floor anyway, but excluding them by
    # coincidence is not the same as excluding them on purpose.
    if not _is_article_title(title):
        return False
    return not (title.startswith("List of ") or title.startswith("Lists of "))


def generate(per_bucket: int, max_pairs: int, seed: int, *,
             extend: bool = False, focus: list[str] | None = None,
             focus_added: bool = False) -> None:
    if not GRAPH_PATH.exists():
        raise SystemExit("No snapshot graph found. Run `python -m snapshot.build_snapshot` first.")
    if not FAME_PATH.exists():
        raise SystemExit("No fame data found. Run `python -m snapshot.fetch_fame` first.")

    data = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    adjacency = induced_adjacency(data["adjacency"])
    fame: dict[str, int] = json.loads(
        FAME_PATH.read_text(encoding="utf-8"))["langlinks"]
    # Unrecognizable articles stay in the graph (they make fine intermediate
    # hops) but never become a start or a target.
    titles = [t for t in data["titles"]
              if adjacency.get(t) and _usable_endpoint(t, fame.get(t, 0))]
    title_set = set(titles)
    if len(titles) < 2:
        raise SystemExit("Fame floor left too few usable articles - is fame.json stale?")

    ranked = sorted(fame.get(t, 0) for t in titles)
    easy_floor = ranked[int(len(ranked) * EASY_PCT)]
    medium_floor = ranked[int(len(ranked) * MEDIUM_PCT)]

    deg = in_degrees(adjacency)
    median_deg = statistics.median(deg.values()) if deg else 0.0

    rng = random.Random(seed)

    # --extend keeps every existing prompt and only appends new ones, so the live
    # "spread" is never thrown away. We de-dupe new pairs against the existing set.
    existing: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    if extend and PROMPTS_PATH.exists():
        prev = json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
        existing = prev.get("prompts", [])
        for p in existing:
            seen_pairs.add((p["start"], p["target"]))

    # A focus set biases every NEW prompt to involve one of these articles - this
    # is how freshly-seeded pages (people/animals) actually get into rotation.
    focus_set: set[str] = set(focus or [])
    if focus_added and META_PATH.exists():
        try:
            meta = json.loads(META_PATH.read_text(encoding="utf-8"))
            focus_set |= set(meta.get("last_added", []))
        except Exception:
            pass
    focus_set &= title_set
    focus_list = sorted(focus_set)
    if (focus or focus_added) and not focus_list:
        raise SystemExit("Focus set is empty (no usable articles in the snapshot). "
                         "Did the --extend build add anything, and are titles spelled right?")

    buckets: dict[str, list[dict]] = {"easy": [], "medium": [], "hard": []}
    attempts = 0
    target_new = per_bucket * 3
    while attempts < max_pairs and sum(len(v) for v in buckets.values()) < target_new:
        attempts += 1
        if focus_list:
            hub = rng.choice(focus_list)
            other = rng.choice(titles)
            start, target = (hub, other) if rng.random() < 0.5 else (other, hub)
        else:
            start, target = rng.sample(titles, 2)
        if start == target or (start, target) in seen_pairs:
            continue
        seen_pairs.add((start, target))

        hops = shortest_hops(adjacency, start, target, max_depth=5)
        if hops is None or hops < 2:
            continue

        difficulty = bucket_difficulty(hops, fame.get(start, 0), fame.get(target, 0),
                                       easy_floor, medium_floor)
        if len(buckets[difficulty]) >= per_bucket:
            continue
        buckets[difficulty].append({
            "start": start,
            "target": target,
            "hops": hops,
            "difficulty": difficulty,
            "start_fame": fame.get(start, 0),
            "target_fame": fame.get(target, 0),
        })

    new_prompts = [p for bucket in buckets.values() for p in bucket]
    rng.shuffle(new_prompts)
    all_prompts = existing + new_prompts
    counts: dict[str, int] = {"easy": 0, "medium": 0, "hard": 0}
    for p in all_prompts:
        counts[p["difficulty"]] = counts.get(p["difficulty"], 0) + 1
    PROMPTS_PATH.write_text(json.dumps({
        "median_in_degree": median_deg,
        "fame_floor": FAME_FLOOR,
        "easy_floor": easy_floor,
        "medium_floor": medium_floor,
        "counts": counts,
        "prompts": all_prompts,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if extend:
        print(f"Added {len(new_prompts)} prompt(s) ({len(existing)} kept) "
              f"-> {len(all_prompts)} total at {PROMPTS_PATH}")
    else:
        print(f"Wrote {len(all_prompts)} prompts -> {PROMPTS_PATH}")
    print("  counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"  fame floors: easy>={easy_floor}, medium>={medium_floor}, "
          f"endpoints need >={FAME_FLOOR} langlinks "
          f"({len(titles)} of {len(data['titles'])} articles eligible)")
    if focus_list:
        print(f"  new prompts focused on {len(focus_list)} article(s).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-bucket", type=int, default=40)
    parser.add_argument("--max-pairs", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--extend", action="store_true",
                        help="keep existing prompts and append new ones")
    parser.add_argument("--focus-added", action="store_true",
                        help="bias new prompts toward articles added by the last "
                             "--extend snapshot build (reads meta last_added)")
    parser.add_argument("--focus", nargs="+", metavar="TITLE",
                        help="bias new prompts toward these article titles")
    args = parser.parse_args()
    generate(args.per_bucket, args.max_pairs, args.seed,
             extend=args.extend, focus=args.focus, focus_added=args.focus_added)


if __name__ == "__main__":
    main()
