import subprocess

import numpy as np
import pytest

from cardline import audio, narration
from cardline.video import ffmpeg_exe

SR = audio.SR


def band(signal: np.ndarray, start: float, end: float, low: float, high: float) -> float:
    """Energia do trecho [start, end) entre as frequências low e high."""
    seg = signal[int(start * SR):int(end * SR)]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    freqs = np.fft.rfftfreq(len(seg), 1 / SR)
    return float(spec[(freqs >= low) & (freqs < high)].sum())


def sine_video(path, seconds=3):
    subprocess.run([ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=64x64:rate=10:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)


def test_cash_sound_hits_exactly_when_each_card_appears():
    track = audio.card_sounds([1.0, 2.0], 3 * SR, volume=0.5)
    for t in (1.0, 2.0):
        window = np.abs(track[int((t - 0.1) * SR):int((t + 0.1) * SR)])
        assert abs((t - 0.1) + int(np.argmax(window > 0.5 * window.max())) / SR - t) < 0.01  # o golpe cai no instante
    assert np.abs(track).max() == pytest.approx(np.abs(audio._clip(audio.CASH)).max() * 0.5, rel=1e-3)  # × volume
    assert not audio.card_sounds([1.0], SR * 2, volume=0).any()


def test_original_sound_starts_after_the_cover(tmp_path):
    video = tmp_path / "v.mp4"
    sine_video(video, seconds=1)
    track = audio.soundtrack(video, intro=0.5, duration=2.0)
    assert track.shape == (2 * SR, 2)
    assert np.abs(track[: int(0.45 * SR)]).max() < 1e-4  # capa: silêncio
    assert np.sqrt(np.mean(track[int(0.6 * SR):int(1.4 * SR)] ** 2)) > 0.05


def test_effects_are_background_level_and_applause_starts_at_the_celebration():
    assert audio.max_loudness(audio.CASH) == pytest.approx(-20, abs=0.5)  # o nível da fala do narrador
    assert audio.max_loudness(audio.APPLAUSE) == pytest.approx(-20, abs=0.5)
    track = audio.effects({"sounds": [], "celebration": 1.0, "celebration_volume": 0.3}, 4 * SR)
    assert not track[: SR - 10].any() and np.abs(track[SR:SR + 2 * SR]).max() > 0
    assert not audio.effects({"sounds": [1.0], "sound_volume": 0, "celebration": None}, 2 * SR).any()


def test_narration_lowers_the_original_but_not_the_cash_sound(tmp_path):
    pytest.importorskip("torchaudio")
    video = tmp_path / "overlay.mp4"
    sine_video(video)
    silent = tmp_path / "fala.wav"
    narration.write_wav(silent, np.zeros(SR, np.float32), SR)  # fala muda: só o efeito da mistura aparece
    meta = {"intro": 0.0, "sounds": [1.2], "sound_volume": 0.5, "celebration": None}
    ratios = []
    for segments in ([(5.0, 5.1)], [(1.0, 2.0)]):  # narrador longe da carta / falando junto com o "ka-ching"
        out = tmp_path / f"narrado-{segments[0][0]}.mp4"
        narration.mix(video, out, [(silent, 1.0)], segments, 3.0, source=video, meta=meta)
        mixed = audio.decode(out)[:, 0]
        ratios.append(band(mixed, 1.2, 1.6, 3000, 8000) / band(mixed, 1.2, 1.6, 300, 600))
    # com a fala, o seno (som original) cai ~14 dB e a campainha só ~4 dB: a proporção sobe ~10 dB
    assert 6 * ratios[0] < ratios[1] < 16 * ratios[0]
