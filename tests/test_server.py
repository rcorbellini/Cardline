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


def test_big_videos_arrive_in_parts(tmp_path, monkeypatch):  # pelo Cloudflare, cada requisição vai até 100 MB
    import hashlib
    import subprocess

    from cardline import db
    from cardline.video import ffmpeg_exe

    monkeypatch.setattr("cardline.money.usd_brl", lambda s: (5.0, "2026-10-06"))
    monkeypatch.setattr("cardline.server.usd_brl", lambda s: (5.0, "2026-10-06"))
    monkeypatch.setattr("cardline.server.Runner.start", lambda self: None)  # a pipeline só entra na fila
    monkeypatch.setattr("cardline.server.UPLOAD_CHUNK", 1024)
    video = tmp_path / "gravado.mp4"
    subprocess.run([ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=2",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    data = video.read_bytes()
    assert len(data) > 3 * 1024
    with TestClient(create_app(Settings(root=tmp_path))) as client:
        start = client.post("/api/uploads", json={"filename": "abertura.mp4", "size": len(data)})
        up = start.json()["id"]
        assert start.json()["chunk"] == 1024
        r = client.put(f"/api/uploads/{up}?offset=1024", content=data[1024:2048])
        assert r.status_code == 409 and r.json()["detail"]["size"] == 0  # fora de ordem: diz de onde continuar
        assert client.post(f"/api/runs?filename=abertura.mp4&upload={up}").status_code == 400  # ainda não terminou
        offset = 0
        while offset < len(data):
            offset = client.put(f"/api/uploads/{up}?offset={offset}", content=data[offset:offset + 1024]).json()["size"]
        r = client.put(f"/api/uploads/{up}?offset=0", content=data[:1024])  # repetida: a resposta tinha se perdido
        assert r.status_code == 409 and r.json()["detail"]["size"] == len(data)
        run_id = client.post(f"/api/runs?filename=abertura.mp4&kind=cadastro&upload={up}").json()["id"]
        row = db.connect(tmp_path / "data" / "cardline.db").execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["video_name"] == "abertura.mp4" and row["video_sha1"] == hashlib.sha1(data).hexdigest()
        assert client.post(f"/api/runs?filename=abertura.mp4&upload={up}").status_code == 404  # já virou pipeline
        assert list((tmp_path / "runs" / "_incoming").iterdir()) == []
        other = client.post("/api/uploads", json={"filename": "x.mp4", "size": 10}).json()["id"]
        assert client.delete(f"/api/uploads/{other}").status_code == 200
        assert list((tmp_path / "runs" / "_incoming").iterdir()) == []


def test_private_files_never_go_to_a_shared_cache(client, tmp_path):
    from cardline import pipeline

    video = tmp_path / "abertura.mp4"
    video.write_bytes(b"x")
    run_id = pipeline.create_run(Settings(root=tmp_path), video)
    (tmp_path / "runs" / str(run_id) / "overlay.jpg").write_bytes(b"capa")
    assert client.get(f"/runs/{run_id}/overlay.jpg").headers["cache-control"] == "private, no-cache"
    assert client.get("/api/runs").headers["cache-control"] == "no-store"
    assert client.get("/").headers["cache-control"] == "no-cache"  # a página revalida: atualizações aparecem na hora
    assert client.get("/app.js").headers["cache-control"] == "no-cache"


def test_cloudflare_visitors_go_to_https_on_the_bare_domain(client):
    def via_cloudflare(url, scheme, host):
        return client.get(url, headers={"cf-visitor": f'{{"scheme":"{scheme}"}}', "host": host}, follow_redirects=False)

    r = via_cloudflare("/#/resumo", "http", "cardiline.com.br")
    assert r.status_code == 308 and r.headers["location"] == "https://cardiline.com.br/"
    r = via_cloudflare("/api/auth/me?x=1", "https", "www.cardiline.com.br")
    assert r.status_code == 308 and r.headers["location"] == "https://cardiline.com.br/api/auth/me?x=1"
    assert via_cloudflare("/api/auth/me", "https", "cardiline.com.br").status_code == 200
    assert client.get("/api/auth/me").status_code == 200  # pela rede de casa, nada muda


def test_an_old_domain_goes_to_the_public_address(tmp_path, monkeypatch):
    monkeypatch.setattr("cardline.money.usd_brl", lambda s: (5.0, "2026-10-06"))
    monkeypatch.setattr("cardline.server.usd_brl", lambda s: (5.0, "2026-10-06"))
    with TestClient(create_app(Settings(root=tmp_path, public_url="https://cardline.com.br"))) as client:
        def via(host, path="/api/auth/me?x=1"):
            return client.get(path, headers={"cf-visitor": '{"scheme":"https"}', "host": host}, follow_redirects=False)

        assert via("cardiline.com.br").headers["location"] == "https://cardline.com.br/api/auth/me?x=1"
        assert via("www.cardline.com.br").headers["location"] == "https://cardline.com.br/api/auth/me?x=1"
        assert via("cardline.com.br").status_code == 200
        assert client.get("/api/auth/me").status_code == 200  # pela rede de casa, nada muda
