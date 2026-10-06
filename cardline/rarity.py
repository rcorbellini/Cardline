"""Rótulos e cores de raridade/tinta, compartilhados pelo overlay e pela página da coleção."""

from __future__ import annotations

RARITIES = {  # ordem = da mais comum para a mais rara
    "Common": ("Comum", "#AEB7C2"),
    "Uncommon": ("Incomum", "#4CC38A"),
    "Rare": ("Rara", "#E39350"),
    "Super_rare": ("Super Rara", "#8EC5FF"),
    "Legendary": ("Lendária", "#F5C542"),
    "Epic": ("Épica", "#C084FC"),
    "Enchanted": ("Encantada", "#FF8AD8"),
    "Iconic": ("Icônica", "#FF5C7A"),
    "Promo": ("Promo", "#64D2FF"),
}

INKS = {
    "Amber": ("Âmbar", "#F4B223"),
    "Amethyst": ("Ametista", "#8E4FA5"),
    "Emerald": ("Esmeralda", "#2E9E5B"),
    "Ruby": ("Rubi", "#D3303A"),
    "Sapphire": ("Safira", "#1E8AD6"),
    "Steel": ("Aço", "#97A3AE"),
}


def label(rarity: str | None) -> str:
    return RARITIES.get(rarity or "", (rarity or "?", ""))[0]


def color(rarity: str | None) -> str:
    return RARITIES.get(rarity or "", ("", "#AEB7C2"))[1]


def rank(rarity: str | None) -> int:
    keys = list(RARITIES)
    return keys.index(rarity) if rarity in RARITIES else -1
