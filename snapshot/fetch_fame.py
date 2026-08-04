"""Fetch an article "fame" signal for every page in the snapshot.

Difficulty is driven by how *recognizable* the endpoints are, not by how many
hops apart they sit: `Cow -> Pig` in 5 hops is easy because you can reason about
both words, while an obscure pair 2 hops apart is hard because you cannot. Raw
in-degree can't measure that - it only describes connectivity inside our own
1500-page crawl, which is topically skewed (it ranks `Mean anomaly` above
`Olympus Mons`).

Interlanguage link count is a far better proxy for real-world notability: a
concept every culture has an article for is a concept players recognize. We
fetch it once here and freeze it into the snapshot, so difficulty bucketing at
prompt-generation time stays entirely offline.

Usage:
    python -m snapshot.fetch_fame
    python -m snapshot.fetch_fame --refresh    # refetch titles already cached
"""

from __future__ import annotations

import argparse
import json
import time

import requests

from wikirace.snapshot_store import FAME_PATH, GRAPH_PATH
from wikirace.wiki import USER_AGENT

API_URL = "https://en.wikipedia.org/w/api.php"
# The API caps prop=langlinks at 50 titles per query for anonymous clients.
BATCH_SIZE = 50


def _get(session: requests.Session, params: dict, timeout: int,
         delay: float) -> requests.Response:
    """GET with backoff. Continuation makes this endpoint chatty enough to trip
    the API's rate limiter, and a 429 mid-run would otherwise drop a whole batch
    of 50 articles on the floor."""
    for attempt in range(6):
        resp = session.get(API_URL, params=params,
                           headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if resp.status_code not in (429, 503):
            resp.raise_for_status()
            return resp
        retry_after = resp.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else 0.0
        except ValueError:
            wait = 0.0
        time.sleep(max(wait, delay * (2 ** attempt), 1.0))
    resp.raise_for_status()
    return resp


def _fetch_batch(session: requests.Session, titles: list[str],
                 timeout: int, delay: float = 0.1) -> dict[str, int]:
    """Return {snapshot_title: langlink_count} for one batch of titles.

    MediaWiki applies `lllimit` across the whole multi-title result, so a batch
    routinely spills over several continuation rounds; we accumulate until the
    API stops handing back a `continue` token. Redirect/normalization mappings
    are folded back so counts land on the title the snapshot actually uses.
    """
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "prop": "langlinks",
        "lllimit": "max",
        "redirects": "1",
        "titles": "|".join(titles),
    }
    requested = set(titles)
    counts: dict[str, int] = {}
    alias: dict[str, set[str]] = {}  # resolved title -> titles we asked for
    while True:
        data = _get(session, params, timeout, delay).json()
        query = data.get("query", {})
        # `normalized` and `redirects` tell us how each requested title was
        # rewritten; chain them so the final page title maps back to our own.
        # A title can be BOTH requested in its own right and the redirect target
        # of another requested title (e.g. we hold "Animals" and "Animal"), so a
        # resolved page may owe its count to several of our titles at once -
        # attributing it to just one would zero out the other.
        for hop in ("normalized", "redirects"):
            for entry in query.get(hop, []) or []:
                frm, to = entry.get("from"), entry.get("to")
                alias.setdefault(to, set()).update(alias.get(frm) or {frm})
        for page in query.get("pages", []) or []:
            title = page.get("title")
            if title is None or page.get("missing"):
                continue
            n = len(page.get("langlinks") or [])
            owners = set(alias.get(title) or ())
            if title in requested:
                owners.add(title)
            for own in owners or {title}:
                counts[own] = counts.get(own, 0) + n
        cont = data.get("continue")
        if not cont:
            return counts
        params = {**params, **cont}
        if delay:
            time.sleep(delay)


def fetch(delay: float, timeout: int, refresh: bool) -> None:
    if not GRAPH_PATH.exists():
        raise SystemExit(
            "No snapshot graph found. Run `python -m snapshot.build_snapshot` first.")

    titles: list[str] = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))["titles"]

    # Resume support: a partial run leaves a usable file behind, so re-running
    # after a network blip only fetches what is still missing.
    known: dict[str, int] = {}
    if FAME_PATH.exists() and not refresh:
        try:
            known = json.loads(FAME_PATH.read_text(encoding="utf-8")).get("langlinks", {})
        except Exception:
            known = {}

    todo = [t for t in titles if t not in known]
    print(f"{len(titles)} article(s) in snapshot; {len(known)} cached, {len(todo)} to fetch.")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    done = 0
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        try:
            known.update(_fetch_batch(session, batch, timeout, delay))
        except requests.RequestException as exc:
            print(f"  ! batch failed ({batch[0]!r}...): {exc}")
            continue
        # Titles the API never returned (deleted/renamed since the crawl) would
        # otherwise be retried forever; pin them at 0 so the run terminates.
        for t in batch:
            known.setdefault(t, 0)
        done += len(batch)
        print(f"  [{min(done, len(todo))}/{len(todo)}] fetched")
        if delay:
            time.sleep(delay)

    payload = {
        "source": "langlinks",
        "articles": len(known),
        "langlinks": {t: known[t] for t in sorted(known)},
    }
    FAME_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = FAME_PATH.with_suffix(FAME_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(FAME_PATH)

    vals = sorted(known.values())
    if vals:
        print(f"\nDone. {len(known)} article(s) -> {FAME_PATH}")
        print(f"  langlinks  min={vals[0]}  median={vals[len(vals) // 2]}  max={vals[-1]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=0.1,
                        help="seconds to sleep between requests (be polite to the API)")
    parser.add_argument("--timeout", type=int, default=20,
                        help="per-request timeout in seconds")
    parser.add_argument("--refresh", action="store_true",
                        help="refetch every title instead of only the missing ones")
    args = parser.parse_args()
    fetch(args.delay, args.timeout, args.refresh)


if __name__ == "__main__":
    main()
