"""Rótulos e cores de raridade/tinta, compartilhados pelo overlay e pela página da coleção (por jogo: games.py)."""

from __future__ import annotations

from . import games

RARITIES = games.LORCANA.rarities  # os de Lorcana, para quem ainda não sabe de jogos
INKS = games.LORCANA.colors


def _game(game: games.Game | str | None) -> games.Game:
    return game if isinstance(game, games.Game) else games.get(game)


def label(rarity: str | None, game: games.Game | str | None = None) -> str:
    return _game(game).rarity_label(rarity)


def color(rarity: str | None, game: games.Game | str | None = None) -> str:
    return _game(game).rarity_color(rarity)


def rank(rarity: str | None, game: games.Game | str | None = None) -> int:
    return _game(game).rarity_rank(rarity)
