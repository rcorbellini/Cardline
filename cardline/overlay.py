"""Vídeo com overlay: etiqueta de preço em cada carta revelada e o total do booster acumulando.

Elementos (desenhados com Pillow e mesclados no frame com numpy):
  * HUD no topo: booster atual, cartas reveladas, total animado e um slot por carta com a cor da raridade;
  * etiqueta da carta (raridade, foil, nome, preço) ancorada logo abaixo da carta, com pop-in;
  * "+US$ x" voando da etiqueta até o total;
  * resumo no fim (frame congelado): cartas ordenadas por valor e o total.
"""

from __future__ import annotations

import colorsys
import math
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import rarity
from .config import Settings
from .money import Money
from .scan import Progress
from .video import VideoWriter, probe, read_frames

FPS = 30
FONTS = Path(__file__).parent / "assets" / "fonts"
SS = 2  # supersampling dos sprites, para bordas suaves

PANEL = (12, 17, 38, 220)
GOLD = (242, 193, 78, 255)
WHITE = (255, 255, 255, 255)
MUTED = (178, 188, 214, 255)
DIM = (255, 255, 255, 46)

POP_IN = 0.35  # duração das animações, em segundos
FADE_OUT = 0.25
FLY = 0.75
COUNT_UP = 0.5
PULSE = 0.3


@lru_cache(maxsize=None)
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"Poppins-{weight}.ttf"), size)


def rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha


def price_color(usd: float | None) -> tuple[int, int, int, int]:
    if usd is None or usd < 1:
        return WHITE
    if usd < 5:
        return rgba("#5BE49B")
    if usd < 20:
        return rgba("#F5C542")
    return rgba("#FF7AB6")


def ease_out_back(x: float) -> float:
    c = 1.70158
    return 1 + (c + 1) * (x - 1) ** 3 + c * (x - 1) ** 2


def ease_in_out(x: float) -> float:
    return 3 * x * x - 2 * x * x * x


def clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


class Sprite:
    """Desenho em supersampling: coordenadas e tamanhos em pixels finais."""

    def __init__(self, w: float, h: float):
        self.w, self.h = int(round(w)), int(round(h))
        self.im = Image.new("RGBA", (self.w * SS, self.h * SS), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.im)

    def rrect(self, box, r, fill=None, outline=None, width=0):
        args = ([v * SS for v in box],)
        kw = {"radius": r * SS, "fill": fill, "outline": outline, "width": int(width * SS)}
        if any(c is not None and c[3] < 255 for c in (fill, outline)) and self.im.getbbox():
            # o ImageDraw substitui pixels em vez de compor: forma translúcida vai numa camada à parte
            layer = Image.new("RGBA", self.im.size, (0, 0, 0, 0))
            ImageDraw.Draw(layer).rounded_rectangle(*args, **kw)
            self.im.alpha_composite(layer)
        else:
            self.d.rounded_rectangle(*args, **kw)

    def text(self, xy, s, weight, size, fill, anchor="la"):
        self.d.text((xy[0] * SS, xy[1] * SS), s, font=font(weight, int(size * SS)), fill=fill, anchor=anchor)

    def textlen(self, s, weight, size) -> float:
        return self.d.textlength(s, font=font(weight, int(size * SS))) / SS

    def paste(self, img: Image.Image, xy):
        self.im.alpha_composite(img.resize((img.width * SS, img.height * SS), Image.LANCZOS), (int(xy[0] * SS), int(xy[1] * SS)))

    def done(self, shadow: float = 0) -> Image.Image:
        img = self.im.resize((self.w, self.h), Image.LANCZOS)
        return with_shadow(img, shadow) if shadow else img


def with_shadow(img: Image.Image, blur: float) -> Image.Image:
    """Sombra suave em volta do sprite (mantém o tamanho; o sprite precisa de margem própria)."""
    alpha = img.getchannel("A").filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * 0.55))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 255))
    shadow.putalpha(alpha)
    shadow.alpha_composite(img)
    return shadow


def rainbow(w: int, h: int, r: float) -> Image.Image:
    """Pílula com gradiente holográfico (selo FOIL / slot da foil)."""
    xs = np.linspace(0, 1, max(w, 1))
    row = np.array([colorsys.hsv_to_rgb((0.55 + 0.9 * x) % 1, 0.45, 1.0) for x in xs]) * 255
    grad = Image.fromarray(np.repeat(row[None].astype(np.uint8), max(h, 1), axis=0)).convert("RGBA")
    mask = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w * SS - 1, h * SS - 1], radius=r * SS, fill=255)
    grad.putalpha(mask.resize((w, h), Image.LANCZOS))
    return grad


