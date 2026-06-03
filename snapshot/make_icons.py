"""Generate WikiRyvals rank icons + WR logo/favicon as standalone SVGs.

Run from the repo root:  python -m snapshot.make_icons
Writes to extension/icons/ (logo + favicon) and extension/icons/ranks/.

Each rank icon is a gem-shield badge: a tier-coloured gradient body with a
metallic rim and a tier-specific emblem (stacked chevrons for the climb,
a star for Featured, a crown for Legend, and the "Ry" monogram for Ryval).
The design is intentionally simple and flat so it reads at 16px in the
leaderboard and 64px on the results screen.
"""

from __future__ import annotations

import pathlib

ICONS = pathlib.Path(__file__).resolve().parent.parent / "extension" / "icons"
RANKS = ICONS / "ranks"

# tier slug -> (dark stop, light stop, rim colour)
TIERS = {
    "iron":     ("#52575c", "#888d92", "#3a3e42"),
    "bronze":   ("#9c5a21", "#cd7f4a", "#7a431a"),
    "silver":   ("#9aa1a8", "#e2e8ee", "#777d83"),
    "gold":     ("#d9a21b", "#ffd968", "#a9790b"),
    "plat":     ("#2f9d8f", "#79e6d6", "#1f7c70"),
    "diamond":  ("#2f7fd6", "#7cc0ff", "#1f5ea8"),
    "featured": ("#c79200", "#ffd54a", "#9a6f00"),
    "legend":   ("#d2641e", "#ff9d4d", "#a44b12"),
    "ryval":    ("#2a4bd0", "#6699ff", "#1d3596"),
}

# Number of chevrons that mark the climb (purely decorative tier signal).
CHEVRONS = {"iron": 1, "bronze": 2, "silver": 3, "gold": 4, "plat": 5, "diamond": 6}


def shield_path() -> str:
    # A rounded gem/shield silhouette centred in a 64x64 box.
    return ("M32 4 L56 14 L56 34 Q56 52 32 60 Q8 52 8 34 L8 14 Z")


def chevrons(n: int) -> str:
    out = []
    base_y = 42
    for i in range(n):
        y = base_y - i * 7
        out.append(
            f'<path d="M22 {y} L32 {y-7} L42 {y}" fill="none" '
            f'stroke="#ffffff" stroke-width="3.2" stroke-linecap="round" '
            f'stroke-linejoin="round" opacity="{0.95 - i*0.06:.2f}"/>'
        )
    return "\n    ".join(out)


def star(cx: float, cy: float, r: float) -> str:
    import math
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.45
        pts.append(f"{cx + rad*math.cos(ang):.1f},{cy + rad*math.sin(ang):.1f}")
    return f'<polygon points="{" ".join(pts)}" fill="#ffffff"/>'


def crown() -> str:
    return (
        '<path d="M18 40 L18 26 L26 33 L32 22 L38 33 L46 26 L46 40 Z" '
        'fill="#ffffff"/>'
        '<rect x="18" y="42" width="28" height="5" rx="1.5" fill="#ffffff"/>'
    )


def ry_monogram() -> str:
    return (
        '<text x="32" y="42" text-anchor="middle" '
        'font-family="Georgia, \'Times New Roman\', serif" font-size="26" '
        'font-weight="700" fill="#ffffff">Ry</text>'
    )


def emblem(slug: str) -> str:
    if slug in CHEVRONS:
        return chevrons(CHEVRONS[slug])
    if slug == "featured":
        return star(32, 34, 15)
    if slug == "legend":
        return crown()
    if slug == "ryval":
        return ry_monogram()
    return ""


def rank_svg(slug: str) -> str:
    dark, light, rim = TIERS[slug]
    apex = slug == "ryval"
    glow = (
        '<filter id="g" x="-20%" y="-20%" width="140%" height="140%">'
        '<feGaussianBlur stdDeviation="1.4"/></filter>'
        if apex else ""
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" role="img" aria-label="{slug} rank">
  <defs>
    <linearGradient id="body" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{light}"/>
      <stop offset="1" stop-color="{dark}"/>
    </linearGradient>
    {glow}
  </defs>
  <path d="{shield_path()}" fill="{rim}"/>
  <path d="{shield_path()}" fill="url(#body)" transform="translate(32 32) scale(0.88) translate(-32 -32)"/>
  <path d="M32 7 L52 15.5 Q52 17 32 24 Q12 17 12 15.5 Z" fill="#ffffff" opacity="0.16"/>
  {emblem(slug)}
</svg>
'''


# Wikipedia-esque logo: a white "page" tile with a faint puzzle-globe nod
# (meridians/latitudes) and a black serif "WR" monogram with a white halo so
# it stays crisp over the arcs.
WR_LOGO = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" role="img" aria-label="WikiRyvals">
  <rect x="3" y="3" width="58" height="58" rx="12" fill="#ffffff"/>
  <rect x="3.5" y="3.5" width="57" height="57" rx="11.5" fill="none" stroke="#a2a9b1" stroke-width="1.5"/>
  <g stroke="#cdd2d9" stroke-width="1.4" fill="none">
    <circle cx="32" cy="32" r="21"/>
    <ellipse cx="32" cy="32" rx="8.5" ry="21"/>
    <ellipse cx="32" cy="32" rx="16.5" ry="21"/>
    <line x1="11" y1="32" x2="53" y2="32"/>
    <path d="M13.5 21 Q32 16 50.5 21"/>
    <path d="M13.5 43 Q32 48 50.5 43"/>
  </g>
  <text x="32" y="42" text-anchor="middle" font-family="'Linux Libertine','Georgia','Times New Roman',serif" font-size="28" font-weight="700" fill="#202122" letter-spacing="-1" stroke="#ffffff" stroke-width="3.4" paint-order="stroke">WR</text>
</svg>
'''

# Favicon: same idea, simplified globe (one meridian + equator) so it stays
# clean down to 16px.
FAVICON = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32" role="img" aria-label="WikiRyvals">
  <rect x="1" y="1" width="30" height="30" rx="7" fill="#ffffff"/>
  <rect x="1.5" y="1.5" width="29" height="29" rx="6.5" fill="none" stroke="#a2a9b1" stroke-width="1.3"/>
  <g stroke="#cdd2d9" stroke-width="1.2" fill="none">
    <circle cx="16" cy="16" r="11"/>
    <ellipse cx="16" cy="16" rx="4.5" ry="11"/>
    <line x1="5" y1="16" x2="27" y2="16"/>
  </g>
  <text x="16" y="21.5" text-anchor="middle" font-family="'Linux Libertine','Georgia','Times New Roman',serif" font-size="15" font-weight="700" fill="#202122" letter-spacing="-0.5" stroke="#ffffff" stroke-width="2.6" paint-order="stroke">WR</text>
</svg>
'''


def main() -> None:
    RANKS.mkdir(parents=True, exist_ok=True)
    for slug in TIERS:
        (RANKS / f"{slug}.svg").write_text(rank_svg(slug), encoding="utf-8")
    (ICONS / "wr-logo.svg").write_text(WR_LOGO, encoding="utf-8")
    (ICONS / "favicon.svg").write_text(FAVICON, encoding="utf-8")
    print(f"wrote {len(TIERS)} rank icons + logo + favicon to {ICONS}")


if __name__ == "__main__":
    main()
