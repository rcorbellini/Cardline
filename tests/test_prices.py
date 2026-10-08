import threading
import time

import pytest
from fastapi.testclient import TestClient

from cardline import db, pipeline
from cardline.collection import load_scan, save_scan
from cardline.config import Settings
from cardline.server import create_app


def lorcast_card(cid: str, number: str, usd: float, usd_foil: float) -> dict:
    return {
        "id": cid, "name": f"Carta {number}", "collector_number": number, "rarity": "Common", "type": ["Item"],
        "set": {"id": "set_1", "code": "1", "name": "The First Chapter"},
        "prices": {"usd": usd, "usd_foil": usd_foil}, "image_uris": {"digital": {}},
    }


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("cardline.money.usd_brl", lambda s: (5.0, "2026-10-06"))
    monkeypatch.setattr("cardline.server.usd_brl", lambda s: (5.0, "2026-10-06"))
    s = Settings(root=tmp_path)
    con = db.connect(s.db_path)
    with con:
        con.execute("INSERT INTO sets(code, id, name) VALUES ('1', 'set_1', 'The First Chapter')")
        # dia da abertura: a carta A valia 1,00 e a B 2,00
        db.upsert_cards(con, [lorcast_card("crd_a", "1", 1.0, 5.0), lorcast_card("crd_b", "2", 2.0, 6.0)],
                        "2026-10-06T12:00:00-03:00")
    return s


@pytest.fixture
def run(settings, tmp_path):
    video = tmp_path / "abertura.mp4"
    video.write_bytes(b"fake")
    run_id = pipeline.create_run(settings, video)
    con = db.connect(settings.db_path)
    with con:
        con.execute("UPDATE runs SET recorded_at = '2026-10-06T14:10:08.000000Z' WHERE id = ?", (run_id,))
    save_scan(settings.runs_dir / str(run_id), {
        "duration": 30.0, "sets": ["1"], "scanned_at": "2026-10-06T12:00:00-03:00",
        "cards": [{"uid": "01", "card_id": "crd_a", "set": "1", "number": "1", "name": "Carta 1", "rarity": "Common",
                   "foil": False, "t": 1.0, "pack": 1, "slot": 1}],
    })
    return run_id


def test_price_on_uses_the_last_known_price_up_to_that_day(settings):
    con = db.connect(settings.db_path)
    with con:
        db.upsert_cards(con, [lorcast_card("crd_a", "1", 9.0, 9.5)], "2026-10-20T12:00:00-03:00")
    assert db.price_on(con, "crd_a", False, "2026-10-06") == 1.0
    assert db.price_on(con, "crd_a", True, "2026-10-10") == 5.0
    assert db.price_on(con, "crd_a", False, "2026-10-01") == 1.0  # antes do histórico: o primeiro conhecido
    assert db.price_on(con, "crd_a", False, "2026-10-25") == 9.0


def test_reprocessing_keeps_the_price_at_opening(settings, run, monkeypatch):
    ctx = pipeline.RunContext(settings, db.connect(settings.db_path), run)
    monkeypatch.setattr(pipeline.lorcast, "fetch_set_cards", lambda set_id: [
        lorcast_card("crd_a", "1", 1.0, 5.0), lorcast_card("crd_b", "2", 2.0, 6.0)])
    pipeline._prices(ctx)
    assert load_scan(ctx.dir)["cards"][0]["price_usd"] == 1.0

    # semanas depois o mercado sobe; uma carta é inserida na pipeline e ela é reprocessada
    monkeypatch.setattr(db, "now", lambda: "2026-10-27T12:00:00-03:00")
    monkeypatch.setattr(pipeline.lorcast, "fetch_set_cards", lambda set_id: [
        lorcast_card("crd_a", "1", 3.0, 7.0), lorcast_card("crd_b", "2", 4.0, 8.0)])
    scan = load_scan(ctx.dir)
    scan["cards"].append({"uid": "x", "card_id": "crd_b", "set": "1", "number": "2", "name": "Carta 2",
                          "rarity": "Common", "foil": False, "t": 2.0, "pack": 1, "slot": 2})
    save_scan(ctx.dir, scan)
    pipeline._prices(ctx)
    prices = {c["card_id"]: c["price_usd"] for c in load_scan(ctx.dir)["cards"]}
    assert prices["crd_a"] == 1.0  # preço da abertura mantido
    assert prices["crd_b"] == 2.0  # carta nova: preço do dia da abertura, não o de hoje
    assert db.card(ctx.con, "crd_a")["usd"] == 3.0  # o valor de hoje foi atualizado


def test_changing_the_video_currency_keeps_paid_and_marks_the_video_stale(settings, run):
    con = db.connect(settings.db_path)
    with con:
        con.execute("UPDATE runs SET status = 'done', paid = 45, paid_currency = 'BRL' WHERE id = ?", (run,))
        con.execute("UPDATE run_steps SET status = 'done' WHERE run_id = ?", (run,))
    with TestClient(create_app(settings)) as client:
        assert client.patch(f"/api/runs/{run}", json={"currency": "BRL"}).status_code == 200
        detail = client.get(f"/api/runs/{run}").json()
    assert detail["options"]["currency"] == "BRL" and detail["paid"] == 45
    assert detail["status"] == "stale" and detail["resume_from"] == "overlay"


