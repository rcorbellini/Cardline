import json
import time
import urllib.parse

import pytest

from cardline import instagram
from cardline.config import Settings

TOKEN = "IGAA" + "x" * 60


@pytest.fixture
def settings(tmp_path):
    return Settings(root=tmp_path)


class FakeGraph:
    """Respostas da API do Instagram por caminho, na ordem; registra o que foi pedido."""

    def __init__(self, monkeypatch):
        self.routes: dict[str, list] = {}
        self.calls: list[tuple[str, str, dict]] = []
        monkeypatch.setattr(instagram, "_call", self.call)

    def on(self, method, path, *responses):
        self.routes.setdefault(f"{method} {path}", []).extend(responses)

    def call(self, method, path, params, timeout=60):
        self.calls.append((method, path, params))
        queue = self.routes.get(f"{method} {path}")
        if not queue:
            raise AssertionError(f"chamada inesperada: {method} {path}")
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def connected(settings):
    instagram.folder(settings).mkdir(parents=True, exist_ok=True)
    (instagram.folder(settings) / "token.json").write_text(json.dumps(
        {"access_token": TOKEN, "user_id": "17841", "username": "eu", "saved_at": time.time(), "expires_at": time.time() + 9e5}))


def test_token_is_checked_and_kept_private(settings, monkeypatch):
    g = FakeGraph(monkeypatch)
    with pytest.raises(ValueError):
        instagram.save_token(settings, "curto")
    g.on("GET", "me", {"user_id": "17841", "username": "eu", "account_type": "PERSONAL"})
    with pytest.raises(ValueError):  # conta pessoal não publica pela API
        instagram.save_token(settings, TOKEN)
    g.routes["GET me"] = [{"user_id": "17841", "username": "eu", "account_type": "MEDIA_CREATOR"}]
    assert instagram.save_token(settings, TOKEN)["username"] == "eu"
    assert (instagram.folder(settings) / "token.json").stat().st_mode & 0o777 == 0o600


def test_reel_is_published_in_three_steps(settings, monkeypatch):
    connected(settings)
    monkeypatch.setattr(instagram.time, "sleep", lambda s: None)
    g = FakeGraph(monkeypatch)
    g.on("POST", "17841/media", {"id": "container1"})
    g.on("GET", "container1", {"status_code": "IN_PROGRESS"}, {"status_code": "FINISHED"})
    g.on("POST", "17841/media_publish", {"id": "media1"})
    g.on("GET", "media1", {"id": "media1", "permalink": "https://www.instagram.com/reel/ABC/", "shortcode": "ABC"})
    steps = []
    media = instagram.publish_reel(settings, "https://tunel/runs/1/narrado.mp4", "Legenda", lambda f, m: steps.append(m))
    assert media["permalink"] == "https://www.instagram.com/reel/ABC/"
    create = g.calls[0][2]
    assert create["media_type"] == "REELS" and create["video_url"] == "https://tunel/runs/1/narrado.mp4"
    assert create["access_token"] == TOKEN and g.calls[-2][2]["creation_id"] == "container1"
    assert steps[-1] == "Publicado"


def test_reel_that_instagram_cannot_download_fails_with_a_hint(settings, monkeypatch):
    connected(settings)
    g = FakeGraph(monkeypatch)
    g.on("POST", "17841/media", {"id": "c2"})
    g.on("GET", "c2", {"status_code": "ERROR", "status": "Error: media download failed"})
    with pytest.raises(instagram.InstagramError, match="túnel"):
        instagram.publish_reel(settings, "https://tunel/v.mp4", "x")


def test_numbers_come_from_the_media_and_its_insights(settings, monkeypatch):
    connected(settings)
    g = FakeGraph(monkeypatch)
    g.on("GET", "m1", {"like_count": 31, "comments_count": 4, "timestamp": "2026-10-07T12:00:00+0000"})
    g.on("GET", "m1/insights", {"data": [{"name": "views", "values": [{"value": 980}]},
                                         {"name": "shares", "values": [{"value": 7}]},
                                         {"name": "saved", "values": [{"value": 3}]}]})
    assert instagram.stats(settings, ["m1"])["m1"] == {"likes": 31, "comments": 4, "views": 980, "shares": 7, "saves": 3,
                                                        "published_at": "2026-10-07T12:00:00+0000", "url": None}


def test_reel_linked_by_its_code_is_found_in_the_account(settings, monkeypatch):
    connected(settings)
    g = FakeGraph(monkeypatch)
    g.on("GET", "17841/media", {"data": [{"id": "m9", "shortcode": "XYZ"}, {"id": "m8", "shortcode": "ABC"}]})
    assert instagram.find_media(settings, "ABC")["id"] == "m8"
    assert instagram.find_media(settings, "nada") is None


def test_expired_token_asks_to_connect_again(settings, monkeypatch):
    connected(settings)

    def expired(req, timeout):
        raise instagram.urllib.error.HTTPError(req.full_url, 400, "Bad", {}, _Body(
            json.dumps({"error": {"code": 190, "message": "Session has expired"}}).encode()))

    monkeypatch.setattr(instagram.urllib.request, "urlopen", expired)
    with pytest.raises(instagram.NotConnected):
        instagram.stats(settings, ["m1"])


class _Body:
    def __init__(self, data):
        self.data = data

    def read(self, *args):
        return self.data

    def close(self):
        pass


def test_old_token_is_renewed(settings, monkeypatch):
    connected(settings)
    tok = instagram.token(settings)
    (instagram.folder(settings) / "token.json").write_text(json.dumps({**tok, "saved_at": time.time() - 8 * 86400}))
    g = FakeGraph(monkeypatch)
    g.on("GET", instagram.REFRESH_URL, {"access_token": "IGnovo" + "y" * 40, "expires_in": 5184000})
    instagram.refresh(settings)
    assert instagram.token(settings)["access_token"].startswith("IGnovo")
    assert urllib.parse.urlparse(instagram.REFRESH_URL).netloc == "graph.instagram.com"
