# WikiRyvals

**"Find your Ryval."** WikiRyvals is competitive, **ranked** Wikiracing: race from
one Wikipedia article to another using only in-article links, on a real skill
ladder with matchmaking, ranks, and seasons. The casual wiki-game space is crowded,
but nobody ships a true ranked ladder + skill-based matchmaking - that's the wedge
("Chess.com for Wikiracing").

It ships as a **Manifest V3 Chrome extension** that plays on **live
`en.wikipedia.org`**, backed by a **FastAPI** service that owns every race's state
and **validates every hop server-side** (the anti-cheat core). No Wikipedia content
is hosted by us and there's no scraping - all network calls go through the
extension's background worker.

See [`docs/SPEC.md`](docs/SPEC.md) for the full product/architecture spec (rating
math, SBMM, anti-cheat) and [`docs/NEXT-STEPS.md`](docs/NEXT-STEPS.md) for the
roadmap and what's shipped.

## Status

Phase 0 (snapshot/BFS difficulty pipeline + server-validated solo race) and Phase 1
(accounts, Glicko-2 ranks, matchmaking, lobbies) are **done and verified**, along
with later tiers:

- **Real-time** WebSocket matches (live opponent progress, instant resolution).
- **Daily** challenge, **weekly** puzzle, and **seasons** with soft reset.
- Ranked **Duos (2v2)** with balanced teams and shared team EP.
- **Friends** + **party invites** (queue duos with a friend on the same team).
- **Spectator / watch-party** page and an **admin** dashboard.

`pytest` is green (**59 offline tests**).

> **Naming:** the visible ladder score shows as **Edit Points (EP)** in the UI; the
> internal data field is still `rp`.

## Game modes

- **Ranked 1v1** - both players get the same start + target and race
  simultaneously; first to the target wins (tiebreak: fewer clicks), a flagged
  finish can't win. Drives Glicko-2 rating + EP.
- **Quick Match** - solo race against **par** (no opponent), good for warmups.
- **Ranked Duos (2v2)** - four near-rated players snake-seed into two balanced
  teams (ghosts fill empty seats); a team's result is its fastest clean finisher and
  both teammates share one EP delta.
- **Daily challenge** - one shared start->target per UTC day, one ranked attempt
  (first finish locks in; replays are practice), board sorted finishers -> clean ->
  fastest -> fewest clicks.
- **Weekly puzzle** - a rotating weekly route with its own board.
- **Private rooms** - code-based 1v1 (create -> 6-char code -> opponent joins by
  code), unranked.
- **Your Ryval** - the app floats your most-faced human opponent as a recurring
  rival.

## Ranking system

Two numbers per player, deliberately separated (see `wikirace/ranks.py`):

- **Rating (Glicko-2)** - rating / RD / volatility (start 1500 / 350 / 0.06). The
  honest, mostly-hidden skill estimate that drives matchmaking and the expected
  score used to scale EP.
- **EP (Edit Points)** - the visible ladder number. It only moves on ranked results
  and the amount is **variable**: beating a favourite or winning cleanly (few clicks
  vs par, big time margin) is worth more than scraping past an underdog.

**Ladder:** Iron, Bronze, Silver, Gold, Platinum, Diamond (each split into divisions
III / II / I), then the single-division apex tiers **Featured, Legend, Ryval**.
Hitting Ryval = "find your Ryval." The first **5** ranked games are hidden
placements; rank reveals after the 5th. A CS2-style **promo series** gates tier
promotions.

**Matchmaking (SBMM):** pair the closest-rated compatible opponent within a rating
window (starts +/-120, widens ~+/-45/sec, caps +/-1400) so a small pool never
deadlocks. If no human appears within ~8s, a believable **async ghost** (jittered
rating, simulated clicks-over-par + per-hop reading time) fills the queue so a solo
player still gets a real match.

**Seasons:** auto-created Season 1; an admin-gated rollover archives final standings
(with an end-of-season tier reward) and soft-resets everyone (rating compressed
toward 1500, RD re-inflated, EP halved, placements reopened).

