import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from cardline import auth, db, packs, pipeline, sealed
from cardline.config import Settings
from cardline.server import create_app

ART = {"a": np.tile(np.linspace(0, 255, 40, dtype=np.float32), (72, 1)),  # duas "artes" bem diferentes
       "b": np.tile(np.linspace(255, 0, 72, dtype=np.float32)[:, None], (1, 40))}


def frame(i, code, inliers=150, x=0.5, art="a"):
    quad = [[x - 0.2, 0.3], [x + 0.2, 0.3], [x + 0.2, 0.8], [x - 0.2, 0.8]]
    return {"i": i, "set": code, "inliers": inliers, "quad": quad, "sig": ART[art] + np.random.default_rng(i).normal(0, 3, (72, 40))}


def test_each_new_set_on_top_is_a_booster_and_short_passes_do_not_count():
    recs = [{"i": 0}] + [frame(i, "9") for i in range(1, 6)] + [frame(6, "7")] + [{"i": 7}] + \
           [frame(i, "8") for i in range(8, 12)] + [frame(i, "9") for i in range(12, 16)]
    found = packs.segment(recs)
    assert [f[0]["set"] for f in found] == ["9", "8", "9"]  # o 7 de passagem (um frame) não conta


def test_same_set_boosters_split_by_art_or_by_a_new_place_but_not_by_a_nudge():
    still = lambda i0, n, **kw: [frame(i, "9", **kw) for i in range(i0, i0 + n)]  # noqa: E731
    # mesma arte, mesma posição, a mão mexe um pouco: um booster só
    assert len(packs.segment(still(0, 4) + [frame(4, "9", inliers=120, x=0.52)] + still(5, 4, x=0.51))) == 1
    # outra arte do mesmo set: outro booster
    assert len(packs.segment(still(0, 4) + still(4, 4, art="b"))) == 2
    # mesma arte, mas parou noutro lugar depois de um movimento (os pontos casados caem): outro booster
    moved = still(0, 4) + [frame(4, "9", inliers=60, x=0.6), frame(5, "9", inliers=70, x=0.65)] + still(6, 4, x=0.66)
    assert len(packs.segment(moved)) == 2
    # andou sem a queda (ajeitando o booster na mão): continua um só
    assert len(packs.segment(still(0, 4) + still(4, 4, x=0.66))) == 1


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("cardline.money.usd_brl", lambda s: (5.0, "2026-10-08"))
    monkeypatch.setattr("cardline.server.usd_brl", lambda s: (5.0, "2026-10-08"))
    s = Settings(root=tmp_path)
    con = db.connect(s.db_path)
    with con:
        con.execute("INSERT INTO sets(code, id, name, tcg_group_id) VALUES ('9', 'set_9', 'Fabled', 24348)")
        con.execute("INSERT INTO sets(code, id, name, tcg_group_id) VALUES ('7', 'set_7', 'Archazia''s Island', 24011)")
    prices = {24348: {91: 6.5}, 24011: {71: 5.0}}

    def fetch(url, timeout=60):
        group = int(url.split("/")[-2])
        if url.endswith("/products"):
            name = "Fabled" if group == 24348 else "Archazia's Island"
            return json.dumps({"results": [{"productId": pid, "name": f"Disney Lorcana: {name} Booster Pack",
                                            "imageUrl": f"https://x/{pid}.jpg", "extendedData": []} for pid in prices[group]]}).encode()
        return json.dumps({"results": [{"productId": k, "marketPrice": v, "subTypeName": "Normal"}
                                       for k, v in prices[group].items()]}).encode()

    monkeypatch.setattr(sealed.lorcast, "fetch", fetch)
    s.prices = prices
    return s


