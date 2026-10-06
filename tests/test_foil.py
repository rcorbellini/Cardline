from cardline.foil import assign_foils

PACK = ["Common"] * 6 + ["Uncommon"] * 3 + ["Rare", "Super_rare"] + ["Uncommon"]  # ordem do vídeo de exemplo


def cards(rarities: list[str], pack: int = 1) -> list[dict]:
    return [{"pack": pack, "slot": i + 1, "rarity": r} for i, r in enumerate(rarities)]


def foils(cs: list[dict]) -> list[int]:
    return [i for i, c in enumerate(cs) if c["foil"]]


def test_foil_is_the_extra_rarity_at_the_back_of_the_pack():
    cs = cards(PACK)
    assign_foils(cs, 12)
    assert foils(cs) == [11]
    assert cs[11]["foil_reason"] == "booster"


def test_pack_opened_from_the_back_puts_the_foil_first():
    cs = cards(PACK[::-1])
    assign_foils(cs, 12)
    assert foils(cs) == [0]


def test_extra_common_means_common_foil():
    cs = cards(["Common"] * 6 + ["Uncommon"] * 3 + ["Rare", "Legendary"] + ["Common"])
    assign_foils(cs, 12)
    assert foils(cs) == [11]


def test_enchanted_takes_the_foil_slot():
    cs = cards(["Common"] * 6 + ["Uncommon"] * 3 + ["Rare", "Rare"] + ["Enchanted"])
    assign_foils(cs, 12)
    assert foils(cs) == [11]
    assert cs[11]["foil_reason"] == "raridade"


def test_incomplete_pack_is_not_guessed():
    cs = cards(["Common"] * 4 + ["Uncommon"])
    assign_foils(cs, 12)
    assert foils(cs) == []


def test_each_pack_gets_its_own_foil():
    cs = cards(PACK, pack=1) + cards(PACK, pack=2)
    assign_foils(cs, 12)
    assert foils(cs) == [11, 23]
