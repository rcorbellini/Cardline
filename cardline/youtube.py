"""YouTube: conectar o canal, postar o vídeo de uma abertura e acompanhar visualizações e reações.

A conta é conectada pelo fluxo de dispositivo do OAuth: a página mostra um código para digitar em
google.com/device, o que funciona de qualquer aparelho (inclusive do celular, pelo túnel). O cliente OAuth é
do tipo "TVs e dispositivos de entrada limitada", criado pelo usuário no Google Cloud, com o escopo `youtube`
(postar e ler os números do canal). Credenciais e token ficam em data/youtube/, fora do git.

Atenção: o YouTube trava como privado todo vídeo enviado por um projeto de API que ainda não passou pela
auditoria do YouTube API Services. Até lá, o caminho é postar pelo app do YouTube e vincular o vídeo aqui.
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

SCOPE = "https://www.googleapis.com/auth/youtube"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
CHUNK = 8 * 1024 * 1024  # múltiplo de 256 KB, como o upload retomável exige
PRIVACY = ("public", "unlisted", "private")
GAMING = "20"  # categoria do vídeo no YouTube


class NotConnected(Exception):
    """Sem canal conectado (ou a autorização foi revogada)."""


class YouTubeError(Exception):
    """Erro da API do YouTube, com a mensagem que ela devolveu."""


# --- credenciais --------------------------------------------------------------------------------


def folder(settings: Settings) -> Path:
    return settings.data_dir / "youtube"


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.chmod(0o600)  # segredo do cliente e token do canal: só o dono lê
    tmp.replace(path)


def client(settings: Settings) -> dict | None:
    return _read(folder(settings) / "client.json")


def save_client(settings: Settings, client_id: str, client_secret: str) -> None:
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id.endswith(".apps.googleusercontent.com") or not client_secret:
        raise ValueError("Use o ID e a chave secreta de um cliente OAuth do Google (o ID termina em "
                         ".apps.googleusercontent.com).")
    _write(folder(settings) / "client.json", {"client_id": client_id, "client_secret": client_secret})


def token(settings: Settings) -> dict | None:
    return _read(folder(settings) / "token.json")


def status(settings: Settings) -> dict:
    tok = token(settings) or {}
    return {"configured": client(settings) is not None, "connected": bool(tok.get("refresh_token")),
            "channel": tok.get("channel")}


# --- HTTP ---------------------------------------------------------------------------------------


def _request(method: str, url: str, *, form: dict | None = None, body: dict | None = None,
             headers: dict | None = None, data: bytes | None = None, timeout: float = 60):
    """Faz a chamada e devolve (status, cabeçalhos, JSON). Erros HTTP voltam como status, não como exceção."""
    headers = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json; charset=UTF-8"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, resp.headers, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            payload = {"error": raw.decode(errors="replace")[:300]}
        return e.code, e.headers, payload


def _message(payload: dict) -> str:
    err = payload.get("error")
    if isinstance(err, dict):  # API do YouTube
        reasons = [e.get("reason") for e in err.get("errors", []) if e.get("reason")]
        return f"{err.get('message', 'erro')}{f' ({reasons[0]})' if reasons else ''}"
    return str(payload.get("error_description") or err or payload)


# --- conexão (fluxo de dispositivo) -------------------------------------------------------------


def start_device_login(settings: Settings) -> dict:
    """Pede um código ao Google: o usuário abre `verification_url` e digita `user_code`."""
    cfg = client(settings)
    if cfg is None:
        raise NotConnected("Configure o cliente OAuth primeiro.")
    code, _, payload = _request("POST", DEVICE_URL, form={"client_id": cfg["client_id"], "scope": SCOPE})
    if code != 200:
        raise YouTubeError(f"O Google recusou o pedido de código: {_message(payload)}")
    return payload  # device_code, user_code, verification_url, expires_in, interval


def poll_device_login(settings: Settings, device_code: str) -> str:
    """Uma consulta ao Google: "pending", "slow_down" ou "done" (token salvo). Erros viram exceção."""
    cfg = client(settings)
    code, _, payload = _request("POST", TOKEN_URL, form={
        "client_id": cfg["client_id"], "client_secret": cfg["client_secret"], "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    if code == 200:
        _save_token(settings, payload)
        tok = token(settings)
        tok["channel"] = my_channel(settings)
        _write(folder(settings) / "token.json", tok)
        return "done"
    error = payload.get("error")
    if error in ("authorization_pending", "slow_down"):
        return "pending" if error == "authorization_pending" else "slow_down"
    if error == "access_denied":
        raise YouTubeError("A autorização foi negada na conta do Google.")
    if error == "expired_token":
        raise YouTubeError("O código expirou. Peça outro.")
    raise YouTubeError(f"O Google recusou a conexão: {_message(payload)}")


def _save_token(settings: Settings, payload: dict) -> None:
    old = token(settings) or {}
    _write(folder(settings) / "token.json", {
        **old,
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token") or old.get("refresh_token"),  # a renovação não manda outro
        "expires_at": time.time() + float(payload.get("expires_in", 3600)),
        "scope": payload.get("scope", SCOPE),
    })


def access_token(settings: Settings) -> str:
    tok, cfg = token(settings), client(settings)
    if not tok or not tok.get("refresh_token") or not cfg:
        raise NotConnected("Conecte o canal do YouTube.")
    if tok.get("expires_at", 0) - 60 > time.time():
        return tok["access_token"]
    code, _, payload = _request("POST", TOKEN_URL, form={
        "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
        "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
    if code != 200:
        if payload.get("error") == "invalid_grant":  # revogado, ou o app em "teste" no Google (token dura 7 dias)
            raise NotConnected("A autorização do YouTube expirou ou foi revogada. Conecte o canal de novo.")
        raise YouTubeError(f"Não consegui renovar o acesso ao YouTube: {_message(payload)}")
    _save_token(settings, payload)
    return token(settings)["access_token"]


def disconnect(settings: Settings) -> None:
    tok = token(settings)
    if tok and tok.get("refresh_token"):
        _request("POST", REVOKE_URL, form={"token": tok["refresh_token"]}, timeout=15)  # se falhar, o token sai igual
    (folder(settings) / "token.json").unlink(missing_ok=True)


def _api(settings: Settings, method: str, path: str, **kwargs) -> dict:
    code, _, payload = _request(method, f"{API}/{path}", headers={"Authorization": f"Bearer {access_token(settings)}"},
                                **kwargs)
    if code >= 400:
        raise YouTubeError(_message(payload))
    return payload


def my_channel(settings: Settings) -> dict | None:
    items = _api(settings, "GET", "channels?part=snippet,contentDetails&mine=true").get("items") or []
    if not items:
        return None
    c = items[0]
    return {"id": c["id"], "title": c["snippet"]["title"],
            "thumbnail": c["snippet"].get("thumbnails", {}).get("default", {}).get("url"),
            "uploads": c.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")}


# --- vídeos -------------------------------------------------------------------------------------


def video_id(text: str) -> str | None:
    """O ID de 11 caracteres a partir do link (watch, youtu.be, shorts, live, embed) ou do próprio ID."""
    text = text.strip()
    if re.fullmatch(r"[\w-]{11}", text):
        return text
    m = re.search(r"(?:youtu\.be/|/shorts/|/live/|/embed/|[?&]v=)([\w-]{11})", text)
    return m.group(1) if m else None


def url(vid: str) -> str:
    return f"https://youtu.be/{vid}"


def upload(settings: Settings, path: Path, title: str, description: str, tags: list[str], privacy: str,
           progress=lambda fraction: None) -> dict:
    """Envia o vídeo (upload retomável, em partes, com progresso) e devolve o recurso que o YouTube criou."""
    if privacy not in PRIVACY:
        raise ValueError("Visibilidade deve ser public, unlisted ou private.")
    size = path.stat().st_size
    meta = {"snippet": {"title": title[:100], "description": description[:5000], "tags": tags[:30],
                        "categoryId": GAMING, "defaultLanguage": "pt-BR", "defaultAudioLanguage": "pt-BR"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    auth = {"Authorization": f"Bearer {access_token(settings)}"}
    code, headers, payload = _request("POST", UPLOAD_URL, body=meta, headers={
        **auth, "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
    if code != 200 or not headers.get("Location"):
        raise YouTubeError(f"O YouTube recusou o envio: {_message(payload)}")
    session, sent, failures = headers["Location"], 0, 0
    with open(path, "rb") as f:
        while True:
            f.seek(sent)
            chunk = f.read(CHUNK)
            last = sent + len(chunk) - 1
            try:
                code, headers, payload = _request("PUT", session, data=chunk, timeout=300, headers={
                    **auth, "Content-Length": str(len(chunk)), "Content-Range": f"bytes {sent}-{last}/{size}"})
            except OSError:  # conexão caiu no meio: pergunta ao YouTube até onde chegou e continua
                code, headers, payload = 503, {}, {}
            if code in (200, 201):
                progress(1.0)
                return payload
            if code == 308:  # parte recebida; "Range: bytes=0-N" diz até onde
                received = headers.get("Range")
                sent = int(received.rsplit("-", 1)[1]) + 1 if received else 0
                failures = 0
                progress(sent / size)
                continue
            if code in (500, 502, 503, 504) and failures < 5:
                failures += 1
                time.sleep(2 ** failures)
                try:  # até onde o YouTube recebeu?
                    code, headers, payload = _request("PUT", session, data=b"", headers={
                        **auth, "Content-Range": f"bytes */{size}"})
                except OSError:
                    continue
                if code in (200, 201):  # a última parte tinha chegado
                    progress(1.0)
                    return payload
                received = headers.get("Range") if code == 308 else None
                sent = int(received.rsplit("-", 1)[1]) + 1 if received else 0
                continue
            raise YouTubeError(f"O envio parou: {_message(payload)}")


def stats(settings: Settings, ids: list[str]) -> dict[str, dict]:
    """Números e situação de cada vídeo (até 50 por chamada; 1 unidade de cota cada)."""
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ",".join(ids[i:i + 50])
        for item in _api(settings, "GET", f"videos?part=statistics,status,snippet&id={chunk}").get("items", []):
            s, st = item.get("statistics", {}), item.get("status", {})
            out[item["id"]] = {
                "views": int(s.get("viewCount", 0)), "likes": int(s["likeCount"]) if "likeCount" in s else None,
                "comments": int(s["commentCount"]) if "commentCount" in s else None,
                "privacy": st.get("privacyStatus"), "upload_status": st.get("uploadStatus"),
                "title": item.get("snippet", {}).get("title"), "published_at": item.get("snippet", {}).get("publishedAt"),
            }
    return out


def recent_uploads(settings: Settings, n: int = 8) -> list[dict]:
    """Os últimos vídeos do canal (para vincular o que foi postado pelo app do YouTube)."""
    tok = token(settings) or {}
    uploads = (tok.get("channel") or {}).get("uploads") or (my_channel(settings) or {}).get("uploads")
    if not uploads:
        return []
    items = _api(settings, "GET", f"playlistItems?part=snippet,contentDetails&maxResults={n}&playlistId={uploads}")
    return [{"id": it["contentDetails"]["videoId"], "title": it["snippet"]["title"],
             "published_at": it["contentDetails"].get("videoPublishedAt") or it["snippet"].get("publishedAt"),
             "thumbnail": it["snippet"].get("thumbnails", {}).get("medium", {}).get("url")}
            for it in items.get("items", [])]


def suggestion(run, scan: dict, set_names: dict[str, str]) -> dict:
    """Título, descrição e tags sugeridos para o vídeo da abertura (sem spoiler do resultado)."""
    packs = max((c["pack"] or 1 for c in scan["cards"]), default=1)
    sets = list(dict.fromkeys(set_names.get(c["set"], f"set {c['set']}") for c in scan["cards"]))
    what = "um booster" if packs == 1 else f"{packs} boosters"
    title = f"Abrindo {what} de {' + '.join(sets)} | Disney Lorcana #shorts"
    description = (f"Abertura de {what} de Disney Lorcana: {', '.join(sets)}.\n"
                   "Preço de cada carta pelo mercado (TCGplayer) no dia da abertura. Será que valeu?\n\n"
                   "#lorcana #disneylorcana #tcg #booster #shorts")
    tags = ["lorcana", "disney lorcana", "booster", "abertura de booster", "tcg", *sets]
    return {"title": title[:100], "description": description, "tags": tags}


# --- banco ----------------------------------------------------------------------------------------


def save_post(con, run_id: int, vid: str, *, via: str, title: str | None = None, variant: str | None = None,
              privacy: str | None = None, published_at: str | None = None) -> None:
    from . import db

    with con:
        con.execute("DELETE FROM youtube_stats WHERE run_id = ? AND run_id NOT IN"
                    " (SELECT run_id FROM youtube_posts WHERE run_id = ? AND video_id = ?)", (run_id, run_id, vid))
        con.execute("INSERT OR REPLACE INTO youtube_posts(run_id, video_id, title, via, variant, privacy, posted_at,"
                    " published_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, vid, title, via, variant, privacy, db.now(), published_at))


def save_stats(con, by_video: dict[str, dict]) -> int:
    """Grava uma leitura dos números de cada vídeo vinculado e atualiza título e visibilidade."""
    from . import db

    stamp, n = db.now(), 0
    with con:
        for run_id, vid in con.execute("SELECT run_id, video_id FROM youtube_posts").fetchall():
            info = by_video.get(vid)
            if info is None:
                continue
            con.execute("INSERT OR REPLACE INTO youtube_stats(run_id, fetched_at, views, likes, comments)"
                        " VALUES (?, ?, ?, ?, ?)", (run_id, stamp, info["views"], info["likes"], info["comments"]))
            con.execute("UPDATE youtube_posts SET privacy = ?, title = COALESCE(?, title),"
                        " published_at = COALESCE(?, published_at) WHERE run_id = ?",
                        (info["privacy"], info["title"], info["published_at"], run_id))
            n += 1
    return n


def post_info(con, run_id: int) -> dict | None:
    post = con.execute("SELECT * FROM youtube_posts WHERE run_id = ?", (run_id,)).fetchone()
    if post is None:
        return None
    last = con.execute("SELECT * FROM youtube_stats WHERE run_id = ? ORDER BY fetched_at DESC LIMIT 1", (run_id,)).fetchone()
    return {"video_id": post["video_id"], "url": url(post["video_id"]), "title": post["title"], "via": post["via"],
            "variant": post["variant"], "privacy": post["privacy"], "posted_at": post["posted_at"],
            "published_at": post["published_at"], "views": last["views"] if last else None,
            "likes": last["likes"] if last else None, "comments": last["comments"] if last else None,
            "fetched_at": last["fetched_at"] if last else None}
