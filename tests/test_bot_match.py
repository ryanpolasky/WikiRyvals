import time

import pytest
from fastapi import HTTPException

import wikirace.accounts as accounts_mod
import wikirace.app as app_mod
from wikirace.accounts import AccountStore
from wikirace.duos import DuoMatchMaker
from wikirace.matchmaking import MATCH_TTL, MatchMaker


def _user(uid, rating=1500, rp=0):
    return {
        "id": uid,
        "username": uid,
        "rating": rating,
        "rd": 200.0,
        "vol": 0.06,
        "rp": rp,
        "region": "NA",
        "tags": [],
    }


def _account(store, email, username, monkeypatch, *, admin=False):
    monkeypatch.setattr(accounts_mod, "CODE_RESEND_COOLDOWN_SECONDS", 0.0)
    user = store.verify_login_code(email, store.issue_login_code(email))
    store.set_profile(user["id"], username, "NA")
    if admin:
        store.set_admin(user["id"], True)
    return store.get_user(user["id"]), store.create_session(user["id"])


class _PlayGraph:
    stats = {"nodes": 0, "edges": 0}

    def record(self, title, links):
        return None

    def links_of(self, title):
        return []


@pytest.fixture
def bot_env(tmp_path, monkeypatch):
    store = AccountStore(path=tmp_path / "accounts.sqlite3")
    admin, admin_token = _account(
        store, "admin@example.com", "admin", monkeypatch, admin=True
    )
    other_admin, other_admin_token = _account(
        store, "other-admin@example.com", "otheradmin", monkeypatch, admin=True
    )
    plain, plain_token = _account(
        store, "plain@example.com", "plain", monkeypatch
    )
    prompt = {"start": "A", "target": "C", "hops": 2, "difficulty": "medium"}
    mm = MatchMaker(
        prompt_picker=lambda difficulty: prompt,
        par_fn=lambda start, target: 2,
        on_persist=store.save_active_match,
        on_forget=store.delete_active_match,
    )
    dmm = DuoMatchMaker(
        prompt_picker=lambda difficulty: prompt,
        par_fn=lambda start, target: 2,
        on_persist=store.save_active_match,
        on_forget=store.delete_active_match,
    )
    graph = {"A": {"B"}, "B": {"C"}, "C": set()}
    monkeypatch.setattr(app_mod, "accounts", store)
    monkeypatch.setattr(app_mod, "matchmaker", mm)
    monkeypatch.setattr(app_mod, "duo_matchmaker", dmm)
    monkeypatch.setattr(app_mod, "EXT_RACES", {})
    monkeypatch.setattr(app_mod, "RANKED_RESULTS", {})
    monkeypatch.setattr(app_mod, "play_graph", _PlayGraph())
    monkeypatch.setattr(app_mod, "_merged_neighbors", lambda node: set(graph.get(node, set())))
    return {
        "store": store,
        "admin": admin,
        "admin_token": admin_token,
        "other_admin": other_admin,
        "other_admin_token": other_admin_token,
        "plain": plain,
        "plain_token": plain_token,
        "mm": mm,
        "dmm": dmm,
        "graph": graph,
    }


def _launch(env):
    return app_mod.admin_bot_match(
        app_mod.AdminBotMatchReq(token=env["admin_token"], difficulty="medium")
    )


def _assert_status(status, call):
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == status


