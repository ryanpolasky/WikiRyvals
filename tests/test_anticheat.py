import pytest

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


@pytest.fixture
def race_env(tmp_path, monkeypatch):
    store = AccountStore(path=tmp_path / "accounts.sqlite3")
    prompt = {"start": "A", "target": "C", "hops": 2, "difficulty": "medium"}
    mm = MatchMaker(
        prompt_picker=lambda difficulty: prompt,
        par_fn=lambda start, target: 2,
    )
    dmm = DuoMatchMaker(
        prompt_picker=lambda difficulty: prompt,
        par_fn=lambda start, target: 2,
    )
    monkeypatch.setattr(app_mod, "accounts", store)
    monkeypatch.setattr(app_mod, "matchmaker", mm)
    monkeypatch.setattr(app_mod, "duo_matchmaker", dmm)
    monkeypatch.setattr(app_mod, "EXT_RACES", {})
    monkeypatch.setattr(app_mod, "RANKED_RESULTS", {})
    monkeypatch.setattr(app_mod, "play_graph", _PlayGraph())
    monkeypatch.setattr(app_mod, "_merged_neighbors", lambda node: set())
    return store


def _new_race(start, target):
    return app_mod.ext_new_race(start=start, target=target)["race_id"]


def _visit(race_id, title, links=None, via=None, via_from=None, nav=None):
    return app_mod.ext_visit(
        app_mod.ExtVisitRequest(
            race_id=race_id,
            title=title,
            links=links,
            via=via,
            via_from=via_from,
            nav=nav,
        )
    )


def _hop(race_id, title, **kw):
    state = _visit(race_id, title, **kw)
    assert "legal" in state
    return state


def test_legal_click_is_not_flagged(race_env):
    race = _new_race("A", "C")
    _visit(race, "A", links=["B", "C"])
    state = _hop(race, "B", links=["C"], via="B", via_from="A")
    assert state["legal"] is True
    assert state["verified"] is True
    assert state["flagged"] is False
    assert state["clicks"] == 1
    assert state["path"] == ["A", "B"]


def test_redirect_hop_is_legal_when_the_clicked_link_was_on_the_page(race_env):
    race = _new_race("A", "New York City")
    _visit(race, "A", links=["Nyc"])
    state = _hop(race, "New York City", links=[], via="Nyc", via_from="A")
    assert state["legal"] is True
    assert state["flagged"] is False
    assert state["finished"] is True


def test_hop_from_an_unobserved_page_is_accepted_as_unverified(race_env):
    race = _new_race("A", "C")
    _visit(race, "A")
    state = _hop(race, "B", links=["C"])
    assert state["verified"] is False
    assert state["legal"] is True
    assert state["flagged"] is False


def test_dropped_visit_report_does_not_flag_the_next_click(race_env):
    race = _new_race("A", "D")
    _visit(race, "A", links=["B"])
    state = _hop(race, "C", links=["D"], via="C", via_from="B")
    assert state["legal"] is True
    assert state["flagged"] is False


def test_stale_current_page_honours_via_from_when_that_page_was_observed(race_env):
    race = _new_race("A", "D")
    _visit(race, "A", links=["B", "C"])
    _hop(race, "B", links=["A"], via="B", via_from="A")
    state = _hop(race, "C", links=["D"], via="C", via_from="A")
    assert state["legal"] is True
    assert state["flagged"] is False


def test_reload_and_same_page_restore_are_no_ops(race_env):
    race = _new_race("A", "C")
    _visit(race, "A", links=["B"])
    reload_state = _visit(race, "A", links=["B"], nav="reload")
    restore_state = _visit(race, "A", links=["B"], nav="back_forward")
    for state in (reload_state, restore_state):
        assert "legal" not in state
        assert state["flagged"] is False
        assert state["clicks"] == 0
        assert state["path"] == ["A"]


def test_empty_link_report_does_not_erase_a_known_link_set(race_env):
    race = _new_race("A", "C")
    _visit(race, "A", links=["B"])
    _visit(race, "A", links=[])
    state = _hop(race, "B", links=["C"], via="B", via_from="A")
    assert state["legal"] is True
    assert state["flagged"] is False


def test_late_observed_links_are_honoured(race_env):
    race = _new_race("A", "C")
    _visit(race, "A", links=["B"])
    _visit(race, "A", links=["B", "C"])
    state = _hop(race, "C", links=[], via="C", via_from="A")
    assert state["legal"] is True
    assert state["flagged"] is False


def test_first_landing_on_a_redirect_anchors_without_flagging(race_env):
    race = _new_race("Nyc", "C")
    state = _visit(race, "New York City", links=["C"])
    assert state["flagged"] is False
    assert state["current"] == "New York City"
    assert state["path"] == ["New York City"]
    assert state["clicks"] == 0


def test_titles_are_normalised_before_comparison(race_env):
    race = _new_race("A", "Z")
    _visit(race, "A", links=["/wiki/new York City"])
    state = _hop(race, "New_York_City", links=[], via="new_York City", via_from="A")
    assert state["legal"] is True
    assert state["flagged"] is False
    assert state["current"] == "New York City"


