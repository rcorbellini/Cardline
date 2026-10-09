import base64
import json

import pytest
from fastapi.testclient import TestClient

from cardline import auth, db, pipeline, youtube
from cardline.config import Settings
from cardline.server import create_app

pytestmark = pytest.mark.contas  # aqui o login é o de verdade


def jwt(claims: dict) -> str:
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{part({'alg': 'RS256'})}.{part(claims)}.assinatura"


class FakeGoogle:
    """O Google do fluxo de dispositivo: o código e, na segunda consulta, a conta que autorizou."""

    def __init__(self, monkeypatch):
        self.email = None
        self.polls = 0
        monkeypatch.setattr(youtube, "_request", self.request)

    def request(self, method, url, **kwargs):
        if url == youtube.DEVICE_URL:
            assert kwargs["form"]["scope"] == auth.SCOPE
            return 200, {}, {"device_code": "dc", "user_code": "ABCD-EFGH", "verification_url": "https://www.google.com/device",
                             "expires_in": 1800, "interval": 0}
        self.polls += 1
        if self.polls % 2:
            return 428, {}, {"error": "authorization_pending"}
        return 200, {}, {"access_token": "x", "id_token": jwt({"email": self.email, "email_verified": True, "name": "Fulano"})}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("cardline.money.usd_brl", lambda s: (5.0, "2026-10-08"))
    monkeypatch.setattr("cardline.server.usd_brl", lambda s: (5.0, "2026-10-08"))
    s = Settings(root=tmp_path, admin_email="dono@teste.dev")
    youtube.save_client(s, "123-abc.apps.googleusercontent.com", "segredo")
    return s


def login(client, google, email):
    google.email = email
    started = client.post("/api/auth/start").json()
    assert started["user_code"] == "ABCD-EFGH"
    for _ in range(3):
        r = client.post("/api/auth/poll", json={"id": started["id"]})
        if r.status_code != 200 or r.json()["status"] == "done":
            return r
    return r


def new_run(settings, name, user_id=None):
    video = settings.root / name
    video.write_bytes(name.encode())
    run_id = pipeline.create_run(settings, video, user_id=user_id)
    (settings.runs_dir / str(run_id) / "overlay.mp4").write_bytes(b"video")
    return run_id


def test_login_with_a_google_code_opens_a_session(settings, monkeypatch):
    google = FakeGoogle(monkeypatch)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/auth/me").json() == {"user": None, "google": True, "web": False}
        assert client.get("/api/runs").status_code == 401
        r = login(client, google, "Dono@Teste.dev")
        assert r.json()["user"]["email"] == "dono@teste.dev" and auth.COOKIE in r.cookies
        me = client.get("/api/auth/me").json()
        assert me["user"]["name"] == "Fulano" and me["admin"] is True
        assert client.get("/api/runs").status_code == 200
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/runs").status_code == 401


def test_only_the_admin_invited_and_allowed_emails_get_in(settings, monkeypatch):
    google = FakeGoogle(monkeypatch)
    with TestClient(create_app(settings)) as client:
        r = login(client, google, "estranho@teste.dev")
        assert r.status_code == 403 and "não tem acesso" in r.json()["detail"]
        login(client, google, "dono@teste.dev")
        assert client.post("/api/shares", json={"email": "amigo@teste.dev"}).json() == {"mine": ["amigo@teste.dev"]}
        assert client.post("/api/shares", json={"email": "não é e-mail"}).status_code == 400
        other = TestClient(client.app)
        assert login(other, google, "amigo@teste.dev").json()["status"] == "done"  # convidado entra


def test_each_one_sees_and_edits_only_their_own_and_shares_are_read_only(settings, monkeypatch):
    google = FakeGoogle(monkeypatch)
    with TestClient(create_app(settings)) as owner:
        login(owner, google, "dono@teste.dev")
        con = db.connect(settings.db_path)
        boss = auth.by_email(con, "dono@teste.dev")
        mine = new_run(settings, "dono.mp4", boss.id)
        friend = TestClient(owner.app)
        settings.allowed_emails = ["amigo@teste.dev"]
        login(friend, google, "amigo@teste.dev")
        pal = auth.by_email(con, "amigo@teste.dev")
        theirs = new_run(settings, "amigo.mp4", pal.id)
        assert [r["id"] for r in owner.get("/api/runs").json()] == [mine]
        assert [r["id"] for r in friend.get("/api/runs").json()] == [theirs]
        assert friend.get(f"/api/runs/{mine}").status_code == 404  # nem existe para quem não é dono
        assert friend.delete(f"/api/runs/{mine}").status_code == 404
        assert friend.get(f"/runs/{mine}/overlay.mp4").status_code == 404
        view = {"X-Cardline-Owner": str(boss.id)}
        assert friend.get("/api/runs", headers=view).status_code == 403  # ainda não compartilhou
        owner.post("/api/shares", json={"email": "amigo@teste.dev"})
        assert friend.get("/api/auth/me").json()["shared_with_me"][0]["email"] == "dono@teste.dev"
        assert [r["id"] for r in friend.get("/api/runs", headers=view).json()] == [mine]  # vê a do dono
        assert friend.get(f"/api/runs/{mine}", headers=view).status_code == 200
        assert friend.get(f"/runs/{mine}/overlay.mp4").content == b"video"
        assert friend.delete(f"/api/runs/{mine}", headers=view).status_code == 403  # só visualização
        assert friend.patch(f"/api/runs/{mine}", json={"paid": 1}).status_code == 404  # em nome dele: não é dele
        owner.delete("/api/shares/amigo@teste.dev")
        assert friend.get("/api/runs", headers=view).status_code == 403