def test_practice_match_is_ephemeral_and_owner_scoped():
    persisted = []
    forgotten = []
    mm = MatchMaker(
        prompt_picker=lambda difficulty: {"start": "A", "target": "C", "hops": 2},
        par_fn=lambda start, target: 2,
        on_persist=lambda match_id, blob: persisted.append((match_id, blob)),
        on_forget=forgotten.append,
    )
    match = mm.create_bot_match(_user("admin"), "medium")
    assert match.mode == "practice"
    assert match.b.is_bot is True
    assert match.b.user_id == f"debug-bot:{match.match_id}"
    assert match.public("admin")["opponent"]["username"] == "Debug Bot"
    assert persisted == []
    assert mm.practice_context(match.match_id, "admin")["bot_user_id"] == match.b.user_id
    assert mm.practice_context(match.match_id, "stranger") is None
    assert mm.get_match(match.match_id, "stranger") is None
    assert mm.bind_race(match.match_id, match.b.user_id, "bot-race") is True
    match.b.last_seen = time.monotonic() - 100
    assert mm.timed_out_sides(time.monotonic(), 20) == []
    assert mm.submit(
        match.match_id,
        match.b.user_id,
        finished=False,
        clicks=0,
        time_ms=0,
        flagged=False,
    ) is None
    resolution = mm.submit(
        match.match_id,
        "admin",
        finished=True,
        clicks=2,
        time_ms=1000,
        flagged=False,
    )
    assert resolution["mode"] == "practice"
    match.last_touch = time.monotonic() - MATCH_TTL - 1
    mm._sweep()
    assert forgotten == []
    assert persisted == []


def test_race_ids_cannot_be_shared_between_matches():
    mm = MatchMaker(
        prompt_picker=lambda difficulty: {"start": "A", "target": "C", "hops": 2},
        par_fn=lambda start, target: 2,
    )
    first = mm.create_duel(_user("a1"), _user("a2"), "medium")
    second = mm.create_duel(_user("b1"), _user("b2"), "medium")
    assert mm.bind_race(first.match_id, "a1", "shared-race") is True
    assert mm.bind_race(first.match_id, "a2", "shared-race") is False
    assert mm.bind_race(second.match_id, "b1", "shared-race") is False
    assert mm.match_for_race("shared-race") == (first.match_id, "a1")


def test_duo_race_ids_cannot_be_shared_between_matches():
    dmm = DuoMatchMaker(
        prompt_picker=lambda difficulty: {"start": "A", "target": "C", "hops": 2},
        par_fn=lambda start, target: 2,
    )
    first_ticket = dmm.enqueue(_user("a1"), "medium")
    for uid in ("a2", "a3", "a4"):
        dmm.enqueue(_user(uid), "medium")
    second_ticket = dmm.enqueue(_user("b1"), "medium")
    for uid in ("b2", "b3", "b4"):
        dmm.enqueue(_user(uid), "medium")
    first_id = first_ticket.match_id
    second_id = second_ticket.match_id
    assert first_id and second_id and first_id != second_id
    assert dmm.bind_race(first_id, "a1", "shared-race") is True
    assert dmm.bind_race(first_id, "a2", "shared-race") is False
    assert dmm.bind_race(second_id, "b1", "shared-race") is False
    assert dmm.match_for_race("shared-race") == (first_id, "a1")


def test_bot_match_requires_admin_and_actions_require_owner(bot_env):
    env = bot_env
    _assert_status(
        401,
        lambda: app_mod.admin_bot_match(
            app_mod.AdminBotMatchReq(), authorization=None
        ),
    )
    _assert_status(
        403,
        lambda: app_mod.admin_bot_match(
            app_mod.AdminBotMatchReq(token=env["plain_token"])
        ),
    )
    launch = _launch(env)
    match_id = launch["match"]["match_id"]
    _assert_status(
        403,
        lambda: app_mod.admin_bot_action(
            app_mod.AdminBotActionReq(
                token=env["plain_token"], match_id=match_id, action="status"
            )
        ),
    )
    _assert_status(
        404,
        lambda: app_mod.admin_bot_action(
            app_mod.AdminBotActionReq(
                token=env["other_admin_token"], match_id=match_id, action="status"
            )
        ),
    )
    _assert_status(
        400,
        lambda: app_mod.admin_bot_action(
            app_mod.AdminBotActionReq(
                token=env["admin_token"], match_id=match_id, action="teleport"
            )
        ),
    )


