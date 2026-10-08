"""Vídeo com overlay: etiqueta de preço em cada carta revelada e o total do booster acumulando.

Elementos (desenhados com Pillow e mesclados no frame com numpy):
  * HUD no topo: booster atual, cartas reveladas, total animado e um slot por carta com a cor da raridade;
  * etiqueta da carta (raridade, foil, nome, preço) ancorada logo abaixo da carta, com pop-in;
  * "+US$ x" voando da etiqueta até o total;
  * capa no começo (primeiro frame congelado): o booster e o valor pago entram, e o booster voa para o painel;
  * resumo no fim (frame congelado): cartas ordenadas por valor e o total.
"""

from __future__ import annotations

import colorsys
import math
import itertools
import json
import subprocess
from collections import Counter
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import audio, db, rarity
from .config import Settings
from .logo import CORNERS
from .money import Money
from .scan import Progress
from .video import VideoWriter, ffmpeg_exe, probe, read_frames

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
INTRO_IN = 0.6  # entrada da capa
INTRO_OUT = 0.6  # saída da capa: o booster voa até o lugar do ícone no painel
HUD_IN = 0.35  # sem capa, o painel entra deslizando
LOGO_SIZE = 110  # lado maior do logo, em px num vídeo de 1080 no lado menor: cabe ao lado do painel do topo
LOGO_MARGIN = 20


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


def ease_out(x: float) -> float:
    return 1 - (1 - x) ** 3


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


def blend_posed(dst: np.ndarray, img: Image.Image, cx: float, cy: float, scale: float, angle: float, opacity: float) -> None:
    """Mescla o sprite centrado em (cx, cy), com escala e rotação (graus, anti-horário)."""
    if opacity <= 0 or scale <= 0:
        return
    if abs(scale - 1) > 1e-3:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    if abs(angle) > 0.05:
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True)
    blend(dst, np.asarray(img), round(cx - img.width / 2), round(cy - img.height / 2), opacity)


@lru_cache(maxsize=8)
def _diagonal(w: int, h: int) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    return (xx / w + 0.6 * yy / h) / 1.6


def shine(img: Image.Image, p: float, width: float = 0.09) -> Image.Image:
    """Faixa de brilho diagonal atravessando o booster (p de 0 a 1), como o reflexo do plástico."""
    a = np.asarray(img).astype(np.float32)
    band = np.exp(-(((_diagonal(img.width, img.height) - (p * 1.3 - 0.15)) / width) ** 2)) * 0.6
    a[..., :3] += (255 - a[..., :3]) * band[..., None]
    return Image.fromarray(a.clip(0, 255).astype(np.uint8), "RGBA")


