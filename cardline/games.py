"""Os jogos do cardline: Disney Lorcana, Magic: The Gathering e Pokémon TCG.

Cada set e cada carta pertencem a um jogo. Os de Lorcana mantêm os códigos e ids do Lorcast ("1", "P1", "crd_…");
os outros levam um prefixo ("mtg-fra", "pkm-sv01", "mtg-<id do Scryfall>") para não colidir. Aqui fica o que muda
de um jogo para outro: raridades, as "cores" (tintas de Lorcana, cores de Magic, tipos de Pokémon), o tamanho do
booster, a categoria no TCGplayer, os idiomas das cartas e os textos da narração e das redes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings


@dataclass(frozen=True)
class Game:
    key: str
    name: str  # "Disney Lorcana"
    short: str  # "Lorcana"
    prefix: str  # dos códigos de set e ids de carta
    tcg_category: int  # categoria no TCGplayer (tcgcsv.com)
    pack_size: int  # cartas por booster
    langs: tuple[str, ...]  # idiomas das cartas que o reconhecimento compara
    color_label: str  # o que as "cores" são neste jogo
    foil_label: str
    rarities: dict[str, tuple[str, str]]  # chave → (rótulo, cor), da mais comum para a mais rara
    colors: dict[str, tuple[str, str]]
    common: str  # a raridade comum (a narração faz presságio com ela)
    reactions: dict[str, list[str]]  # falas da narração quando sai uma carta de raridade alta
    foil_only: frozenset[str] = frozenset()  # raridades que só existem em foil
    tags: tuple[str, ...] = ()  # tags dos vídeos no YouTube
    hashtags: str = ""
    image_ext: str = ".jpg"
    joke_hint: str = ""  # para a narração: como chamar personagens e cartas
    product_prefix: str = ""  # o que tirar do começo do nome de um lacrado no TCGplayer
    extra: dict = field(default_factory=dict)

    def rarity_label(self, rarity: str | None) -> str:
        return self.rarities.get(rarity or "", (rarity or "?", ""))[0]

    def rarity_color(self, rarity: str | None) -> str:
        return self.rarities.get(rarity or "", ("", "#AEB7C2"))[1]

    def rarity_rank(self, rarity: str | None) -> int:
        keys = list(self.rarities)
        return keys.index(rarity) if rarity in self.rarities else -1


LORCANA = Game(
    key="lorcana", name="Disney Lorcana", short="Lorcana", prefix="", tcg_category=71, pack_size=12, langs=("en",),
    color_label="Tinta", foil_label="Foil",
    rarities={
        "Common": ("Comum", "#AEB7C2"),
        "Uncommon": ("Incomum", "#4CC38A"),
        "Rare": ("Rara", "#E39350"),
        "Super_rare": ("Super Rara", "#8EC5FF"),
        "Legendary": ("Lendária", "#F5C542"),
        "Epic": ("Épica", "#C084FC"),
        "Enchanted": ("Encantada", "#FF8AD8"),
        "Iconic": ("Icônica", "#FF5C7A"),
        "Promo": ("Promo", "#64D2FF"),
    },
    colors={
        "Amber": ("Âmbar", "#F4B223"),
        "Amethyst": ("Ametista", "#8E4FA5"),
        "Emerald": ("Esmeralda", "#2E9E5B"),
        "Ruby": ("Rubi", "#D3303A"),
        "Sapphire": ("Safira", "#1E8AD6"),
        "Steel": ("Aço", "#97A3AE"),
    },
    common="Common",
    reactions={
        "Rare": ["Uma rara!", "Opa... uma rara!"],
        "Super_rare": ["Super rara! Agora vai!", "Super rara?! Calma, coração."],
        "Legendary": ["Lendária?! Será que eu me enganei?", "Uma lendária! A profecia treme."],
        "Epic": ["Épica?! Eu preciso sentar."],
        "Enchanted": ["Encantada?! Isso não estava na profecia!"],
        "Iconic": ["Icônica?! Isso não estava no roteiro!"],
    },
    foil_only=frozenset({"Enchanted", "Epic", "Iconic"}),
    tags=("lorcana", "disney lorcana", "booster", "abertura de booster", "tcg", "br", "brasil"),
    hashtags="#lorcana #disneylorcana #tcg #booster",
    image_ext=".avif",
    joke_hint="use os nomes como são conhecidos no Brasil, ex.: Tinker Bell = Sininho, Goofy = Pateta",
    product_prefix=r"^Disney Lorcana:?\s*",
)

MAGIC = Game(
    key="magic", name="Magic: The Gathering", short="Magic", prefix="mtg-", tcg_category=1, pack_size=14,
    langs=("en", "pt"), color_label="Cor", foil_label="Foil",
    rarities={
        "common": ("Comum", "#AEB7C2"),
        "uncommon": ("Incomum", "#B8C7D9"),
        "rare": ("Rara", "#E5C55A"),
        "mythic": ("Mítica", "#F07B2A"),
        "special": ("Especial", "#C084FC"),
        "bonus": ("Bônus", "#FF8AD8"),
    },
    colors={
        "W": ("Branco", "#F3E6B5"),
        "U": ("Azul", "#2C7BCB"),
        "B": ("Preto", "#5A5058"),
        "R": ("Vermelho", "#D9472B"),
        "G": ("Verde", "#2E8B57"),
        "C": ("Incolor", "#B5AFA8"),
    },
    common="common",
    reactions={
        "rare": ["Uma rara!", "Opa... uma rara!"],
        "mythic": ["Mítica?! Será que eu me enganei?", "Uma mítica! A profecia treme."],
        "special": ["Especial?! Isso não estava na profecia!"],
        "bonus": ["Bônus?! Isso não estava no roteiro!"],
    },
    tags=("magic", "magic the gathering", "mtg", "booster", "abertura de booster", "tcg", "br", "brasil"),
    hashtags="#magicthegathering #mtg #tcg #booster",
    image_ext=".jpg",
    joke_hint="use o nome da carta em português quando houver tradução conhecida",
    product_prefix=r"^Magic:? The Gathering:?\s*",
)

POKEMON = Game(
    key="pokemon", name="Pokémon TCG", short="Pokémon", prefix="pkm-", tcg_category=3, pack_size=10,
    langs=("en", "pt"), color_label="Tipo", foil_label="Reverse holo",
    rarities={
        "Common": ("Comum", "#AEB7C2"),
        "Uncommon": ("Incomum", "#4CC38A"),
        "Rare": ("Rara", "#E39350"),
        "Rare Holo": ("Rara Holo", "#EFA35C"),
        "Holo Rare": ("Rara Holo", "#EFA35C"),
        "Holo Rare V": ("Rara Holo V", "#8EC5FF"),
        "Holo Rare VMAX": ("Rara Holo VMAX", "#8EC5FF"),
        "Holo Rare VSTAR": ("Rara Holo VSTAR", "#8EC5FF"),
        "Double rare": ("Rara Dupla", "#8EC5FF"),
        "Radiant Rare": ("Rara Radiante", "#64D2FF"),
        "Amazing Rare": ("Rara Incrível", "#64D2FF"),
        "ACE SPEC Rare": ("ACE SPEC", "#64D2FF"),
        "Shiny rare": ("Rara Brilhante", "#9BE0C4"),
        "Ultra Rare": ("Ultra Rara", "#F5C542"),
        "Full Art Trainer": ("Treinador Arte Completa", "#F5C542"),
        "Shiny Ultra Rare": ("Ultra Rara Brilhante", "#F5C542"),
        "Illustration rare": ("Ilustração Rara", "#C084FC"),
        "Secret Rare": ("Rara Secreta", "#D9A6FF"),
        "Special illustration rare": ("Ilustração Rara Especial", "#FF8AD8"),
        "Black White Rare": ("Rara Preto e Branco", "#FF8AD8"),
        "Hyper rare": ("Hiper Rara", "#FF5C7A"),
        "Mega Hyper Rare": ("Mega Hiper Rara", "#FF3D5A"),
        "Promo": ("Promo", "#64D2FF"),
    },
    colors={
        "Grass": ("Planta", "#4CAF50"),
        "Fire": ("Fogo", "#E8502F"),
        "Water": ("Água", "#3C8DD9"),
        "Lightning": ("Elétrico", "#F2C531"),
        "Psychic": ("Psíquico", "#9C5CC7"),
        "Fighting": ("Lutador", "#C0602A"),
        "Darkness": ("Sombrio", "#3F4A57"),
        "Metal": ("Metal", "#9AA6AE"),
        "Dragon": ("Dragão", "#B39535"),
        "Fairy": ("Fada", "#E58FC0"),
        "Colorless": ("Incolor", "#CFCFC4"),
    },
    common="Common",
    reactions={
        "Double rare": ["Rara dupla! Agora vai!", "Rara dupla?! Calma, coração."],
        "Ultra Rare": ["Ultra rara?! Será que eu me enganei?", "Uma ultra rara! A profecia treme."],
        "Illustration rare": ["Ilustração rara?! Eu preciso sentar."],
        "Special illustration rare": ["Ilustração rara especial?! Isso não estava na profecia!"],
        "Hyper rare": ["Hiper rara?! Isso não estava no roteiro!"],
        "Mega Hyper Rare": ["Mega hiper rara?! Alguém me belisca."],
        "ACE SPEC Rare": ["Uma ACE SPEC! Opa!"],
    },
    tags=("pokemon", "pokémon", "pokemon tcg", "booster", "abertura de booster", "tcg", "br", "brasil"),
    hashtags="#pokemon #pokemontcg #tcg #booster",
    image_ext=".webp",
    joke_hint="use os nomes dos Pokémon como são (eles são os mesmos no Brasil) e traduza o resto",
    product_prefix=r"^Pok[eé]mon:?\s*",
)

GAMES: dict[str, Game] = {g.key: g for g in (LORCANA, MAGIC, POKEMON)}


def get(key: str | None) -> Game:
    return GAMES.get(key or "lorcana", LORCANA)


def of_code(code: str | None) -> Game:
    """O jogo de um código de set ou de um id de carta, pelo prefixo."""
    for g in (MAGIC, POKEMON):
        if (code or "").startswith(g.prefix):
            return g
    return LORCANA


def pack_size(settings: Settings, game: Game | str | None) -> int:
    """Cartas por booster: o `pack_size` do cardline.toml vale para Lorcana; os outros usam o do jogo."""
    g = game if isinstance(game, Game) else get(game)  # scan sem jogo (de antes dos jogos): Lorcana
    return settings.pack_size if g is LORCANA else g.pack_size


def short_code(code: str) -> str:
    """O código do set como o jogo escreve ("mtg-fra" → "FRA", "pkm-sv01" → "sv01", "1" → "1")."""
    g = of_code(code)
    bare = code[len(g.prefix):]
    return bare.upper() if g is MAGIC else bare