def test_bot_controls_advance_flag_and_finish_on_legal_edges(bot_env):
    env = bot_env
    launch = _launch(env)
    match_id = launch["match"]["match_id"]
    assert launch["match"]["mode"] == "practice"
    assert launch["bot"]["current"] == "A"
    assert launch["bot"]["clicks"] == 0
    assert launch["bot"]["finished"] is False
    advanced = app_mod.admin_bot_action(
        app_mod.AdminBotActionReq(
            token=env["admin_token"], match_id=match_id, action="advance"
        )
    )
    assert advanced["bot"]["current"] == "B"
    assert advanced["bot"]["path"] == ["A", "B"]
    assert advanced["bot"]["finished"] is False
    flagged = app_mod.admin_bot_action(
        app_mod.AdminBotActionReq(
            token=env["admin_token"], match_id=match_id, action="flag"
        )
    )
    assert flagged["bot"]["flagged"] is True
    finished = app_mod.admin_bot_action(
        app_mod.AdminBotActionReq(
            token=env["admin_token"], match_id=match_id, action="finish"
        )
    )
    assert finished["bot"]["current"] == "C"
    assert finished["bot"]["path"] == ["A", "B", "C"]
    assert finished["bot"]["finished"] is True
    assert finished["bot"]["flagged"] is True


def test_bot_finish_is_clean_and_requires_a_known_route(bot_env, monkeypatch):
    env = bot_env
    launch = _launch(env)
    match_id = launch["match"]["match_id"]
    finished = app_mod.admin_bot_action(
        app_mod.AdminBotActionReq(
            token=env["admin_token"], match_id=match_id, action="finish"
        )
    )
    assert finished["bot"]["path"] == ["A", "B", "C"]
    assert finished["bot"]["finished"] is True
    assert finished["bot"]["flagged"] is False
    launch = _launch(env)
    match_id = launch["match"]["match_id"]
    monkeypatch.setattr(app_mod, "_merged_neighbors", lambda node: set())
    _assert_status(
        409,
        lambda: app_mod.admin_bot_action(
            app_mod.AdminBotActionReq(
                token=env["admin_token"], match_id=match_id, action="finish"
            )
        ),
    )


def test_idle_bot_resolves_without_ranked_or_persistent_side_effects(bot_env):
    env = bot_env
    launch = _launch(env)
    match_id = launch["match"]["match_id"]
    before = env["store"].get_user(env["admin"]["id"])
    race = app_mod.ext_new_race(start="A", target="C")
    bound = app_mod.mm_bind(
        app_mod.BindReq(
            token=env["admin_token"], match_id=match_id, race_id=race["race_id"]
        )
    )
    assert bound == {"ok": True, "debug_bot": True}
    app_mod.ext_visit(
        app_mod.ExtVisitRequest(
            race_id=race["race_id"], title="A", links=["B"], token=env["admin_token"]
        )
    )
    app_mod.ext_visit(
        app_mod.ExtVisitRequest(
            race_id=race["race_id"],
            title="B",
            links=["C"],
            via="B",
            via_from="A",
            token=env["admin_token"],
        )
    )
    state = app_mod.ext_visit(
        app_mod.ExtVisitRequest(
            race_id=race["race_id"],
            title="C",
            links=[],
            via="C",
            via_from="B",
            token=env["admin_token"],
        )
    )
    assert state["finished"] is True
    result = app_mod.RANKED_RESULTS[match_id][env["admin"]["id"]]
    assert result["status"] == "resolved"
    assert result["mode"] == "practice"
    assert result["ranked"] is False
    assert result["won"] is True
    assert result["rp"]["delta"] == 0
    assert result["rating"]["delta"] == 0
    assert set(app_mod.RANKED_RESULTS[match_id]) == {env["admin"]["id"]}
    after = env["store"].get_user(env["admin"]["id"])
    for key in ("rating", "rd", "vol", "rp", "games", "wins", "losses", "flags"):
        assert after[key] == before[key]
    assert env["store"].history(env["admin"]["id"]) == []
    assert env["store"].load_active_matches() == []
    assert env["store"].match_events(match_id) == []
    assert env["store"].season_stats(env["admin"]["id"]) is None
    _assert_status(
        409,
        lambda: app_mod.admin_bot_action(
            app_mod.AdminBotActionReq(
                token=env["admin_token"], match_id=match_id, action="advance"
            )
        ),
    )
    status = app_mod.admin_bot_action(
        app_mod.AdminBotActionReq(
            token=env["admin_token"], match_id=match_id, action="status"
        )
    )
    assert status["resolved"] is True
    assert status["result"]["mode"] == "practice"


