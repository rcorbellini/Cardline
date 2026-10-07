"""Leitura e escrita de vídeo via ffmpeg (por padrão o binário estático do imageio-ffmpeg).

Vídeos HDR (HLG/PQ, como os do Pixel) são convertidos para SDR BT.709 com tone mapping,
tanto para a análise quanto para o vídeo final.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}


def ffmpeg_exe() -> str:
    if exe := os.environ.get("CARDLINE_FFMPEG"):
        return exe
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


@dataclass
class VideoInfo:
    path: Path
    width: int  # já com a rotação aplicada (como o vídeo é exibido)
    height: int
    duration: float
    fps: float
    transfer: str | None
    creation_time: str | None
    has_audio: bool

    @property
    def hdr(self) -> bool:
        return self.transfer in HDR_TRANSFERS

    def scaled(self, short_side: int) -> tuple[int, int]:
        k = short_side / min(self.width, self.height)
        return round(self.width * k / 2) * 2, round(self.height * k / 2) * 2


def probe(path: Path) -> VideoInfo:
    err = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    stream = re.search(r"Stream #\d+:\d+.*?: Video: (.*)", err)
    if not stream:
        raise SystemExit(f"{path}: nenhuma trilha de vídeo encontrada.")
    line = stream.group(1)
    w, h = map(int, re.search(r", (\d{2,5})x(\d{2,5})", line).groups())
    rotation = re.search(r"rotation of (-?[\d.]+) degrees", err)
    if rotation and round(abs(float(rotation.group(1)))) % 180 == 90:
        w, h = h, w
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    color = re.search(r"\((?:tv|pc)(?:, ([\w-]+)(?:/[\w-]+/([\w-]+))?)?", line)
    transfer = (color.group(2) or color.group(1)) if color else None
    fps = re.search(r"([\d.]+) fps", line)
    created = re.search(r"creation_time\s*:\s*(\S+)", err)
    return VideoInfo(
        path=Path(path),
        width=w,
        height=h,
        duration=int(dur[1]) * 3600 + int(dur[2]) * 60 + float(dur[3]) if dur else 0.0,
        fps=float(fps.group(1)) if fps else 30.0,
        transfer=transfer,
        creation_time=created.group(1) if created else None,
        has_audio=bool(re.search(r"Stream #\d+:\d+.*?: Audio:", err)),
    )


def _filter(info: VideoInfo, size: tuple[int, int], fps: float | None) -> str:
    w, h = size
    chain = [f"fps={fps}"] if fps else []
    if info.hdr:
        chain.append(
            f"zscale=w={w}:h={h}:t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
        )
    else:
        chain.append(f"scale={w}:{h}:flags=area")
    chain.append("format=rgb24")
    return ",".join(chain)


def read_frames(
    info: VideoInfo, short_side: int, fps: float | None = None, start: float | None = None, duration: float | None = None
) -> Iterator[np.ndarray]:
    """Frames RGB (uint8, HxWx3) já em SDR; com `fps`, o frame i está em start + i/fps segundos."""
    size = info.scaled(short_side)
    cmd = [ffmpeg_exe(), "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(info.path)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vf", _filter(info, size, fps), "-f", "rawvideo", "pipe:1"]
    w, h = size
    nbytes = w * h * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    try:
        while len(buf := proc.stdout.read(nbytes)) == nbytes:
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def read_frame_at(info: VideoInfo, t: float, short_side: int) -> np.ndarray:
    return next(read_frames(info, short_side, start=max(0.0, t), duration=1.0))


class VideoWriter:
    """Recebe frames RGB e codifica H.264 (SDR BT.709), copiando o áudio do vídeo original."""

    def __init__(self, out: Path, size: tuple[int, int], fps: float, audio_from: Path | None = None, crf: int = 18,
                 audio_delay: float = 0.0):
        w, h = size
        cmd = [ffmpeg_exe(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
               "-r", str(fps), "-i", "pipe:0"]
        if audio_from:
            # apad + shortest: o áudio original é estendido com silêncio durante o resumo final;
            # adelay: começa depois da capa (o vídeo original só entra quando ela sai)
            delay = f"adelay={round(audio_delay * 1000)}:all=1," if audio_delay > 0 else ""
            cmd += ["-i", str(audio_from), "-map", "0:v:0", "-map", "1:a:0?", "-af", f"{delay}apad", "-shortest",
                    "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
                "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-movflags", "+faststart", str(out)]
        out.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def write(self, frame: np.ndarray) -> None:
        self.proc.stdin.write(np.ascontiguousarray(frame).tobytes())

    def close(self) -> None:
        self.proc.stdin.close()
        if self.proc.wait() != 0:
            raise SystemExit("ffmpeg falhou ao gerar o vídeo.")
