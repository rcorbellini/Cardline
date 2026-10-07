"""Posts de uma abertura nas redes (YouTube, Instagram, TikTok): vínculo, números e a legenda sugerida.

Cada pipeline tem no máximo um post por rede. O post entra pela API da rede (YouTube, Instagram) ou é
vinculado pelo link depois de postado pelo app. Os números vêm da API quando ela existe e está conectada;
no TikTok (sem API sem a aprovação do app), dá para informar à mão. Cada leitura fica guardada.
"""

from __future__ import annotations

import re
import urllib.request

from . import db

NETWORKS = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok"}
HASHTAGS = {"youtube": "#shorts", "instagram": "#reels", "tiktok": "#fyp"}
METRICS = ("views", "likes", "comments", "shares", "saves")


def detect(link: str) -> tuple[str, str | None, str] | None:
    """(rede, ID na rede, link) a partir do link do post; None se não for de uma rede conhecida."""
    from .youtube import video_id

    link = link.strip()
    if re.search(r"(youtube\.com|youtu\.be)/", link) or re.fullmatch(r"[\w-]{11}", link):
        vid = video_id(link)
        return ("youtube", vid, f"https://youtu.be/{vid}") if vid else None
    if m := re.search(r"instagram\.com/(?:[\w.]+/)?(?:reels?|p|tv)/([\w-]+)", link):
        return "instagram", m.group(1), f"https://www.instagram.com/reel/{m.group(1)}/"
    if "tiktok.com" in link:
        if re.search(r"//(vm|vt)\.tiktok\.com/|tiktok\.com/t/", link):  # link curto do app: abre para achar o vídeo
            try:
                req = urllib.request.Request(link if link.startswith("http") else f"https://{link}",
                                             headers={"User-Agent": "Mozilla/5.0 (cardline)"})
                link = urllib.request.urlopen(req, timeout=15).geturl()
            except OSError:
                return "tiktok", None, link
        m = re.search(r"tiktok\.com/@([\w.-]+)/video/(\d+)", link)
        return ("tiktok", m.group(2), f"https://www.tiktok.com/@{m.group(1)}/video/{m.group(2)}") if m else ("tiktok", None, link)
    return None


def save_post(con, run_id: int, network: str, post_id: str | None, url: str, *, via: str, title: str | None = None,
              variant: str | None = None, privacy: str | None = None, published_at: str | None = None) -> None:
    """Vincula o post à abertura (um por rede); trocar de post apaga os números do anterior."""
    with con:
        old = con.execute("SELECT post_id, url FROM posts WHERE run_id = ? AND network = ?", (run_id, network)).fetchone()
        if old and (old["post_id"], old["url"]) != (post_id, url):
            con.execute("DELETE FROM posts WHERE run_id = ? AND network = ?", (run_id, network))  # e os números (cascata)
        con.execute("INSERT INTO posts(run_id, network, post_id, url, title, via, variant, privacy, posted_at, published_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(run_id, network) DO UPDATE SET"
                    " title = COALESCE(excluded.title, title), privacy = COALESCE(excluded.privacy, privacy),"
                    " published_at = COALESCE(excluded.published_at, published_at)",
                    (run_id, network, post_id, url, title, via, variant, privacy, db.now(), published_at))


def save_stats(con, run_id: int, network: str, numbers: dict, *, manual: bool = False) -> None:
    with con:
        con.execute("INSERT OR REPLACE INTO post_stats(run_id, network, fetched_at, views, likes, comments, shares, saves,"
                    " manual) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, network, db.now(), *(numbers.get(k) for k in METRICS), int(manual)))


def update_post(con, run_id: int, network: str, **fields) -> None:
    fields = {k: v for k, v in fields.items() if v is not None and k in ("title", "privacy", "published_at")}
    if fields:
        with con:
            con.execute(f"UPDATE posts SET {', '.join(f'{k} = ?' for k in fields)} WHERE run_id = ? AND network = ?",
                        (*fields.values(), run_id, network))


def posts(con, run_id: int) -> dict[str, dict]:
    """Os posts da abertura por rede, cada um com a última leitura dos números."""
    out = {}
    for p in con.execute("SELECT * FROM posts WHERE run_id = ?", (run_id,)).fetchall():
        last = con.execute("SELECT * FROM post_stats WHERE run_id = ? AND network = ? ORDER BY fetched_at DESC LIMIT 1",
                           (run_id, p["network"])).fetchone()
        out[p["network"]] = {
            "network": p["network"], "post_id": p["post_id"], "url": p["url"], "title": p["title"], "via": p["via"],
            "variant": p["variant"], "privacy": p["privacy"], "posted_at": p["posted_at"], "published_at": p["published_at"],
            **{k: last[k] if last else None for k in METRICS},
            "fetched_at": last["fetched_at"] if last else None, "manual": bool(last["manual"]) if last else False,
        }
    return out


def linked(con, network: str) -> list[tuple[int, str]]:
    """(run_id, ID na rede) dos posts dessa rede que têm ID, para buscar os números."""
    return [(r[0], r[1]) for r in con.execute("SELECT run_id, post_id FROM posts WHERE network = ? AND post_id IS NOT NULL",
                                              (network,))]


def suggestion(scan: dict, set_names: dict[str, str]) -> dict:
    """Título e legenda sugeridos para o vídeo da abertura (sem spoiler do resultado)."""
    packs = max((c.get("pack") or 1 for c in scan["cards"]), default=1)
    sets = list(dict.fromkeys(set_names.get(c["set"], f"set {c['set']}") for c in scan["cards"] if c.get("set"))) or ["Lorcana"]
    what = "um booster" if packs == 1 else f"{packs} boosters"
    title = f"Abrindo {what} de {' + '.join(sets)} | Disney Lorcana"
    caption = (f"Abertura de {what} de Disney Lorcana: {', '.join(sets)}.\n"
               "Preço de cada carta pelo mercado (TCGplayer) no dia da abertura. Será que valeu?\n\n"
               "#lorcana #disneylorcana #tcg #booster")
    return {"title": title[:100], "caption": caption, "tags": ["lorcana", "disney lorcana", "booster", "tcg", *sets],
            "hashtags": HASHTAGS}