def blend(dst: np.ndarray, sprite: np.ndarray, x: int, y: int, opacity: float = 1.0) -> None:
    """Mescla um sprite RGBA (uint8) no frame RGB, recortando nas bordas."""
    if opacity <= 0:
        return
    H, W = dst.shape[:2]
    h, w = sprite.shape[:2]
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
    if x0 >= x1 or y0 >= y1:
        return
    s = sprite[y0 - y : y1 - y, x0 - x : x1 - x].astype(np.float32)
    a = s[..., 3:4] * (opacity / 255.0)
    region = dst[y0:y1, x0:x1]
    region[:] = (s[..., :3] * a + region * (1.0 - a)).astype(np.uint8)


def blend_scaled(dst: np.ndarray, img: Image.Image, cx: float, cy: float, scale: float, opacity: float) -> None:
    """Mescla um sprite centrado em (cx, cy) com escala (para pop-in/pulso)."""
    if abs(scale - 1) > 1e-3:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.BILINEAR)
    blend(dst, np.asarray(img), round(cx - img.width / 2), round(cy - img.height / 2), opacity)


class Overlay:
    def __init__(self, scan: dict, money: Money, size: tuple[int, int], pack_size: int, paid_usd: float | None = None):
        self.cards = scan["cards"]
        self.money = money
        self.paid_usd = paid_usd
        self.W, self.H = size
        self.u = min(size) / 1080  # unidade: 1 px num vídeo 1080 de lado menor
        self.pack_size = pack_size
        self.margin = 36 * self.u
        self.tags = [self._tag(c) for c in self.cards]
        self.tag_pos = [self._anchor(c, tag) for c, tag in zip(self.cards, self.tags)]
        self.flyers = [self._flyer(c) for c in self.cards]
        self._hud_cache: dict = {}
        # instante em que o valor de cada carta "chega" ao total (fim do voo)
        self.arrive = [c["t"] + POP_IN + FLY for c in self.cards]

    # ---- sprites ---------------------------------------------------------------------------

    def _tag(self, c: dict) -> Image.Image:
        u = self.u
        pad = 26 * u
        name = c["name"]
        version = c.get("version") or ""
        price = self.money.fmt(c.get("price_usd"))
        m = Sprite(1, 1)  # só para medir texto
        badge_text = rarity.label(c["rarity"]).upper()
        badge_w = m.textlen(badge_text, "SemiBold", 21 * u) + 28 * u
        foil_w = m.textlen("FOIL", "Bold", 21 * u) + 36 * u if c.get("foil") else 0
        price_w = m.textlen(price, "ExtraBold", 58 * u)
        name_size = 44 * u
        max_w = self.W - 2 * self.margin
        while True:
            left_w = max(m.textlen(name, "Bold", name_size), m.textlen(version, "Medium", 27 * u),
                         badge_w + foil_w + 10 * u)
            w = pad + 14 * u + left_w + 40 * u + price_w + pad
            if w <= max_w or name_size <= 30 * u:
                break
            name_size -= 2 * u
        w = min(max(w, 560 * u), max_w)
        h = (190 if version else 158) * u
        shadow = 18 * u
        s = Sprite(w + 2 * shadow, h + 2 * shadow)
        ox, oy = shadow, shadow
        s.rrect([ox, oy, ox + w, oy + h], 28 * u, fill=PANEL)
        accent = rgba(rarity.color(c["rarity"]))
        s.rrect([ox, oy, ox + 12 * u, oy + h], 6 * u, fill=accent)
        x = ox + pad + 14 * u
        y = oy + 22 * u
        s.rrect([x, y, x + badge_w, y + 38 * u], 19 * u, fill=accent)
        s.text((x + badge_w / 2, y + 19 * u), badge_text, "SemiBold", 21 * u, (14, 18, 34, 255), anchor="mm")
        if foil_w:
            s.paste(rainbow(round(foil_w), round(38 * u), 19 * u), (x + badge_w + 10 * u, y))
            s.text((x + badge_w + 10 * u + foil_w / 2, y + 19 * u), "FOIL", "Bold", 21 * u, (20, 20, 40, 255), anchor="mm")
        s.text((x, y + 50 * u), name, "Bold", name_size, WHITE)
        if version:
            s.text((x, y + 50 * u + name_size * 1.2), version, "Medium", 27 * u, MUTED)
        s.text((ox + w - pad, oy + h / 2), price, "ExtraBold", 58 * u, price_color(c.get("price_usd")), anchor="rm")
        return s.done(shadow=10 * u)

    def _flyer(self, c: dict) -> Image.Image | None:
        if c.get("price_usd") is None:
            return None
        u = self.u
        text = self.money.fmt(c["price_usd"], sign=True)
        w = Sprite(1, 1).textlen(text, "ExtraBold", 48 * u) + 40 * u
        s = Sprite(w, 90 * u)
        s.text((w / 2, 45 * u), text, "ExtraBold", 48 * u, price_color(c["price_usd"]), anchor="mm")
        return s.done(shadow=6 * u)

    def _anchor(self, c: dict, tag: Image.Image) -> tuple[float, float]:
        """Centro da etiqueta: logo abaixo da carta; se não couber, acima; senão, no rodapé."""
        if not c.get("quad"):  # carta inserida à mão, sem posição no vídeo
            return self.W / 2, self.H - 60 * self.u - tag.height / 2
        q = np.array(c["quad"]) * [self.W, self.H]
        cx = float(np.clip(q[:, 0].mean(), tag.width / 2, self.W - tag.width / 2))
        gap = 8 * self.u
        below = q[:, 1].max() + gap + tag.height / 2
        if below + tag.height / 2 <= self.H - 40 * self.u:
            return cx, below
        above = q[:, 1].min() - gap - tag.height / 2
        if above - tag.height / 2 >= self.hud_bottom + 20 * self.u:
            return cx, above
        return cx, self.H - 60 * self.u - tag.height / 2

    @property
    def hud_bottom(self) -> float:
        return 56 * self.u + 190 * self.u

    def _hud(self, pack: int, revealed: int, total_text: str, slots: tuple, session_text: str | None) -> Image.Image:
        key = (pack, revealed, total_text, slots, session_text)
        if key in self._hud_cache:
            return self._hud_cache[key]
        u = self.u
        w, h = 660 * u, (176 if session_text else 150) * u
        shadow = 18 * u
        s = Sprite(w + 2 * shadow, h + 2 * shadow)
        ox, oy = shadow, shadow
        s.rrect([ox, oy, ox + w, oy + h], 32 * u, fill=PANEL, outline=(242, 193, 78, 120), width=2 * u)
        label = f"BOOSTER {pack}" if self.n_packs > 1 else "BOOSTER"
        s.text((ox + 30 * u, oy + 26 * u), label, "SemiBold", 25 * u, GOLD)
        s.text((ox + 30 * u, oy + 60 * u), f"{revealed}/{self.pack_size} cartas", "Medium", 25 * u, MUTED)
        s.text((ox + w - 30 * u, oy + 58 * u), total_text, "ExtraBold", 62 * u, WHITE, anchor="rm")
        n = self.pack_size
        gap = 8 * u
        slot_w = (w - 60 * u - gap * (n - 1)) / n
        y = oy + 112 * u
        for i in range(n):
            x = ox + 30 * u + i * (slot_w + gap)
            state = slots[i] if i < len(slots) else None
            if state is None:
                s.rrect([x, y, x + slot_w, y + 14 * u], 7 * u, fill=DIM)
            elif state == "foil":
                s.paste(rainbow(round(slot_w), round(14 * u), 7 * u), (x, y))
            else:
                s.rrect([x, y, x + slot_w, y + 14 * u], 7 * u, fill=rgba(state))
        if session_text:
            s.text((ox + w / 2, oy + h - 22 * u), session_text, "Medium", 21 * u, MUTED, anchor="mm")
        img = s.done(shadow=12 * u)
        self._hud_cache[key] = img
        return img

    @property
    def n_packs(self) -> int:
        return max((c["pack"] for c in self.cards), default=1)

    # ---- composição por frame --------------------------------------------------------------

    def _total_at(self, t: float, cards: list[int]) -> float:
        total = 0.0
        for k in cards:
            p = self.cards[k].get("price_usd") or 0.0
            total += p * clamp01((t - self.arrive[k]) / COUNT_UP)
        return total

    def draw(self, frame: np.ndarray, t: float) -> None:
        u = self.u
        current = max((k for k, c in enumerate(self.cards) if c["t"] <= t), default=None)
        pack = self.cards[current]["pack"] if current is not None else 1
        in_pack = [k for k, c in enumerate(self.cards) if c["pack"] == pack]
        shown = [k for k in in_pack if self.cards[k]["t"] <= t]
        slots = tuple(
            ("foil" if self.cards[k].get("foil") else rarity.color(self.cards[k]["rarity"])) if k in shown else None
            for k in in_pack
        )
        session = None
        if self.n_packs > 1:
            all_shown = [k for k, c in enumerate(self.cards) if c["t"] <= t]
            session = f"Total da sessão: {self.money.fmt(self._total_at(t, all_shown))}"
        hud = self._hud(pack, len(shown), self.money.fmt(self._total_at(t, in_pack)), slots, session)
        pulse = 1.0
        for k in in_pack:
            x = (t - self.arrive[k]) / PULSE
            if 0 <= x <= 1 and self.cards[k].get("price_usd"):
                pulse = 1 + 0.06 * math.sin(math.pi * x)
        hud_cx, hud_cy = self.W / 2, 56 * u + hud.height / 2 - 18 * u
        blend_scaled(frame, hud, hud_cx, hud_cy, pulse, 1.0)

        for k, c in enumerate(self.cards):
            start = c["t"]
            end = self.cards[k + 1]["t"] if k + 1 < len(self.cards) else math.inf
            if not start <= t < end + FADE_OUT:
                continue
            x = clamp01((t - start) / POP_IN)
            scale = 0.82 + 0.18 * ease_out_back(x)
            opacity = clamp01(x * 1.6) * (1 - clamp01((t - end) / FADE_OUT))
            cx, cy = self.tag_pos[k]
            blend_scaled(frame, self.tags[k], cx, cy, scale, opacity)

            flyer = self.flyers[k]
            fx = (t - start - POP_IN) / FLY
            if flyer is not None and 0 <= fx <= 1:
                e = ease_in_out(fx)
                sx, sy = cx + self.tags[k].width * 0.28, cy
                tx, ty = hud_cx + 170 * u, hud_cy - 10 * u
                px, py = sx + (tx - sx) * e, sy + (ty - sy) * e - math.sin(math.pi * e) * 120 * u
                blend_scaled(frame, flyer, px, py, 1.1 - 0.35 * e, 1 - 0.6 * e ** 3)

    # ---- resumo final ----------------------------------------------------------------------

    def summary(self) -> Image.Image:
        u = self.u
        rows = sorted(self.cards, key=lambda c: -(c.get("price_usd") or 0))
        extra = max(0, len(rows) - 12)
        rows = rows[:12]
        w = min(940 * u, self.W - 2 * self.margin)
        row_h = 62 * u
        head = (210 * u if self.n_packs > 1 else 180 * u) + (48 * u if self.paid_usd else 0)
        h = head + len(rows) * row_h + (50 * u if extra else 0) + 40 * u
        shadow = 24 * u
        s = Sprite(w + 2 * shadow, h + 2 * shadow)
        ox, oy = shadow, shadow
        s.rrect([ox, oy, ox + w, oy + h], 36 * u, fill=(10, 14, 32, 242), outline=(242, 193, 78, 150), width=2 * u)
        title = "RESUMO DO BOOSTER" if self.n_packs == 1 else f"RESUMO · {self.n_packs} BOOSTERS"
        s.text((ox + w / 2, oy + 46 * u), title, "SemiBold", 28 * u, GOLD, anchor="mm")
        total = sum(c.get("price_usd") or 0 for c in self.cards)
        s.text((ox + w / 2, oy + 112 * u), self.money.fmt(total), "ExtraBold", 80 * u, WHITE, anchor="mm")
        if self.n_packs > 1:
            per_pack = " · ".join(
                f"#{p}: {self.money.fmt(sum(c.get('price_usd') or 0 for c in self.cards if c['pack'] == p))}"
                for p in range(1, self.n_packs + 1)
            )
            s.text((ox + w / 2, oy + 168 * u), per_pack, "Medium", 22 * u, MUTED, anchor="mm")
        if self.paid_usd:
            diff = total - self.paid_usd
            pct = f" ({diff / self.paid_usd * 100:+.0f}%)".replace("-", "−")
            line = f"pago {self.money.fmt(self.paid_usd)}  ·  resultado {self.money.fmt(diff, sign=True)}{pct}"
            color = rgba("#5BE49B") if diff >= 0 else rgba("#FF7A7A")
            s.text((ox + w / 2, oy + head - 34 * u), line, "SemiBold", 25 * u, color, anchor="mm")
        y = oy + head
        for i, c in enumerate(rows):
            if i == 0:
                s.rrect([ox + 18 * u, y + 3 * u, ox + w - 18 * u, y + row_h - 3 * u], 18 * u,
                        fill=(242, 193, 78, 38), outline=(242, 193, 78, 200), width=2 * u)
            mid = y + row_h / 2
            s.d.ellipse([(ox + 40 * u) * SS, (mid - 9 * u) * SS, (ox + 58 * u) * SS, (mid + 9 * u) * SS],
                        fill=rgba(rarity.color(c["rarity"])))
            price = self.money.fmt(c.get("price_usd"))
            price_w = s.textlen(price, "Bold", 32 * u)
            name = c["name"] + (f" - {c['version']}" if c.get("version") else "")
            room = w - 80 * u - price_w - 40 * u - (96 * u if c.get("foil") else 0)
            while s.textlen(name, "SemiBold", 29 * u) > room and len(name) > 4:
                name = name[:-2].rstrip() + "…"
            s.text((ox + 76 * u, mid), name, "SemiBold", 29 * u, WHITE, anchor="lm")
            if c.get("foil"):
                fx = ox + 76 * u + s.textlen(name, "SemiBold", 29 * u) + 14 * u
                s.paste(rainbow(round(78 * u), round(30 * u), 15 * u), (fx, mid - 15 * u))
                s.text((fx + 39 * u, mid), "FOIL", "Bold", 17 * u, (20, 20, 40, 255), anchor="mm")
            s.text((ox + w - 40 * u, mid), price, "Bold", 32 * u, price_color(c.get("price_usd")), anchor="rm")
            y += row_h
        if extra:
            s.text((ox + w / 2, y + 25 * u), f"+ {extra} cartas", "Medium", 24 * u, MUTED, anchor="mm")
        return s.done(shadow=16 * u)