## Anti-cheat

The server owns each race's state (start, target, **current page**, click path,
clock). Because the browser loads real Wikipedia directly, validation is *post-hoc*:
each time the content script reports the page the player landed on, the server checks
whether it was a legal link from the page they were previously on. The link set is
taken (in priority order) from what the content script saw on that previous page, the
play-built graph, then the snapshot - never a bare client claim about the
destination. Illegal hops (URL-bar jumps, search-box bypass, back-button abuse) are
**flagged**, a flagged path can't count as a clean finish, and the full path is
stored for replay/audit. The on-page search box is disabled and Ctrl+F is suppressed
while a race is live.

**Par (optimal click count).** Par is a BFS shortest path over the **merged** graph:
the play-built adjacency (learned from real races, no API crawl) unioned with the
seed snapshot. If neither graph can reach the target within the search depth, par is
**hidden entirely** (no fake guess) in both the HUD and the finish card. As the play
graph fills in from real play, par appears for more matchups over time
(self-improving, never spamming Wikipedia). The **missed-win** callout only needs the
links on pages the player actually walked through, so it works on any race.

## Repo layout

### Backend - `wikirace/` (FastAPI)

| File                | Role                                                                                                                                                   |
|---------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------|
| `wiki.py`           | Fetch + sanitize Wikipedia article HTML; extract the internal-link set. Single source of truth for "what's a valid link."                              |
| `snapshot_store.py` | Loads the frozen snapshot; lazily fetches + caches any article a player clicks into so play isn't limited to the snapshot.                             |
| `graph.py`          | BFS, merged-graph shortest path, in-degrees, difficulty bucketing.                                                                                     |
| `play_graph.py`     | SQLite adjacency graph grown incrementally from real play (self-healing, "latest observation wins"); powers par + missed-win without crawling the API. |
| `glicko2.py`        | Glicko-2 rating math (rating/RD/volatility, expected score, season soft-reset).                                                                        |
| `ranks.py`          | The EP ladder (Iron -> Ryval, divisions + apex) and variable EP on top of Glicko-2.                                                                    |
| `accounts.py`       | SQLite accounts: passwordless email login, sessions, ratings, match history, dailies/weeklies, seasons, friends.                                       |
| `matchmaking.py`    | In-memory 1v1 ranked queue, async ghost fallback, and code-based private lobbies.                                                                      |
| `duos.py`           | 2v2 ranked queue + matches (parallel to `matchmaking`), balanced teams, shared team EP.                                                                |
| `realtime.py`       | WebSocket pub/sub hub keyed by match id (live opponent progress + instant resolution).                                                                 |
| `admin.py`          | Helpers behind the gated `/api/ext/admin/*` routes (account search, tagging, season rollover).                                                         |
| `app.py`            | The FastAPI app: wires every `/api/ext/*` route + WebSockets, server-authoritative hop validation, par, and result/rating finalization.                |

### Snapshot pipeline - `snapshot/`

| File                                      | Role                                                                                                  |
|-------------------------------------------|-------------------------------------------------------------------------------------------------------|
| `build_snapshot.py`                       | BFS-crawl the MediaWiki API from seed articles into a bounded, frozen snapshot (graph + cached HTML). |
| `generate_prompts.py`                     | BFS shortest-paths over the snapshot -> difficulty-bucketed (easy/medium/hard) race prompts.          |
| `seeds.py`                                | High-in-degree, well-connected seed articles for the crawl.                                           |
| `make_icons.py` / `gen_division_icons.py` | Generate the WR logo PNGs + rank/division SVG emblems.                                                |

### Chrome extension - `extension/` (MV3)

