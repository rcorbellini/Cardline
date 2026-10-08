"""Logo no vídeo com overlay: semitransparente num canto, do começo ao fim (capa e resumo incluídos).

Os logos ficam em data/logos (fora do git): `padrao.png` é o padrão das pipelines novas, e os enviados na
criação de uma pipeline ganham o nome pelo conteúdo (o mesmo arquivo enviado de novo não duplica).
"""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

from PIL import Image

from .config import Settings

DEFAULT = "padrao.png"
MAX_SIDE = 600  # o logo ocupa ~13% da largura do vídeo: mais que isso não aparece
CORNERS = ("top-right", "top-left", "bottom-right", "bottom-left")


def folder(settings: Settings) -> Path:
    return settings.data_dir / "logos"


def path(settings: Settings, name: str | None) -> Path | None:
    """O arquivo do logo `name` (só nomes desta pasta), ou None se não existir."""
    if not name or not re.fullmatch(r"[\w-]+\.png", name):
        return None
    p = folder(settings) / name
    return p if p.exists() else None


def default(settings: Settings) -> str | None:
    return DEFAULT if path(settings, DEFAULT) else None


def save(settings: Settings, data: bytes, name: str | None = None) -> str:
    """Guarda a imagem como PNG com transparência (no máximo MAX_SIDE de lado) e devolve o nome."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001 - qualquer arquivo que o Pillow não abre
        raise ValueError("O arquivo não parece ser uma imagem.") from e
    img = img.convert("RGBA")
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    name = name or f"{hashlib.sha1(out.getvalue()).hexdigest()[:12]}.png"
    folder(settings).mkdir(parents=True, exist_ok=True)
    (folder(settings) / name).write_bytes(out.getvalue())
    return name


def load(settings: Settings, name: str | None) -> Image.Image | None:
    p = path(settings, name)
    return Image.open(p).convert("RGBA") if p else None
