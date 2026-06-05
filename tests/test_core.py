"""Offline tests for the sanitizer, link extraction, and graph helpers (no network)."""

import time

import wikirace.accounts as accounts_mod
from wikirace.accounts import AccountError, AccountStore, RateLimitError
from wikirace.glicko2 import DEFAULT_RD, Rating, expected_score, rate_1v1
from wikirace.graph import (
    bucket_difficulty,
    in_degrees,
    induced_adjacency,
    shortest_hops,
    shortest_hops_via,
)
from wikirace.matchmaking import GRACE_MS, MatchMaker, Side, _score
from wikirace.play_graph import PlayGraph
from wikirace.ranks import (
    FEATURED_RP,
    PLACEMENT_GAMES,
    RYVAL_RP,
    compute_rp,
    next_tier_border,
    promo_zone_floor,
    rank_for_rp,
)
from wikirace.wiki import normalize_title, sanitize

SAMPLE_HTML = """
<html><body>
<section>
  <p><a rel="mw:WikiLink" href="./World_War_II">WWII</a> involved
     <a href="/wiki/France">France</a> and the
     <a href="//en.wikipedia.org/wiki/Soviet_Union">USSR</a>.</p>
  <p>See also <a href="./File:Flag.svg">a file</a>,
     <a href="./Category:Wars">a category</a>,
     <a href="https://example.com/external">an external link</a>,
     and <a href="#Notes">a fragment</a>.</p>
  <table class="navbox"><tr><td><a href="./Hidden_Navbox_Link">nav</a></td></tr></table>
  <sup class="reference"><a href="./Citation">[1]</a></sup>
</section>
</body></html>
"""


def test_normalize_title():
    assert normalize_title("./World_War_II") == "World War II"
    assert normalize_title("/wiki/France#History") == "France"
    assert normalize_title("napoleon") == "Napoleon"


def test_sanitize_extracts_only_article_links():
    html, links = sanitize(SAMPLE_HTML)
    assert links == ["World War II", "France", "Soviet Union"]
    # Namespaced/external/fragment links are flattened, not playable.
    assert "File:" not in html
    assert "example.com" not in html
    # Navbox + reference chrome is stripped, so their links never count.
    assert "Hidden Navbox Link" not in links
    assert "Citation" not in links
    # Playable links carry the data-title hook the frontend/validator rely on.
    assert 'data-title="France"' in html
    assert html.count('class="wr-link"') == 3


def test_shortest_hops():
    adj = {"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": ["E"], "E": []}
    assert shortest_hops(adj, "A", "A") == 0
    assert shortest_hops(adj, "A", "D") == 2
    assert shortest_hops(adj, "A", "E") == 3
    assert shortest_hops(adj, "E", "A") is None


def test_shortest_hops_via_merges_two_graphs():
    snapshot = {"A": ["B"], "B": ["C"]}      # A->B->C, no direct A->C
    play = {"A": ["C"]}                        # play graph learned a shortcut A->C

    def neighbors(node):
        return set(snapshot.get(node, ())) | set(play.get(node, ()))

    # Merged graph finds the 1-hop shortcut the snapshot alone can't.
    assert shortest_hops_via(neighbors, "A", "C") == 1
    # Snapshot-only would be 2 hops.
    assert shortest_hops(snapshot, "A", "C") == 2
    # Unknown nodes are unreachable -> None (caller hides par).
    assert shortest_hops_via(neighbors, "A", "Nowhere") is None


def test_induced_adjacency_drops_dangling_edges():
    adj = {"A": ["B", "Z"], "B": ["A"]}  # Z is not a node
    induced = induced_adjacency(adj)
    assert induced == {"A": ["B"], "B": ["A"]}


def test_in_degrees_and_difficulty():
    adj = {"A": ["B", "C"], "B": ["C"], "C": []}
    deg = in_degrees(adj)
    assert deg == {"A": 0, "B": 1, "C": 2}
    # 2-hop to a well-connected target is easy; to an obscure target is hard.
    assert bucket_difficulty(2, target_in_degree=10, median_in_degree=2) == "easy"
    assert bucket_difficulty(2, target_in_degree=0, median_in_degree=2) == "hard"
    assert bucket_difficulty(4, target_in_degree=10, median_in_degree=2) == "hard"


def test_play_graph_latest_observation_wins(tmp_path):
    pg = PlayGraph(path=tmp_path / "pg.json", flush_interval=0.0)
    pg.record("A", ["B", "C", "D"])
    assert pg.links_of("A") == ["B", "C", "D"]
    # Re-observing with a link removed prunes it (graph self-heals to live wiki).
    pg.record("A", ["B", "C"])
    assert pg.links_of("A") == ["B", "C"]
    # An empty observation must never wipe a node (failed/partial parse guard).
    pg.record("A", [])
    assert pg.links_of("A") == ["B", "C"]
    pg.flush()
    # Reloads from disk with the pruned set intact.
    pg2 = PlayGraph(path=tmp_path / "pg.json")
    assert pg2.links_of("A") == ["B", "C"]
    assert pg2.stats == {"nodes": 1, "edges": 2}


# --- Glicko-2 --------------------------------------------------------------

def test_glicko2_win_raises_rating_and_shrinks_rd():
    p = Rating()  # 1500 / 350 / 0.06
    opp = Rating()
    after = rate_1v1(p, opp, 1.0)
    assert after.rating > p.rating          # winning raises rating
    assert after.rd < p.rd                  # a result reduces uncertainty
    # A symmetric loss for the opponent moves them down by the same magnitude.
    opp_after = rate_1v1(opp, p, 0.0)
    assert opp_after.rating < opp.rating
    assert round(after.rating - 1500) == round(1500 - opp_after.rating)


def test_glicko2_expected_score_symmetry_and_favouritism():
    even = expected_score(Rating(1500), Rating(1500))
    assert abs(even - 0.5) < 1e-9
    fav = expected_score(Rating(1900, 50), Rating(1500, 50))
    assert fav > 0.8                        # strong favourite


