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
