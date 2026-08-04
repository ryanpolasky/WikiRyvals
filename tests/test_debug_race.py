"""Verbose anti-cheat tracing for solo races (/api/ext/new?debug=1).

The trace exists to explain false flags, so the tests that matter most are the
ones proving it is a pure observer: a traced run must reach exactly the same
verdicts as an untraced one, and nothing the client says in its diagnostics may
change a verdict.
"""

import pytest
from fastapi import HTTPException

import wikirace.accounts as accounts_mod
import wikirace.app as app_mod
from wikirace.accounts import AccountStore
from wikirace.duos import DuoMatchMaker
from wikirace.matchmaking import MatchMaker


class _PlayGraph:
    stats = {"nodes": 0, "edges": 0}

    def record(self, title, links):
        return None

    def links_of(self, title):
        return []


def _account(store, email, username, monkeypatch, *, admin=False):
    monkeypatch.setattr(accounts_mod, "CODE_RESEND_COOLDOWN_SECONDS", 0.0)
    user = store.verify_login_code(email, store.issue_login_code(email))
    store.set_profile(user["id"], username, "NA")
    if admin:
        store.set_admin(user["id"], True)
    return store.get_user(user["id"]), store.create_session(user["id"])


@pytest.fixture
def debug_env(tmp_path, monkeypatch):
    store = AccountStore(path=tmp_path / "accounts.sqlite3")
    admin, admin_token = _account(
        store, "admin@example.com", "admin", monkeypatch, admin=True
    )
    plain, plain_token = _account(store, "plain@example.com", "plain", monkeypatch)
    prompt = {"start": "A", "target": "C", "hops": 2, "difficulty": "medium"}
    mm = MatchMaker(prompt_picker=lambda d: prompt, par_fn=lambda s, t: 2)
    dmm = DuoMatchMaker(prompt_picker=lambda d: prompt, par_fn=lambda s, t: 2)
    monkeypatch.setattr(app_mod, "accounts", store)
    monkeypatch.setattr(app_mod, "matchmaker", mm)
    monkeypatch.setattr(app_mod, "duo_matchmaker", dmm)
    monkeypatch.setattr(app_mod, "EXT_RACES", {})
    monkeypatch.setattr(app_mod, "RANKED_RESULTS", {})
    monkeypatch.setattr(app_mod, "play_graph", _PlayGraph())
    monkeypatch.setattr(app_mod, "_merged_neighbors", lambda node: set())
    return {
        "store": store, "admin": admin, "admin_token": admin_token,
        "plain": plain, "plain_token": plain_token, "mm": mm,
    }


def _new(start, target, **kw):
    # authorization is a Header default; FastAPI passes None when the header is
    # absent, so direct calls have to do the same.
    kw.setdefault("authorization", None)
    return app_mod.ext_new_race(start=start, target=target, **kw)


def _dump(race_id, **kw):
    kw.setdefault("authorization", None)
    return app_mod.ext_race_debug(race_id, **kw)


def _visit(race_id, title, **kw):
    return app_mod.ext_visit(
        app_mod.ExtVisitRequest(race_id=race_id, title=title, **kw)
    )


def _assert_status(status, call):
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == status


# --------------------------------------------------------------- authorization

def test_debug_race_requires_an_admin(debug_env):
    _assert_status(401, lambda: _new("A", "C", debug=True))
    _assert_status(
        403, lambda: _new("A", "C", debug=True, token=debug_env["plain_token"]))


def test_admin_can_start_a_traced_race(debug_env):
    race = _new("A", "C", debug=True, token=debug_env["admin_token"])
    assert race["debug"] is True


def test_untraced_race_needs_no_token_and_is_not_traced(debug_env):
    race = _new("A", "C")
    assert race["debug"] is False
    assert app_mod.EXT_RACES[race["race_id"]].debug_log == []


def test_dump_is_admin_only_and_rejects_untraced_races(debug_env):
    traced = _new("A", "C", debug=True, token=debug_env["admin_token"])["race_id"]
    plain_race = _new("A", "C")["race_id"]
    _assert_status(401, lambda: _dump(traced))
    _assert_status(403, lambda: _dump(traced, token=debug_env["plain_token"]))
    _assert_status(400, lambda: _dump(plain_race, token=debug_env["admin_token"]))
    _assert_status(404, lambda: _dump("nope", token=debug_env["admin_token"]))


# ------------------------------------------------------------------- observer