def test_glicko2_rd_is_clamped():
    p = Rating(1500, 60, 0.06)
    # Many wins must not collapse RD to ~0 (which would freeze the rating).
    for _ in range(50):
        p = rate_1v1(p, Rating(1500, 60), 1.0)
    assert p.rd >= 30.0
    assert p.rd <= DEFAULT_RD


# --- ranks + variable RP ---------------------------------------------------

def test_rank_for_rp_boundaries():
    assert rank_for_rp(0).name == "Iron III"
    assert rank_for_rp(100).name == "Iron II"
    assert rank_for_rp(FEATURED_RP).name == "Featured"
    assert rank_for_rp(RYVAL_RP).name == "Ryval"
    # Ryval is the apex: no next rank.
    assert rank_for_rp(RYVAL_RP + 500).next_name is None
    # RP is floored, never negative.
    assert rank_for_rp(-50).name == "Iron III"


def test_variable_rp_scales_with_opponent_strength():
    me = Rating(1500, 60)
    beat_favourite = compute_rp(me, Rating(1900, 60), True).delta
    beat_underdog = compute_rp(me, Rating(1100, 60), True).delta
    assert beat_favourite > beat_underdog   # upsets are worth more
    # Losing to a much weaker player stings more than losing to a stronger one.
    lose_to_weak = compute_rp(me, Rating(1100, 60), False).delta
    lose_to_strong = compute_rp(me, Rating(1900, 60), False).delta
    assert lose_to_weak < lose_to_strong < 0


def test_placement_amplifies_rp():
    me, opp = Rating(1500, 60), Rating(1500, 60)
    normal = compute_rp(me, opp, True, placement=False).delta
    placing = compute_rp(me, opp, True, placement=True).delta
    assert placing > normal


# --- matchmaking -----------------------------------------------------------

def _mm():
    prompts = [{"start": "A", "target": "B", "hops": 3}]
    return MatchMaker(prompt_picker=lambda d: prompts[0], par_fn=lambda s, t: 3)


def _user(uid, rating=1500, rp=0, name=None, tags=None):
    return {"id": uid, "username": name or uid, "rating": rating, "rd": 200.0,
            "vol": 0.06, "rp": rp, "region": "NA", "tags": tags or []}


def test_matchmaking_pairs_two_close_players():
    mm = _mm()
    t1 = mm.enqueue(_user("u1", 1500), "any")
    mm.enqueue(_user("u2", 1520), "any")
    out = mm.poll(t1.ticket_id)
    assert out["status"] == "found"
    assert out["match"]["opponent"]["is_bot"] is False


def test_match_payload_exposes_opponent_tags():
    # An opponent's account tags (e.g. beta_tester) must ride along in the
    # match payload so the VS screen can render their badge.
    mm = _mm()
    t1 = mm.enqueue(_user("u1", 1500), "any")
    mm.enqueue(_user("u2", 1520, tags=["beta_tester"]), "any")
    out = mm.poll(t1.ticket_id)
    assert out["status"] == "found"
    assert out["match"]["opponent"]["tags"] == ["beta_tester"]
    # ...and survive a serialize/restore round-trip.
    blob = mm._matches[t1.match_id].to_dict()
    from wikirace.matchmaking import Match
    assert Match.from_dict(blob).b.tags == ["beta_tester"]


def test_poll_reports_searching_count():
    mm = _mm()
    t1 = mm.enqueue(_user("u1", 1500), "any")
    # One player alone: counts only themselves.
    assert mm.poll(t1.ticket_id)["searching"] == 1
    mm.enqueue(_user("u2", 1900), "any")  # far rating -> won't instantly pair
    # Both visible in the queue count.
    assert mm.poll(t1.ticket_id)["searching"] == 2


def test_matchmaking_solo_keeps_searching_without_opponent():
    mm = _mm()
    t = mm.enqueue(_user("solo", 1500), "any")
    # No bot/ghost fallback: even after a long wait, a lone queuer never matches.
    mm._tickets[t.ticket_id].enqueued_at -= 600
    mm.poll(t.ticket_id)
    assert mm._tickets[t.ticket_id].match_id is None
    assert mm._tickets[t.ticket_id].status == "searching"


def test_matchmaking_resolves_faster_finisher_as_winner():
    mm = _mm()
    t1 = mm.enqueue(_user("u1", 1500), "any")
    mm.enqueue(_user("u2", 1520), "any")
    mid = t1.match_id
    # u2 finishes well behind u1 (outside the grace window) -> u1 wins on time,
    # even though u1 took more clicks.
    assert mm.submit(mid, "u2", finished=True, clicks=2,
                     time_ms=60000, flagged=False) is None
    res = mm.submit(mid, "u1", finished=True, clicks=4,
                    time_ms=10000, flagged=False)
    assert res is not None
    a = res["a"] if res["a"]["side"].user_id == "u1" else res["b"]
    assert a["score"] == 1.0                # finished faster -> win


def test_matchmaking_flagged_finish_cannot_win():
    mm = _mm()
    t1 = mm.enqueue(_user("u1", 1500), "any")
    mm.enqueue(_user("u2", 1520), "any")
    mid = t1.match_id
    assert mm.submit(mid, "u2", finished=True, clicks=5,
                     time_ms=50000, flagged=False) is None
    # u1 is fastest AND fewest clicks, but flagged -> still can't win.
    res = mm.submit(mid, "u1", finished=True, clicks=2, time_ms=1, flagged=True)
    me = res["a"] if res["a"]["side"].user_id == "u1" else res["b"]
    assert me["score"] != 1.0               # flagged run can't be a win


def _gside(uid, *, finished=True, clicks=3, time_ms=10000, flagged=False):
    return Side(user_id=uid, username=uid, rating=Rating(1500), rp=0,
                finished=finished, clicks=clicks, time_ms=time_ms, flagged=flagged)


def test_grace_window_fewer_clicks_steals_within_window():
    # opp finished ~10s sooner, but I took the tighter route -> I steal it.
    me = _gside("me", clicks=3, time_ms=20000)
    opp = _gside("opp", clicks=6, time_ms=10000)
    assert abs(me.time_ms - opp.time_ms) <= GRACE_MS
    assert _score(me, opp) == 1.0
    assert _score(opp, me) == 0.0


