from ffm.news import InjuryNote, NewsItem, match_injuries, match_news
from ffm.players import PlayerDB, canonical_position, eligible_positions, normalize_name, slot_allows


PLAYERS = PlayerDB(
    {
        "1": {"full_name": "Odell Beckham Jr.", "position": "WR", "team": "MIA", "active": True},
        "2": {"full_name": "Josh Allen", "position": "QB", "team": "BUF", "active": True},
        "3": {"full_name": "Josh Allen", "position": "LB", "team": "JAX", "active": True, "fantasy_positions": ["LB"]},
        "4": {"full_name": "Maxx Crosby", "position": "DE", "team": "LV", "active": True, "fantasy_positions": ["DL"]},
        "5": {"full_name": "Andrew Van Ginkel", "position": "LB", "team": "MIN", "active": True, "fantasy_positions": ["DL", "LB"]},
        "WAS": {"first_name": "Washington", "last_name": "Commanders", "position": "DEF", "team": "WAS", "active": True},
    }
)


def test_normalize_name():
    assert normalize_name("Odell Beckham Jr.") == "odell beckham"
    assert normalize_name("Marvin Harrison III") == "marvin harrison"
    assert normalize_name("Ja'Marr Chase") == "jamarr chase"


def test_positions():
    assert canonical_position({"position": "DE"}) == "DL"
    assert canonical_position({"position": "CB"}) == "DB"
    assert eligible_positions(PLAYERS.get("5")) == {"DL", "LB"}
    assert PLAYERS.position("4") == "DL" and PLAYERS.raw_position("4") == "DE"
    assert slot_allows("DL", "DE") and slot_allows("DB", "S") and not slot_allows("LB", "DE")
    assert slot_allows("DL", "LB", ["DL", "LB"])
    assert slot_allows("IDP_FLEX", "CB") and not slot_allows("FLEX", "CB")
    assert PLAYERS.label("4") == "Maxx Crosby (DL/DE, LV)"
    assert PLAYERS.label("2") == "Josh Allen (QB, BUF)"


def test_match_injuries_uses_team_to_disambiguate():
    notes = [
        InjuryNote("Josh Allen", "BUF", "QB", "Questionable", "2026-09-07T00:00Z", "shoulder", "long text"),
        InjuryNote("Josh Allen", "JAX", "LB", "Out", None, "knee", ""),
        InjuryNote("Odell Beckham Jr.", "MIA", "WR", "Active", None, "", ""),
    ]
    matched = match_injuries(notes, PLAYERS, ["1", "2", "3", "4"])
    assert matched["2"].status == "Questionable"
    assert matched["3"].status == "Out"
    assert matched["1"].is_healthy
    assert "4" not in matched


def test_match_news_by_full_name():
    items = [
        NewsItem("Maxx Crosby limited in practice", "The Raiders edge rusher ...", "2026-09-07T10:00Z"),
        NewsItem("Week 1 preview", "Josh Allen and the Bills host Houston", None),
    ]
    matched = match_news(items, PLAYERS, ["2", "4", "WAS"])
    assert [i.headline for i in matched["4"]] == ["Maxx Crosby limited in practice"]
    assert "2" in matched and "WAS" not in matched


def test_search_handles_suffixes_and_idp():
    assert [p["player_id"] for p in PLAYERS.search("beckham")] == ["1"]
    assert {p["player_id"] for p in PLAYERS.search("josh allen")} == {"2", "3"}
    assert PLAYERS.search("WAS")[0]["player_id"] == "WAS"
