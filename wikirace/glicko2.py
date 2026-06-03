"""Glicko-2 rating system (Glickman 2012), implemented for 1v1 ranked matches.

Glicko-2 is preferred over plain Elo because it tracks a *rating deviation* (RD,
how uncertain we are about a player's rating) and a *volatility* (how erratic
their results are). With a sparse, intermittent playerbase that matters: new and
returning players' ratings move fast and confidently converge, while veterans
stay stable. See the spec (§5) for why we picked this over TrueSkill.

Public surface:
  * ``Rating`` dataclass: ``(rating, rd, vol)`` in the familiar 1500/350/0.06 scale.
  * ``rate_1v1(player, opponent, score)`` -> new ``Rating`` for ``player`` given a
    single match outcome (1.0 win / 0.5 draw / 0.0 loss) against ``opponent``'s
    pre-match rating. Call it once per player with the *opponent's* pre-match
    numbers so both updates use consistent inputs.
  * ``expected_score(player, opponent)`` -> win probability in [0, 1], used for
    matchmaking fit and variable-RP performance scaling.

The math is kept self-contained (no numpy) and unit-tested in tests/test_core.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# System constant: constrains how much volatility can change between periods.
# 0.5 is the value Glickman recommends for most applications.
TAU = 0.5
# Convergence tolerance for the volatility iteration.
EPSILON = 1e-6
# Glicko-2 works in an internal scale; 173.7178 converts to/from the public
# (Elo-like) scale where the default rating is 1500.
SCALE = 173.7178

DEFAULT_RATING = 1500.0
DEFAULT_RD = 350.0
DEFAULT_VOL = 0.06


@dataclass(frozen=True)
class Rating:
    rating: float = DEFAULT_RATING
    rd: float = DEFAULT_RD
    vol: float = DEFAULT_VOL

    # ---- conversions between public and internal (mu, phi) scales ----------
    @property
    def mu(self) -> float:
        return (self.rating - DEFAULT_RATING) / SCALE

    @property
    def phi(self) -> float:
        return self.rd / SCALE


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _e(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def expected_score(player: Rating, opponent: Rating) -> float:
    """Probability that ``player`` beats ``opponent`` (RD-aware)."""
    return _e(player.mu, opponent.mu, opponent.phi)


def _new_volatility(phi: float, delta: float, v: float, sigma: float) -> float:
    """Illinois-algorithm root find for the updated volatility (Glickman step 5)."""
    a = math.log(sigma * sigma)

    def f(x: float) -> float:
        ex = math.exp(x)
        num = ex * (delta * delta - phi * phi - v - ex)
        den = 2.0 * (phi * phi + v + ex) ** 2
        return num / den - (x - a) / (TAU * TAU)

    A = a
    if delta * delta > phi * phi + v:
        B = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * TAU) < 0:
            k += 1
        B = a - k * TAU

    fa, fb = f(A), f(B)
    while abs(B - A) > EPSILON:
        C = A + (A - B) * fa / (fb - fa)
        fc = f(C)
        if fc * fb <= 0:
            A, fa = B, fb
        else:
            fa /= 2.0
        B, fb = C, fc
    return math.exp(A / 2.0)


def rate_1v1(player: Rating, opponent: Rating, score: float) -> Rating:
    """Return ``player``'s updated rating after one match vs ``opponent``.

    ``score`` is 1.0 (win), 0.5 (draw) or 0.0 (loss). Uses the opponent's
    pre-match (rating, RD) so a symmetric call for the opponent is consistent.
    """
    mu, phi = player.mu, player.phi
    mu_j, phi_j = opponent.mu, opponent.phi

    g_j = _g(phi_j)
    e = _e(mu, mu_j, phi_j)

    v = 1.0 / (g_j * g_j * e * (1.0 - e))
    delta = v * g_j * (score - e)

    new_vol = _new_volatility(phi, delta, v, player.vol)

    phi_star = math.sqrt(phi * phi + new_vol * new_vol)
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    new_mu = mu + new_phi * new_phi * g_j * (score - e)

    new_rating = new_mu * SCALE + DEFAULT_RATING
    new_rd = new_phi * SCALE
    # Clamp RD to a sane band so a long win/loss streak can't collapse it to ~0
    # (which would freeze a player's rating) or balloon it unbounded.
    new_rd = max(30.0, min(DEFAULT_RD, new_rd))
    return Rating(rating=new_rating, rd=new_rd, vol=new_vol)


def season_soft_reset(r: Rating) -> Rating:
    """Compress a rating toward the mean and re-inflate RD for a new season, so
    veterans keep an edge but everyone gets a fresh, fast-moving climb (spec §5)."""
    compressed = DEFAULT_RATING + 0.5 * (r.rating - DEFAULT_RATING)
    return Rating(rating=compressed, rd=min(DEFAULT_RD, max(r.rd, 150.0)), vol=r.vol)