def test_grace_window_same_clicks_faster_wins():
    # Within the window but tied on clicks -> the faster finisher wins.
    me = _gside("me", clicks=4, time_ms=10000)
    opp = _gside("opp", clicks=4, time_ms=20000)
    assert _score(me, opp) == 1.0
    assert _score(opp, me) == 0.0


def test_grace_window_missed_window_decided_on_time():
    # opp finished >GRACE_MS sooner: my fewer clicks don't matter, I was too slow.
    me = _gside("me", clicks=2, time_ms=40000)
    opp = _gside("opp", clicks=9, time_ms=10000)
    assert abs(me.time_ms - opp.time_ms) > GRACE_MS
    assert _score(me, opp) == 0.0
    assert _score(opp, me) == 1.0


def test_private_lobby_create_and_join_starts_match():
    mm = _mm()
    lobby = mm.create_lobby(_user("host"), "medium")
    assert len(lobby.code) == 6
    res = mm.join_lobby(lobby.code, _user("guest"))
    assert res.match_id is not None
    poll = mm.poll_lobby(lobby.code, "host")
    assert poll["status"] == "started"
    assert poll["match"]["mode"] == "private"


# --- accounts --------------------------------------------------------------

def test_account_login_code_flow_and_username(tmp_path, monkeypatch):
    # This test re-issues codes for the same email rapidly; disable the anti-bomb
    # cooldown here (rate-limiting has its own dedicated test below).
    monkeypatch.setattr(accounts_mod, "CODE_RESEND_COOLDOWN_SECONDS", 0.0)
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    code = store.issue_login_code("a@b.com")
    user = store.verify_login_code("a@b.com", code)
    assert user["needs_username"]
    # Same email logs back into the same account.
    code2 = store.issue_login_code("a@b.com")
    again = store.verify_login_code("a@b.com", code2)
    assert again["id"] == user["id"]
    # Wrong code is rejected.
    store.issue_login_code("a@b.com")
    try:
        store.verify_login_code("a@b.com", "000000")
        assert False, "bad code should raise"
    except AccountError:
        pass
    # Username uniqueness is enforced.
    store.set_profile(user["id"], "ryan", "NA")
    other = store.verify_login_code("c@d.com", store.issue_login_code("c@d.com"))
    try:
        store.set_profile(other["id"], "ryan", "EU")
        assert False, "duplicate username should raise"
    except AccountError:
        pass


def test_login_code_rate_limited(tmp_path, monkeypatch):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    # First request is fine; an immediate second one trips the per-email cooldown.
    store.issue_login_code("a@b.com")
    try:
        store.issue_login_code("a@b.com")
        assert False, "rapid re-request should be rate limited"
    except RateLimitError:
        pass
    # With the cooldown disabled, the rolling hourly cap still applies (use a
    # fresh email so the count starts clean).
    monkeypatch.setattr(accounts_mod, "CODE_RESEND_COOLDOWN_SECONDS", 0.0)
    for _ in range(accounts_mod.CODE_MAX_PER_HOUR):
        store.issue_login_code("cap@b.com")
    try:
        store.issue_login_code("cap@b.com")
        assert False, "hourly cap should be enforced"
    except RateLimitError:
        pass
    # A different email is unaffected by another address's limits.
    assert store.issue_login_code("other@b.com")


def test_account_session_and_result_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts_mod, "CODE_RESEND_COOLDOWN_SECONDS", 0.0)
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    user = store.verify_login_code("a@b.com", store.issue_login_code("a@b.com"))
    store.set_profile(user["id"], "ryan", "NA")
    token = store.create_session(user["id"])
    assert store.user_by_token(token)["id"] == user["id"]
    updated = store.apply_result(
        user["id"], Rating(1560, 200, 0.06), 30, won=True,
        time_ms=42000, flagged=False, is_placement=True,
    )
    assert updated["rp"] == 30
    assert updated["wins"] == 1 and updated["games"] == 1
    # Shows up on the leaderboard (has games > 0).
    board = store.leaderboard()
    assert board and board[0]["username"] == "ryan"


# --- Tier 1: durable matches + real-time channel ---------------------------

def test_match_serialization_round_trips():
    from wikirace.matchmaking import Match, Side

    mm = _mm()
    lobby = mm.create_lobby(_user("host", 1600), "medium")
    mm.join_lobby(lobby.code, _user("guest", 1580))
    mid = mm._matches and next(iter(mm._matches))
    original = mm._matches[mid]
    original.a.race_id = "race-a"
    original.a.clicks = 4
    original.a.submitted = True

    restored = Match.from_dict(original.to_dict())
    assert restored.match_id == original.match_id
    assert restored.start == original.start and restored.target == original.target
    assert restored.a.race_id == "race-a"
    assert restored.a.clicks == 4 and restored.a.submitted is True
    assert restored.b.user_id == original.b.user_id
    assert restored.a.rating.rating == original.a.rating.rating


def test_match_for_race_reverse_lookup():
    mm = _mm()
    t = mm.enqueue(_user("solo", 1500), "any")
    mm._tickets[t.ticket_id].enqueued_at -= GHOST_AFTER_SECONDS + 1
    match = mm.poll(t.ticket_id)["match"]
    mid = match["match_id"]
    mm.bind_race(mid, "solo", "race-xyz")
    assert mm.match_for_race("race-xyz") == (mid, "solo")
    assert mm.match_for_race("nope") is None


