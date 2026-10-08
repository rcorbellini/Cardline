import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cardline import db, pipeline, youtube
from cardline.config import Settings
from cardline.server import create_app

CLIENT_ID = "123-abc.apps.googleusercontent.com"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("cardline.money.usd_brl", lambda s: (5.0, "2026-10-06"))
    monkeypatch.setattr("cardline.server.usd_brl", lambda s: (5.0, "2026-10-06"))
    s = Settings(root=tmp_path)
    youtube.save_client(s, CLIENT_ID, "segredo")
    return s


class FakeGoogle:
    """Respostas do Google por URL, na ordem; registra o que foi pedido."""

    def __init__(self, monkeypatch):
        self.routes: dict[str, list] = {}
        self.calls: list[tuple[str, str, dict]] = []
        monkeypatch.setattr(youtube, "_request", self.request)

    def on(self, method, prefix, *responses):
        self.routes.setdefault(f"{method} {prefix}", []).extend(responses)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for key, queue in self.routes.items():
            m, prefix = key.split(" ", 1)
            if m == method and url.startswith(prefix) and queue:
                code, headers, payload = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(payload, Exception):
                    raise payload
                return code, headers, payload
        raise AssertionError(f"chamada inesperada: {method} {url}")


def connected(settings):
    (youtube.folder(settings) / "token.json").write_text(json.dumps({
        "access_token": "tok", "refresh_token": "ref", "expires_at": time.time() + 3600,
        "channel": {"id": "UC1", "title": "Meu canal", "uploads": "UU1"}}))


def test_links_of_every_kind_give_the_video_id():
    for text in ["https://youtu.be/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3",
                 "https://youtube.com/shorts/dQw4w9WgXcQ?si=x", "dQw4w9WgXcQ", "https://m.youtube.com/live/dQw4w9WgXcQ"]:
        assert youtube.video_id(text) == "dQw4w9WgXcQ"
    assert youtube.video_id("https://example.com/video") is None


def test_client_is_validated_and_kept_private(settings):
    with pytest.raises(ValueError):
        youtube.save_client(settings, "abc", "x")
    path = youtube.folder(settings) / "client.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert youtube.status(settings) == {"configured": True, "connected": False, "channel": None}


