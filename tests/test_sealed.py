import json

import pytest
from fastapi.testclient import TestClient

from cardline import db, sealed
from cardline.config import Settings
from cardline.server import create_app

PRODUCTS = [
    {"productId": 1, "name": "Disney Lorcana: The First Chapter Booster Box", "imageUrl": "https://x/1_200w.jpg", "extendedData": []},
    {"productId": 2, "name": "Disney Lorcana: The First Chapter Booster Pack", "imageUrl": "https://x/2_200w.jpg", "extendedData": []},
    {"productId": 3, "name": "Mickey Mouse - Brave Little Tailor", "extendedData": [{"name": "Number", "value": "1/204"}]},
]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("cardline.money.usd_brl", lambda s: (5.0, "2026-10-08"))
    monkeypatch.setattr("cardline.server.usd_brl", lambda s: (5.0, "2026-10-08"))
    s = Settings(root=tmp_path)
    con = db.connect(s.db_path)
    with con:
        con.execute("INSERT INTO sets(code, id, name, tcg_group_id) VALUES ('1', 'set_1', 'The First Chapter', 22937)")
    return s


@pytest.fixture
def tcg(monkeypatch):
    prices = {1: 479.18, 2: 17.23}

    def fetch(url, timeout=60):
        assert url.startswith("https://tcgcsv.com/tcgplayer/71/22937/")
        if url.endswith("/products"):
            return json.dumps({"results": PRODUCTS}).encode()
        return json.dumps({"results": [{"productId": k, "marketPrice": v, "subTypeName": "Normal"} for k, v in prices.items()]}).encode()

    monkeypatch.setattr(sealed.lorcast, "fetch", fetch)
    return prices


def test_sealed_products_come_from_the_set_group_without_the_cards(settings, tcg):
    with TestClient(create_app(settings)) as client:
        found = client.get("/api/sealed/products?set=1").json()
        assert [(p["product_id"], p["name"], p["usd"]) for p in found] == [(2, "Booster Pack", 17.23), (1, "Booster Box", 479.18)]
        assert client.get("/api/sealed/products?set=99").status_code == 404


def test_sealed_items_have_value_paid_and_follow_the_prices(settings, tcg):
    with TestClient(create_app(settings)) as client:
        r = client.post("/api/sealed", json={"set_code": "1", "product_id": 2, "qty": 3, "paid": 40, "paid_currency": "BRL"})
        assert r.status_code == 201
        item_id = r.json()["id"]
        assert client.post("/api/sealed", json={"set_code": "1", "product_id": 3, "qty": 1}).status_code == 404  # é carta
        listed = client.get("/api/sealed").json()
        item = listed["items"][0]
        assert (item["name"], item["qty"], item["value_usd"], item["paid_usd"]) == ("Booster Pack", 3, pytest.approx(51.69), 8.0)
        assert listed["paid_usd"] == 24.0 and listed["value_usd"] == pytest.approx(51.69)
        assert client.patch(f"/api/sealed/{item_id}", json={"qty": 2}).status_code == 200
        assert client.patch(f"/api/sealed/{item_id}", json={"paid": None}).status_code == 200
        item = client.get("/api/sealed").json()["items"][0]
        assert item["qty"] == 2 and item["paid"] is None
        tcg[2] = 20.0  # o preço de mercado mudou
        con = db.connect(settings.db_path)
        assert sealed.refresh_prices(settings, con) == 1
        assert client.get("/api/sealed").json()["value_usd"] == 40.0
        day = client.get("/api/history").json()["value"][-1]
        assert day["sealed_usd"] == 40.0  # o gráfico de valor por data ganha a linha dos lacrados
        assert client.delete(f"/api/sealed/{item_id}").status_code == 200
        assert client.get("/api/sealed").json()["items"] == []
        assert client.delete(f"/api/sealed/{item_id}").status_code == 404