def test_active_match_persist_and_restore(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    # A matchmaker that persists live matches into the store, then a fresh one
    # that restores them (simulating a server restart).
    mm = MatchMaker(prompt_picker=lambda d: {"start": "A", "target": "B", "hops": 3},
                    par_fn=lambda s, t: 3,
                    on_persist=store.save_active_match,
                    on_forget=store.delete_active_match)
    lobby = mm.create_lobby(_user("host", 1600), "medium")
    mm.join_lobby(lobby.code, _user("guest", 1580))
    mid = next(iter(mm._matches))
    mm.bind_race(mid, "host", "race-h")

    # A brand-new matchmaker restores the in-flight match from the DB.
    mm2 = MatchMaker(prompt_picker=lambda d: None, par_fn=lambda s, t: 3,
                     on_persist=store.save_active_match,
                     on_forget=store.delete_active_match)
    assert mm2.restore(store.load_active_matches()) == 1
    assert mm2.get_match(mid, "host") is not None
    assert mm2.match_for_race("race-h") == (mid, "host")

    # Resolving forgets it, so it won't be restored again.
    mm2.submit(mid, "host", finished=True, clicks=2, time_ms=1000, flagged=False)
    mm2.submit(mid, "guest", finished=True, clicks=3, time_ms=2000, flagged=False)
    assert store.load_active_matches() == []


def test_match_events_append_and_replay(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    store.append_match_event("m1", "u1", seq=1, title="A", clicks=1, flagged=False, finished=False)
    store.append_match_event("m1", "u1", seq=2, title="B", clicks=2, flagged=False, finished=True)
    store.append_match_event("m1", "u2", seq=1, title="A", clicks=1, flagged=False, finished=False)
    events = store.match_events("m1")
    assert len(events) == 3
    u1 = [e for e in events if e["user_id"] == "u1"]
    assert [e["title"] for e in u1] == ["A", "B"]
    assert u1[-1]["finished"] == 1


def test_match_hub_broadcasts_to_room_members():
    import asyncio
    from wikirace.realtime import MatchHub

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, msg): self.sent.append(msg)

    async def scenario():
        hub = MatchHub()
        a, b, other = FakeWS(), FakeWS(), FakeWS()
        await hub.connect("m1", a)
        await hub.connect("m1", b)
        await hub.connect("m2", other)
        await hub.broadcast("m1", {"type": "progress", "clicks": 3})
        assert a.sent == b.sent == [{"type": "progress", "clicks": 3}]
        assert other.sent == []            # different room, untouched
        await hub.disconnect("m1", a)
        await hub.broadcast("m1", {"type": "resolved"})
        assert len(b.sent) == 2 and len(a.sent) == 1  # a no longer receives

    asyncio.run(scenario())


def test_match_hub_drops_dead_sockets():
    import asyncio
    from wikirace.realtime import MatchHub

    class DeadWS:
        async def send_json(self, msg): raise RuntimeError("closed")

    async def scenario():
        hub = MatchHub()
        await hub.connect("m1", DeadWS())
        await hub.broadcast("m1", {"x": 1})   # must not raise
        assert hub.room_size("m1") == 0       # dead socket pruned

    asyncio.run(scenario())


# --- daily challenge -------------------------------------------------------

def test_daily_first_attempt_is_official(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    r1 = store.record_daily_result("2026-06-01", "u1", "alice", "A", "Z",
                                   clicks=3, time_ms=9000, flagged=False, finished=True)
    assert r1["recorded"] is True
    # A faster second run does NOT overwrite the official first attempt.
    r2 = store.record_daily_result("2026-06-01", "u1", "alice", "A", "Z",
                                   clicks=1, time_ms=2000, flagged=False, finished=True)
    assert r2["recorded"] is False
    stored = store.daily_result("2026-06-01", "u1")
    assert stored["time_ms"] == 9000 and stored["clicks"] == 3


def test_daily_streak_extends_resets_and_breaks(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    # A real user row is needed for the streak counters to update.
    with store._lock:
        store._conn.execute(
            "INSERT INTO users (id,email,username,region,created_at,rating,rd,vol) "
            "VALUES ('u1','a@b.com','alice','NA',?,1500,350,0.06)", (time.time(),))
        store._conn.commit()

    def play(date, finished=True):
        store.record_daily_result(date, "u1", "alice", "A", "Z",
                                  clicks=3, time_ms=5000, flagged=False, finished=finished)

    play("2026-06-01")
    assert store.daily_streak_state("u1", "2026-06-01") == {
        "streak": 1, "best": 1, "played_today": True, "at_risk": False}
    # Consecutive day extends to 2.
    play("2026-06-02")
    assert store.daily_streak_state("u1", "2026-06-02")["streak"] == 2
    # A gap resets the live streak to 1 (best stays 2).
    play("2026-06-05")
    s = store.daily_streak_state("u1", "2026-06-05")
    assert s["streak"] == 1 and s["best"] == 2
    # The day after, the streak is alive-but-at-risk until today's daily is done.
    risk = store.daily_streak_state("u1", "2026-06-06")
    assert risk["at_risk"] is True and risk["played_today"] is False and risk["streak"] == 1
    # Two days idle and the streak is broken (display 0), best preserved.
    broken = store.daily_streak_state("u1", "2026-06-08")
    assert broken["streak"] == 0 and broken["best"] == 2 and broken["at_risk"] is False
    # A DNF (not finished) does not advance the streak.
    play("2026-06-08", finished=False)
    assert store.daily_streak_state("u1", "2026-06-08")["played_today"] is False


def test_top_ryval_is_most_faced_human_opponent(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    # No matches yet -> no ryval to float.
    assert store.top_rival("u1") is None

    def m(opp, result, bot=0, ts=0.0):
        store.record_match(user_id="u1", opponent=opp, opponent_bot=bot,
                           mode="ranked", result=result, created_at=ts)

    # nova faced 3x (2-1), zed faced once, and a ghost we should ignore.
    m("nova", "win", ts=1.0)
    m("nova", "loss", ts=2.0)
    m("nova", "win", ts=3.0)
    m("zed", "loss", ts=4.0)
    m("ghostly", "win", bot=1, ts=5.0)
    r = store.top_rival("u1")
    assert r["username"] == "nova"
    assert r["wins"] == 2 and r["losses"] == 1 and r["games"] == 3
    assert r["last_played"] == 3.0


def test_daily_board_orders_finishers_then_time_then_clicks(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    d = "2026-06-01"
    store.record_daily_result(d, "u1", "slow", "A", "Z", 2, 8000, False, True)
    store.record_daily_result(d, "u2", "fast", "A", "Z", 4, 4000, False, True)
    store.record_daily_result(d, "u3", "dnf", "A", "Z", 1, 1000, False, False)
    store.record_daily_result(d, "u4", "cheat", "A", "Z", 1, 500, True, True)
    board = store.daily_board(d)
    names = [r["username"] for r in board]
    # finishers (clean) first by time, then flagged finisher, then DNF last.
    assert names == ["fast", "slow", "cheat", "dnf"]
    assert board[0]["position"] == 1
    assert store.daily_count(d) == 4


def test_weekly_first_attempt_official_board_order_and_no_streak(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    # A real user row so we can prove the weekly puzzle never bumps the daily streak.
    with store._lock:
        store._conn.execute(
            "INSERT INTO users (id,email,username,region,created_at,rating,rd,vol) "
            "VALUES ('u1','a@b.com','alice','NA',?,1500,350,0.06)", (time.time(),))
        store._conn.commit()
    wk = "2026-W23"
    r1 = store.record_weekly_result(wk, "u1", "alice", "A", "Z",
                                    clicks=3, time_ms=9000, flagged=False, finished=True)
    assert r1["recorded"] is True
    # First attempt is official; a faster replay doesn't overwrite it.
    r2 = store.record_weekly_result(wk, "u1", "alice", "A", "Z",
                                    clicks=1, time_ms=2000, flagged=False, finished=True)
    assert r2["recorded"] is False
    assert store.weekly_result(wk, "u1")["time_ms"] == 9000
    # Weekly completion must NOT advance the daily-play streak.
    assert store.daily_streak_state("u1", "2026-06-01")["streak"] == 0
    # Board ordering matches the daily: clean finishers by time, flagged, then DNF.
    store.record_weekly_result(wk, "u2", "fast", "A", "Z", 4, 4000, False, True)
    store.record_weekly_result(wk, "u3", "dnf", "A", "Z", 1, 1000, False, False)
    store.record_weekly_result(wk, "u4", "cheat", "A", "Z", 1, 500, True, True)
    names = [r["username"] for r in store.weekly_board(wk)]
    assert names == ["fast", "alice", "cheat", "dnf"]
    assert store.weekly_count(wk) == 4


def _placed_user(store, uid="p1", username="pat", rp=0):
    """Insert a user already through placements at a chosen RP (so the promo
    gate, which only applies to ranked players, is in play)."""
    with store._lock:
        store._conn.execute(
            "INSERT INTO users (id,email,username,region,created_at,rating,rd,vol,"
            "rp,placement_games) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uid, uid + "@x.com", username, "NA", time.time(),
             1500, 80, 0.06, rp, PLACEMENT_GAMES))
        store._conn.commit()
    return uid


def test_promo_zone_helpers():
    # Every tier is 300 wide; borders sit at multiples of 300 up to Ryval.
    assert next_tier_border(0) == 300
    assert next_tier_border(260) == 300
    assert next_tier_border(2380) == RYVAL_RP == 2400
    assert next_tier_border(RYVAL_RP) is None      # apex: nothing above Ryval
    assert promo_zone_floor(300) == 270            # top 10% of the tier


def test_promo_triggers_then_win_crosses_border(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    uid = _placed_user(store, rp=260)              # Iron, just below the promo zone
    # A win into the top slice pins to 99% and arms the promo (no auto-promote).
    out = store.apply_result(uid, Rating(1520, 80, 0.06), 25, won=True,
                             time_ms=5000, flagged=False, is_placement=False)
    assert out["rp"] == 299                        # held at 99%, not 285
    assert out["promo"]["in_promo"] is True
    assert out["promo"]["target_name"] == "Bronze III"
    assert out["_promo"]["entered"] is True
    # The promo game: a win carries you over the border into the next tier.
    out2 = store.apply_result(uid, Rating(1540, 80, 0.06), 25, won=True,
                              time_ms=5000, flagged=False, is_placement=False)
    assert out2["rp"] == 324
    assert rank_for_rp(out2["rp"]).tier == "Bronze"
    assert out2["promo"]["in_promo"] is False
    assert out2["_promo"]["won"] is True


def test_promo_loss_drops_normal_amount_and_stays_in_tier(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    uid = _placed_user(store, rp=290)
    store.apply_result(uid, Rating(1520, 80, 0.06), 25, won=True,
                       time_ms=5000, flagged=False, is_placement=False)
    assert store.get_user(uid)["rp"] == 299        # pinned to 99%
    # Lose the promo: normal loss applies from the pin, you stay below the border.
    out = store.apply_result(uid, Rating(1490, 80, 0.06), -22, won=False,
                             time_ms=None, flagged=False, is_placement=False)
    assert out["rp"] == 277
    assert rank_for_rp(out["rp"]).tier == "Iron"
    assert out["promo"]["in_promo"] is False
    assert out["_promo"]["lost"] is True


def test_promo_not_armed_mid_tier_or_on_loss(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    uid = _placed_user(store, rp=100)
    out = store.apply_result(uid, Rating(1520, 80, 0.06), 25, won=True,
                             time_ms=5000, flagged=False, is_placement=False)
    assert out["rp"] == 125 and out["promo"]["in_promo"] is False
    # A loss never arms a promo even if it lands you in the zone is impossible,
    # but confirm a near-border loss just drops.
    uid2 = _placed_user(store, uid="p2", username="pat2", rp=295)
    out2 = store.apply_result(uid2, Rating(1480, 80, 0.06), -22, won=False,
                              time_ms=None, flagged=False, is_placement=False)
    assert out2["rp"] == 273 and out2["promo"]["in_promo"] is False


def test_promo_gates_the_top_into_ryval_but_not_above(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    uid = _placed_user(store, rp=2380)             # Legend, approaching Ryval
    out = store.apply_result(uid, Rating(2390, 70, 0.06), 25, won=True,
                             time_ms=5000, flagged=False, is_placement=False)
    assert out["rp"] == RYVAL_RP - 1
    assert out["promo"]["target_name"] == "Ryval"
    out2 = store.apply_result(uid, Rating(2400, 70, 0.06), 25, won=True,
                              time_ms=5000, flagged=False, is_placement=False)
    assert rank_for_rp(out2["rp"]).tier == "Ryval"
    # Above the Ryval floor there is no further border, so no promo ever arms.
    out3 = store.apply_result(uid, Rating(2410, 70, 0.06), 25, won=True,
                              time_ms=5000, flagged=False, is_placement=False)
    assert out3["promo"]["in_promo"] is False


def test_promo_skipped_during_placements(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    # Fresh user still in placements: a near-border win promotes normally, no gate.
    user = store.verify_login_code("a@b.com", store.issue_login_code("a@b.com"))
    store.set_profile(user["id"], "rook", "NA")
    with store._lock:
        store._conn.execute("UPDATE users SET rp=280 WHERE id=?", (user["id"],))
        store._conn.commit()
    out = store.apply_result(user["id"], Rating(1520, 200, 0.06), 25, won=True,
                             time_ms=5000, flagged=False, is_placement=True)
    assert out["rp"] == 305
    assert out["promo"]["in_promo"] is False


def test_daily_prompt_is_deterministic_per_date():
    # Same hashing logic the server uses: a given date always maps to one route.
    import hashlib
    pool = list(range(120))
    def pick(date):
        seed = int(hashlib.sha256(date.encode()).hexdigest(), 16)
        return pool[seed % len(pool)]
    assert pick("2026-06-01") == pick("2026-06-01")
    # Different days generally differ (not a hard guarantee, but true for these).
    assert pick("2026-06-01") != pick("2026-06-02")


# --- seasons ---------------------------------------------------------------

def test_season_autocreates_and_rolls_over(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    s1 = store.current_season()
    assert s1["label"] == "Season 1" and s1["status"] == "active"

    # Two players with different standings before rollover.
    u1 = store.verify_login_code("a@b.com", store.issue_login_code("a@b.com"))
    store.set_profile(u1["id"], "veteran", "NA")
    u2 = store.verify_login_code("c@d.com", store.issue_login_code("c@d.com"))
    store.set_profile(u2["id"], "rookie", "NA")
    # Give the veteran a big rating/RP lead.
    store.apply_result(u1["id"], Rating(rating=2200, rd=80, vol=0.06), rp_delta=1400,
                        won=True, time_ms=5000, flagged=False, is_placement=False)
    store.apply_result(u2["id"], Rating(rating=1600, rd=120, vol=0.06), rp_delta=300,
                        won=True, time_ms=9000, flagged=False, is_placement=False)
    vet_before = store.get_user(u1["id"])
    rp_before = vet_before["rp"]

    out = store.rollover_season("Season 2")
    assert out["archived"] == 2
    assert out["season"]["label"] == "Season 2" and out["season"]["status"] == "active"
    assert store.current_season()["label"] == "Season 2"

    # Final standings of S1 archived, veteran on top with a tier reward.
    standings = store.season_standings(s1["id"])
    assert standings[0]["username"] == "veteran" and standings[0]["position"] == 1
    assert standings[0]["reward"] and "(Season 1)" in standings[0]["reward"]

    # Soft reset: RP halved, rating compressed toward the mean, placements reopened.
    vet_after = store.get_user(u1["id"])
    assert vet_after["rp"] == rp_before // 2
    assert vet_after["rating"] < vet_before["rating"]   # compressed down
    assert vet_after["rating"] > 1500                   # but still above the mean
    assert vet_after["placement_games"] == 0            # re-places next season


# --- duos (2v2) ------------------------------------------------------------

def _dmm():
    prompts = [{"start": "A", "target": "B", "hops": 3}]
    from wikirace.duos import DuoMatchMaker
    return DuoMatchMaker(prompt_picker=lambda d: prompts[0], par_fn=lambda s, t: 3)


def test_duos_groups_four_into_two_balanced_teams():
    mm = _dmm()
    t1 = mm.enqueue(_user("u1", 1500), "any")
    mm.enqueue(_user("u2", 1480), "any")
    mm.enqueue(_user("u3", 1520), "any")
    mm.enqueue(_user("u4", 1510), "any")
    out = mm.poll(t1.ticket_id)
    assert out["status"] == "found"
    m = out["match"]
    # You + a teammate vs two opponents, all human.
    assert m["teammate"] is not None
    assert len(m["opponents"]) == 2
    # Snake seeding balances the teams: strongest pairs with weakest.
    match = mm._matches[m["match_id"]]
    avg_a = sum(s.rating.rating for s in match.team_a) / 2
    avg_b = sum(s.rating.rating for s in match.team_b) / 2
    assert abs(avg_a - avg_b) <= 30


def test_duos_ghost_fills_a_short_queue():
    mm = _dmm()
    t = mm.enqueue(_user("solo", 1500), "any")
    mm._tickets[t.ticket_id].enqueued_at -= GHOST_AFTER_SECONDS + 1
    out = mm.poll(t.ticket_id)
    assert out["status"] == "found"
    m = out["match"]
    match = mm._matches[m["match_id"]]
    # Four seats total, exactly one human (the soloist), three ghosts.
    assert len(match.all_sides()) == 4
    assert sum(1 for s in match.all_sides() if s.user_id is None) == 3


def test_duos_team_wins_on_fastest_finisher():
    from wikirace.duos import team_score
    from wikirace.matchmaking import Side

    def side(uid, t_ms, clicks=3, finished=True, flagged=False):
        s = Side(uid, uid, Rating(1500, 200, 0.06), 0)
        s.finished, s.time_ms, s.clicks, s.flagged = finished, t_ms, clicks, flagged
        return s

    # Team A's best (4s) beats team B's best (6s) even though A also has a slowpoke.
    team_a = [side("a1", 9000), side("a2", 4000)]
    team_b = [side("b1", 6000), side("b2", 7000)]
    assert team_score(team_a, team_b) == 1.0
    assert team_score(team_b, team_a) == 0.0


def test_duos_flagged_finish_cannot_clinch_for_team():
    from wikirace.duos import team_score
    from wikirace.matchmaking import Side

    def side(uid, t_ms, flagged=False, finished=True):
        s = Side(uid, uid, Rating(1500, 200, 0.06), 0)
        s.finished, s.time_ms, s.clicks, s.flagged = finished, t_ms, 3, flagged
        return s

    # A's only fast finish is flagged; its clean finisher is slower than B's.
    team_a = [side("a1", 1000, flagged=True), side("a2", 8000)]
    team_b = [side("b1", 5000), side("b2", 9000)]
    assert team_score(team_a, team_b) == 0.0   # flagged run can't clinch


def test_duos_resolves_when_all_humans_submit():
    mm = _dmm()
    t1 = mm.enqueue(_user("u1", 1500), "any")
    mm.enqueue(_user("u2", 1500), "any")
    mm.enqueue(_user("u3", 1500), "any")
    mm.enqueue(_user("u4", 1500), "any")
    m = mm.poll(t1.ticket_id)["match"]
    mid = m["match_id"]
    humans = [s.user_id for s in mm._matches[mid].all_sides() if s.user_id]
    assert len(humans) == 4
    # First three submissions keep the match open.
    for uid in humans[:3]:
        assert mm.submit(mid, uid, finished=True, clicks=3, time_ms=5000,
                         flagged=False) is None
    res = mm.submit(mid, humans[3], finished=True, clicks=3, time_ms=5000, flagged=False)
    assert res is not None
    assert "team_a" in res and "team_b" in res and "a_score" in res


def test_duos_match_serialization_round_trips():
    from wikirace.duos import DuoMatch

    mm = _dmm()
    t = mm.enqueue(_user("solo", 1500), "any")
    mm._tickets[t.ticket_id].enqueued_at -= GHOST_AFTER_SECONDS + 1
    m = mm.poll(t.ticket_id)["match"]
    original = mm._matches[m["match_id"]]
    original.team_a[0].race_id = "race-x"
    restored = DuoMatch.from_dict(original.to_dict())
    assert restored.match_id == original.match_id
    assert len(restored.team_a) == 2 and len(restored.team_b) == 2
    assert restored.to_dict()["kind"] == "duo"
    assert any(s.race_id == "race-x" for s in restored.all_sides())


def test_duos_persist_and_restore_only_duo_blobs(tmp_path):
    from wikirace.duos import DuoMatchMaker

    store = AccountStore(path=tmp_path / "acc.sqlite3")
    # A 1v1 match and a duos match both persisted to the same store.
    one = MatchMaker(prompt_picker=lambda d: {"start": "A", "target": "B", "hops": 3},
                     par_fn=lambda s, t: 3, on_persist=store.save_active_match,
                     on_forget=store.delete_active_match)
    lobby = one.create_lobby(_user("host", 1600), "medium")
    one.join_lobby(lobby.code, _user("guest", 1580))

    duo = DuoMatchMaker(prompt_picker=lambda d: {"start": "A", "target": "B", "hops": 3},
                        par_fn=lambda s, t: 3, on_persist=store.save_active_match,
                        on_forget=store.delete_active_match)
    t = duo.enqueue(_user("solo", 1500), "any")
    duo._tickets[t.ticket_id].enqueued_at -= GHOST_AFTER_SECONDS + 1
    duo.poll(t.ticket_id)

    blobs = store.load_active_matches()
    # Each matcher restores only its own kind.
    duo2 = DuoMatchMaker(prompt_picker=lambda d: None, par_fn=lambda s, t: 3)
    one2 = MatchMaker(prompt_picker=lambda d: None, par_fn=lambda s, t: 3)
    assert duo2.restore(blobs) == 1
    assert one2.restore(blobs) == 1


# --- Friends + party (duo with a friend) -----------------------------------

def _account(store, email, username, region="NA"):
    u = store.verify_login_code(email, store.issue_login_code(email))
    store.set_profile(u["id"], username, region)
    return store.get_user(u["id"])


def test_friend_request_accept_and_list(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    a = _account(store, "a@b.com", "ace")
    b = _account(store, "c@d.com", "bolt")

    out = store.send_friend_request(a["id"], "bolt")
    assert out["status"] == "pending"
    # Pending shows up as outgoing for A, incoming for B.
    assert store.list_friends(a["id"])["outgoing"][0]["username"] == "bolt"
    assert store.list_friends(b["id"])["incoming"][0]["username"] == "ace"
    assert not store.are_friends(a["id"], b["id"])

    store.respond_friend_request(b["id"], a["id"], accept=True)
    assert store.are_friends(a["id"], b["id"])
    # Now mutual, no longer pending on either side.
    fa = store.list_friends(a["id"])
    assert [f["username"] for f in fa["friends"]] == ["bolt"]
    assert fa["incoming"] == [] and fa["outgoing"] == []


def test_friend_request_by_unknown_username_and_self_rejected(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    a = _account(store, "a@b.com", "ace")
    for bad in [lambda: store.send_friend_request(a["id"], "nobody"),
                lambda: store.send_friend_request(a["id"], "ace")]:
        try:
            bad()
            assert False, "should raise"
        except AccountError:
            pass


def test_mutual_pending_request_auto_accepts(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    a = _account(store, "a@b.com", "ace")
    b = _account(store, "c@d.com", "bolt")
    store.send_friend_request(a["id"], "bolt")
    # B sends back to A while A's request is pending -> becomes friends.
    out = store.send_friend_request(b["id"], "ace")
    assert out["status"] == "accepted"
    assert store.are_friends(a["id"], b["id"])


def test_remove_friend(tmp_path):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    a = _account(store, "a@b.com", "ace")
    b = _account(store, "c@d.com", "bolt")
    store.send_friend_request(a["id"], "bolt")
    store.respond_friend_request(b["id"], a["id"], accept=True)
    store.remove_friend(a["id"], b["id"])
    assert not store.are_friends(a["id"], b["id"])
    assert store.list_friends(a["id"])["friends"] == []


def test_duos_party_seats_friends_on_the_same_team():
    mm = _dmm()
    # Two friends queue as a premade party; two strangers solo-queue. Ratings
    # are chosen so the plain snake seed (strongest+weakest) would SPLIT the
    # friends apart -- proving the party override keeps them together.
    p = "party-1"
    t1 = mm.enqueue(_user("f1", 1500), "any", party_id=p)
    mm.enqueue(_user("f2", 1520), "any", party_id=p)
    mm.enqueue(_user("s1", 1505), "any")
    mm.enqueue(_user("s2", 1495), "any")
    out = mm.poll(t1.ticket_id)
    assert out["status"] == "found"
    match = mm._matches[out["match"]["match_id"]]
    team_ids = [{s.user_id for s in match.team_a}, {s.user_id for s in match.team_b}]
    # The two friends land on one team; the strangers form the other.
    assert {"f1", "f2"} in team_ids
    assert {"s1", "s2"} in team_ids


def test_duos_party_ghost_fills_opposing_pair():
    mm = _dmm()
    p = "party-1"
    t1 = mm.enqueue(_user("f1", 1500), "any", party_id=p)
    mm.enqueue(_user("f2", 1520), "any", party_id=p)
    # No opponents show up: party waits past the ghost threshold then fills.
    for t in mm._tickets.values():
        t.enqueued_at -= GHOST_AFTER_SECONDS + 1
    out = mm.poll(t1.ticket_id)
    assert out["status"] == "found"
    match = mm._matches[out["match"]["match_id"]]
    team_ids = [{s.user_id for s in match.team_a}, {s.user_id for s in match.team_b}]
    # Friends together; the opposing seats are ghosts (user_id None).
    assert {"f1", "f2"} in team_ids
    assert {None} in team_ids


# --- Admin + account tagging -----------------------------------------------

def _mk_user(store, email, username, monkeypatch):
    monkeypatch.setattr(accounts_mod, "CODE_RESEND_COOLDOWN_SECONDS", 0.0)
    u = store.verify_login_code(email, store.issue_login_code(email))
    store.set_profile(u["id"], username, "NA")
    return store.get_user(u["id"])


def test_admin_grant_and_revoke(tmp_path, monkeypatch):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    u = _mk_user(store, "a@b.com", "ryan", monkeypatch)
    assert u["is_admin"] is False
    assert store.list_admins() == []
    store.set_admin(u["id"], True)
    assert store.get_user(u["id"])["is_admin"] is True
    assert [a["username"] for a in store.list_admins()] == ["ryan"]
    store.set_admin(u["id"], False)
    assert store.get_user(u["id"])["is_admin"] is False
    assert store.list_admins() == []


def test_account_tag_crud_and_validation(tmp_path, monkeypatch):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    u = _mk_user(store, "a@b.com", "ryan", monkeypatch)
    assert store.tags_for(u["id"]) == []
    assert store.add_tag(u["id"], "Beta_Tester")["tags"] == ["beta_tester"]  # normalized
    # Idempotent: re-adding the same tag doesn't duplicate it.
    assert store.add_tag(u["id"], "beta_tester")["tags"] == ["beta_tester"]
    assert store.get_user(u["id"])["tags"] == ["beta_tester"]
    assert store.remove_tag(u["id"], "beta_tester")["tags"] == []
    for bad in ("bad tag", "no!!", "x" * 33, ""):
        try:
            store.add_tag(u["id"], bad)
            assert False, f"bad tag {bad!r} should raise"
        except AccountError:
            pass
    try:
        store.add_tag("ghost", "beta_tester")
        assert False, "tagging a missing account should raise"
    except AccountError:
        pass


def test_search_and_find_accounts(tmp_path, monkeypatch):
    store = AccountStore(path=tmp_path / "acc.sqlite3")
    ryan = _mk_user(store, "ryan@x.com", "ryan", monkeypatch)
    nova = _mk_user(store, "nova@x.com", "novabeta", monkeypatch)
    store.add_tag(nova["id"], "beta_tester")
    # find_user resolves by username or email, case-insensitively.
    assert store.find_user("RYAN")["id"] == ryan["id"]
    assert store.find_user("nova@x.com")["id"] == nova["id"]
    assert store.find_user("nobody") is None
    # search by substring hits username and email; admin card carries email+tags.
    hits = {a["username"]: a for a in store.search_accounts("nova")}
    assert set(hits) == {"novabeta"}
    assert hits["novabeta"]["email"] == "nova@x.com"
    assert hits["novabeta"]["tags"] == ["beta_tester"]
    # Empty query returns accounts (newest first), capped by limit.
    assert len(store.search_accounts("")) == 2


def test_admin_api_is_session_gated(tmp_path, monkeypatch):
    import wikirace.app as app_mod
    from wikirace.app import AdminTagReq
    from fastapi import HTTPException

    store = AccountStore(path=tmp_path / "acc.sqlite3")
    monkeypatch.setattr(app_mod, "accounts", store)
    admin = _mk_user(store, "admin@x.com", "boss", monkeypatch)
    plain = _mk_user(store, "plain@x.com", "rando", monkeypatch)
    store.set_admin(admin["id"], True)
    admin_tok = store.create_session(admin["id"])
    plain_tok = store.create_session(plain["id"])

    # No token at all -> 401.
    try:
        app_mod.admin_accounts(q="", token=None, authorization=None)
        assert False, "anonymous should be rejected"
    except HTTPException as e:
        assert e.status_code == 401
    # Non-admin session -> 403.
    try:
        app_mod.admin_accounts(q="", token=plain_tok)
        assert False, "non-admin should be 403"
    except HTTPException as e:
        assert e.status_code == 403
    try:
        app_mod.admin_tag(AdminTagReq(token=plain_tok, user_id=plain["id"], tag="beta_tester"))
        assert False, "non-admin tag should be 403"
    except HTTPException as e:
        assert e.status_code == 403

    # Admin session: search + tag + untag round-trip works end to end.
    out = app_mod.admin_accounts(q="rando", token=admin_tok)
    assert [a["username"] for a in out["accounts"]] == ["rando"]
    tagged = app_mod.admin_tag(AdminTagReq(token=admin_tok, user_id=plain["id"], tag="beta_tester"))
    assert tagged["ok"] and tagged["tags"] == ["beta_tester"]
    # Tag surfaces in the target's own me payload (drives the badge).
    assert store.get_user(plain["id"])["tags"] == ["beta_tester"]
    untagged = app_mod.admin_untag(AdminTagReq(token=admin_tok, user_id=plain["id"], tag="beta_tester"))
    assert untagged["ok"] and untagged["tags"] == []
