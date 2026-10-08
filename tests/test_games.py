import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from cardline import catalog, db, games, scryfall, sealed, tcgdex
from cardline.config import Settings
from cardline.index import image_path
from cardline.scan import _segment
from cardline.server import create_app


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("cardline.money.usd_brl", lambda s: (5.0, "2026-10-08"))
    monkeypatch.setattr("cardline.server.usd_brl", lambda s: (5.0, "2026-10-08"))
    return Settings(root=tmp_path)


def scry(number, name, rarity="common", colors=("U",), usd="0.10", foil="0.50", lang="en", printed=None, image="normal.jpg"):
    return {"id": f"id-{lang}-{number}", "set": "m21", "collector_number": number, "name": name, "printed_name": printed,
            "lang": lang, "rarity": rarity, "colors": list(colors), "type_line": "Creature", "cmc": 2.0,
            "image_uris": {"small": "s.jpg", "normal": image, "large": "l.jpg"}, "image_status": "highres_scan",
            "prices": {"usd": usd, "usd_foil": foil}, "purchase_uris": {"tcgplayer": "https://tcgplayer.com/x"}}


def test_magic_cards_come_with_the_portuguese_print(monkeypatch):
    def search(query):
        if "lang:pt" in query:
            return [scry("1", "Ugin, the Spirit Dragon", lang="pt", printed="Ugin, o Dragão Espírito", image="pt.jpg")]
        return [scry("1", "Ugin, the Spirit Dragon", "mythic", (), "12.23", None), scry("2", "Bolt", colors=("R", "U"))]

    monkeypatch.setattr(scryfall, "_search", search)
    ugin, bolt = scryfall.fetch_set_cards("m21")
    assert ugin["id"] == "mtg-id-en-1" and ugin["set_code"] == "mtg-m21" and ugin["game"] == "magic"
    assert ugin["name_pt"] == "Ugin, o Dragão Espírito" and ugin["image_pt"] == "pt.jpg"
    assert ugin["ink"] == "C" and ugin["usd"] == 12.23 and ugin["usd_foil"] is None  # incolor; sem preço foil
    assert bolt["ink"] == "R/U" and bolt["image_pt"] is None and bolt["usd_foil"] == 0.5


def test_pokemon_prices_holo_and_reverse():
    card = {"id": "sv01-025", "localId": "025", "name": "Pikachu", "rarity": "Rare", "types": ["Lightning"],
            "category": "Pokemon", "stage": "Basic", "image": "https://assets.tcgdex.net/en/sv/sv01/025", "set": {"id": "sv01"},
            "pricing": {"tcgplayer": {"holofoil": {"productId": 7, "marketPrice": 3.0},
                                      "reverse-holofoil": {"productId": 7, "marketPrice": 4.5}}}}
    row = tcgdex.to_row(card, {"name": "Pikachu", "image": "https://assets.tcgdex.net/pt/sv/sv01/025"})
    assert row["id"] == "pkm-sv01-025" and row["set_code"] == "pkm-sv01" and row["ink"] == "Lightning"
    assert row["usd"] == 3.0 and row["usd_foil"] == 4.5  # a rara só sai holo: holo é o preço normal; o reverse é o "foil"
    assert row["image_normal"].endswith("/en/sv/sv01/025/high.webp") and row["image_pt"].endswith("/pt/sv/sv01/025/high.webp")
    assert row["tcgplayer_url"] == "https://www.tcgplayer.com/product/7" and row["type"] == "Pokemon · Basic"
    common = tcgdex.to_row({**card, "pricing": {"tcgplayer": {"normal": {"marketPrice": 0.1}, "holofoil": {"marketPrice": 2}}}})
    assert common["usd"] == 0.1 and common["usd_foil"] == 2 and common["name_pt"] is None


def test_codes_and_images_by_game(settings):
    assert games.of_code("mtg-fra") is games.MAGIC and games.of_code("pkm-sv01-001") is games.POKEMON
    assert games.of_code("P1") is games.LORCANA and games.short_code("mtg-fra") == "FRA"
    assert catalog.full_code("FRA", "magic") == "mtg-fra" and catalog.full_code("sv01", "pokemon") == "pkm-sv01"
    assert catalog.full_code("1") == "1" and catalog.full_code("mtg-fra", "magic") == "mtg-fra"
    assert image_path(settings, "1", "crd_x").name == "crd_x.avif"
    assert image_path(settings, "pkm-sv01", "pkm-sv01-001", "pt").name == "pkm-sv01-001@pt.webp"
    assert games.pack_size(settings, None) == 12 and games.pack_size(settings, "magic") == 14


def test_boosters_on_tcgplayer():
    mtg = [{"name": "Reality Fracture - Collector Booster"}, {"name": "Reality Fracture - Sleeved Play Booster"},
           {"name": "Reality Fracture - Play Booster"}, {"name": "Reality Fracture - Play Booster Display"}]
    assert catalog.booster_pack(mtg)["name"] == "Reality Fracture - Play Booster"
    pkm = [{"name": "Code Card - Shrouded Fable Booster Pack"}, {"name": "Shrouded Fable Booster Pack"}]
    assert catalog.booster_pack(pkm)["name"] == "Shrouded Fable Booster Pack"
    assert sealed.short_name("Reality Fracture - Play Booster", "Reality Fracture", "magic") == "Play Booster"
    assert catalog.is_booster_name("Play Booster") and not catalog.is_booster_name("Collector Booster")


