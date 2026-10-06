"""Segunda opinião opcional: um modelo de visão local (Ollama) lê nome e número de cada recorte.

O SIFT casa a *imagem*; o modelo lê o *texto*: são sinais independentes. Como o modelo pode
alucinar, ele nunca troca a carta sozinho — divergências viram aviso com a correção sugerida.
"""

from __future__ import annotations

import base64
import json
import os
import re
import unicodedata
import urllib.request
from pathlib import Path

from . import db
from .config import Settings

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
PROMPT = (
    "This is a photo of a Disney Lorcana trading card. Return JSON with: name (the big title) and "
    "collector_number (the bottom-left line, like '169/204 • EN • 1')."
)
SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "collector_number": {"type": "string"}},
    "required": ["name", "collector_number"],
}
COLLECTOR = re.compile(r"(\d+)\s*/\s*\d+(?:\W+[A-Z]{2}\W+(\w+))?")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower())


def _read_card(model: str, image: Path) -> dict:
    body = {
        "model": model, "prompt": PROMPT, "images": [base64.b64encode(image.read_bytes()).decode()],
        "stream": False, "format": SCHEMA, "think": False, "options": {"temperature": 0},
    }
    req = urllib.request.Request(
        f"{OLLAMA}/api/generate", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(json.load(resp)["response"])


class Unavailable(RuntimeError):
    pass


def verify(settings: Settings, run_dir: Path, scan: dict, model: str, progress) -> list[str]:
    """Confere cada carta e grava o resultado em `check`; devolve um aviso por divergência."""
    try:
        urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3).close()
    except OSError as e:
        raise Unavailable(f"Ollama indisponível em {OLLAMA}") from e
    con = db.connect(settings.db_path)
    warnings = []
    for i, c in enumerate(scan["cards"], 1):
        progress(i / len(scan["cards"]), f"Conferindo carta {i}/{len(scan['cards'])} com {model}")
        if not c.get("crop"):
            continue
        read = _read_card(model, run_dir / c["crop"])
        m = COLLECTOR.search(read.get("collector_number", ""))
        number = m.group(1).lstrip("0") if m else None
        name_ok = _norm(read.get("name", "")) == _norm(c["name"])
        number_ok = number == c["number"].lstrip("0")
        status = "ok" if name_ok and number_ok else "parcial" if name_ok or number_ok else "divergente"
        c["check"] = {"model": model, "status": status, "name": read.get("name"), "number": read.get("collector_number")}
        if status != "divergente":
            continue
        hint = ""
        if number:
            set_code = (m.group(2) if m.group(2) and m.group(2) in scan["sets"] else None) or c["set"]
            alt = con.execute("SELECT * FROM cards WHERE set_code = ? AND number = ?", (set_code, number)).fetchone()
            if alt:
                hint = f" — se for {db.display_name(alt)}, corrija para {set_code}/{number}"
        warnings.append(f"#{i}: identificada como {db.display_name(c)} ({c['set']}/{c['number']}), "
                        f"mas {model} leu {read.get('name')!r} / {read.get('collector_number')!r}{hint}")
    return warnings
