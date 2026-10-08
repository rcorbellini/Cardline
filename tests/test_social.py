import io
import json
import sqlite3
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from cardline import db, instagram, pipeline, social
from cardline.config import Settings
from cardline.server import create_app


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("cardline.money.usd_brl", lambda s: (5.0, "2026-10-06"))
    monkeypatch.setattr("cardline.server.usd_brl", lambda s: (5.0, "2026-10-06"))
    return Settings(root=tmp_path)


def new_run(settings, name="v.mp4"):
    video = settings.root / name
    video.write_bytes(name.encode())
    run_id = pipeline.create_run(settings, video)
    con = db.connect(settings.db_path)
    with con:
        con.execute("UPDATE runs SET status = 'done' WHERE id = ?", (run_id,))
    (settings.runs_dir / str(run_id) / "overlay.mp4").write_bytes(b"video")
    return run_id


def ig_connected(settings):
    instagram.folder(settings).mkdir(parents=True, exist_ok=True)
    (instagram.folder(settings) / "token.json").write_text(json.dumps({"access_token": "IG" + "x" * 40, "user_id": "17",
                                                                       "username": "eu", "saved_at": time.time()}))


class Tunnel:
    """A API local do ngrok: fechada (sem ngrok) ou com o túnel para a porta 8000."""

    def __init__(self):
        self.open = False

    def __call__(self, *args, **kwargs):
        if not self.open:
            raise OSError("sem ngrok")
        return io.BytesIO(json.dumps({"tunnels": [{"public_url": "https://abc.ngrok-free.app",
                                                   "config": {"addr": "http://localhost:8000"}}]}).encode())


def wait_job(client, run_id, network):
    for _ in range(50):
        job = client.get(f"/api/runs/{run_id}").json()["post_jobs"].get(network)
        if job and job["status"] != "sending":
            return job
        time.sleep(0.05)
    return job


def test_links_tell_the_network():
    assert social.detect("https://youtube.com/shorts/dQw4w9WgXcQ?si=x") == ("youtube", "dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ")
    assert social.detect("https://www.instagram.com/reel/DAbc_12-xY/?igsh=abc") == (
        "instagram", "DAbc_12-xY", "https://www.instagram.com/reel/DAbc_12-xY/")
    assert social.detect("https://www.instagram.com/meu.perfil/reel/DAbc_12-xY/")[1] == "DAbc_12-xY"
    assert social.detect("https://www.tiktok.com/@meu.perfil/video/7421234567890123456?lang=pt") == (
        "tiktok", "7421234567890123456", "https://www.tiktok.com/@meu.perfil/video/7421234567890123456")
    assert social.detect("https://example.com/video") is None


def test_short_tiktok_links_are_opened_to_find_the_video(monkeypatch):
    class Reply:
        def geturl(self):
            return "https://www.tiktok.com/@perfil/video/7420000000000000001?_r=1"

    monkeypatch.setattr(social.urllib.request, "urlopen", lambda req, timeout: Reply())
    assert social.detect("https://vm.tiktok.com/ZMabc123/")[:2] == ("tiktok", "7420000000000000001")


