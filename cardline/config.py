"""Configuração do projeto: caminhos e preferências lidas de `cardline.toml`."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class Settings:
    root: Path
    currency: str = "USD"  # USD (preço TCGplayer) ou BRL (convertido pela cotação do dia)
    pack_size: int = 12  # cartas por booster; agrupa os totais e a detecção de foil
    analysis_fps: float = 10.0  # frames por segundo analisados no scan
    output_short_side: int = 1080  # lado menor do vídeo com overlay (1080 = 1080x1920 em retrato)
    outro_seconds: float = 4.0  # duração do resumo no fim do vídeo (frame congelado)
    intro_seconds: float = 3.0  # capa no começo do vídeo: booster e valor pago sobre o primeiro frame (0 = sem capa)
    card_sound_volume: float = 0.25  # "ka-ching" a cada carta, relativo à voz do narrador (0,25 ≈ 12 dB abaixo; 0 = sem)
    celebration_volume: float = 0.3  # aplausos quando a soma alcança o valor pago, relativo à voz (0 = sem)
    workers: int = max(1, min(8, (os.cpu_count() or 2) - 1))
    verify_model: str = ""  # modelo de visão do Ollama para conferir cada carta (ex.: "qwen3.5:4b"); vazio = desligado
    narration_voice: str = "Damien Black"  # voz do XTTS-v2 na narração (lista: cardline voz)
    narration_writer: str = "gemma3:4b"  # modelo do Ollama que escreve as piadas do roteiro; vazio = roteiro padrão
    public_url: str = ""  # endereço público do cardline (o Instagram baixa o vídeo dele); vazio = o do túnel ngrok

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def images_dir(self) -> Path:
        return self.cache_dir / "images"

    @property
    def index_dir(self) -> Path:
        return self.cache_dir / "index"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cardline.db"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def site_dir(self) -> Path:
        return self.root / "site"


def load_settings(root: Path | None = None) -> Settings:
    root = Path(root or os.environ.get("CARDLINE_HOME") or Path.cwd()).resolve()
    cfg: dict = {}
    path = root / "cardline.toml"
    if path.exists():
        cfg = tomllib.loads(path.read_text())
    known = {f.name for f in fields(Settings)} - {"root"}
    unknown = set(cfg) - known
    if unknown:
        raise SystemExit(f"cardline.toml: chaves desconhecidas: {', '.join(sorted(unknown))}")
    settings = Settings(root=root, **cfg)
    settings.currency = settings.currency.upper()
    if settings.currency not in ("USD", "BRL"):
        raise SystemExit("cardline.toml: currency deve ser USD ou BRL")
    return settings
