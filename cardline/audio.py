"""Áudio do vídeo com overlay: o som original (depois da capa) e os efeitos de fundo.

Efeitos: o "ka-ching" de caixa registradora a cada carta e os aplausos quando a soma das cartas alcança o valor
pago. Os dois são de fundo: os arquivos estão no mesmo nível da fala do narrador (-20 LUFS de intensidade
momentânea máxima) e o volume de cada um é relativo a ela (0,25 ≈ 12 dB abaixo da voz).

- caixa.wav: trecho (alavanca + campainha) de "Cash register.ogg", Wikimedia Commons, domínio público;
- aplausos.wav: trecho de "277021 sandermotions applause-2.wav", Wikimedia Commons, CC0.

A narração refaz a mistura a partir das mesmas peças: abaixa bem o som original e só um pouco os efeitos.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np

from .video import ffmpeg_exe

SR = 48000
SOUNDS = Path(__file__).parent / "assets" / "sounds"
CASH, CASH_HIT = SOUNDS / "caixa.wav", 0.074  # o golpe da caixa, em segundos desde o começo do arquivo
APPLAUSE = SOUNDS / "aplausos.wav"  # começa baixo e cresce: entra no instante da comemoração


def decode(path: Path, sr: int = SR) -> np.ndarray:
    """O áudio do arquivo em estéreo float32 (n, 2); vazio se ele não tiver som."""
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(path), "-vn", "-ac", "2", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2).copy()


def _clip(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as f:
        assert f.getframerate() == SR and f.getnchannels() == 1
        return np.frombuffer(f.readframes(f.getnframes()), "<i2").astype(np.float32) / 32767


def place(path: Path, times: list[float], length: int, volume: float, offset: float = 0.0, sr: int = SR) -> np.ndarray:
    """Trilha mono com o efeito tocando em cada instante de `times` (o ponto `offset` do arquivo cai no instante)."""
    track = np.zeros(length, np.float32)
    if volume <= 0 or not times:
        return track
    clip = _clip(path) * volume
    for t in times:
        a = int(round((t - offset) * sr))
        lo, hi = max(0, a), min(length, a + len(clip))
        if lo < hi:
            track[lo:hi] += clip[lo - a:hi - a]
    return track


def card_sounds(times: list[float], length: int, volume: float, sr: int = SR) -> np.ndarray:
    """O "ka-ching" batendo em cada instante de `times` (segundos)."""
    return place(CASH, times, length, volume, CASH_HIT, sr)


def effects(meta: dict, length: int) -> np.ndarray:
    """Os efeitos de fundo anotados no overlay.json: os "ka-chings" e os aplausos da comemoração."""
    track = card_sounds(meta.get("sounds", []), length, meta.get("sound_volume", 0))
    if meta.get("celebration") is not None:
        track += place(APPLAUSE, [meta["celebration"]], length, meta.get("celebration_volume", 0))
    return track


def soundtrack(source: Path | None, intro: float, duration: float, meta: dict | None = None,
               original_gain: np.ndarray | None = None, effects_gain: np.ndarray | None = None) -> np.ndarray:
    """Som do vídeo com overlay: o original começando depois da capa + os efeitos (cada parte com ganho opcional)."""
    n = int(round(duration * SR))
    out = np.zeros((n, 2), np.float32)
    if source is not None:
        orig = decode(source)[: max(0, n - int(round(intro * SR)))]
        start = int(round(intro * SR))
        out[start:start + len(orig)] = orig
    if original_gain is not None:
        out *= original_gain[:n, None]
    fx = effects(meta or {}, n)
    if effects_gain is not None:
        fx *= effects_gain[:n]
    out += fx[:, None]
    return out


def max_loudness(path: Path) -> float:
    """Intensidade momentânea máxima (LUFS, EBU R128) do arquivo, medida pelo ffmpeg."""
    import re

    err = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=framelog=info",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    return max(float(v) for v in re.findall(r"M:\s*(-?[\d.]+)", err))


def write_wav(path: Path, stereo: np.ndarray, sr: int = SR) -> None:
    stereo = stereo * min(1.0, 0.98 / max(1e-6, float(np.abs(stereo).max())))  # sem estourar
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((stereo * 32767).astype("<i2").tobytes())
