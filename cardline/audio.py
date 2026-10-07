"""Áudio do vídeo com overlay: o som original (depois da capa) e o "ka-ching" de caixa registradora a cada carta.

O som da caixa é um trecho (alavanca + campainha) de "Cash register.ogg", do Wikimedia Commons, em domínio
público. A narração refaz a mistura a partir das mesmas peças, para abaixar só o som original enquanto o
narrador fala: o "ka-ching" continua inteiro.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np

from .video import ffmpeg_exe

SR = 48000
CASH = Path(__file__).parent / "assets" / "sounds" / "caixa.wav"
CASH_HIT = 0.074  # o golpe da caixa, em segundos desde o começo do arquivo


def decode(path: Path, sr: int = SR) -> np.ndarray:
    """O áudio do arquivo em estéreo float32 (n, 2); vazio se ele não tiver som."""
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(path), "-vn", "-ac", "2", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2).copy()


def _clip() -> np.ndarray:
    with wave.open(str(CASH), "rb") as f:
        assert f.getframerate() == SR and f.getnchannels() == 1
        return np.frombuffer(f.readframes(f.getnframes()), "<i2").astype(np.float32) / 32767


def card_sounds(times: list[float], length: int, volume: float, sr: int = SR) -> np.ndarray:
    """Trilha mono com o "ka-ching" batendo em cada instante de `times` (segundos)."""
    track = np.zeros(length, np.float32)
    if volume <= 0:
        return track
    clip = _clip() * volume
    for t in times:
        a = int(round((t - CASH_HIT) * sr))
        lo, hi = max(0, a), min(length, a + len(clip))
        if lo < hi:
            track[lo:hi] += clip[lo - a:hi - a]
    return track


def soundtrack(source: Path | None, intro: float, duration: float, times: list[float], volume: float,
               original_gain: np.ndarray | None = None) -> np.ndarray:
    """Som do vídeo com overlay: o original começando depois da capa (com ganho opcional) + os "ka-chings"."""
    n = int(round(duration * SR))
    out = np.zeros((n, 2), np.float32)
    if source is not None:
        orig = decode(source)[: max(0, n - int(round(intro * SR)))]
        start = int(round(intro * SR))
        out[start:start + len(orig)] = orig
    if original_gain is not None:
        out *= original_gain[:n, None]
    out += card_sounds(times, n, volume)[:, None]
    return out


def write_wav(path: Path, stereo: np.ndarray, sr: int = SR) -> None:
    stereo = stereo * min(1.0, 0.98 / max(1e-6, float(np.abs(stereo).max())))  # sem estourar
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((stereo * 32767).astype("<i2").tobytes())