def test_practice_is_isolated_from_queues_lobbies_and_real_matches(bot_env):
    env = bot_env
    env["mm"].enqueue(env["admin"], "medium")
    env["dmm"].enqueue(env["admin"], "medium")
    _launch(env)
    assert all(ticket.user_id != env["admin"]["id"] for ticket in env["mm"]._tickets.values())
    assert all(ticket.user_id != env["admin"]["id"] for ticket in env["dmm"]._tickets.values())
    _assert_status(
        409,
        lambda: app_mod.mm_enqueue(
            app_mod.EnqueueReq(token=env["admin_token"], difficulty="medium")
        ),
    )
    _assert_status(
        409,
        lambda: app_mod.mm_duo_enqueue(
            app_mod.EnqueueReq(token=env["admin_token"], difficulty="medium")
        ),
    )
    _assert_status(
        409,
        lambda: app_mod.lobby_create(
            app_mod.LobbyCreateReq(token=env["admin_token"], difficulty="medium")
        ),
    )


def test_real_match_blocks_bot_lab_launch(bot_env):
    env = bot_env
    lobby = env["mm"].create_lobby(env["admin"], "medium")
    env["mm"].join_lobby(lobby.code, env["plain"])
    _assert_status(409, lambda: _launch(env))


def test_found_match_ticket_survives_rejected_bot_lab_launch(bot_env):
    env = bot_env
    ticket = env["mm"].enqueue(env["admin"], "medium")
    env["mm"].enqueue(env["plain"], "medium")
    assert ticket.status == "found"
    _assert_status(409, lambda: _launch(env))
    assert env["mm"].poll(ticket.ticket_id)["status"] == "found"
    assert ticket.ticket_id in env["mm"]._tickets


def test_match_binding_rejects_started_wrong_route_and_nonparticipants(bot_env):
    env = bot_env
    launch = _launch(env)
    match_id = launch["match"]["match_id"]
    started = app_mod.ext_new_race(start="A", target="C")
    app_mod.ext_visit(
        app_mod.ExtVisitRequest(race_id=started["race_id"], title="A", links=["B"])
    )
    _assert_status(
        409,
        lambda: app_mod.mm_bind(
            app_mod.BindReq(
                token=env["admin_token"], match_id=match_id, race_id=started["race_id"]
            )
        ),
    )
    wrong = app_mod.ext_new_race(start="A", target="Z")
    _assert_status(
        400,
        lambda: app_mod.mm_bind(
            app_mod.BindReq(
                token=env["admin_token"], match_id=match_id, race_id=wrong["race_id"]
            )
        ),
    )
    race = app_mod.ext_new_race(start="A", target="C")
    app_mod.mm_bind(
        app_mod.BindReq(
            token=env["admin_token"], match_id=match_id, race_id=race["race_id"]
        )
    )
    _assert_status(
        404,
        lambda: app_mod.mm_bind(
            app_mod.BindReq(
                token=env["plain_token"], match_id=match_id, race_id=race["race_id"]
            )
        ),
    )
    _assert_status(
        403,
        lambda: app_mod.ext_visit(
            app_mod.ExtVisitRequest(
                race_id=race["race_id"], title="A", token=env["plain_token"]
            )
        ),
    )
    _assert_status(
        404,
        lambda: app_mod.mm_match(match_id, token=env["plain_token"]),
    )