def test_the_same_card_in_two_languages_is_one_reveal():
    rec = lambda i, card: {"i": i, "matches": [{"card": card, "inliers": 40}]}  # noqa: E731
    records = [rec(i, ["en", "pt"][i % 2]) for i in range(8)] + [rec(i, "outra") for i in range(8, 14)]
    events = _segment(records, 10, key=lambda c: "carta" if c in ("en", "pt") else c)
    assert [e["card"] for e in events] == ["carta", "outra"] and len(events[0]["frames"]) == 8


def test_old_database_gets_games_and_value_per_game(tmp_path):
    path = tmp_path / "data" / "cardline.db"
    path.parent.mkdir()
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE sets (code TEXT PRIMARY KEY, id TEXT NOT NULL, name TEXT NOT NULL, released_at TEXT, icon TEXT,
                           icon_source TEXT, tcg_group_id INTEGER);
        CREATE TABLE user_values (user_id INTEGER NOT NULL, day TEXT NOT NULL, cards_usd REAL NOT NULL, sealed_usd REAL,
                                  cards INTEGER NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY (user_id, day));
        INSERT INTO sets VALUES ('1', 'set_1', 'The First Chapter', '2023-08-18', NULL, NULL, NULL);
        INSERT INTO sets VALUES ('P1', 'set_p1', 'Promo', '2023-08-18', NULL, NULL, NULL);
        INSERT INTO user_values VALUES (1, '2026-10-01', 10.0, NULL, 3, '2026-10-01T12:00:00');
    """)
    con.execute("PRAGMA user_version = 11")
    con.commit()
    con.close()
    con = db.connect(path)
    assert [tuple(r) for r in con.execute("SELECT game, day, cards_usd FROM user_values")] == [("lorcana", "2026-10-01", 10.0)]
    assert {r["code"]: (r["game"], r["booster"]) for r in con.execute("SELECT * FROM sets")} == \
        {"1": ("lorcana", 1), "P1": ("lorcana", 0)}


def test_value_is_recorded_per_game(settings):
    from cardline import auth

    con = db.connect(settings.db_path)
    user = auth.upsert_user(con, "dono@teste.dev")
    db.upsert_set(con, {"code": "mtg-m21", "id": "x", "name": "Core Set 2021", "game": "magic", "booster": True})
    db.upsert_card_rows(con, [{"id": "mtg-a", "game": "magic", "set_code": "mtg-m21", "number": "1", "name": "A",
                               "usd": 2.0, "usd_foil": 5.0}], "2026-10-08T12:00:00-03:00")
    with con:
        con.execute("INSERT INTO collection(card_id, foil, added_at, user_id, lang) VALUES ('mtg-a', 1, 'x', ?, 'pt')", (user.id,))
    db.record_value(con, user.id)
    rows = [tuple(r) for r in con.execute("SELECT game, cards_usd, cards FROM user_values WHERE user_id = ?", (user.id,))]
    assert rows == [("magic", 5.0, 1)]  # Lorcana e Pokémon, sem nada, não ganham pontos zerados


def test_api_speaks_games(settings):
    con = db.connect(settings.db_path)
    db.upsert_set(con, {"code": "pkm-sv01", "id": "sv01", "name": "Scarlet & Violet", "game": "pokemon", "booster": True,
                        "name_pt": "Escarlate e Violeta", "released_at": "2023-03-31"})
    db.upsert_set(con, {"code": "pkm-svp", "id": "svp", "name": "Promos", "game": "pokemon", "booster": False})
    db.upsert_card_rows(con, [{"id": "pkm-sv01-001", "game": "pokemon", "set_code": "pkm-sv01", "number": "001",
                               "name": "Pineco", "name_pt": "Pineco", "rarity": "Common", "usd": 0.1}], db.now())
    con.commit()
    with TestClient(create_app(settings)) as client:
        meta = client.get("/api/meta").json()
        assert [g["key"] for g in meta["games"]] == ["lorcana", "magic", "pokemon"]
        pokemon = next(g for g in meta["games"] if g["key"] == "pokemon")
        assert pokemon["foil_label"] == "Reverse holo" and pokemon["pack_size"] == 10
        assert [s["code"] for s in meta["sets"]] == ["pkm-sv01"]  # só os sets já baixados de Pokémon
        sets = client.get("/api/sets?game=pokemon").json()
        assert [(s["short"], s["name_pt"], s["cards"]) for s in sets] == [("sv01", "Escarlate e Violeta", 1)]
        assert client.get("/api/sets?game=lorcana").json() == []
        card = client.get("/api/collection").json()
        assert card == {"cards": {}, "owned": []}
        assert client.get("/api/history?game=pokemon").json() == {"value": [], "views": []}