def render(
    settings: Settings, scan: dict, out: Path, money: Money, progress: Progress, paid_usd: float | None = None
) -> Path:
    if not scan["cards"]:
        raise RuntimeError("Nenhuma carta identificada; nada para sobrepor.")
    video = settings.root / scan["video"]
    info = probe(video)
    size = info.scaled(settings.output_short_side)
    ov = Overlay(scan, money, size, settings.pack_size, paid_usd)
    tmp = out.with_suffix(".part.mp4")  # o vídeo anterior continua válido até o novo ficar pronto
    writer = VideoWriter(tmp, size, FPS, audio_from=video if info.has_audio else None)
    total_frames = int(info.duration * FPS)
    for i, frame in enumerate(read_frames(info, settings.output_short_side, fps=FPS)):
        canvas = frame.copy()
        ov.draw(canvas, i / FPS)
        writer.write(canvas)
        progress(0.95 * i / total_frames, f"Renderizando vídeo ({i}/{total_frames} frames)")
    # segura o último frame até a soma da última carta terminar de animar
    t = (i + 1) / FPS
    while t < max(ov.arrive) + COUNT_UP + 0.4:
        canvas = frame.copy()
        ov.draw(canvas, t)
        writer.write(canvas)
        t += 1 / FPS
    # resumo: crossfade do último frame (com overlay) para ele mesmo desfocado e escurecido,
    # e o painel entrando por cima
    last = canvas.astype(np.float32)
    bg = Image.fromarray(frame).filter(ImageFilter.GaussianBlur(14 * ov.u))
    bg = np.asarray(bg).astype(np.float32) * 0.45
    panel = ov.summary()
    for j in range(int(settings.outro_seconds * FPS)):
        x = clamp01(j / (0.45 * FPS))
        out_frame = (last * (1 - x) + bg * x).astype(np.uint8)
        e = ease_out_back(x)
        blend_scaled(out_frame, panel, ov.W / 2, ov.H / 2 + (1 - e) * 60 * ov.u, 0.94 + 0.06 * e, clamp01(x * 1.4))
        writer.write(out_frame)
    writer.close()
    tmp.replace(out)
    return out
