"""Cliente mínimo da API pública do Lorcast (https://lorcast.com/docs/api).

Fornece o catálogo de Lorcana, imagens oficiais e preços de mercado do TCGplayer
(`prices.usd` / `prices.usd_foil`).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.lorcast.com/v0"
USER_AGENT = "cardline/0.1 (pipeline local de abertura de boosters)"
_API_INTERVAL = 0.1  # a documentação pede 50–100 ms entre chamadas
_last_api_call = 0.0


def _fetch(url: str, *, timeout: float = 30, retries: int = 3) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise AssertionError("unreachable")


def _api(path: str) -> object:
    global _last_api_call
    wait = _API_INTERVAL - (time.monotonic() - _last_api_call)
    if wait > 0:
        time.sleep(wait)
    _last_api_call = time.monotonic()
    return json.loads(_fetch(API + path))


def fetch_sets() -> list[dict]:
    return _api("/sets")["results"]


def fetch_set_cards(set_id: str) -> list[dict]:
    return _api(f"/sets/{set_id}/cards")


def download(url: str, dest: Path) -> None:
    """Baixa um arquivo (imagens vêm da CDN, sem o limite de taxa da API)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(_fetch(url, timeout=60))
    tmp.replace(dest)
