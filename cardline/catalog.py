"""Sincroniza catálogo e preços (Lorcast para Lorcana, Scryfall para Magic, TCGdex para Pokémon), baixa as imagens
oficiais e monta os índices de reconhecimento.

Lorcana sincroniza tudo. Magic tem mais de mil sets, então para Magic e Pokémon a sincronização traz a lista de
sets e só baixa as cartas dos sets pedidos (`--sets`, ou "Baixar" na aba Sets) e dos que já foram baixados antes.
"""

from __future__ import annotations

import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import db, games, lorcast, scryfall, tcgdex
from .config import Settings
from .index import build_set_index, image_path, index_is_fresh

# Ícones dos sets: Lorcana não tem símbolo de expansão nas cartas (o símbolo do rodapé é a raridade),
# então o ícone de cada set é a foto oficial do booster no TCGplayer, via tcgcsv.com (espelho público). Magic e
# Pokémon seguem o mesmo padrão (a capa do vídeo mostra o booster).


def tcgcsv(game: str = "lorcana") -> str:
    """O espelho do TCGplayer na categoria do jogo (71 = Lorcana, 1 = Magic, 3 = Pokémon)."""
    return f"https://tcgcsv.com/tcgplayer/{games.get(game).tcg_category}"


TCGCSV = tcgcsv("lorcana")
ICON_MAX = 1000  # resolução cheia: o vídeo mostra o booster grande na capa
THUMB_MAX = 320  # miniatura leve para a página
LEGACY_MAX = 600  # tamanho em que as fotos eram guardadas antes; essas são baixadas de novo


def is_booster_set(code: str) -> bool:
    """Lorcana: os sets principais (códigos numéricos) são os que saem em booster (os outros jogos: sets.booster)."""
    return code.isdigit()


def full_code(code: str, game: str | None = None) -> str:
    """"fra" com o jogo magic → "mtg-fra"; um código que já tem o prefixo (ou de Lorcana) fica como está."""
    code = code.strip()
    if game and game != "lorcana" and not code.startswith(games.get(game).prefix):
        code = games.get(game).prefix + code.lower() if game == "magic" else games.get(game).prefix + code
    return code


def active_sets(con, game: str) -> list[str]:
    """Os sets de Magic ou Pokémon que já foram baixados (têm cartas no catálogo)."""
    return [r[0] for r in con.execute("SELECT DISTINCT set_code FROM cards WHERE game = ? ORDER BY set_code", (game,))]


def fetch_cards(con, code: str, langs: tuple[str, ...] = ("en", "pt")) -> list[dict]:
    """As cartas de um set, já no formato da tabela, da fonte do jogo dele (Lorcast, Scryfall ou TCGdex)."""
    game = games.of_code(code)
    if game is games.MAGIC:
        return scryfall.fetch_set_cards(code.removeprefix(game.prefix), langs)
    if game is games.POKEMON:
        return tcgdex.fetch_set_cards(code.removeprefix(game.prefix), langs)
    row = con.execute("SELECT id FROM sets WHERE code = ?", (code,)).fetchone()
    return [db.lorcast_row(c) for c in lorcast.fetch_set_cards(row["id"] if row else code)]


def sync(settings: Settings, set_codes: list[str] | None = None, images: bool = True, game: str | None = None) -> None:
    """Catálogo, preços, imagens e índices. Sem `game` nem `set_codes`: os três jogos (Lorcana inteiro; de Magic e
    Pokémon, a lista de sets e os sets já baixados). Com `set_codes`: esses sets (e só os jogos deles)."""
    con = db.connect(settings.db_path)
    wanted = [full_code(c, game) for c in set_codes] if set_codes else None
    todo = [game] if game else sorted({games.of_code(c).key for c in wanted}) if wanted else list(games.GAMES)
    for key in todo:
        codes = [c for c in wanted if games.of_code(c).key == key] if wanted else None
        (_sync_lorcana if key == "lorcana" else _sync_other)(settings, con, key, codes, images)
    db.record_value(con)  # preços novos: um ponto no gráfico de valor da coleção por data
    try:
        synced = sync_set_icons(settings, con)
        print(f"Ícones de set: {synced} novos" if synced else "Ícones de set: em dia")
    except (OSError, ValueError, KeyError) as e:  # tcgcsv fora do ar não invalida o catálogo já sincronizado
        print(f"Ícones de set: não consegui buscar agora ({e}); ficam os que já existem")
    print("Sync concluído.")


def _sync_lorcana(settings: Settings, con, game: str, set_codes: list[str] | None, images: bool) -> None:
    fetched_at = db.now()
    sets = lorcast.fetch_sets()
    print(f"Lorcana: {len(sets)} sets no Lorcast")
    for s in sets:
        cards = lorcast.fetch_set_cards(s["id"])
        db.upsert_set(con, {**s, "game": "lorcana", "booster": is_booster_set(s["code"]), "abbr": s["code"]})
        db.upsert_cards(con, cards, fetched_at)
        con.commit()
        print(f"  {s['code']:>8}  {s['name']:<42} {len(cards):>4} cartas")
    if not images:
        return
    known = {s["code"] for s in sets}
    for code in set_codes or [s["code"] for s in sets if is_booster_set(s["code"])]:
        if code not in known:
            raise SystemExit(f"Set {code!r} não existe no Lorcast.")
        prepare_set(settings, con, code)


