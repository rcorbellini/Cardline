"""Inferência de qual carta de cada booster é a foil, a partir da estrutura do booster.

Um booster de Lorcana tem 6 comuns, 3 incomuns, 2 raras+ (Rare/Super Rare/Legendary) e
1 foil de qualquer raridade, nessa ordem de colação. Então:
  * raridades que só existem em foil (Enchanted, Epic, Iconic) são foil;
  * a classe de raridade com uma carta a mais que o esperado contém a foil regular;
  * dentro dela, a foil é a carta mais próxima da ponta do booster onde a foil fica
    (o fim, se as comuns vieram primeiro; o começo, se o booster foi aberto ao contrário).
Visualmente o brilho foil é pouco confiável no vídeo (reflexo, mão, sombra), então ele não é usado.
"""

from __future__ import annotations

FOIL_ONLY = {"Enchanted", "Epic", "Iconic"}
EXPECTED = {"C": 6, "U": 3, "R": 2}  # por booster de 12 cartas, fora a foil
RARITY_CLASS = {"Common": "C", "Uncommon": "U", "Rare": "R", "Super_rare": "R", "Legendary": "R"}


def assign_foils(cards: list[dict], pack_size: int, game: str = "lorcana") -> None:
    """A foil de cada booster de Lorcana. Magic e Pokémon ainda não deduzem (o "foil" fica para a edição): as
    estruturas dos boosters deles têm mais de um slot especial e precisam de vídeos de verdade para calibrar."""
    if game != "lorcana":
        for c in cards:
            c.setdefault("foil", False)
            c.setdefault("foil_reason", None)
        return
    packs: dict[int, list[dict]] = {}
    for c in cards:
        packs.setdefault(c["pack"], []).append(c)
    for pack in packs.values():
        for c in pack:
            c["foil"], c["foil_reason"] = c["rarity"] in FOIL_ONLY, "raridade" if c["rarity"] in FOIL_ONLY else None
        regular = [c for c in pack if c["rarity"] not in FOIL_ONLY]
        counts = {k: sum(RARITY_CLASS.get(c["rarity"]) == k for c in regular) for k in EXPECTED}
        scale = pack_size / 12
        excess = [k for k, n in counts.items() if n > EXPECTED[k] * scale]
        commons = [i for i, c in enumerate(pack) if RARITY_CLASS.get(c["rarity"]) == "C"]
        rares = [i for i, c in enumerate(pack) if RARITY_CLASS.get(c["rarity"]) == "R"]
        foil_at_end = not (commons and rares and sum(commons) / len(commons) > sum(rares) / len(rares))
        if len(excess) == 1:
            candidates = [c for c in regular if RARITY_CLASS.get(c["rarity"]) == excess[0]]
            reason = "booster"
        elif not excess and len(pack) == pack_size and len(regular) == len(pack):
            candidates, reason = regular, "posição"  # contagem não fecha (carta trocada?): chuta pela posição
        else:
            continue
        pick = candidates[-1] if foil_at_end else candidates[0]
        pick["foil"], pick["foil_reason"] = True, reason