@pytest.fixture
def run(settings, tmp_path):
    video = tmp_path / "pilha.mp4"
    video.write_bytes(b"pilha")
    run_id = pipeline.create_run(settings, video, kind="lacrados", paid=60, paid_currency="BRL")
    folder = settings.runs_dir / str(run_id)
    boosters = [{"uid": f"{n:02d}", "set": code, "set_name": "Fabled" if code == "9" else "Archazia's Island",
                 "name": "Booster Pack", "t": float(n), "quad": [[0.3, 0.3], [0.7, 0.3], [0.7, 0.8], [0.3, 0.8]], "crop": None}
                for n, code in enumerate(["9", "7", "9"], 1)]
    (folder / "scan.json").write_text(json.dumps({"kind": "lacrados", "video": "pilha.mp4", "duration": 5.0,
                                                  "packs": boosters, "cards": [], "sets": ["7", "9"]}))
    con = db.connect(settings.db_path)
    with con:
        con.execute("UPDATE runs SET status = 'done' WHERE id = ?", (run_id,))
        con.execute("UPDATE run_steps SET status = 'done' WHERE run_id = ?", (run_id,))
    auth.upsert_user(con, "eu@teste.dev")
    auth.adopt(con, settings)  # a pipeline (criada sem dono) fica com o primeiro usuário
    return run_id


def test_boosters_are_priced_and_registered_as_sealed_items(settings, run):
    con = db.connect(settings.db_path)
    folder = settings.runs_dir / str(run)
    scan = json.loads((folder / "scan.json").read_text())
    assert sealed.price_packs(settings, con, scan) == (0, [])
    assert [p["price_usd"] for p in scan["packs"]] == [6.5, 5.0, 6.5]
    assert sealed.register_packs(settings, con, run, scan, 60.0, "BRL") == 3
    uid = con.execute("SELECT user_id FROM runs WHERE id = ?", (run,)).fetchone()[0]
    items = {x["name"] + x["set_code"]: x for x in sealed.items(con, uid)}
    fabled = items["Booster Pack9"]
    assert (fabled["qty"], fabled["registered_usd"], fabled["paid"], fabled["paid_usd"], fabled["run_id"]) == (2, 6.5, 20.0, 4.0, run)
    taken = sealed.take(con, fabled["id"], 1)  # um Fabled aberto numa abertura
    settings.prices[24348][91] = 8.0  # o preço subiu: ao reprocessar, o preço do registro fica
    scan["packs"] = scan["packs"][:2]  # um Fabled saiu da identificação
    assert sealed.price_packs(settings, con, scan) == (2, [])
    sealed.register_packs(settings, con, run, scan, 60.0, "BRL")
    row = con.execute("SELECT qty, opened, registered_usd FROM sealed WHERE id = ?", (taken["id"],)).fetchone()
    assert tuple(row) == (1, 1, 6.5)  # o aberto continua aberto; nenhum Fabled fechado
    assert [x["set_code"] for x in sealed.items(con, uid)] == ["7"]


def test_the_page_edits_the_boosters_and_shows_the_sealed_run(settings, run):
    with TestClient(create_app(settings)) as client:
        detail = client.get(f"/api/runs/{run}").json()
        assert detail["packs"] == 3 and [p["set"] for p in detail["pack_items"]] == ["9", "7", "9"]
        assert [s["label"] for s in detail["steps"]][:2] == ["Identificar boosters", "Preço dos boosters"]
        assert detail["post_suggestion"]["title"] == "Contando 3 boosters lacrados | Disney Lorcana"
        assert client.patch(f"/api/runs/{run}/packs/02", json={"set": "9"}).status_code == 200
        assert client.delete(f"/api/runs/{run}/packs/03").status_code == 200
        uid = client.post(f"/api/runs/{run}/packs", json={"set": "7", "t": 4.5}).json()["uid"]
        assert client.post(f"/api/runs/{run}/packs", json={"set": "7"}).status_code == 400
        detail = client.get(f"/api/runs/{run}").json()
        assert [(p["uid"], p["set"]) for p in detail["pack_items"]] == [("01", "9"), ("02", "9"), (uid, "7")]
        assert [p["uid"] for p in detail["removed"]] == ["03"]
        assert detail["status"] == "stale" and detail["resume_from"] == "prices"
        assert client.post(f"/api/runs/{run}/packs/03/restore").status_code == 200
        assert client.patch(f"/api/runs/{run}/packs/99", json={"set": "9"}).status_code == 404
        assert client.patch(f"/api/runs/{run}", json={"paid": 80, "paid_currency": "BRL"}).status_code == 200


