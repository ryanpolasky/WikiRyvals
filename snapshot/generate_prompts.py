"""Generate difficulty-bucketed race prompts from the snapshot graph.

Samples (start, target) pairs, computes BFS shortest hops over the induced
snapshot graph, and buckets them into easy/medium/hard. The output feeds the
prototype's "new race" endpoint.

Usage:
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
from wikirace.snapshot_store import GRAPH_PATH, PROMPTS_PATH


def generate(per_bucket: int, max_pairs: int, seed: int) -> None:
    if not GRAPH_PATH.exists():
        raise SystemExit("No snapshot graph found. Run `python -m snapshot.build_snapshot` first.")

    data = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    adjacency = induced_adjacency(data["adjacency"])
    titles = [t for t in data["titles"] if adjacency.get(t)]

    deg = in_degrees(adjacency)
    median_deg = statistics.median(deg.values()) if deg else 0.0

    rng = random.Random(seed)
    buckets: dict[str, list[dict]] = {"easy": [], "medium": [], "hard": []}
    seen_pairs: set[tuple[str, str]] = set()

    attempts = 0
    target_total = per_bucket * 3
    while attempts < max_pairs and sum(len(v) for v in buckets.values()) < target_total:
        attempts += 1
        start, target = rng.sample(titles, 2)
        if (start, target) in seen_pairs:
            continue
        seen_pairs.add((start, target))

        hops = shortest_hops(adjacency, start, target, max_depth=5)
        if hops is None or hops < 2:
            continue

        difficulty = bucket_difficulty(hops, deg.get(target, 0), median_deg)
        if len(buckets[difficulty]) >= per_bucket:
            continue
        buckets[difficulty].append({
            "start": start,
            "target": target,
            "hops": hops,
            "difficulty": difficulty,
        })

    prompts = [p for bucket in buckets.values() for p in bucket]
    rng.shuffle(prompts)
    PROMPTS_PATH.write_text(json.dumps({
        "median_in_degree": median_deg,
        "counts": {k: len(v) for k, v in buckets.items()},
        "prompts": prompts,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote {len(prompts)} prompts -> {PROMPTS_PATH}")
    print(f"  counts: " + ", ".join(f"{k}={len(v)}" for k, v in buckets.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-bucket", type=int, default=40)
    parser.add_argument("--max-pairs", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    generate(args.per_bucket, args.max_pairs, args.seed)


if __name__ == "__main__":
    main()