def test_instagram_downloads_with_a_signed_link_and_old_data_goes_to_the_admin(settings, monkeypatch):
    legacy = new_run(settings, "antiga.mp4")  # de antes das contas: sem dono
    youtube_token = settings.data_dir / "youtube" / "token.json"
    youtube_token.write_text("{}")
    with TestClient(create_app(settings)) as client:  # a subida adota o que não tem dono
        con = db.connect(settings.db_path)
        boss = auth.by_email(con, "dono@teste.dev")
        assert con.execute("SELECT user_id FROM runs WHERE id = ?", (legacy,)).fetchone()[0] == boss.id
        assert (settings.data_dir / "youtube" / f"u{boss.id}" / "token.json").exists() and not youtube_token.exists()
        path = f"{legacy}/overlay.mp4"
        assert client.get(f"/runs/{path}?k={auth.sign(settings, path)}").content == b"video"
        assert client.get(f"/runs/{path}?k=errada").status_code == 404
        assert client.get(f"/runs/{legacy}/../../data/cardline.db?k={auth.sign(settings, f'{legacy}/../../data/cardline.db')}").status_code == 404


def test_without_an_id_token_the_account_comes_from_userinfo(settings, monkeypatch):
    def google(method, url, **kwargs):
        if url == youtube.TOKEN_URL:
            return 200, {}, {"access_token": "tok"}
        assert url == auth.USERINFO_URL and kwargs["headers"]["Authorization"] == "Bearer tok"
        return 200, {}, {"email": "Alguem@Gmail.com", "email_verified": True, "name": "Alguém"}

    monkeypatch.setattr(youtube, "_request", google)
    assert auth.poll_login(settings, "dc") == {"email": "alguem@gmail.com", "name": "Alguém", "picture": None}


def test_who_can_get_in(settings, tmp_path):
    con = db.connect(settings.db_path)
    assert auth.allowed(con, settings, "Dono@Teste.dev")  # o administrador, mesmo antes da primeira entrada
    assert not auth.allowed(con, settings, "qualquer@teste.dev")
    settings.allowed_emails = ["*"]
    assert auth.allowed(con, settings, "qualquer@teste.dev")
    fresh = Settings(root=tmp_path / "nova")  # sem admin_email: o primeiro a entrar vira o administrador
    con = db.connect(fresh.db_path)
    assert auth.allowed(con, fresh, "primeiro@teste.dev")
    auth.upsert_user(con, "primeiro@teste.dev")
    assert auth.is_admin(con, fresh, auth.by_email(con, "primeiro@teste.dev"))
    assert not auth.allowed(con, fresh, "segundo@teste.dev")