class Overlay:
    def __init__(self, scan: dict, money: Money, size: tuple[int, int], pack_size: int, paid_usd: float | None = None,
                 icons: dict[str, Image.Image] | None = None, names: dict[str, str] | None = None,
                 paid: tuple[float, str] | None = None, intro: float = 0.0, logo: Image.Image | None = None,
                 logo_corner: str = "top-right", logo_opacity: float = 0.6):
        self.cards = scan["cards"]
        self.money = money
        self.paid_usd = paid_usd
        self.paid = paid  # (valor, moeda) como foi informado, para a capa
        self.icons = icons or {}  # ícone (foto do booster) de cada set
        self.names = names or {}  # nome de cada set
        self.intro = intro  # segundos de capa antes do vídeo (0 = sem capa)
        k = min(1.0, intro / (INTRO_IN + INTRO_OUT + 0.4)) if intro else 1.0
        self.intro_in, self.intro_out = INTRO_IN * k, INTRO_OUT * k  # capa curta: animações proporcionais
        self._icon_cache: dict = {}
        self._cover: dict | None = None
        # o set de cada booster é o mais comum entre as cartas dele
        self.pack_set = {p: Counter(c["set"] for c in self.cards if c["pack"] == p).most_common(1)[0][0]
                         for p in {c["pack"] for c in self.cards}}
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
        self.logo = None
        if logo is not None:  # semitransparente num canto, em todos os frames
            img = logo.copy()
            img.thumbnail((round(LOGO_SIZE * self.u),) * 2, Image.LANCZOS)
            m = round(LOGO_MARGIN * self.u)
            self.logo = np.asarray(img)
            self.logo_xy = (m if "left" in logo_corner else self.W - m - img.width,
                            m if "top" in logo_corner else self.H - m - img.height)
            self.logo_opacity = max(0.0, min(1.0, logo_opacity))

    def stamp(self, frame: np.ndarray) -> None:
        """O logo no canto (capa, vídeo e resumo)."""
        if self.logo is not None:
            blend(frame, self.logo, *self.logo_xy, self.logo_opacity)

    # ---- sprites ---------------------------------------------------------------------------

    def _tag(self, c: dict) -> Image.Image:
        u = self.u
        pad = 26 * u
        name = c["name"]
        version = c.get("version") or ""
        price = self.money.fmt(c.get("price_usd"))
        m = Sprite(1, 1)  # só para medir texto
        badge_text, accent = self._badge(c)
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

    def _badge(self, c: dict) -> tuple[str, tuple]:
        """Selo da etiqueta: a raridade da carta, na cor dela."""
        return rarity.label(c["rarity"]).upper(), rgba(rarity.color(c["rarity"]))

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
        h = (176 if session_text else 150) * u
        code = self.pack_set.get(pack)
        icon = self._set_icon(code, h - 26 * u) if code else None
        lead = icon.width + 14 * u if icon else 0  # o ícone do set fica à esquerda e o painel alarga
        w = 660 * u + lead
        shadow = 18 * u
        s = Sprite(w + 2 * shadow, h + 2 * shadow)
        ox, oy = shadow, shadow
        s.rrect([ox, oy, ox + w, oy + h], 32 * u, fill=PANEL, outline=(242, 193, 78, 120), width=2 * u)
        if icon:
            s.paste(icon, (ox + 20 * u, oy + (h - icon.height) / 2))
        x0 = ox + 30 * u + lead
        label = f"BOOSTER {pack}" if self.n_packs > 1 else "BOOSTER"
        s.text((x0, oy + 26 * u), label, "SemiBold", 25 * u, GOLD)
        s.text((x0, oy + 60 * u), f"{revealed}/{self.pack_size} cartas", "Medium", 25 * u, MUTED)
        s.text((ox + w - 30 * u, oy + 58 * u), total_text, "ExtraBold", 62 * u, WHITE, anchor="rm")
        n = self.pack_size
        gap = 8 * u
        slot_w = (w - 60 * u - lead - gap * (n - 1)) / n
        y = oy + 112 * u
        for i in range(n):
            x = x0 + i * (slot_w + gap)
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

    def _set_icon(self, code: str, height: float) -> Image.Image:
        """Foto do booster do set no tamanho do painel; sem foto, um selo hexagonal com o número do set."""
        key = (code, round(height))
        if key not in self._icon_cache:
            src = self.icons.get(code)
            if src is not None:
                img = src.copy()
                img.thumbnail((round(height * 0.8), round(height)), Image.LANCZOS)
            else:
                w = height * 0.86
                s = Sprite(w, height)
                hexagon = [(w / 2, 0), (w, height * 0.25), (w, height * 0.75), (w / 2, height), (0, height * 0.75), (0, height * 0.25)]
                s.d.polygon([(x * SS, y * SS) for x, y in hexagon], fill=GOLD)
                size = height * 0.38
                size *= min(1.0, w * 0.74 / s.textlen(code, "ExtraBold", size))  # códigos longos encolhem para caber
                s.text((w / 2, height / 2), code, "ExtraBold", size, (14, 18, 34, 255), anchor="mm")
                img = s.done()
            self._icon_cache[key] = img
        return self._icon_cache[key]

    @property
    def n_packs(self) -> int:
        return max((c["pack"] for c in self.cards), default=1)

    # ---- composição por frame --------------------------------------------------------------

    def celebration_time(self) -> float | None:
        """Instante (no vídeo, antes da capa) em que a soma de todas as cartas alcança o valor pago, ou None."""
        if not self.paid_usd:
            return None
        before = 0.0
        for k in sorted(range(len(self.cards)), key=lambda k: self.arrive[k]):
            price = self.cards[k].get("price_usd") or 0.0
            if price and before + price >= self.paid_usd:  # no meio da contagem animada do total
                return self.arrive[k] + COUNT_UP * min(1.0, (self.paid_usd - before) / price)
            before += price
        return None

    def _total_at(self, t: float, cards: list[int]) -> float:
        total = 0.0
        for k in cards:
            p = self.cards[k].get("price_usd") or 0.0
            total += p * clamp01((t - self.arrive[k]) / COUNT_UP)
        return total

    def _hud_at(self, t: float) -> tuple[Image.Image, float, float, float, list[int]]:
        """O painel no instante t do vídeo: sprite, centro, escala do pulso e as cartas do booster atual."""
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
        return hud, self.W / 2, 56 * self.u + hud.height / 2 - 18 * self.u, pulse, in_pack

    def draw(self, frame: np.ndarray, t: float) -> None:
        u = self.u
        hud, hud_cx, hud_cy, pulse, _ = self._hud_at(t)
        if not self.intro and t < HUD_IN:  # sem capa, o painel desce do topo; com capa, ele já está no lugar
            e = ease_out(clamp01(t / HUD_IN))
            blend_scaled(frame, hud, hud_cx, hud_cy - (1 - e) * 40 * u, pulse, e)
        else:
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
                tx, ty = hud_cx + hud.width / 2 - 178 * u, hud_cy - 10 * u  # centro do total no painel
                px, py = sx + (tx - sx) * e, sy + (ty - sy) * e - math.sin(math.pi * e) * 120 * u
                blend_scaled(frame, flyer, px, py, 1.1 - 0.35 * e, 1 - 0.6 * e ** 3)

    # ---- capa ------------------------------------------------------------------------------

    def _cover_sprites(self) -> dict:
        """Booster(s) em tamanho de capa, painel com o set e o valor pago, e o brilho atrás."""
        if self._cover is not None:
            return self._cover
        u, W, H = self.u, self.W, self.H
        packs = sorted(self.pack_set)
        codes = [self.pack_set[p] for p in packs]
        title = "BOOSTER" if len(packs) == 1 else f"{len(packs)} BOOSTERS"
        names = " + ".join(dict.fromkeys(self.names.get(c, f"Set {c}") for c in codes))
        lines = [(title, "SemiBold", 30 * u, GOLD), (names, "SemiBold", 42 * u, WHITE)]
        if self.paid:
            value, currency = self.paid
            lines.append((Money(currency).fmt(value), "ExtraBold", 108 * u, WHITE))
        m = Sprite(1, 1)
        max_w = W * 0.88 - 96 * u
        fitted = []
        for text, weight, size, color in lines:  # nome de set longo encolhe para caber
            while m.textlen(text, weight, size) > max_w and size > 22 * u:
                size *= 0.94
            fitted.append((text, weight, size, color))
        gaps = [16 * u, 22 * u]
        pad = 48 * u
        w = min(W * 0.88, max(560 * u, max(m.textlen(t, wt, sz) for t, wt, sz, _ in fitted) + 2 * pad))
        h = 2 * pad + sum(sz for _, _, sz, _ in fitted) + sum(gaps[: len(fitted) - 1])
        shadow = 24 * u
        s = Sprite(w + 2 * shadow, h + 2 * shadow)
        s.rrect([shadow, shadow, shadow + w, shadow + h], 36 * u, fill=(10, 14, 32, 236), outline=(242, 193, 78, 150),
                width=2 * u)
        y = shadow + pad
        for i, (text, weight, size, color) in enumerate(fitted):
            s.text((shadow + w / 2, y + size / 2), text, weight, size, color, anchor="mm")
            y += size + (gaps[i] if i < len(gaps) else 0)
        panel = s.done(shadow=16 * u)

        gap = 36 * u
        ph = min(0.44 * H, 0.9 * H - gap - panel.height, 900 * u) * (0.86 if len(packs) > 1 else 1)
        images = []
        for code in codes[:3]:
            src = self.icons.get(code)
            if src is not None:
                img = src.copy()
                img.thumbnail((round(ph), round(ph)), Image.LANCZOS)
            else:
                img = self._set_icon(code, ph)  # sem foto: o selo com o código do set
            images.append(img)
        main = images[0]
        top = (H - (main.height + gap + panel.height)) / 2
        cy = top + main.height / 2
        glow = Image.new("RGBA", (round(main.width * 2.2), round(main.height * 1.6)), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.ellipse([glow.width * 0.2, glow.height * 0.15, glow.width * 0.8, glow.height * 0.85], fill=(242, 193, 78, 120))
        glow = glow.filter(ImageFilter.GaussianBlur(70 * u))
        self._cover = {"images": images, "panel": panel, "glow": glow, "cx": W / 2, "cy": cy,
                       "panel_cy": top + main.height + gap + panel.height / 2}
        return self._cover

    def _hud_icon(self) -> tuple[float, float, float]:
        """Centro e altura do ícone do set no painel do instante 0: é onde o booster da capa pousa."""
        u = self.u
        hud, cx, cy, _, _ = self._hud_at(0.0)
        h = (176 if self.n_packs > 1 else 150) * u
        icon = self._set_icon(self.pack_set[1] if 1 in self.pack_set else next(iter(self.pack_set.values())), h - 26 * u)
        shadow = 18 * u
        return cx - hud.width / 2 + shadow + 20 * u + icon.width / 2, cy - hud.height / 2 + shadow + h / 2, icon.height

    def intro_frame(self, first: np.ndarray, dim: np.ndarray, t: float) -> np.ndarray:
        """Quadro da capa no instante t (0 até self.intro): `first` é o primeiro frame, `dim` ele escurecido e desfocado."""
        u, cov = self.u, self._cover_sprites()
        t_out = self.intro - self.intro_out
        x_in = clamp01(t / self.intro_in)
        x_out = clamp01((t - t_out) / self.intro_out) if self.intro_out else 0.0
        k_bg = ease_in_out(clamp01(t / (0.75 * self.intro_in))) * (1 - ease_in_out(x_out))
        frame = (first * (1 - k_bg) + dim * k_bg).astype(np.uint8)
        cx, cy = cov["cx"], cov["cy"]
        blend_scaled(frame, cov["glow"], cx, cy, 1.0, 0.9 * k_bg)
        hud_op = clamp01((x_out - 0.45) / 0.55)  # o painel do topo aparece enquanto o booster chega
        if hud_op > 0:
            hud, hcx, hcy, _, _ = self._hud_at(0.0)
            blend_scaled(frame, hud, hcx, hcy, 1.0, hud_op)
        x_txt = clamp01((t - 0.35 * self.intro_in) / (0.75 * self.intro_in))
        txt_op = ease_in_out(x_txt) * (1 - clamp01(x_out * 2.5))
        if txt_op > 0:
            blend_scaled(frame, cov["panel"], cx, cov["panel_cy"] + (1 - ease_out(x_txt)) * 60 * u, 1.0, txt_op)

        images = cov["images"]
        floating = math.sin(2 * math.pi * max(0.0, t - self.intro_in) / 2.4) * 7 * u * (1 - x_out)
        rise = (1 - ease_out(x_in)) * 150 * u
        pop = 0.6 + 0.4 * ease_out_back(x_in)
        hx, hy, hh = self._hud_icon()
        e = ease_in_out(x_out)
        side = [(-1, 1), (1, 2)] if len(images) == 3 else [(1, 1)] if len(images) == 2 else []
        for offset, i in side:  # os outros boosters, em leque atrás do primeiro
            img = images[i]
            angle = -3 + 9 * offset
            ox = offset * 0.42 * images[0].width * pop
            blend_posed(frame, img, cx + ox, cy + rise + floating, pop, angle, clamp01(x_in * 2.2) * (1 - clamp01(x_out * 2)))
        main = images[0]
        sweep = (t - (self.intro_in + 0.15)) / 0.8
        if 0 <= sweep <= 1:
            main = shine(main, sweep)
        scale = pop + (hh / main.height - pop) * e
        px, py = cx + (hx - cx) * e, cy + rise + floating + (hy - cy) * e
        blend_posed(frame, main, px, py, scale, (-10 + 7 * ease_out(x_in)) * (1 - e), clamp01(x_in * 2.2))
        return frame

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


class SealedOverlay(Overlay):
    """Vídeo do registro de lacrados: cada booster colocado na pilha ganha a etiqueta com o set e o preço; o
    painel do topo conta os boosters e soma o valor; a capa mostra os sets e o resumo agrupa por set."""

    def __init__(self, scan: dict, money: Money, size: tuple[int, int], paid_usd: float | None = None,
                 icons: dict[str, Image.Image] | None = None, names: dict[str, str] | None = None,
                 paid: tuple[float, str] | None = None, intro: float = 0.0, logo: Image.Image | None = None,
                 logo_corner: str = "top-right", logo_opacity: float = 0.6):
        names = names or {}
        packs = [{**p, "name": p.get("set_name") or names.get(p["set"], f"Set {p['set']}"), "version": None,
                  "rarity": None, "foil": False, "pack": 1} for p in scan["packs"]]
        super().__init__({"cards": packs}, money, size, max(1, len(packs)), paid_usd, icons, names, paid, intro,
                         logo, logo_corner, logo_opacity)

    def _badge(self, c: dict) -> tuple[str, tuple]:
        return "BOOSTER", GOLD

    def _sets_in_order(self) -> list[str]:
        """Os sets na ordem em que aparecem no vídeo, cada um uma vez."""
        return list(dict.fromkeys(c["set"] for c in self.cards))

    def _sealed_hud(self, code: str | None, shown: int, total_text: str) -> Image.Image:
        key = ("lacrados", code, shown, total_text)
        if key in self._hud_cache:
            return self._hud_cache[key]
        u = self.u
        h = 150 * u
        icon = self._set_icon(code, h - 26 * u) if code else None
        lead = icon.width + 14 * u if icon else 0
        w = 660 * u + lead
        shadow = 18 * u
        s = Sprite(w + 2 * shadow, h + 2 * shadow)
        ox, oy = shadow, shadow
        s.rrect([ox, oy, ox + w, oy + h], 32 * u, fill=PANEL, outline=(242, 193, 78, 120), width=2 * u)
        if icon:
            s.paste(icon, (ox + 20 * u, oy + (h - icon.height) / 2))
        x0 = ox + 30 * u + lead
        s.text((x0, oy + 34 * u), "LACRADOS", "SemiBold", 25 * u, GOLD)
        s.text((x0, oy + 72 * u), f"{shown} {'booster' if shown == 1 else 'boosters'}", "Medium", 27 * u, MUTED)
        s.text((ox + w - 30 * u, oy + h / 2), total_text, "ExtraBold", 62 * u, WHITE, anchor="rm")
        img = s.done(shadow=12 * u)
        self._hud_cache[key] = img
        return img

    def _hud_at(self, t: float) -> tuple[Image.Image, float, float, float, list[int]]:
        everything = list(range(len(self.cards)))
        shown = [k for k in everything if self.cards[k]["t"] <= t]
        code = self.cards[shown[-1]]["set"] if shown else (self.cards[0]["set"] if self.cards else None)
        hud = self._sealed_hud(code, len(shown), self.money.fmt(self._total_at(t, shown)))
        pulse = 1.0
        for k in shown:
            x = (t - self.arrive[k]) / PULSE
            if 0 <= x <= 1 and self.cards[k].get("price_usd"):
                pulse = 1 + 0.06 * math.sin(math.pi * x)
        return hud, self.W / 2, 56 * self.u + hud.height / 2 - 18 * self.u, pulse, everything

    def _hud_icon(self) -> tuple[float, float, float]:
        u = self.u
        hud, cx, cy, _, _ = self._hud_at(0.0)
        h = 150 * u
        icon = self._set_icon(self.cards[0]["set"], h - 26 * u)
        shadow = 18 * u
        return cx - hud.width / 2 + shadow + 20 * u + icon.width / 2, cy - hud.height / 2 + shadow + h / 2, icon.height

    def _cover_sprites(self) -> dict:
        """Capa: os boosters dos primeiros sets em leque e o painel com quantos são (o valor fica para o fim)."""
        if self._cover is not None:
            return self._cover
        n = len(self.cards)
        sets = self._sets_in_order()
        self.pack_set = {i + 1: code for i, code in enumerate(sets[:3])}  # a capa da abertura usa o mapa booster→set
        cover = super()._cover_sprites()
        u, W = self.u, self.W
        lines = [("REGISTRO DE LACRADOS", "SemiBold", 30 * u, GOLD),
                 (f"{n} {'booster' if n == 1 else 'boosters'}", "ExtraBold", 96 * u, WHITE),
                 (f"{len(sets)} {'set' if len(sets) == 1 else 'sets'}", "SemiBold", 34 * u, MUTED)]
        if self.paid:
            lines.append((f"pago {Money(self.paid[1]).fmt(self.paid[0])}", "SemiBold", 34 * u, WHITE))
        m = Sprite(1, 1)
        pad, gap = 44 * u, 16 * u
        w = min(W * 0.88, max(560 * u, max(m.textlen(t, wt, sz) for t, wt, sz, _ in lines) + 2 * pad))
        h = 2 * pad + sum(sz for _, _, sz, _ in lines) + gap * (len(lines) - 1)
        shadow = 24 * u
        s = Sprite(w + 2 * shadow, h + 2 * shadow)
        s.rrect([shadow, shadow, shadow + w, shadow + h], 36 * u, fill=(10, 14, 32, 236), outline=(242, 193, 78, 150),
                width=2 * u)
        y = shadow + pad
        for text, weight, size, color in lines:
            s.text((shadow + w / 2, y + size / 2), text, weight, size, color, anchor="mm")
            y += size + gap
        cover["panel"] = s.done(shadow=16 * u)
        main, gap = cover["images"][0], 36 * u  # recentraliza com o painel novo (outra altura)
        top = (self.H - (main.height + gap + cover["panel"].height)) / 2
        cover.update(cy=top + main.height / 2, panel_cy=top + main.height + gap + cover["panel"].height / 2)
        self.pack_set = {1: self.cards[0]["set"]} if self.cards else {}
        return cover

    def summary(self) -> Image.Image:
        """Resumo: o total, quantos boosters e, por set, quantos e quanto valem (do que vale mais)."""
        u = self.u
        groups: dict[str, list[dict]] = {}
        for c in self.cards:
            groups.setdefault(c["set"], []).append(c)
        rows = sorted(groups.items(), key=lambda kv: -sum(c.get("price_usd") or 0 for c in kv[1]))
        extra = max(0, len(rows) - 12)
        rows = rows[:12]
        w = min(940 * u, self.W - 2 * self.margin)
        row_h = 62 * u
        head = 210 * u + (48 * u if self.paid_usd else 0)
        h = head + len(rows) * row_h + (50 * u if extra else 0) + 40 * u
        shadow = 24 * u
        s = Sprite(w + 2 * shadow, h + 2 * shadow)
        ox, oy = shadow, shadow
        s.rrect([ox, oy, ox + w, oy + h], 36 * u, fill=(10, 14, 32, 242), outline=(242, 193, 78, 150), width=2 * u)
        s.text((ox + w / 2, oy + 46 * u), "RESUMO DOS LACRADOS", "SemiBold", 28 * u, GOLD, anchor="mm")
        total = sum(c.get("price_usd") or 0 for c in self.cards)
        s.text((ox + w / 2, oy + 112 * u), self.money.fmt(total), "ExtraBold", 80 * u, WHITE, anchor="mm")
        n = len(self.cards)
        s.text((ox + w / 2, oy + 168 * u), f"{n} {'booster' if n == 1 else 'boosters'} · {len(groups)} "
               f"{'set' if len(groups) == 1 else 'sets'}", "Medium", 24 * u, MUTED, anchor="mm")
        if self.paid_usd:
            diff = total - self.paid_usd
            pct = f" ({diff / self.paid_usd * 100:+.0f}%)".replace("-", "−")
            line = f"pago {self.money.fmt(self.paid_usd)}  ·  resultado {self.money.fmt(diff, sign=True)}{pct}"
            color = rgba("#5BE49B") if diff >= 0 else rgba("#FF7A7A")
            s.text((ox + w / 2, oy + head - 34 * u), line, "SemiBold", 25 * u, color, anchor="mm")
        y = oy + head
        for i, (code, items) in enumerate(rows):
            if i == 0:
                s.rrect([ox + 18 * u, y + 3 * u, ox + w - 18 * u, y + row_h - 3 * u], 18 * u,
                        fill=(242, 193, 78, 38), outline=(242, 193, 78, 200), width=2 * u)
            mid = y + row_h / 2
            icon = self._set_icon(code, 48 * u)
            s.paste(icon, (ox + 40 * u, mid - icon.height / 2))
            value = sum(c.get("price_usd") or 0 for c in items)
            price = self.money.fmt(value)
            price_w = s.textlen(price, "Bold", 32 * u)
            name = f"{items[0]['name']}  ×{len(items)}"
            room = w - 120 * u - price_w - 40 * u
            while s.textlen(name, "SemiBold", 29 * u) > room and len(name) > 4:
                name = name[:-2].rstrip() + "…"
            s.text((ox + 100 * u, mid), name, "SemiBold", 29 * u, WHITE, anchor="lm")
            s.text((ox + w - 40 * u, mid), price, "Bold", 32 * u, price_color(value), anchor="rm")
            y += row_h
        if extra:
            s.text((ox + w / 2, y + 25 * u), f"+ {extra} sets", "Medium", 24 * u, MUTED, anchor="mm")
        return s.done(shadow=16 * u)


def render(
    settings: Settings, scan: dict, out: Path, money: Money, progress: Progress, paid_usd: float | None = None,
    paid: float | None = None, paid_currency: str | None = None, logo: Image.Image | None = None,
) -> Path:
    """Gera o vídeo com overlay e, ao lado, a capa (`.jpg`) e os tempos (`.json`: capa, resumo e fim)."""
    sealed = scan.get("kind") == "lacrados"
    if not (scan["packs"] if sealed else scan["cards"]):
        raise RuntimeError("Nenhum booster identificado; nada para sobrepor." if sealed
                           else "Nenhuma carta identificada; nada para sobrepor.")
    video = settings.root / scan["video"]
    info = probe(video)
    size = info.scaled(settings.output_short_side)
    con = db.connect(settings.db_path)
    codes = sorted({c["set"] for c in (scan["packs"] if sealed else scan["cards"])})
    icons, names = {}, {}
    for r in con.execute(f"SELECT code, name, icon FROM sets WHERE code IN ({','.join('?' * len(codes))})", codes):
        names[r["code"]] = r["name"]
        if r["icon"] and (settings.root / r["icon"]).exists():
            icons[r["code"]] = Image.open(settings.root / r["icon"]).convert("RGBA")
    intro = max(0.0, settings.intro_seconds)
    corner = settings.logo_corner if settings.logo_corner in CORNERS else CORNERS[0]
    paid_as = (paid, paid_currency or "BRL") if paid else None
    if sealed:
        ov = SealedOverlay(scan, money, size, paid_usd, icons, names, paid_as, intro, logo, corner, settings.logo_opacity)
    else:
        ov = Overlay(scan, money, size, settings.pack_size, paid_usd, icons, names, paid_as, intro, logo, corner,
                     settings.logo_opacity)
    tmp = out.with_name(out.stem + ".imagem.part.mp4")  # o vídeo anterior continua válido até o novo ficar pronto
    writer = VideoWriter(tmp, size, FPS)  # o som entra depois, misturado com o "ka-ching" de cada carta
    written = 0
    frames = read_frames(info, settings.output_short_side, fps=FPS)
    first = next(frames)
    n_intro = round(intro * FPS)
    total_frames = int(info.duration * FPS) + n_intro
    poster = None
    if n_intro:  # capa: o primeiro frame parado, com o booster e o valor pago
        sharp = first.astype(np.float32)
        dim = np.asarray(Image.fromarray(first).filter(ImageFilter.GaussianBlur(10 * ov.u))).astype(np.float32) * 0.5
        poster_at = min(n_intro - 1, round((ov.intro_in + 0.5) * FPS))
        for j in range(n_intro):
            canvas = ov.intro_frame(sharp, dim, j / FPS)
            ov.stamp(canvas)
            writer.write(canvas)
            written += 1
            if j == poster_at:
                poster = canvas
            progress(0.95 * j / total_frames, f"Renderizando a capa ({j}/{n_intro} frames)")
    for i, frame in enumerate(itertools.chain([first], frames)):
        canvas = frame.copy()
        ov.draw(canvas, i / FPS)
        ov.stamp(canvas)
        writer.write(canvas)
        written += 1
        progress(0.95 * (n_intro + i) / total_frames, f"Renderizando vídeo ({n_intro + i}/{total_frames} frames)")
    # segura o último frame até a soma da última carta terminar de animar
    t = (i + 1) / FPS
    while t < max(ov.arrive) + COUNT_UP + 0.4:
        canvas = frame.copy()
        ov.draw(canvas, t)
        ov.stamp(canvas)
        writer.write(canvas)
        written += 1
        t += 1 / FPS
    summary_at = intro + t
    # resumo: crossfade do último frame (com overlay) para ele mesmo desfocado e escurecido,
    # e o painel entrando por cima
    last = canvas.astype(np.float32)
    bg = Image.fromarray(frame).filter(ImageFilter.GaussianBlur(14 * ov.u))
    bg = (np.asarray(bg).astype(np.float32) * 0.45).astype(np.uint8)
    ov.stamp(bg)  # o logo fica igual durante a transição (os dois lados já têm ele)
    bg = bg.astype(np.float32)
    panel = ov.summary()
    out_frame = canvas
    for j in range(int(settings.outro_seconds * FPS)):
        x = clamp01(j / (0.45 * FPS))
        out_frame = (last * (1 - x) + bg * x).astype(np.uint8)
        e = ease_out_back(x)
        blend_scaled(out_frame, panel, ov.W / 2, ov.H / 2 + (1 - e) * 60 * ov.u, 0.94 + 0.06 * e, clamp01(x * 1.4))
        writer.write(out_frame)
        written += 1
    writer.close()
    # som: o original depois da capa, o "ka-ching" quando a etiqueta de cada carta aparece e os aplausos quando
    # a soma alcança o valor pago
    progress(0.97, "Misturando o som")
    celebration = ov.celebration_time()
    effects = {"sounds": [round(intro + c["t"], 3) for c in ov.cards] if settings.card_sound_volume > 0 else [],
               "sound_volume": settings.card_sound_volume,
               "celebration": round(intro + celebration, 3) if celebration is not None and settings.celebration_volume > 0 else None,
               "celebration_volume": settings.celebration_volume}
    track = out.with_name(out.stem + ".som.wav")
    try:
        audio.write_wav(track, audio.soundtrack(video if info.has_audio else None, intro, written / FPS, effects))
        mixed = out.with_suffix(".part.mp4")
        subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-i", str(tmp), "-i", str(track), "-map", "0:v", "-map", "1:a",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(mixed)],
                       check=True, capture_output=True)
        mixed.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
        track.unlink(missing_ok=True)
    # capa do vídeo na página: a capa do começo (sem spoiler); sem capa, o resumo final
    image = Image.fromarray(poster if poster is not None else out_frame)
    image.thumbnail((720, 720))
    image.save(out.with_suffix(".jpg"), quality=85)
    timing = {"intro": intro, "summary": round(summary_at, 3),
              "end": round(summary_at + int(settings.outro_seconds * FPS) / FPS, 3), **effects}
    out.with_suffix(".json").write_text(json.dumps(timing))
    return out


def timing(video: Path) -> dict:
    """Tempos do vídeo com overlay (capa, resumo, fim); vídeos de antes da capa não têm o .json."""
    path = video.with_suffix(".json")
    return json.loads(path.read_text()) if path.exists() else {"intro": 0.0}
