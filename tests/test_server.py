import pytest
from fastapi.testclient import TestClient

from cardline.config import Settings
from cardline.server import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("cardline.money.usd_brl", lambda s: (5.0, "2026-10-06"))
    monkeypatch.setattr("cardline.server.usd_brl", lambda s: (5.0, "2026-10-06"))
    with TestClient(create_app(Settings(root=tmp_path))) as c:
        yield c


def test_meta_exposes_steps_and_rates(client):
    meta = client.get("/api/meta").json()
    assert [s["name"] for s in meta["steps"]] == ["scan", "verify", "prices", "commit", "overlay", "narrate"]
    assert meta["rates"] == {"USD": 1.0, "BRL": 5.0}


def test_empty_collection_and_runs(client):
    assert client.get("/api/collection").json() == {"cards": {}, "owned": []}
    assert client.get("/api/runs").json() == []


def test_upload_rejects_files_that_are_not_videos(client, tmp_path):
    r = client.post("/api/runs?filename=lixo.mp4", content=b"\x00" * 4096)
    assert r.status_code == 400
    assert list((tmp_path / "runs" / "_incoming").iterdir()) == []


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/99").status_code == 404
    assert client.post("/api/runs/99/rerun", json={}).status_code == 404


def test_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Nova pipeline" in r.text


def test_runs_bring_the_value_of_each_booster(client, tmp_path):
    from cardline import db, pipeline
    from cardline.collection import save_scan

    settings = Settings(root=tmp_path)
    con = db.connect(settings.db_path)
    for code in ("1", "2"):
        db.upsert_set(con, {"code": code, "id": f"set_{code}", "name": f"Set {code}", "released_at": "2024-01-01"})
    con.executemany("INSERT INTO cards(id, set_code, number, name, usd, usd_foil) VALUES (?, ?, ?, ?, ?, ?)",
                    [("crd_a", "1", "1", "A", 1.0, 3.0), ("crd_b", "1", "2", "B", 2.0, 4.0), ("crd_c", "2", "3", "C", 5.0, 9.0)])
    con.commit()
    video = tmp_path / "abertura.mp4"
    video.write_bytes(b"x")
    run_id = pipeline.create_run(settings, video)
    card = lambda cid, s, pack, foil, price: {"card_id": cid, "set": s, "pack": pack, "slot": 1, "t": 1.0,  # noqa: E731
                                              "foil": foil, "price_usd": price}
    save_scan(settings.runs_dir / str(run_id), {"video": "abertura.mp4", "sets": ["1", "2"], "cards": [
        card("crd_a", "1", 1, False, 0.5), card("crd_b", "1", 1, False, 1.5), card("crd_c", "2", 2, True, 8.0)]})
    listed = next(r for r in client.get("/api/runs").json() if r["id"] == run_id)
    # valor pelas cartas de cada booster: na abertura (preço fixado) e hoje (preço atual, foil onde é foil)
    assert listed["pack_values"] == [{"pack": 1, "set": "1", "cards": 2, "value_open": 2.0, "value_now": 3.0},
                                     {"pack": 2, "set": "2", "cards": 1, "value_open": 8.0, "value_now": 9.0}]
