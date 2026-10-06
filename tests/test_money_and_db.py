import pytest

from cardline import db
from cardline.money import Money


def test_money_formats_in_pt_br():
    assert Money("USD").fmt(1234.5) == "US$ 1.234,50"
    assert Money("USD").fmt(0.07, sign=True) == "+US$ 0,07"
    assert Money("BRL", rate=5.0).fmt(2.32) == "R$ 11,60"
    assert Money("USD").fmt(None) == "—"


def lorcast_card(cid: str, number: str, name: str, version: str | None, usd=0.1, usd_foil=0.5) -> dict:
    return {
        "id": cid, "name": name, "version": version, "collector_number": number, "rarity": "Common",
        "ink": "Sapphire", "type": ["Item"], "cost": 1, "set": {"id": "set_x", "code": "1", "name": "The First Chapter"},
        "prices": {"usd": usd, "usd_foil": usd_foil}, "image_uris": {"digital": {}},
    }


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.upsert_cards(con, [
        lorcast_card("crd_a", "169", "Magic Golden Flower", None),
        lorcast_card("crd_b", "21", "Stitch", "Carefree Surfer"),
        lorcast_card("crd_c", "22", "Stitch", "New Dog", usd=None, usd_foil=3.0),
    ], "2026-10-06T12:00:00-03:00")
    return con


def test_resolve_by_set_and_number(con):
    assert db.resolve_card(con, "1/169")["id"] == "crd_a"
    assert db.resolve_card(con, "1-21")["id"] == "crd_b"


def test_resolve_by_name_and_version(con):
    assert db.resolve_card(con, "magic golden")["id"] == "crd_a"
    assert db.resolve_card(con, "Stitch - New Dog")["id"] == "crd_c"


def test_ambiguous_name_lists_candidates(con):
    with pytest.raises(SystemExit, match="ambíguo"):
        db.resolve_card(con, "Stitch")


def test_price_falls_back_between_finishes(con):
    assert db.price_usd(db.card(con, "crd_a"), foil=True) == 0.5
    assert db.price_usd(db.card(con, "crd_c"), foil=False) == 3.0