def test_url_bar_teleport_is_flagged(race_env):
    race = _new_race("A", "Z")
    _visit(race, "A", links=["B"])
    state = _hop(race, "Q", links=[])
    assert state["legal"] is False
    assert state["flagged"] is True


def test_url_bar_jump_to_a_linked_article_is_flagged(race_env):
    # The destination is a visible link on the current page, but no click (via)
    # was reported - typing the URL is still cheating.
    race = _new_race("A", "C")
    _visit(race, "A", links=["B", "C"])
    state = _hop(race, "C", links=[])
    assert state["legal"] is False
    assert state["flagged"] is True


def test_back_forward_is_flagged_even_when_the_destination_is_linked(race_env):
    race = _new_race("A", "D")
    _visit(race, "A", links=["B"])
    _hop(race, "B", links=["A", "C"], via="B", via_from="A")
    state = _hop(race, "A", links=["B"], nav="back_forward")
    assert state["legal"] is False
    assert state["flagged"] is True


def test_flag_is_sticky_and_survives_a_later_legal_hop(race_env):
    race = _new_race("A", "Z")
    _visit(race, "A", links=["B"])
    assert _hop(race, "Q", links=["R"])["flagged"] is True
    state = _hop(race, "R", links=[], via="R", via_from="Q")
    assert state["legal"] is True
    assert state["flagged"] is True


def test_flagged_finish_still_finishes_but_stays_flagged(race_env):
    race = _new_race("A", "Z")
    _visit(race, "A", links=["B"])
    state = _hop(race, "Z", links=[])
    assert state["legal"] is False
    assert state["finished"] is True
    assert state["flagged"] is True


def _make_competitive(race_id):
    app_mod.EXT_RACES[race_id].bound_match_id = "m1"


def test_competitive_hop_confirmed_by_wikipedia_is_legal(race_env, monkeypatch):
    monkeypatch.setattr(app_mod, "_verify_hop_against_wikipedia",
                        lambda *a, **kw: True)
    race = _new_race("A", "C")
    _make_competitive(race)
    _visit(race, "A", links=["B"])
    state = _hop(race, "B", links=["C"], via="B", via_from="A")
    assert state["legal"] is True
    assert state["flagged"] is False


def test_competitive_hop_with_forged_links_is_flagged(race_env, monkeypatch):
    # Client claims B links to Q, but live Wikipedia says it definitively
    # does not - the forged observation must not launder the jump.
    monkeypatch.setattr(app_mod, "_verify_hop_against_wikipedia",
                        lambda *a, **kw: False)
    race = _new_race("A", "Q")
    _make_competitive(race)
    _visit(race, "A", links=["Q"])
    state = _hop(race, "Q", links=[], via="Q", via_from="A")
    assert state["legal"] is False
    assert state["flagged"] is True


def test_competitive_hop_is_accepted_when_wikipedia_is_unreachable(race_env, monkeypatch):
    monkeypatch.setattr(app_mod, "_verify_hop_against_wikipedia",
                        lambda *a, **kw: None)
    race = _new_race("A", "C")
    _make_competitive(race)
    _visit(race, "A", links=["B"])
    state = _hop(race, "B", links=["C"], via="B", via_from="A")
    assert state["legal"] is True
    assert state["flagged"] is False


def test_solo_races_skip_the_wikipedia_cross_check(race_env, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("solo races must not hit the Wikipedia API")
    monkeypatch.setattr(app_mod, "_verify_hop_against_wikipedia", boom)
    race = _new_race("A", "C")
    _visit(race, "A", links=["B"])
    state = _hop(race, "B", links=["C"], via="B", via_from="A")
    assert state["legal"] is True


def test_wiki_verify_helpers(monkeypatch):
    app_mod._wiki_check_cache.clear()
    calls = []

    def fake_api(params):
        calls.append(params)
        if params.get("prop") == "links":
            return {"query": {"pages": {"1": {"links": [{"title": "B"}]}}}}
        return {"query": {"pages": {"2": {"title": "B"}}}}

    monkeypatch.setattr(app_mod, "_wiki_api", fake_api)
    # A links to B (confirmed), not to Q.
    assert app_mod._verify_hop_against_wikipedia("A", "B", None, "A") is True
    assert app_mod._verify_hop_against_wikipedia("A", "Q", None, "A") is False
    # Clicked link B redirects to landing title B (identity).
    assert app_mod._verify_hop_against_wikipedia("A", "B", "B", "A") is True
    # Results are cached: repeating the confirmed check adds no API calls.
    n = len(calls)
    assert app_mod._verify_hop_against_wikipedia("A", "B", None, "A") is True
    assert len(calls) == n
    # API failure means unknown, not guilty.
    monkeypatch.setattr(app_mod, "_wiki_api", lambda params: None)
    app_mod._wiki_check_cache.clear()
    assert app_mod._verify_hop_against_wikipedia("A", "Q", None, "A") is None
