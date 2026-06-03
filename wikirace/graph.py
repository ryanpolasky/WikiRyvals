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


def in_degrees(adjacency: dict[str, list[str]]) -> dict[str, int]:
    deg: dict[str, int] = {n: 0 for n in adjacency}
    for links in adjacency.values():
        for t in links:
            if t in deg:
                deg[t] += 1
    return deg


def bucket_difficulty(hops: int, target_in_degree: int, median_in_degree: float) -> str:
    """Coarse difficulty: more hops and a more obscure (low in-degree) target are
    harder. This is a heuristic; production should recalibrate from real solve
    times (see spec §12)."""
    obscure = target_in_degree < median_in_degree
    if hops <= 2:
        return "hard" if obscure else "easy"
    if hops == 3:
        return "hard" if obscure else "medium"
    return "hard"
