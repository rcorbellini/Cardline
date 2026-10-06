"""Valores: preços vêm em USD (TCGplayer); opcionalmente exibidos em BRL pela cotação do dia."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import date

from .config import Settings

FX_URL = "https://economia.awesomeapi.com.br/json/last/USD-BRL"
CURRENCIES = ("USD", "BRL")


@dataclass
class Money:
    currency: str  # "USD" ou "BRL"
    rate: float = 1.0  # unidades da moeda por USD
    rate_day: str | None = None

    @property
    def symbol(self) -> str:
        return "R$" if self.currency == "BRL" else "US$"

    def convert(self, usd: float | None) -> float | None:
        return None if usd is None else usd * self.rate

    def fmt(self, usd: float | None, sign: bool = False) -> str:
        value = self.convert(usd)
        if value is None:
            return "—"
        text = f"{abs(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        prefix = ("+" if value >= 0 else "−") if sign else ("−" if value < 0 else "")
        return f"{prefix}{self.symbol} {text}"


def usd_brl(settings: Settings) -> tuple[float, str] | None:
    """Cotação USD→BRL do dia (cacheada); a última conhecida se estiver offline, ou None."""
    cache = settings.cache_dir / "fx.json"
    today = date.today().isoformat()
    cached = json.loads(cache.read_text()) if cache.exists() else {}
    if cached.get("day") != today:
        try:
            req = urllib.request.Request(FX_URL, headers={"User-Agent": "cardline/0.1"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                cached = {"day": today, "rate": float(json.load(resp)["USDBRL"]["bid"])}
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(cached))
        except (OSError, KeyError, ValueError):
            if not cached:
                return None
    return cached["rate"], cached["day"]


def money_for(settings: Settings, currency: str | None = None) -> Money:
    currency = (currency or settings.currency).upper()
    if currency == "USD":
        return Money("USD")
    fx = usd_brl(settings)
    if fx is None:
        raise SystemExit("Não consegui obter a cotação USD→BRL; use a moeda USD.")
    return Money("BRL", *fx)


def to_usd(settings: Settings, value: float | None, currency: str) -> float | None:
    """Converte um valor informado (ex.: preço pago pelo booster em reais) para USD."""
    if value is None or currency.upper() == "USD":
        return value
    fx = usd_brl(settings)
    return None if fx is None else value / fx[0]
