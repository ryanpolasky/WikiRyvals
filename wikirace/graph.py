"""BFS shortest paths over the snapshot graph + difficulty bucketing."""

from __future__ import annotations

from collections import deque
from typing import Callable, Iterable


def induced_adjacency(adjacency: dict[str, list[str]]) -> dict[str, list[str]]:
    """Restrict edges to targets that are themselves nodes in the snapshot."""
    nodes = set(adjacency)
    return {n: [t for t in links if t in nodes] for n, links in adjacency.items()}


def shortest_hops(adjacency: dict[str, list[str]], start: str, target: str,
                  max_depth: int = 6) -> int | None:
    """Minimum number of clicks (edges) from start to target, or None if
    unreachable within max_depth."""
    if start == target:
        return 0
    if start not in adjacency:
        return None
    visited = {start}
    frontier: deque[tuple[str, int]] = deque([(start, 0)])
    while frontier:
        node, depth = frontier.popleft()
        if depth >= max_depth:
            continue
        for nxt in adjacency.get(node, ()):  # noqa: SIM118
            if nxt == target:
                return depth + 1
            if nxt not in visited:
                visited.add(nxt)
                frontier.append((nxt, depth + 1))
    return None


def shortest_hops_via(neighbors: Callable[[str], Iterable[str]], start: str,
                      target: str, max_depth: int = 6) -> int | None:
    """Like `shortest_hops`, but pulls each node's out-links from a callable
    instead of a fixed dict. Lets us BFS over a *merged* graph (e.g. the
    play-built graph unioned with the snapshot) without materializing it."""
    if start == target:
        return 0
    visited = {start}
    frontier: deque[tuple[str, int]] = deque([(start, 0)])
    while frontier:
        node, depth = frontier.popleft()
        if depth >= max_depth:
            continue
        for nxt in neighbors(node):
            if nxt == target:
                return depth + 1
            if nxt not in visited:
                visited.add(nxt)
                frontier.append((nxt, depth + 1))
    return None


def shortest_path_via(neighbors: Callable[[str], Iterable[str]], start: str,
                      target: str, max_depth: int = 6) -> list[str] | None:
    if start == target:
        return [start]
    parents: dict[str, str | None] = {start: None}
    frontier: deque[tuple[str, int]] = deque([(start, 0)])
    while frontier:
        node, depth = frontier.popleft()
        if depth >= max_depth:
            continue
        for nxt in sorted(set(neighbors(node))):
            if nxt in parents:
                continue
            parents[nxt] = node
            if nxt == target:
                path = [target]
                while parents[path[-1]] is not None:
                    path.append(parents[path[-1]])
                path.reverse()
                return path
            frontier.append((nxt, depth + 1))
    return None


def in_degrees(adjacency: dict[str, list[str]]) -> dict[str, int]:
    deg: dict[str, int] = {n: 0 for n in adjacency}
    for links in adjacency.values():
        for t in links:
            if t in deg:
                deg[t] += 1
    return deg


def bucket_difficulty(hops: int, start_fame: int, target_fame: int,
                      easy_floor: float, medium_floor: float) -> str:
    """Difficulty from how *recognizable* the endpoints are, not how far apart.

    Path length is a poor proxy for effort: `Cattle -> Pig` is easy at five hops
    because you can reason your way there, while two obscure articles two hops
    apart is hard because you have nothing to reason with. So the tier is set by
    the *less* famous endpoint - the one you can't lean on - and both ends count,
    since an unfamiliar starting article is just as disorienting as an
    unfindable target.

    `*_fame` is an interlanguage-link count (see snapshot.fetch_fame); the floors
    are corpus percentiles supplied by the caller. Hops only breaks the tie: a
    long route adds difficulty when you can't navigate by familiarity, but it
    can't make a pair of household names hard.
    """
    weak = min(start_fame, target_fame)
    if weak >= easy_floor:
        return "easy"
    tier = "medium" if weak >= medium_floor else "hard"
    if hops >= 5 and tier == "medium":
        tier = "hard"
    return tier