| File                                    | Role                                                                                                                                                            |
|-----------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `manifest.json`                         | MV3 manifest - content script on `*://en.wikipedia.org/wiki/*`, service worker, side panel, storage + host permissions.                                         |
| `config.js`                             | Single source of truth for the backend origin (change one line for production).                                                                                 |
| `background.js`                         | Service worker: the single network/authority layer. Opens the side panel, calls the backend, persists race state, navigates/creates tabs.                       |
| `content.js` / `content.css`            | Injects the HUD, tracks link clicks + URL changes, reports hops, renders the finish overlay, neutralizes search + Ctrl+F mid-race, and hides donation banners.  |
| `lobby.html` / `lobby.css` / `lobby.js` | The side-panel lobby - Play (Ranked 1v1 / Quick / Daily / Custom / Duos), Friends, Rooms, Board, History, Profile, plus match-found and ranked-results screens. |
| `watch.html` / `watch.js`               | Standalone read-only watch-party page - spectate a match live over the spectate WebSocket.                                                                      |
| `admin.html` / `admin.js`               | Admin dashboard (account search, tagging, season rollover) for `is_admin` accounts.                                                                             |
| `fonts/`                                | Bundled **Linux Libertine** serif (Wikipedia's wordmark font, SIL OFL - `OFL.txt` included), subsetted to woff2 for the WikiRyvals wordmark.                    |
| `icons/`                                | Toolbar/manifest PNGs + the 9 rank emblems + division icons.                                                                                                    |

### Other

- `site/` - standalone marketing/landing page (`index.html`); open it directly in a browser, no server needed.
- `docs/` - product spec, audits, test reports, deploy + privacy + store-listing notes.
- `Dockerfile`, `docker-compose.yml`, `deploy/.env.example` - containerized backend.
- `tests/test_core.py` - the offline unit-test suite.

## Quickstart (local dev)

```bash
# 1. install (Python 3.11+)
uv venv && uv pip install -e .

# 2. build a bounded snapshot (the shipped one is ~1500 articles, ~15 min;
#    use a smaller --max-articles for a quick local build)
python -m snapshot.build_snapshot --max-articles 1500

# 3. generate difficulty-bucketed prompts (shipped pool is 250 per bucket = 750)
python -m snapshot.generate_prompts --per-bucket 250 --max-pairs 80000

# 4. run the backend
uvicorn wikirace.app:app --reload --port 8011
```

Generated data lands in `data/` (gitignored) - rebuild anytime with the commands
above. Accounts, the play graph, daily/season state, etc. are created on first run.

Then load the extension:

1. Open `chrome://extensions/`, enable **Developer mode** (top-right).
2. Click **Load unpacked** and select the `extension/` folder.
3. Click the toolbar icon - the **WikiRyvals lobby opens as a docked side panel**
   next to whatever you're browsing (real Wikipedia stays in the main area).
4. **Sign in** with your email -> 6-digit code (in dev-auth mode the code is shown
   in the response/logs, no mail server needed), then pick a username.
5. Hit **Find a ranked match** (or Quick / Daily / Duos / Custom) - the start
   article opens in a new tab with the HUD (start->target, clicks, timer, par).
   Navigate using only in-article links to reach the target.

The backend only exposes the extension API (`/api/ext/*`) - there's no standalone
web game; you play through the Chrome extension on live Wikipedia.

## Run with Docker

A single self-contained FastAPI container with the prebuilt snapshot baked in (no
crawl, no external DB). Durable state (accounts, ratings, match history, the
self-growing play graph) lives on a named volume.

```bash
cp deploy/.env.example .env      # fill in the SMTP_* block for real email
docker compose up -d             # health: GET /api/ext/health -> {"ok": true}
```

Build/refresh the snapshot (steps 2-3 above) before building the image so it gets
baked in. Full details, reverse-proxy/WebSocket notes, and the scaling caveat are in
[`docs/DEPLOY.md`](docs/DEPLOY.md).

## Configuration (environment variables)

| Var                                                  | Default                                                 | Notes                                                                                                                                           |
|------------------------------------------------------|---------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------|
| `WIKIRYVALS_PORT`                                    | `8011`                                                  | Host port published by compose. Drop if proxying.                                                                                               |
| `WIKIRYVALS_ACCOUNTS`                                | `data/accounts.sqlite3` (`/app/pgdata/...` in Docker)   | Accounts/sessions/matches DB.                                                                                                                   |
| `WIKIRYVALS_PLAY_GRAPH`                              | `data/play_graph.sqlite3` (`/app/pgdata/...` in Docker) | Self-growing play graph.                                                                                                                        |
| `WIKIRYVALS_SMTP_HOST`                               | _(empty)_                                               | **Empty = dev-auth mode** (login code returned in the API response + logged). **Set it in production** to email codes and disable the dev leak. |
| `WIKIRYVALS_SMTP_PORT` / `_USER` / `_PASS` / `_FROM` | `587` / empty / empty / `noreply@wikiryvals.com`        | SMTP delivery for login codes.                                                                                                                  |
| `WIKIRYVALS_ADMIN_TOKEN`                             | _(unset)_                                               | Gates the admin endpoints (account tagging, season rollover).                                                                                   |

To point the extension at a deployed backend, change the one line in
`extension/config.js` and the matching `host_permissions` entry in
`extension/manifest.json` (Chrome needs that URL literally).

## Tests

```bash
uv pip install -e ".[dev]"
pytest -q
```

**59 offline tests** (no network): the sanitizer/link-extraction, the graph +
difficulty helpers, Glicko-2 math, ranks + variable EP, matchmaking (pairing,
ghost fallback, lobbies, resolution rules), accounts/sessions/persistence + match
replay, the real-time hub, and the daily challenge.

## API surface

Everything is under `/api/ext/`:

- **Core race:** `POST /new`, `POST /visit`, `GET /race/{id}`, `GET /health`.
- **Auth/account:** `POST /auth/request-code`, `/auth/verify`, `/auth/profile`,
  `/auth/logout`; `GET /auth/username-available`, `/me`, `/rival`.
- **Daily / weekly / seasons:** `GET|POST /daily*`, `/weekly*`, `GET /season*`.
- **Matchmaking 1v1:** `POST /mm/enqueue|cancel|bind|result`, `GET /mm/poll`,
  `/mm/match/{id}`, `/mm/match/{id}/events`.
- **Duos:** `POST /mm/duo/enqueue|cancel`, `GET /mm/duo/poll`.
- **Private lobbies:** `POST /lobby/create|join`, `GET /lobby/poll`.
- **Friends + parties:** `POST /friends/request|respond|remove`, `GET /friends`;
  `POST /party/invite|accept|decline|cancel`, `GET /party/incoming|poll`.
- **Board / history / spectate:** `GET /leaderboard`, `/history`, `/spectate/{id}`.
- **Admin (gated):** `GET /admin/accounts`, `POST /admin/tag|untag`,
  `/admin/season/rollover`.
- **WebSockets:** `/ws/match/{id}` (players), `/ws/spectate/{id}` (read-only).

## Docs

- [`docs/SPEC.md`](docs/SPEC.md) - the v0 product + architecture spec.
- [`docs/NEXT-STEPS.md`](docs/NEXT-STEPS.md) - roadmap + what's shipped.
- [`docs/DEPLOY.md`](docs/DEPLOY.md) - deployment, env vars, proxy/WS, scaling.
- [`docs/PRIVACY.md`](docs/PRIVACY.md) / [`docs/STORE-LISTING.md`](docs/STORE-LISTING.md) - Chrome Web Store materials.

## Notes / tradeoffs

- The **snapshot** drives prompts + BFS difficulty; for navigability the live app
  also lazily fetches + caches any article a player clicks into, and the play graph
  self-heals to match live Wikipedia. Production should serve a fully frozen
  per-season snapshot (see spec section 6).
- Difficulty buckets use BFS min-hops + target obscurity (in-degree) - a heuristic
  to be recalibrated from real solve-time data (spec section 12).
- The backend runs **one uvicorn worker on purpose** - race/match/graph state lives
  in-process. Horizontal scaling needs a shared store (e.g. Redis) first; one box
  comfortably handles hundreds of concurrent racers.
- Wikipedia content is CC BY-SA; attribute it and respect API etiquette (serving
  snapshots, not live calls per request, is the right long-term move).