def _sync_other(settings: Settings, con, game: str, set_codes: list[str] | None, images: bool) -> None:
    """Magic e Pokémon: a lista de sets sempre; as cartas dos sets pedidos e dos já baixados."""
    g = games.get(game)
    known = {r["code"]: r for r in con.execute("SELECT * FROM sets WHERE game = ?", (game,))}
    sets = scryfall.fetch_sets() if g is games.MAGIC else tcgdex.fetch_sets(known)
    with con:
        for s in sets:
            db.upsert_set(con, s)
    print(f"{g.short}: {len(sets)} sets ({sum(bool(s['booster']) for s in sets)} de booster)")
    codes = {s["code"] for s in sets}
    targets = set_codes or active_sets(con, game)
    for code in targets:
        if code not in codes:
            raise SystemExit(f"Set {code!r} não existe no catálogo de {g.short}.")
        rows = fetch_cards(con, code, g.langs)
        with con:
            db.upsert_card_rows(con, rows, db.now())
        pt = sum(1 for r in rows if r.get("image_pt"))
        print(f"  {code:>14}  {len(rows):>4} cartas" + (f" ({pt} com imagem em português)" if pt else ""), flush=True)
        if images:
            prepare_set(settings, con, code)


def prepare_set(settings: Settings, con, code: str) -> None:
    """Baixa as imagens do set (as duas línguas, quando há) e monta o índice de reconhecimento."""
    entries = []
    for r in db.cards_in_set(con, code):
        if r["image_normal"]:
            entries.append((r["id"], "en", r["image_normal"]))
        if r["image_pt"]:
            entries.append((r["id"], "pt", r["image_pt"]))
    missing = [e for e in entries if not image_path(settings, code, e[0], e[1]).exists()]
    if missing:
        print(f"Set {code}: baixando {len(missing)} imagens...", flush=True)

        def get(e):
            try:
                lorcast.download(e[2], image_path(settings, code, e[0], e[1]))
            except OSError as err:  # uma imagem que falha não derruba o set (ela fica de fora do índice)
                print(f"  imagem de {e[0]} ({e[1]}): {err}", flush=True)

        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(get, missing))
    pairs = [(cid, lang) for cid, lang, _ in entries]
    if not index_is_fresh(settings, code, pairs):
        print(f"Set {code}: indexando features...", flush=True)
        build_set_index(settings, code, pairs)


def icon_path(settings: Settings, code: str) -> Path:
    return settings.cache_dir / "sets" / f"{re.sub(r'[^A-Za-z0-9_-]', '_', code)}.png"


PACK_NAMES = (r"Play Booster$", r"Booster Pack$", r"Draft Booster( Pack)?$", r"Set Booster( Pack)?$")
NOT_A_PACK = re.compile(r"Sleeved|Bundle|Box|Case|Display|Collector|Code Card|Omega|Jumpstart|Set of", re.I)


def is_booster_name(name: str) -> bool:
    """O booster avulso ("Booster Pack", "Play Booster"...), não o "sleeved", a caixa, o display ou os kits."""
    return any(re.search(p, name) for p in PACK_NAMES) and not NOT_A_PACK.search(name)


def booster_pack(products: list[dict]) -> dict | None:
    """O booster avulso do set (no Magic de hoje, o Play Booster; antes, o Booster Pack)."""
    for pattern in PACK_NAMES:
        for p in products:
            name = p.get("name", "")
            if re.search(pattern, name) and not NOT_A_PACK.search(name):
                return p
    return None


def cutout(data: bytes) -> Image.Image:
    """Foto de produto em fundo branco → PNG com fundo transparente, recortado no produto.

    Só sai o branco ligado à borda da foto: partes brancas dentro da arte do booster ficam.
    """
    rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    white = (rgb.min(axis=2) > 232).astype(np.uint8)
    _, labels = cv2.connectedComponents(white, connectivity=4)
    edge = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    background = np.isin(labels, [label for label in np.unique(edge) if label != 0])
    alpha = cv2.GaussianBlur(np.where(background, 0, 255).astype(np.uint8), (3, 3), 0)  # borda suave
    img = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
    img = img.crop(img.getbbox())
    img.thumbnail((ICON_MAX, ICON_MAX), Image.LANCZOS)
    return img


def thumb_path(settings: Settings, code: str) -> Path:
    return icon_path(settings, code).with_suffix(".thumb.webp")


def _thumb(settings: Settings, code: str, img: Image.Image) -> None:
    small = img.copy()
    small.thumbnail((THUMB_MAX, THUMB_MAX), Image.LANCZOS)
    small.save(thumb_path(settings, code), quality=88, method=6)


def _store_icon(settings: Settings, con, code: str, img: Image.Image, source: str) -> None:
    dest = icon_path(settings, code)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    _thumb(settings, code, img)
    with con:
        con.execute("UPDATE sets SET icon = ?, icon_source = ? WHERE code = ?",
                    (os.path.relpath(dest, settings.root), source, code))


