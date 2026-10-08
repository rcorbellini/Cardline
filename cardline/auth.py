"""Contas: entrar com o Google, sessões por cookie e compartilhamento só para ver.

Entrar usa o fluxo de dispositivo do Google, o mesmo da conexão do YouTube e com o mesmo cliente OAuth: a página
mostra um código para digitar em google.com/device. Funciona pelo IP da rede de casa (o botão "Fazer login com o
Google" exige https ou localhost) e pelo túnel. O Google devolve o e-mail da conta; a sessão é um cookie.

Cada pipeline, carta, lacrado, atualização e histórico de valor tem dono. O que existia antes das contas (ou foi
criado pela linha de comando) fica com o administrador (`admin_email`, em data/config.toml). Compartilhar dá a
outro e-mail o direito de ver os dados de quem compartilhou, sem nenhuma ação.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import shutil
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import db, youtube
from .config import Settings

SCOPE = "openid email profile"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
COOKIE = "cardline_sessao"
SESSION_DAYS = 30


@dataclass(frozen=True)
class User:
    id: int
    email: str
    name: str | None = None
    picture: str | None = None


@dataclass(frozen=True)
class Viewer:
    """Quem está usando a página (`user`) e de quem são os dados mostrados (`owner`: ele mesmo, ou quem
    compartilhou com ele; aí é só leitura)."""

    user: User
    owner: int

    @property
    def readonly(self) -> bool:
        return self.owner != self.user.id


CURRENT: ContextVar[Viewer | None] = ContextVar("cardline_viewer", default=None)


def _user(row) -> User:
    return User(row["id"], row["email"], row["name"], row["picture"])


def get_user(con, user_id: int) -> User | None:
    row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user(row) if row else None


def by_email(con, email: str) -> User | None:
    row = con.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    return _user(row) if row else None


def upsert_user(con, email: str, name: str | None = None, picture: str | None = None) -> User:
    email = email.strip().lower()
    with con:
        con.execute("INSERT INTO users(email, name, picture, created_at, last_login) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(email) DO UPDATE SET name = COALESCE(excluded.name, name),"
                    " picture = COALESCE(excluded.picture, picture), last_login = excluded.last_login",
                    (email, name, picture, db.now(), db.now()))
    return by_email(con, email)


def admin(con, settings: Settings, create: bool = True) -> User | None:
    """O administrador: dono dos dados de antes das contas e quem mexe no catálogo (sets). Sem `admin_email`,
    é o primeiro que entrou."""
    if settings.admin_email:
        return by_email(con, settings.admin_email) or (upsert_user(con, settings.admin_email) if create else None)
    row = con.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    return _user(row) if row else None


def is_admin(con, settings: Settings, user: User) -> bool:
    boss = admin(con, settings, create=False)
    return boss is not None and boss.id == user.id


def adopt(con, settings: Settings) -> None:
    """O que não tem dono (de antes das contas, ou criado pela linha de comando) fica com o administrador,
    inclusive as credenciais do YouTube e do Instagram e os logos."""
    boss = admin(con, settings)
    if boss is None:
        return
    with con:
        con.execute("UPDATE runs SET user_id = ? WHERE user_id IS NULL", (boss.id,))
        for table in ("collection", "sealed"):  # o da pipeline de origem; à mão, o administrador
            con.execute(f"UPDATE {table} SET user_id = COALESCE((SELECT user_id FROM runs WHERE runs.id = {table}.run_id), ?)"
                        " WHERE user_id IS NULL", (boss.id,))
        con.execute("UPDATE jobs SET user_id = ? WHERE user_id IS NULL", (boss.id,))
        con.execute("UPDATE OR IGNORE user_values SET user_id = ? WHERE user_id = 0", (boss.id,))
        con.execute("DELETE FROM user_values WHERE user_id = 0")  # o mesmo dia já gravado na conta dele
    for service in ("youtube", "instagram"):
        old = settings.data_dir / service / "token.json"
        new = account_dir(settings, service, boss.id) / "token.json"
        if old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.replace(new)
    logos = settings.data_dir / "logos"
    if logos.is_dir():
        mine = account_dir(settings, "logos", boss.id)
        for f in logos.glob("*.png"):  # os logos de antes das contas
            mine.mkdir(parents=True, exist_ok=True)
            if not (mine / f.name).exists():
                shutil.move(str(f), mine / f.name)


def account_dir(settings: Settings, service: str, user_id: int | None) -> Path:
    """Pasta de um usuário dentro de data/<serviço> (tokens do YouTube e do Instagram, logos)."""
    base = settings.data_dir / service
    return base / f"u{user_id}" if user_id is not None else base


def allowed(con, settings: Settings, email: str) -> bool:
    """Quem pode entrar: o administrador, os e-mails liberados no config e quem recebeu um compartilhamento.
    Sem administrador ainda, o primeiro a entrar."""
    email = email.strip().lower()
    boss = admin(con, settings, create=False)
    if boss is None and not settings.admin_email:
        return True  # ninguém entrou ainda e não há administrador definido: o primeiro vira o administrador
    if email == (boss.email if boss else settings.admin_email.strip().lower()):
        return True
    open_to = {e.strip().lower() for e in settings.allowed_emails}
    if "*" in open_to or email in open_to:
        return True
    return con.execute("SELECT 1 FROM shares WHERE email = ?", (email,)).fetchone() is not None


# --- entrar (fluxo de dispositivo do Google) ---------------------------------------------------


def start_login(settings: Settings) -> dict:
    """Pede um código ao Google; o usuário abre `verification_url` e digita `user_code`."""
    cfg = youtube.client(settings)
    if cfg is None:
        raise LookupError("O cliente OAuth do Google ainda não foi configurado (veja o README).")
    code, _, payload = youtube._request("POST", youtube.DEVICE_URL, form={"client_id": cfg["client_id"], "scope": SCOPE})
    if code != 200:
        raise RuntimeError(f"O Google recusou o pedido de código: {youtube._message(payload)}")
    return payload  # device_code, user_code, verification_url, expires_in, interval


def poll_login(settings: Settings, device_code: str) -> dict | None:
    """Uma consulta ao Google: None enquanto espera; a conta (e-mail, nome, foto) quando autorizou."""
    cfg = youtube.client(settings)
    code, _, payload = youtube._request("POST", youtube.TOKEN_URL, form={
        "client_id": cfg["client_id"], "client_secret": cfg["client_secret"], "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    if code == 200:
        claims = id_claims(payload.get("id_token", ""))
        if not claims.get("email") and payload.get("access_token"):  # sem ID token: pergunta ao Google de quem é a conta
            status, _, info = youtube._request("GET", USERINFO_URL, headers={"Authorization": f"Bearer {payload['access_token']}"})
            claims = info if status == 200 else {}
        if not claims.get("email") or not claims.get("email_verified", True):
            raise RuntimeError("O Google não informou um e-mail verificado para esta conta.")
        return {"email": claims["email"].lower(), "name": claims.get("name"), "picture": claims.get("picture")}
    error = payload.get("error")
    if error in ("authorization_pending", "slow_down"):
        return None
    if error == "access_denied":
        raise PermissionError("O Google negou a autorização. Se o app do Google Cloud estiver em teste, a sua conta "
                              "precisa estar na lista de usuários de teste.")
    if error == "expired_token":
        raise TimeoutError("O código expirou. Peça outro.")
    raise RuntimeError(f"O Google recusou a entrada: {youtube._message(payload)}")


def id_claims(id_token: str) -> dict:
    """O conteúdo do ID token. Ele vem direto do endpoint de token do Google, por HTTPS, então não precisa conferir
    a assinatura (é o que a documentação do Google diz para esse caso)."""
    try:
        part = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


# --- sessões -----------------------------------------------------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(con, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now().astimezone()
    with con:
        con.execute("INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (_hash(token), user_id, now.isoformat(timespec="seconds"),
                     (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")))
    return token


def session_user(con, token: str | None) -> User | None:
    """O dono da sessão, se ela existe e não venceu. Quem usa a página continua entrando: a validade se renova."""
    if not token:
        return None
    row = con.execute("SELECT s.expires_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
                      (_hash(token),)).fetchone()
    if row is None:
        return None
    now = datetime.now().astimezone()
    expires = datetime.fromisoformat(row["expires_at"])
    if expires < now:
        end_session(con, token)
        return None
    if expires - now < timedelta(days=SESSION_DAYS - 1):
        with con:
            con.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                        ((now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds"), _hash(token)))
    return _user(row)


def request_user(con, settings: Settings, token: str | None) -> User | None:
    """O usuário de uma requisição, pelo cookie da sessão (um ponto só, para os testes trocarem)."""
    return session_user(con, token)


def end_session(con, token: str | None) -> None:
    if token:
        with con:
            con.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))


# --- compartilhar ------------------------------------------------------------------------------


def share(con, owner_id: int, email: str) -> None:
    with con:
        con.execute("INSERT OR IGNORE INTO shares(owner_id, email, created_at) VALUES (?, ?, ?)",
                    (owner_id, email.strip().lower(), db.now()))


def unshare(con, owner_id: int, email: str) -> None:
    with con:
        con.execute("DELETE FROM shares WHERE owner_id = ? AND email = ?", (owner_id, email.strip().lower()))


def shares(con, owner_id: int) -> list[str]:
    return [r[0] for r in con.execute("SELECT email FROM shares WHERE owner_id = ? ORDER BY created_at", (owner_id,))]


def shared_with(con, user: User) -> list[User]:
    """Quem compartilhou os dados com este usuário."""
    return [_user(r) for r in con.execute("SELECT u.* FROM shares s JOIN users u ON u.id = s.owner_id WHERE s.email = ?"
                                          " ORDER BY u.email", (user.email,))]


def can_view(con, user: User, owner_id: int) -> bool:
    return owner_id == user.id or con.execute("SELECT 1 FROM shares WHERE owner_id = ? AND email = ?",
                                              (owner_id, user.email)).fetchone() is not None


# --- arquivos com link assinado (o Instagram baixa o vídeo sem sessão) ------------------------


def _secret(settings: Settings) -> bytes:
    path = settings.data_dir / "segredo.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
    return path.read_bytes()


def sign(settings: Settings, path: str) -> str:
    return hmac.new(_secret(settings), path.encode(), hashlib.sha256).hexdigest()[:32]


def signed(settings: Settings, path: str, signature: str | None) -> bool:
    return bool(signature) and hmac.compare_digest(sign(settings, path), signature)