@pytest.mark.parametrize("hops", [
    # (title, links, via, via_from, nav) sequences covering every verdict branch.
    [("A", ["B"], None, None, None), ("B", ["C"], "B", "A", None)],          # clean
    [("A", ["B"], None, None, None), ("Z", ["C"], None, None, None)],        # illegal
    [("A", ["B"], None, None, None), ("B", ["C"], None, None, "back_forward")],
    [("A", ["B"], None, None, None), ("Redir", ["C"], "B", "A", None)],      # redirect
    [("A", ["B"], None, None, None), ("B", ["C"], "C", "Ghost", None)],      # missed step
])
def test_tracing_never_changes_a_verdict(debug_env, hops):
    """The trace must be a pure observer - same inputs, same outcome."""
    def run(**extra):
        race = _new("A", "C", **extra)["race_id"]
        out = []
        for title, links, via, via_from, nav in hops:
            state = _visit(race, title, links=links, via=via,
                           via_from=via_from, nav=nav)
            out.append((state.get("legal"), state["flagged"], tuple(state["path"])))
        return out

    plain = run()
    traced = run(debug=True, token=debug_env["admin_token"])
    assert plain == traced


def test_client_diagnostics_cannot_launder_an_illegal_hop(debug_env):
    race = _new("A", "C", debug=True, token=debug_env["admin_token"])["race_id"]
    _visit(race, "A", links=["B"])
    state = _visit(race, "Z", links=[], client={
        "legal": True, "verified": True, "scrape": {"collected": 999},
    })
    assert state["legal"] is False
    assert state["flagged"] is True
    # Recorded verbatim for the human, but not consulted by validation.
    rec = app_mod.EXT_RACES[race].debug_log[-1]
    assert rec["client"]["legal"] is True
    assert rec["verdict"]["legal"] is False


# ---------------------------------------------------------------- trace content

def test_trace_explains_a_clean_hop_and_a_flagged_one(debug_env):
    race = _new("A", "C", debug=True, token=debug_env["admin_token"])["race_id"]
    _visit(race, "A", links=["B"])
    _visit(race, "B", links=["C"], via="B", via_from="A")
    _visit(race, "Z", links=[])
    dump = _dump(race, token=debug_env["admin_token"])

    assert dump["summary"]["hops"] == 2
    assert dump["summary"]["illegal_hops"] == 1
    assert dump["summary"]["links_seen_counts"]["A"] == 1
    kinds = [e.get("kind") for e in dump["events"]]
    assert "start" in kinds  # the first landing is on the timeline too

    hop = next(e for e in dump["events"] if e.get("reported", {}).get("title") == "B")
    assert hop["verdict"]["legal"] is True
    assert hop["checks"]["via_ok"] is True
    assert hop["checks"]["via_anchor_page"] == "A"
    assert "near_miss" not in hop  # only attached when something went wrong

    bad = next(e for e in dump["events"] if e.get("reported", {}).get("title") == "Z")
    assert bad["verdict"]["legal"] is False
    assert "ILLEGAL" in bad["verdict"]["reason"]
    assert bad["near_miss"]["title_found_on_pages"] == []


def test_trace_pinpoints_a_case_only_title_mismatch(debug_env):
    """The near-miss scan is what turns 'illegal hop' into 'your titles disagree'."""
    race = _new("A", "C", debug=True, token=debug_env["admin_token"])["race_id"]
    _visit(race, "A", links=["Bee gees"])
    _visit(race, "Bee Gees", links=[])
    rec = app_mod.EXT_RACES[race].debug_log[-1]
    assert rec["verdict"]["legal"] is False
    assert rec["near_miss"]["title_case_variants_in_prev"] == ["Bee gees"]


def test_trace_records_repeat_reports_and_is_bounded(debug_env, monkeypatch):
    monkeypatch.setattr(app_mod, "DEBUG_LOG_LIMIT", 5)
    race = _new("A", "C", debug=True, token=debug_env["admin_token"])["race_id"]
    _visit(race, "A", links=["B"])
    for _ in range(12):
        _visit(race, "A", links=["B"])  # reloads / in-page anchors
    log = app_mod.EXT_RACES[race].debug_log
    assert len(log) == 5
    assert log[-1]["kind"] == "same_page"


# ------------------------------------------------------------------- isolation

def test_traced_race_cannot_be_bound_to_a_match(debug_env):
    env = debug_env
    match = env["mm"].create_duel(
        {**env["admin"], "rating": 1500, "rd": 200.0, "vol": 0.06, "tags": []},
        {**env["plain"], "rating": 1500, "rd": 200.0, "vol": 0.06, "tags": []},
        "medium",
    )
    race = _new(match.start, match.target, debug=True,
                token=env["admin_token"])["race_id"]
    _assert_status(409, lambda: app_mod.mm_bind(app_mod.BindReq(
        token=env["admin_token"], match_id=match.match_id, race_id=race)))