def test_refresh_endpoint_updates_today_but_not_the_opening_price(settings, run, monkeypatch):
    con = db.connect(settings.db_path)
    with con:
        con.execute("INSERT INTO collection(card_id, run_id, price_usd, added_at) VALUES ('crd_a', ?, 1.0, 'x')", (run,))
    monkeypatch.setattr("cardline.catalog.lorcast.fetch_set_cards", lambda set_id: [lorcast_card("crd_a", "1", 3.0, 7.0)])
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/prices/refresh").json()["sets"] == ["1"]
        owned = client.get("/api/collection").json()["owned"][0]
    assert owned["price"] == 3.0 and owned["copies"][0]["paid"] == 1.0


def wait_task(client, name):
    for _ in range(100):
        task = client.get("/api/tasks").json()[name]
        if not task["running"]:
            return task
        time.sleep(0.05)
    return task


def test_prices_update_in_the_background_one_at_a_time(settings, run, monkeypatch):
    con = db.connect(settings.db_path)
    with con:
        con.execute("INSERT INTO collection(card_id, run_id, price_usd, added_at) VALUES ('crd_a', ?, 1.0, 'x')", (run,))
    release = threading.Event()

    def slow_fetch(set_id):  # um set por consulta ao Lorcast
        release.wait(5)
        return [lorcast_card("crd_a", "1", 3.0, 7.0)]

    monkeypatch.setattr("cardline.catalog.lorcast.fetch_set_cards", slow_fetch)
    with TestClient(create_app(settings)) as client:
        started = client.post("/api/tasks/prices").json()
        assert started["running"] is True
        assert client.post("/api/tasks/prices").status_code == 409  # uma de cada vez
        for _ in range(100):  # a página mostra em que set está
            if (message := client.get("/api/tasks").json()["prices"]["message"]).startswith("Set"):
                break
            time.sleep(0.02)
        assert message == "Set 1 de 1: The First Chapter"
        release.set()
        task = wait_task(client, "prices")
        assert task["ok"] is True and task["message"] == "Preços de hoje atualizados (1 set)."
        assert client.get("/api/collection").json()["owned"][0]["price"] == 3.0
        assert client.post("/api/tasks/social").json()["skipped"] is True  # nenhum vídeo para ler pela API
        job = client.get("/api/jobs").json()[0]  # a atualização vira uma linha na aba Pipelines
        assert (job["kind"], job["label"], job["status"]) == ("precos", "Atualização de preços", "done")
        assert job["result"]["sets"] == ["The First Chapter"] and job["result"]["cards_after"] == 3.0
        assert client.get(f"/api/jobs/{job['id']}").json()["message"] == "Preços de hoje atualizados (1 set)."
        assert client.get("/api/jobs/999").status_code == 404


def test_an_update_cut_by_a_restart_shows_as_interrupted(settings):
    from cardline import jobs

    jobs.create(db.connect(settings.db_path), "precos")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/jobs").json()[0]["status"] == "interrupted"



def test_value_history_backfills_the_days_with_saved_prices(settings):
    con = db.connect(settings.db_path)
    with con:
        con.executemany("INSERT OR REPLACE INTO price_history(card_id, day, usd, usd_foil) VALUES (?, ?, ?, ?)",
                        [("crd_a", "2026-10-06", 1.0, 5.0), ("crd_b", "2026-10-06", 2.0, 6.0), ("crd_a", "2026-10-07", 1.5, 5.0)])
        con.executemany("INSERT INTO collection(card_id, foil, price_usd, added_at) VALUES (?, ?, ?, ?)",
                        [("crd_a", 0, 1.0, "2026-10-06T14:00:00-03:00"), ("crd_b", 1, 6.0, "2026-10-07T10:00:00-03:00")])
        db._value_history(con)
    rows = [tuple(r) for r in con.execute("SELECT day, cards_usd, cards FROM user_values WHERE user_id = 0 ORDER BY day")]
    # o B (foil) entrou no dia 7 e vale o último preço guardado até ele
    assert rows == [("2026-10-06", 1.0, 1), ("2026-10-07", 7.5, 2)]


def test_each_update_is_one_point_per_day(settings, run, monkeypatch):
    con = db.connect(settings.db_path)
    with con:
        con.execute("INSERT INTO collection(card_id, run_id, price_usd, added_at) VALUES ('crd_a', ?, 1.0, 'x')", (run,))
    monkeypatch.setattr("cardline.catalog.lorcast.fetch_set_cards", lambda set_id: [lorcast_card("crd_a", "1", 3.0, 7.0)])
    with TestClient(create_app(settings)) as client:
        client.post("/api/prices/refresh")
        monkeypatch.setattr("cardline.catalog.lorcast.fetch_set_cards", lambda set_id: [lorcast_card("crd_a", "1", 4.0, 7.0)])
        client.post("/api/prices/refresh")
        value = client.get("/api/history").json()["value"]
    today = db.now()[:10]
    assert [(p["day"], p["cards_usd"], p["cards"]) for p in value] == [(today, 4.0, 1)]  # a última do dia vale


def test_sets_sync_is_recorded_with_its_log(settings, monkeypatch):
    import subprocess
    import sys

    real = subprocess.Popen

    def quick_sync(args, **kwargs):  # no lugar do `cardline sync` de verdade
        return real([sys.executable, "-c", "print('Catálogo: 1 sets'); print('         1  The First Chapter  204 cartas')"], **kwargs)

    monkeypatch.setattr("cardline.server.subprocess.Popen", quick_sync)
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/sets/sync").status_code == 200
        for _ in range(100):
            job = client.get("/api/jobs").json()[0]
            if job["status"] != "running":
                break
            time.sleep(0.05)
        assert (job["kind"], job["status"], job["result"]) == ("sync", "done", {"sets": 1})
        assert "The First Chapter" in client.get(f"/api/jobs/{job['id']}").json()["log"]