def test_youtube_posts_of_the_old_schema_move_to_the_network_table(tmp_path):
    path = tmp_path / "v5.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE runs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'abertura', video TEXT NOT NULL);
        INSERT INTO runs(id, video) VALUES (1, 'v.mp4');
        CREATE TABLE youtube_posts (run_id INTEGER PRIMARY KEY, video_id TEXT NOT NULL, title TEXT, via TEXT NOT NULL,
            variant TEXT, privacy TEXT, posted_at TEXT NOT NULL, published_at TEXT);
        CREATE TABLE youtube_stats (run_id INTEGER NOT NULL, fetched_at TEXT NOT NULL, views INTEGER, likes INTEGER,
            comments INTEGER, PRIMARY KEY (run_id, fetched_at));
        INSERT INTO youtube_posts VALUES (1, '3Jhz1ySIk9M', NULL, 'link', NULL, NULL, '2026-10-07T09:27:20-03:00', NULL);
        INSERT INTO youtube_stats VALUES (1, '2026-10-07T10:00:00-03:00', 50, 4, 1);
        PRAGMA user_version = 5;
    """)
    old.close()
    con = db.connect(path)
    posts = social.posts(con, 1)
    assert posts["youtube"]["url"] == "https://youtu.be/3Jhz1ySIk9M" and posts["youtube"]["views"] == 50
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "youtube_posts" not in tables and "youtube_stats" not in tables


def test_posts_of_schema_6_get_the_scheduled_date(tmp_path):
    path = tmp_path / "v6.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE runs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'abertura', video TEXT NOT NULL);
        INSERT INTO runs(id, video) VALUES (1, 'v.mp4');
        CREATE TABLE posts (run_id INTEGER NOT NULL, network TEXT NOT NULL, post_id TEXT, url TEXT NOT NULL, title TEXT,
            via TEXT NOT NULL, variant TEXT, privacy TEXT, posted_at TEXT NOT NULL, published_at TEXT,
            PRIMARY KEY (run_id, network));
        INSERT INTO posts VALUES (1, 'youtube', 'abc', 'https://youtu.be/abc', NULL, 'link', NULL, NULL,
                                  '2026-10-07T09:00:00-03:00', NULL);
        PRAGMA user_version = 6;
    """)
    old.close()
    con = db.connect(path)
    assert social.posts(con, 1)["youtube"]["scheduled_at"] is None and social.scheduled(con, 1) == {}


def test_changing_the_linked_post_drops_the_old_numbers(settings):
    run_id = new_run(settings)
    con = db.connect(settings.db_path)
    social.save_post(con, run_id, "tiktok", "1", "https://www.tiktok.com/@a/video/1", via="link")
    social.save_stats(con, run_id, "tiktok", {"views": 10}, manual=True)
    assert social.posts(con, run_id)["tiktok"]["views"] == 10
    social.save_post(con, run_id, "tiktok", "2", "https://www.tiktok.com/@a/video/2", via="link")
    assert social.posts(con, run_id)["tiktok"]["views"] is None


def test_suggestion_does_not_spoil_the_result():
    scan = {"cards": [{"set": "1", "pack": 1}, {"set": "1", "pack": 2}]}
    s = social.suggestion(scan, {"1": "The First Chapter"})
    assert s["title"].startswith("Abrindo 2 boosters de The First Chapter") and len(s["title"]) <= 100
    assert s["hashtags"] == {"youtube": "#shorts", "instagram": "#reels", "tiktok": "#fyp"}
    assert not any(w in (s["title"] + s["caption"]).lower() for w in ("prejuízo", "lucro", "r$", "us$"))


def test_routes_link_any_network_take_manual_numbers_and_unlink(settings):
    run_id = new_run(settings)
    with TestClient(create_app(settings)) as client:
        assert client.patch(f"/api/runs/{run_id}/posts/tiktok", json={"views": 5}).status_code == 404  # sem post
        r = client.put(f"/api/runs/{run_id}/posts", json={"url": "https://www.tiktok.com/@eu/video/7421234567890123456"})
        assert r.json() == {"ok": True, "network": "tiktok"}
        r = client.put(f"/api/runs/{run_id}/posts", json={"url": "https://www.instagram.com/reel/DAbc_12-xY/"})
        assert r.json()["network"] == "instagram"
        assert client.patch(f"/api/runs/{run_id}/posts/tiktok", json={}).status_code == 400
        assert client.patch(f"/api/runs/{run_id}/posts/tiktok", json={"views": 1500, "likes": 120, "comments": 9}).status_code == 200
        posts = client.get(f"/api/runs/{run_id}").json()["posts"]
        assert posts["tiktok"]["views"] == 1500 and posts["tiktok"]["manual"] is True
        assert posts["instagram"]["post_id"] is None  # sem Instagram conectado: só o link (o ID vem da API)
        listed = next(r for r in client.get("/api/runs").json() if r["id"] == run_id)
        assert set(listed["posts"]) == {"tiktok", "instagram"}  # o gráfico do Resumo usa a lista
        assert client.post(f"/api/runs/{run_id}/posts/tiktok", json={"title": "x"}).status_code == 400  # TikTok: só pelo app
        assert client.delete(f"/api/runs/{run_id}/posts/tiktok").status_code == 200
        assert set(client.get(f"/api/runs/{run_id}").json()["posts"]) == {"instagram"}


