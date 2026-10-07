import json
import sqlite3
import time

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
    (instagram.folder(settings)).mkdir(parents=True, exist_ok=True)
    (instagram.folder(settings) / "token.json").write_text(json.dumps({"access_token": "IG" + "x" * 40, "user_id": "17",
                                                                       "username": "eu", "saved_at": time.time()}))
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