def sync_set_icons(settings: Settings, con, only: list[str] | None = None, force: bool = False) -> int:
    """Baixa a foto do booster de cada set que ainda não tem ícone; ícone enviado à mão não é trocado. De Magic e
    Pokémon, só os sets já baixados (são centenas)."""
    done = 0
    for game in games.GAMES:
        rows = con.execute("SELECT * FROM sets WHERE game = ?" + ("" if game == "lorcana" else
                           " AND code IN (SELECT DISTINCT set_code FROM cards WHERE game = sets.game)"), (game,)).fetchall()
        rows = [r for r in rows if not only or r["code"] in only]
        if rows:
            done += _sync_icons(settings, con, game, rows, bool(only), force)
    return done


def _sync_icons(settings: Settings, con, game: str, sets: list, strict: bool, force: bool) -> int:
    groups = json.loads(lorcast.fetch(f"{tcgcsv(game)}/groups"))["results"]
    by_abbr = {g["abbreviation"].lower(): g for g in groups if g.get("abbreviation")}
    done = 0
    for s in sets:
        group = by_abbr.get((s["abbr"] or s["code"]).lower())
        if group is None:
            continue
        with con:
            con.execute("UPDATE sets SET tcg_group_id = ? WHERE code = ?", (group["groupId"], s["code"]))
        has_icon = s["icon"] and (settings.root / s["icon"]).exists()
        if has_icon and not thumb_path(settings, s["code"]).exists():
            _thumb(settings, s["code"], Image.open(settings.root / s["icon"]))  # ícones de antes da miniatura
        if has_icon and s["icon_source"] == "tcgplayer" and max(Image.open(settings.root / s["icon"]).size) == LEGACY_MAX:
            has_icon = False  # foto guardada reduzida (versão antiga): baixa de novo em resolução cheia
        if has_icon and (s["icon_source"] == "manual" or not force):
            continue  # ícone enviado à mão só sai pelo "usar a foto do booster" (ou se o arquivo sumir)
        try:
            pack = booster_pack(json.loads(lorcast.fetch(f"{tcgcsv(game)}/{group['groupId']}/products"))["results"])
            if not pack or not pack.get("imageUrl"):
                continue  # set sem booster avulso (promos, quests): a página usa o selo com o número
            image = lorcast.fetch(pack["imageUrl"].replace("_200w.", "_in_1000x1000."))
            _store_icon(settings, con, s["code"], cutout(image), "tcgplayer")
        except (OSError, ValueError, KeyError) as e:
            if strict:
                raise
            print(f"  ícone do set {s['code']}: falhou ({e})", flush=True)  # os outros sets seguem
            continue
        print(f"  ícone do set {s['code']}: {pack['name']}", flush=True)
        done += 1
    return done


def save_manual_icon(settings: Settings, code: str, data: bytes) -> None:
    """Ícone escolhido pelo usuário (ex.: o logo do set); a sincronização não o substitui."""
    con = db.connect(settings.db_path)
    if con.execute("SELECT 1 FROM sets WHERE code = ?", (code,)).fetchone() is None:
        raise LookupError(f"Set {code} não existe.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ValueError("O arquivo não é uma imagem válida.") from e
    img = img.convert("RGBA")
    img.thumbnail((ICON_MAX, ICON_MAX), Image.LANCZOS)
    _store_icon(settings, con, code, img, "manual")


def reset_icon(settings: Settings, code: str) -> None:
    """Volta ao ícone automático (a foto do booster), se o set tiver um."""
    con = db.connect(settings.db_path)
    icon_path(settings, code).unlink(missing_ok=True)
    thumb_path(settings, code).unlink(missing_ok=True)
    with con:
        con.execute("UPDATE sets SET icon = NULL, icon_source = NULL WHERE code = ?", (code,))
    sync_set_icons(settings, con, only=[code], force=True)


def refresh_prices(settings: Settings, set_codes: list[str], progress=lambda fraction, message=None: None) -> dict:
    """Atualiza os preços de hoje dos sets; o preço de cada carta na abertura fica como estava.

    Lorcana e Magic: uma consulta por set (ela traz todas as cartas do set com o preço), não uma por carta. Pokémon:
    o TCGdex dá o preço carta a carta (em paralelo)."""
    con = db.connect(settings.db_path)
    fetched_at = db.now()
    done = []
    for i, code in enumerate(set_codes):
        row = con.execute("SELECT id, name FROM sets WHERE code = ?", (code,)).fetchone()
        if row is None:
            continue
        progress(i / max(1, len(set_codes)), f"Set {i + 1} de {len(set_codes)}: {row['name']}")
        rows = fetch_cards(con, code, langs=())  # só os preços: o nome e a imagem em português ficam como estão
        with con:
            db.upsert_card_rows(con, rows, fetched_at)
        done.append(code)
    db.record_value(con)  # um ponto no gráfico de valor da coleção por data
    return {"updated_at": fetched_at, "sets": done}