def test_instagram_post_needs_a_public_address(settings, monkeypatch):
    run_id = new_run(settings)
    ig_connected(settings)
    monkeypatch.setattr("cardline.server.urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("sem ngrok")))
    published = []

    def fake_publish(s, video_url, caption, progress):
        published.append((video_url, caption))
        progress(1.0, "Publicado")
        return {"id": "1799", "permalink": "https://www.instagram.com/reel/DAbc/", "timestamp": "2026-10-07T12:00:00+0000"}

    monkeypatch.setattr(instagram, "publish_reel", fake_publish)
    monkeypatch.setattr(instagram, "stats", lambda s, ids: {})
    with TestClient(create_app(settings)) as client:
        body = {"caption": "Legenda", "variant": "overlay"}
        local = client.post(f"/api/runs/{run_id}/posts/instagram", json=body, headers={"host": "127.0.0.1:8000"})
        assert local.status_code == 409 and "túnel" in local.json()["detail"]  # aberta localmente e sem ngrok
        lan = client.post(f"/api/runs/{run_id}/posts/instagram", json=body, headers={"host": "192.168.31.51:8000"})
        assert lan.status_code == 409  # pelo IP da rede de casa: o Instagram não chega nele
        r = client.post(f"/api/runs/{run_id}/posts/instagram", json=body, headers={"host": "abc.ngrok-free.app"})
        assert r.status_code == 200
        for _ in range(50):
            job = client.get(f"/api/runs/{run_id}").json()["post_jobs"]["instagram"]
            if job["status"] != "sending":
                break
            time.sleep(0.05)
        assert job["status"] == "done", job
        url, caption = published[0]
        assert url.startswith(f"https://abc.ngrok-free.app/runs/{run_id}/overlay.mp4?v=") and caption == "Legenda\n\n#reels"
        post = client.get(f"/api/runs/{run_id}").json()["posts"]["instagram"]
        assert post["post_id"] == "1799" and post["url"] == "https://www.instagram.com/reel/DAbc/" and post["via"] == "api"


def test_the_cardline_publishes_the_scheduled_reel_at_the_time(settings, monkeypatch):
    run_id = new_run(settings)
    ig_connected(settings)
    tunnel = Tunnel()
    monkeypatch.setattr("cardline.server.urllib.request.urlopen", tunnel)
    published = []

    def fake_publish(s, video_url, caption, progress):
        published.append((video_url, caption))
        return {"id": "1799", "permalink": "https://www.instagram.com/reel/DAbc/", "timestamp": "2026-10-08T21:00:00+0000"}

    monkeypatch.setattr(instagram, "publish_reel", fake_publish)
    monkeypatch.setattr(instagram, "stats", lambda s, ids: {})
    at = (datetime.now().astimezone() + timedelta(hours=2)).replace(microsecond=0)
    body = {"caption": "Legenda", "variant": "overlay", "publish_at": at.isoformat()}
    lan = {"host": "192.168.31.51:8000"}  # pela rede de casa, sem túnel agora: programa mesmo assim
    with TestClient(create_app(settings)) as client:
        r = client.post(f"/api/runs/{run_id}/posts/instagram", json=body, headers=lan)
        assert r.status_code == 200 and r.json()["scheduled"] == at.isoformat()
        assert client.post(f"/api/runs/{run_id}/posts/instagram", json=body, headers=lan).status_code == 409
        publish_due = client.app.state.publish_due
        publish_due(at - timedelta(minutes=1))
        assert not published and client.get(f"/api/runs/{run_id}").json()["scheduled"]["instagram"]["status"] == "waiting"
        publish_due(at + timedelta(minutes=1))  # na hora, com o túnel fechado: espera e diz por quê
        waiting = client.get(f"/api/runs/{run_id}").json()["scheduled"]["instagram"]
        assert not published and waiting["status"] == "waiting" and "túnel" in waiting["error"]
        tunnel.open = True
        publish_due(at + timedelta(minutes=2))
        assert wait_job(client, run_id, "instagram")["status"] == "done"
        url, caption = published[0]
        assert url.startswith(f"https://abc.ngrok-free.app/runs/{run_id}/overlay.mp4?v=") and caption == "Legenda\n\n#reels"
        detail = client.get(f"/api/runs/{run_id}").json()
        assert detail["scheduled"] == {} and detail["posts"]["instagram"]["post_id"] == "1799"


def test_a_scheduled_reel_is_not_published_long_after_the_time(settings, monkeypatch):
    run_id = new_run(settings)
    ig_connected(settings)
    monkeypatch.setattr("cardline.server.urllib.request.urlopen", Tunnel())
    monkeypatch.setattr(instagram, "publish_reel", lambda *a: pytest.fail("publicou fora da hora"))
    monkeypatch.setattr(instagram, "find_media", lambda s, code: {"id": "1800", "timestamp": "2026-10-08T21:00:00+0000"})
    monkeypatch.setattr(instagram, "stats", lambda s, ids: {})
    at = (datetime.now().astimezone() + timedelta(hours=2)).replace(microsecond=0)
    body = {"caption": "Legenda", "variant": "overlay", "publish_at": at.isoformat()}
    with TestClient(create_app(settings)) as client:
        assert client.post(f"/api/runs/{run_id}/posts/instagram", json=body).status_code == 200
        client.app.state.publish_due(at + timedelta(hours=2))  # o cardline estava desligado na hora
        failed = client.get(f"/api/runs/{run_id}").json()["scheduled"]["instagram"]
        assert failed["status"] == "failed" and "desligado" in failed["error"]
        later = {**body, "publish_at": (at + timedelta(days=1)).isoformat()}  # programar de novo substitui a que falhou
        assert client.post(f"/api/runs/{run_id}/posts/instagram", json=later).status_code == 200
        assert client.get(f"/api/runs/{run_id}").json()["scheduled"]["instagram"]["status"] == "waiting"
        assert client.delete(f"/api/runs/{run_id}/scheduled/instagram").status_code == 200
        assert client.get(f"/api/runs/{run_id}").json()["scheduled"] == {}
        assert client.post(f"/api/runs/{run_id}/posts/instagram", json=later).status_code == 200
        assert client.put(f"/api/runs/{run_id}/posts", json={"url": "https://www.instagram.com/reel/DAbc_12-xY/"}).status_code == 200
        assert client.get(f"/api/runs/{run_id}").json()["scheduled"] == {}  # postou pelo app: a programação sai


def test_views_by_day_add_up_the_last_read_of_each_post(settings):
    a, b = new_run(settings, "a.mp4"), new_run(settings, "b.mp4")
    con = db.connect(settings.db_path)
    social.save_post(con, a, "youtube", "aaaaaaaaaaa", "https://youtu.be/aaaaaaaaaaa", via="link")
    social.save_post(con, b, "youtube", "bbbbbbbbbbb", "https://youtu.be/bbbbbbbbbbb", via="link")
    social.save_post(con, a, "tiktok", "1", "https://www.tiktok.com/@a/video/1", via="link")
    with con:
        con.executemany("INSERT INTO post_stats(run_id, network, fetched_at, views, manual) VALUES (?, ?, ?, ?, ?)", [
            (a, "youtube", "2026-10-06T10:00:00-03:00", 50, 0),
            (a, "youtube", "2026-10-06T20:00:00-03:00", 80, 0),   # mesmo dia: vale a última leitura
            (a, "tiktok", "2026-10-07T09:00:00-03:00", 300, 1),   # à mão também conta
            (b, "youtube", "2026-10-08T09:00:00-03:00", 20, 0),   # o B entra no dia em que foi lido
            (a, "youtube", "2026-10-08T09:30:00-03:00", 120, 0),
        ])
    assert social.views_by_day(con) == [
        {"day": "2026-10-06", "views": {"youtube": 80}},
        {"day": "2026-10-07", "views": {"youtube": 80, "tiktok": 300}},
        {"day": "2026-10-08", "views": {"youtube": 140, "tiktok": 300}},
    ]