def test_device_login_waits_then_saves_the_token_and_the_channel(settings, monkeypatch):
    g = FakeGoogle(monkeypatch)
    g.on("POST", youtube.DEVICE_URL, (200, {}, {"device_code": "dev", "user_code": "ABCD-EFGH", "expires_in": 1800,
                                                 "interval": 5, "verification_url": "https://www.google.com/device"}))
    g.on("POST", youtube.TOKEN_URL, (428, {}, {"error": "authorization_pending"}),
         (200, {}, {"access_token": "tok", "refresh_token": "ref", "expires_in": 3599}))
    g.on("GET", f"{youtube.API}/channels", (200, {}, {"items": [{"id": "UC1", "snippet": {"title": "Meu canal"},
                                                               "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}))
    assert youtube.start_device_login(settings)["user_code"] == "ABCD-EFGH"
    assert youtube.poll_device_login(settings, "dev") == "pending"
    assert youtube.poll_device_login(settings, "dev") == "done"
    assert youtube.status(settings) == {"configured": True, "connected": True,
                                        "channel": {"id": "UC1", "title": "Meu canal", "thumbnail": None, "uploads": "UU1"}}
    assert (youtube.folder(settings) / "token.json").stat().st_mode & 0o777 == 0o600


def test_expired_access_is_renewed_and_a_revoked_one_asks_to_connect_again(settings, monkeypatch):
    connected(settings)
    tok = json.loads((youtube.folder(settings) / "token.json").read_text())
    (youtube.folder(settings) / "token.json").write_text(json.dumps({**tok, "expires_at": 0}))
    g = FakeGoogle(monkeypatch)
    g.on("POST", youtube.TOKEN_URL, (200, {}, {"access_token": "novo", "expires_in": 3600}))
    assert youtube.access_token(settings) == "novo"
    assert youtube.token(settings)["refresh_token"] == "ref"  # a renovação não manda outro: o antigo fica

    tok = youtube.token(settings)
    (youtube.folder(settings) / "token.json").write_text(json.dumps({**tok, "expires_at": 0}))
    g.routes[f"POST {youtube.TOKEN_URL}"] = [(400, {}, {"error": "invalid_grant"})]
    with pytest.raises(youtube.NotConnected):
        youtube.access_token(settings)


def test_upload_goes_in_parts_and_resumes_after_a_failure(settings, monkeypatch, tmp_path):
    connected(settings)
    monkeypatch.setattr(youtube, "CHUNK", 4)
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0123456789")  # 10 bytes: partes de 4, 4 e 2
    g = FakeGoogle(monkeypatch)
    g.on("POST", youtube.UPLOAD_URL, (200, {"Location": "https://upload/sessao"}, {}))
    g.on("PUT", "https://upload/sessao",
         (308, {"Range": "bytes=0-3"}, {}),       # 1ª parte
         (503, {}, {}),                           # 2ª parte falha...
         (308, {"Range": "bytes=0-3"}, {}),       # ...a consulta diz que só a 1ª chegou
         (308, {"Range": "bytes=0-7"}, {}),       # 2ª parte de novo
         (201, {}, {"id": "vid12345678", "status": {"privacyStatus": "private"}}))
    seen = []
    created = youtube.upload(settings, video, "Título", "Descrição", ["lorcana"], "public", progress=seen.append)
    assert created["id"] == "vid12345678"
    puts = [c for c in g.calls if c[0] == "PUT"]
    assert [c[2]["headers"]["Content-Range"] for c in puts] == [
        "bytes 0-3/10", "bytes 4-7/10", "bytes */10", "bytes 4-7/10", "bytes 8-9/10"]
    assert seen == [0.4, 0.8, 1.0]
    meta = g.calls[0][2]["body"]
    assert meta["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    assert meta["snippet"]["tags"] == ["lorcana"]


def test_stats_come_from_the_api(settings, monkeypatch):
    connected(settings)
    g = FakeGoogle(monkeypatch)
    g.on("GET", f"{youtube.API}/videos", (200, {}, {"items": [{
        "id": "vid12345678", "statistics": {"viewCount": "120", "likeCount": "9", "commentCount": "2"},
        "status": {"privacyStatus": "public"}, "snippet": {"title": "Abrindo", "publishedAt": "2026-10-06T20:00:00Z"}}]}))
    info = youtube.stats(settings, ["vid12345678"])["vid12345678"]
    assert (info["views"], info["likes"], info["comments"], info["privacy"], info["title"]) == (120, 9, 2, "public", "Abrindo")
    assert info["scheduled_at"] is None  # já publicado


def test_scheduled_upload_is_private_until_youtube_publishes_it(settings, monkeypatch, tmp_path):
    connected(settings)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0123")
    g = FakeGoogle(monkeypatch)
    g.on("POST", youtube.UPLOAD_URL, (200, {"Location": "https://upload/sessao"}, {}))
    g.on("PUT", "https://upload/sessao", (201, {}, {"id": "vid12345678", "status": {"privacyStatus": "private"}}))
    when = datetime(2026, 10, 8, 18, 30, tzinfo=timezone(timedelta(hours=-3)))
    youtube.upload(settings, video, "Título", "Descrição", [], "unlisted", publish_at=when)
    assert g.calls[0][2]["body"]["status"] == {"privacyStatus": "private", "publishAt": "2026-10-08T21:30:00Z",
                                               "selfDeclaredMadeForKids": False}


def new_run(settings, name="v.mp4"):
    video = settings.root / name
    video.write_bytes(name.encode())
    run_id = pipeline.create_run(settings, video)
    con = db.connect(settings.db_path)
    with con:
        con.execute("UPDATE runs SET status = 'done' WHERE id = ?", (run_id,))
    return run_id


def test_routes_link_unlink_and_post(settings, monkeypatch):
    run_id = new_run(settings)
    folder = settings.runs_dir / str(run_id)
    (folder / "overlay.mp4").write_bytes(b"video")
    uploads, sent_tags, sent_when = [], [], []

    def fake_upload(s, path, title, description, tags, privacy, progress=lambda f: None, publish_at=None):
        uploads.append((path.name, title, privacy, description))
        sent_tags.append(tags)
        sent_when.append(publish_at)
        progress(1.0)
        status = {"privacyStatus": "private", **({"publishAt": "2026-10-08T21:30:00Z"} if publish_at else {})}
        return {"id": "novo1234567", "snippet": {"title": title}, "status": status}

    monkeypatch.setattr(youtube, "upload", fake_upload)
    monkeypatch.setattr(youtube, "stats", lambda s, ids: {})
    with TestClient(create_app(settings)) as client:
        assert client.put(f"/api/runs/{run_id}/posts", json={"url": "lixo"}).status_code == 400
        assert client.post(f"/api/runs/{run_id}/posts/youtube", json={"title": "Oi"}).status_code == 409  # sem canal

        # sem canal conectado, o vínculo vale (os números chegam quando conectar)
        r = client.put(f"/api/runs/{run_id}/posts", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        assert r.status_code == 200 and r.json()["network"] == "youtube"
        post = client.get(f"/api/runs/{run_id}").json()["posts"]["youtube"]
        assert post["post_id"] == "dQw4w9WgXcQ" and post["via"] == "link"
        assert client.post(f"/api/runs/{run_id}/posts/youtube", json={"title": "Oi"}).status_code == 409  # já vinculado
        assert client.delete(f"/api/runs/{run_id}/posts/youtube").status_code == 200
        assert client.get(f"/api/runs/{run_id}").json()["posts"] == {}

        connected(settings)
        assert client.post(f"/api/runs/{run_id}/posts/youtube", json={"title": "Abrindo um booster", "caption": "Legenda",
                                                                       "privacy": "public", "variant": "narrado"}).status_code == 200
        for _ in range(50):
            job = client.get(f"/api/runs/{run_id}").json()["post_jobs"]["youtube"]
            if job["status"] != "sending":
                break
            time.sleep(0.05)
        assert job["status"] == "done", job
        assert uploads == [("overlay.mp4", "Abrindo um booster", "public", "Legenda\n\n#shorts")]  # sem narrado.mp4: o overlay
        assert {"lorcana", "booster", "br", "brasil"} <= set(sent_tags[0])  # sem tags na página: as do cardline.toml
        post = client.get(f"/api/runs/{run_id}").json()["posts"]["youtube"]
        assert post["via"] == "api" and post["privacy"] == "private"  # projeto sem auditoria: travado como privado

        assert client.delete(f"/api/runs/{run_id}/posts/youtube").status_code == 200
        assert client.post(f"/api/runs/{run_id}/posts/youtube", json={"title": "De novo", "tags": ["minha tag", "br"]}).status_code == 200
        for _ in range(50):
            if client.get(f"/api/runs/{run_id}").json()["post_jobs"]["youtube"]["status"] != "sending":
                break
            time.sleep(0.05)
        assert sent_tags[1] == ["minha tag", "br"]  # as tags editadas na página
        assert client.get(f"/api/runs/{run_id}").json()["posts"]["youtube"]["scheduled_at"] is None  # postou na hora

        assert client.delete(f"/api/runs/{run_id}/posts/youtube").status_code == 200
        past = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat()
        r = client.post(f"/api/runs/{run_id}/posts/youtube", json={"title": "Depois", "publish_at": past})
        assert r.status_code == 400 and "futuro" in r.json()["detail"]
        later = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
        r = client.post(f"/api/runs/{run_id}/posts/youtube", json={"title": "Depois", "publish_at": later.isoformat()})
        assert r.status_code == 200
        for _ in range(50):
            if client.get(f"/api/runs/{run_id}").json()["post_jobs"]["youtube"]["status"] != "sending":
                break
            time.sleep(0.05)
        assert sent_when[-1] == later  # o YouTube publica sozinho na data
        assert client.get(f"/api/runs/{run_id}").json()["posts"]["youtube"]["scheduled_at"] == "2026-10-08T21:30:00Z"


def test_tags_follow_youtube_rules():
    assert youtube.clean_tags(["Lorcana", "lorcana", " booster  <br> ", "", "brasil"]) == ["Lorcana", "booster br", "brasil"]
    many = youtube.clean_tags([f"tag número {i}" for i in range(80)])  # com espaço: contam as aspas
    used = sum(len(t) + 2 for t in many) + len(many) - 1  # + as vírgulas
    assert used <= 500 < used + len("tag número 99") + 3  # para quando a próxima não cabe

