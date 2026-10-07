"""Instagram (Reels): token gerado no painel da Meta, publicação pela API e números de cada Reel.

Usa a "API do Instagram com login do Instagram" (graph.instagram.com) numa conta profissional que é
testadora do app: publicar na própria conta não precisa de revisão da Meta, e o Reel sai público. O token
vem do painel do app (vale 60 dias) e é renovado aqui antes de vencer. Fica em data/instagram/, fora do git.

Nesse caminho o Instagram baixa o vídeo de um endereço público: o cardline oferece o arquivo pelo túnel (ngrok)
durante a publicação, então o computador e o túnel precisam estar ligados.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import Settings

GRAPH = "https://graph.instagram.com/v23.0"
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
TOKEN_DAYS = 60
PERMISSIONS = ("instagram_business_basic", "instagram_business_content_publish", "instagram_business_manage_insights")


class NotConnected(Exception):
    """Sem token do Instagram (ou ele venceu)."""


class InstagramError(Exception):
    """Erro da API do Instagram, com a mensagem que ela devolveu."""


def folder(settings: Settings) -> Path:
    return settings.data_dir / "instagram"


def token(settings: Settings) -> dict | None:
    path = folder(settings) / "token.json"
    return json.loads(path.read_text()) if path.exists() else None


def _write(settings: Settings, data: dict) -> None:
    path = folder(settings) / "token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.chmod(0o600)  # token da conta: só o dono lê
    tmp.replace(path)


def status(settings: Settings) -> dict:
    tok = token(settings) or {}
    return {"connected": bool(tok.get("access_token")), "username": tok.get("username"),
            "expires_at": tok.get("expires_at")}


def _call(method: str, path: str, params: dict, timeout: float = 60) -> dict:
    url = path if path.startswith("http") else f"{GRAPH}/{path}"
    data = None
    if method == "GET":
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error", {})
        except ValueError:
            err = {}
        if err.get("code") == 190:  # token inválido ou vencido
            raise NotConnected("O token do Instagram venceu ou foi revogado. Gere outro no painel da Meta.") from e
        raise InstagramError(err.get("error_user_msg") or err.get("message") or f"HTTP {e.code}") from e


def _api(settings: Settings, method: str, path: str, **params) -> dict:
    tok = token(settings)
    if not tok or not tok.get("access_token"):
        raise NotConnected("Conecte o Instagram (cole o token gerado no painel da Meta).")
    return _call(method, path, {**params, "access_token": tok["access_token"]})


def save_token(settings: Settings, access_token: str) -> dict:
    """Confere o token na API (conta profissional) e guarda; devolve a situação."""
    access_token = access_token.strip()
    if not re.fullmatch(r"[\w.-]{30,}", access_token):
        raise ValueError("Isso não parece um token do Instagram (é um texto longo que começa com IG…).")
    me = _call("GET", "me", {"fields": "user_id,username,account_type", "access_token": access_token})
    if me.get("account_type") not in ("BUSINESS", "MEDIA_CREATOR", None):
        raise ValueError("A conta precisa ser profissional (Comercial ou Criador de conteúdo) para publicar pela API.")
    _write(settings, {"access_token": access_token, "user_id": str(me.get("user_id") or me.get("id")),
                      "username": me.get("username"), "saved_at": time.time(),
                      "expires_at": time.time() + TOKEN_DAYS * 86400})
    return status(settings)


def refresh(settings: Settings, force: bool = False) -> None:
    """Renova o token (mais 60 dias) quando ele já tem uma semana; tokens com menos de 24 h não renovam."""
    tok = token(settings)
    if not tok or (not force and time.time() - tok.get("saved_at", 0) < 7 * 86400):
        return
    new = _call("GET", REFRESH_URL, {"grant_type": "ig_refresh_token", "access_token": tok["access_token"]})
    _write(settings, {**tok, "access_token": new["access_token"], "saved_at": time.time(),
                      "expires_at": time.time() + float(new.get("expires_in", TOKEN_DAYS * 86400))})


def disconnect(settings: Settings) -> None:
    (folder(settings) / "token.json").unlink(missing_ok=True)


def publish_reel(settings: Settings, video_url: str, caption: str, progress=lambda fraction, message: None,
                 timeout: float = 600, poll: float = 5) -> dict:
    """Publica o Reel: cria o contêiner (o Instagram baixa o vídeo do link), espera o processamento e publica."""
    user = token(settings)["user_id"]
    progress(0.05, "Enviando o link do vídeo ao Instagram")
    container = _api(settings, "POST", f"{user}/media", media_type="REELS", video_url=video_url,
                     caption=caption[:2200], share_to_feed="true")["id"]
    start = time.monotonic()
    while True:
        state = _api(settings, "GET", container, fields="status_code,status")
        code = state.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise InstagramError(f"O Instagram não conseguiu processar o vídeo: {state.get('status') or code}. "
                                 "Ele precisa conseguir baixar o vídeo pelo túnel.")
        if time.monotonic() - start > timeout:
            raise InstagramError("O Instagram demorou demais para processar o vídeo.")
        progress(min(0.85, 0.1 + (time.monotonic() - start) / 120), "O Instagram está processando o vídeo")
        time.sleep(poll)
    progress(0.9, "Publicando")
    media = _api(settings, "POST", f"{user}/media_publish", creation_id=container)["id"]
    info = _api(settings, "GET", media, fields="id,permalink,shortcode,timestamp,caption")
    progress(1.0, "Publicado")
    return info


def find_media(settings: Settings, shortcode: str) -> dict | None:
    """O Reel da conta com esse código (o pedaço do link instagram.com/reel/<código>/)."""
    page = _api(settings, "GET", f"{token(settings)['user_id']}/media", fields="id,shortcode,permalink,timestamp,caption",
                limit=50)
    return next((m for m in page.get("data", []) if m.get("shortcode") == shortcode), None)


def stats(settings: Settings, media_ids: list[str]) -> dict[str, dict]:
    """Visualizações, curtidas, comentários, compartilhamentos e salvamentos de cada Reel."""
    out = {}
    for media in media_ids:
        base = _api(settings, "GET", media, fields="like_count,comments_count,permalink,timestamp")
        numbers = {"likes": base.get("like_count"), "comments": base.get("comments_count"), "views": None,
                   "shares": None, "saves": None}
        try:
            ins = _api(settings, "GET", f"{media}/insights", metric="views,shares,saved")
            for item in ins.get("data", []):
                value = (item.get("values") or [{}])[0].get("value", item.get("total_value", {}).get("value"))
                numbers[{"views": "views", "shares": "shares", "saved": "saves"}[item["name"]]] = value
        except InstagramError:
            pass  # sem a permissão de insights: ficam curtidas e comentários
        out[media] = {**numbers, "published_at": base.get("timestamp"), "url": base.get("permalink")}
    return out