def test_on_the_public_address_the_google_button_signs_in(settings, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    settings.public_url = "https://cardiline.com.br"
    auth.save_web_client(settings, "web-123.apps.googleusercontent.com", "segredo-web")
    exchanged = {}

    def google(method, url, **kwargs):
        assert url == youtube.TOKEN_URL
        exchanged.update(kwargs["form"])
        return 200, {}, {"id_token": jwt({"iss": "https://accounts.google.com", "aud": "web-123.apps.googleusercontent.com",
                                          "email": "Dono@Teste.dev", "email_verified": True, "name": "Dono"})}

    monkeypatch.setattr(youtube, "_request", google)
    with TestClient(create_app(settings), base_url="https://cardiline.com.br") as client:
        lan = "http://192.168.31.51:8000"  # pela rede de casa (http): entra pelo código
        assert client.get(f"{lan}/api/auth/me").json()["web"] is False
        assert client.get(f"{lan}/api/auth/google", follow_redirects=False).status_code == 404
        assert client.get("/api/auth/me").json() == {"user": None, "google": True, "web": True}
        go = client.get("/api/auth/google", follow_redirects=False)
        to = urlparse(go.headers["location"])
        q = {k: v[0] for k, v in parse_qs(to.query).items()}
        assert go.status_code == 303 and to.netloc == "accounts.google.com"
        assert q["redirect_uri"] == "https://cardiline.com.br/api/auth/google/callback" and q["code_challenge_method"] == "S256"
        assert client.get(f"/api/auth/google/callback?state=forjado&code=x", follow_redirects=False).headers["location"].startswith("/?login_erro=")
        back = client.get(f"/api/auth/google/callback?state={q['state']}&code=abc", follow_redirects=False)
        assert back.status_code == 303 and back.headers["location"] == "/" and auth.COOKIE in back.cookies
        assert exchanged["code"] == "abc" and exchanged["redirect_uri"] == q["redirect_uri"] and exchanged["code_verifier"]
        assert client.get("/api/auth/me").json()["user"]["email"] == "dono@teste.dev"
        again = client.get(f"/api/auth/google/callback?state={q['state']}&code=abc", follow_redirects=False)
        assert "login_erro" in again.headers["location"]  # o mesmo retorno não serve duas vezes


def test_logo_social_accounts_and_collection_belong_to_each_user(settings, monkeypatch):
    import dataclasses
    import io

    from PIL import Image

    from cardline import instagram, logo

    settings.allowed_emails = ["amigo@teste.dev"]
    con = db.connect(settings.db_path)
    boss, pal = auth.upsert_user(con, "dono@teste.dev"), auth.upsert_user(con, "amigo@teste.dev")
    of = lambda user: dataclasses.replace(settings, account=user.id)  # noqa: E731
    # o dono tem logo padrão, canal do YouTube e Instagram conectados; o amigo, nada
    png = io.BytesIO()
    Image.new("RGBA", (40, 40), (255, 0, 0, 255)).save(png, "PNG")
    logo.save(of(boss), png.getvalue(), name=logo.DEFAULT)
    youtube._write(youtube.token_path(of(boss)), {"refresh_token": "r", "channel": {"title": "Canal do Dono"}})
    instagram._write(of(boss), {"access_token": "t", "username": "dono_ig"})
    mine = new_run(settings, "dono.mp4", boss.id)
    with con:
        con.execute("INSERT INTO cards(id, set_code, number, name) VALUES ('crd_a', '1', '1', 'A')")
        con.execute("INSERT INTO collection(card_id, run_id, added_at, user_id) VALUES ('crd_a', ?, 'x', ?)", (mine, boss.id))
    with TestClient(create_app(settings)) as client:
        class As:  # um cliente só (a aplicação sobe uma vez), com a sessão de cada conta
            def __init__(self, user):
                self.headers = {"cookie": f"{auth.COOKIE}={auth.new_session(con, user.id)}"}

            def __getattr__(self, method):
                return lambda url, **kw: getattr(client, method)(url, headers=self.headers, **kw)

        owner, friend = As(boss), As(pal)
        m_owner, m_friend = owner.get("/api/meta").json(), friend.get("/api/meta").json()
        assert m_owner["logo"]["default"] == "padrao.png" and m_friend["logo"]["default"] is None
        assert owner.get("/logos/padrao.png").status_code == 200
        assert friend.get("/logos/padrao.png").status_code == 404  # o logo do dono não aparece para o amigo
        assert owner.get("/api/youtube").json()["connected"] and not friend.get("/api/youtube").json()["connected"]
        assert owner.get("/api/instagram").json()["username"] == "dono_ig"
        assert not friend.get("/api/instagram").json()["connected"]
        # o amigo envia o logo dele: fica na pasta dele, e o dono não vê
        sent = friend.post("/api/logos", content=png.getvalue()).json()["logo"]
        assert logo.path(of(pal), sent) and not logo.path(of(boss), sent)
        assert owner.get(f"/logos/{sent}").status_code == 404
        # a coleção e as pipelines: cada um só a sua
        assert len(owner.get("/api/collection").json()["owned"]) == 1
        assert friend.get("/api/collection").json()["owned"] == [] and friend.get("/api/runs").json() == []
        # postar e vincular nas redes só na pipeline que é sua (e com a conta das suas redes)
        assert friend.put(f"/api/runs/{mine}/posts", json={"url": "https://youtu.be/abc"}).status_code == 404
        assert friend.post(f"/api/runs/{mine}/posts/youtube", json={"title": "x"}).status_code == 404
        assert friend.post("/api/youtube/disconnect").status_code == 200  # desconecta só o canal dele (nenhum)
        assert owner.get("/api/youtube").json()["connected"]