def test_sealed_video_counts_boosters_and_sums_their_value(settings):
    from cardline.money import Money
    from cardline.overlay import SealedOverlay

    scan = {"packs": [{"set": "9", "set_name": "Fabled", "t": 1.0, "price_usd": 6.5, "quad": None},
                      {"set": "7", "set_name": "Archazia's Island", "t": 2.0, "price_usd": 5.0, "quad": None},
                      {"set": "9", "set_name": "Fabled", "t": 3.0, "price_usd": 6.5, "quad": None}]}
    ov = SealedOverlay(scan, Money("USD", 1.0), (360, 640), paid_usd=12.0, intro=3.0)
    assert ov.celebration_time() == pytest.approx(3.0 + 0.35 + 0.75 + 0.5 * 0.5 / 6.5)  # no meio do terceiro
    frame = np.zeros((640, 360, 3), np.uint8)
    ov.draw(frame, 2.5)
    assert frame.any()
    assert ov.summary().width > 0 and ov.intro_frame(frame.astype(np.float32), frame.astype(np.float32), 1.5).any()


def test_sealed_narration_is_an_inventory_with_an_ending():
    from cardline.narration import Timeline, write_sealed_script

    tl = Timeline(cards=[{"set": "9", "t": 3.0 + 2 * i, "card_id": "booster:9", "foil": False, "rarity": None}
                         for i in range(8)], summary=20.0, end=26.0, packs=1, paid=60.0, paid_currency="BRL", result=0.4,
                  intro=3.0)
    lines = write_sealed_script(tl, 7)["lines"]
    assert lines[0]["tipo"] == "abertura" and "Sessenta reais" in lines[0]["texto"]
    assert lines[-1]["tipo"] == "desfecho" and lines[-1]["t"] >= 20.7
    assert sum("Tá virando coleção" in line["texto"] for line in lines) == 1  # o terceiro do mesmo set, uma vez
    gaps = [b["t"] - a["t"] for a, b in zip(lines, lines[1:])]
    assert min(gaps) >= 2.0


def test_a_booster_value_can_be_edited_and_stays_fixed(settings, run):
    with TestClient(create_app(settings)) as client:
        assert client.patch(f"/api/runs/{run}/packs/01", json={"value": 50, "currency": "BRL"}).json()["changed"] is True
        assert client.patch(f"/api/runs/{run}/packs/01", json={"value": -1}).status_code == 400
        item = client.get(f"/api/runs/{run}").json()["pack_items"][0]
        assert (item["value_usd"], item["price_now"]) == (10.0, 10.0)  # R$ 50 a US$ 1 = R$ 5
    con = db.connect(settings.db_path)
    folder = settings.runs_dir / str(run)
    scan = json.loads((folder / "scan.json").read_text())
    sealed.price_packs(settings, con, scan)
    assert [p["price_usd"] for p in scan["packs"]] == [10.0, 5.0, 6.5]  # o editado vale; os outros, o mercado
    sealed.register_packs(settings, con, run, scan, None, None)
    rows = {(r["set_code"], r["product_id"]): (r["qty"], r["usd"]) for r in con.execute("SELECT * FROM sealed")}
    assert rows == {("9", None): (1, 10.0), ("9", 91): (1, 6.5), ("7", 71): (1, 5.0)}  # o editado é um item à parte
    settings.prices[24348][91] = 9.0
    sealed.refresh_prices(settings, con)
    assert {r["product_id"]: r["usd"] for r in con.execute("SELECT * FROM sealed WHERE set_code = '9'")} == {None: 10.0, 91: 9.0}
    with TestClient(create_app(settings)) as client:  # apagar o valor volta ao preço de mercado
        assert client.patch(f"/api/runs/{run}/packs/01", json={"value": None}).json()["changed"] is True
        assert client.get(f"/api/runs/{run}").json()["pack_items"][0]["value_usd"] is None
