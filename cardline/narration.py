"""Narração do vídeo com overlay: roteiro engraçado e trágico, sem spoiler, lido por uma voz em português.

O roteiro tem uma estrutura fixa, que garante o tempo e que o resultado só apareça no fim: a aposta (o valor
pago), a reação às cartas raras, a falsa esperança no meio e o desfecho depois do resumo ("Eu avisei.").
Um modelo local do Ollama escreve as piadas de algumas cartas, guiado por exemplos, e piada que foge das
regras é descartada. O roteiro fica em runs/<id>/narracao/roteiro.json e pode ser editado na página.

A voz é o XTTS-v2 (Coqui), na CPU. Como é um modelo generativo, uma fala às vezes sai errada: o Whisper
transcreve cada tomada e fica a que diz o texto certo. As tomadas ficam em cache pelo texto, então editar
uma fala refaz só ela. O som original do vídeo abaixa enquanto o narrador fala.

A voz depende do extra `narracao` (uv sync --extra narracao). Sem ele, o resto do cardline funciona igual;
este módulo só importa torch, XTTS e Whisper dentro das funções que narram.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import subprocess
import sys
import unicodedata
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .collection import load_scan
from .config import Settings
from .rarity import label as rarity_label
from .video import ffmpeg_exe, probe

XTTS = "tts_models/multilingual/multi-dataset/xtts_v2"
REQUIRED = ("TTS", "faster_whisper", "num2words", "torch", "torchaudio")
SPEED = 1.08  # o XTTS fala devagar; um pouco mais rápido cabe melhor entre as cartas
TAKES = 3  # tomadas por fala, no máximo
GOOD = 0.85  # quanto do texto o Whisper precisa reconhecer para a tomada valer de primeira
GAP = 0.12  # respiro mínimo entre duas falas
SR = 48000
DUCK_DB = -14.0  # quanto o som original abaixa durante a fala
ATTACK, RELEASE, RAMP = 0.15, 0.35, 0.12
TAIL = 1.2  # pausa depois da última fala; se o vídeo acabar antes, o último quadro fica mais tempo
MAX_LINES, MAX_CHARS = 20, 160
LICENSE_MSG = (
    "A voz (XTTS-v2, 1,9 GB) ainda não foi baixada. Ela usa a licença CPML, só para uso não comercial "
    "(https://coqui.ai/cpml). Para aceitar e baixar, rode uma vez: COQUI_TOS_AGREED=1 uv run cardline voz"
)

# --- texto ------------------------------------------------------------------------------------

# como a voz deve ler (a página mostra o original); R inicial no Brasil soa como o H aspirado do "Hakuna"
PRONUNCIATION = {r"\bFacilier\b": "Facilliê", r"\bLeFou\b": "Lefú", r"\bHakuna\b": "Rakuna"}


def plain(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return re.sub(r"[^a-z0-9 ]+", " ", "".join(c for c in text if unicodedata.category(c) != "Mn"))


def words(text: str) -> list[str]:
    return plain(text).split()


def money_words(value: float, currency: str) -> str:
    """45 → "quarenta e cinco reais"; 9,04 USD → "nove dólares e quatro centavos"."""
    from num2words import num2words

    units, cents = int(value), round((value - int(value)) * 100)
    if cents == 100:
        units, cents = units + 1, 0
    one, many = ("real", "reais") if currency == "BRL" else ("dólar", "dólares")
    parts = [f"{num2words(units, lang='pt_BR')} {one if units == 1 else many}"] if units or not cents else []
    if cents:
        parts.append(f"{num2words(cents, lang='pt_BR')} centavo{'' if cents == 1 else 's'}")
    return " e ".join(parts)


def spell(text: str) -> str:
    """Números e valores por extenso (a voz lê "R$ 45" mal; o Whisper escreve "45" para "quarenta e cinco")."""
    try:
        from num2words import num2words
    except ImportError:
        return text

    def amount(m: re.Match) -> str:
        return money_words(float(m.group(2).replace(".", "").replace(",", ".")), "BRL" if m.group(1) == "R$" else "USD")

    text = re.sub(r"(R\$|US\$)\s?(\d[\d.]*(?:,\d{1,2})?)", amount, text)
    text = re.sub(r"\b[Dd]r\.", "Doutor", text)
    return re.sub(r"\d+", lambda m: num2words(int(m.group()), lang="pt_BR"), text)


def tts_text(text: str) -> str:
    """Texto para a voz. O XTTS lê ponto final como "ponto": no meio, pontos viram reticências (soam como pausa);
    no fim, saem (reticências no fim esticam a última palavra)."""
    text = spell(" ".join(text.replace("…", "...").split()))
    for pattern, spoken in PRONUNCIATION.items():
        text = re.sub(pattern, spoken, text)
    return re.sub(r"(?<!\.)\.(?!\.)", "...", re.sub(r"[.\s]+$", "", text))


def sound(word: str) -> str:
    """Chave de pronúncia de uma palavra, para o Whisper não ser cobrado pela grafia: "atiçar" = "atissar",
    "Hakuna" = "Acuna", "chamas" = "xamas", "mau" = "mal"."""
    w = plain(word.lower().replace("ç", "s")).strip()
    for a, b in (("ch", "x"), ("sh", "x"), ("lh", "li"), ("nh", "ni"), ("qu", "k"), ("ss", "s"), ("oo", "u"), ("y", "i"),
                 ("w", "u")):
        w = w.replace(a, b)
    w = re.sub(r"c(?=[ei])", "s", w).replace("c", "k")
    w = re.sub(r"^h|(?<=[^aeiou])h", "", w)
    w = re.sub(r"l$", "u", w)  # L final soa como U no Brasil: "mal" = "mau", "sinal" = "sinau"
    return re.sub(r"(.)\1+", r"\1", w)


def sounds(text: str) -> list[str]:
    return [sound(w) for w in words(spell(text).lower().replace("ç", "s"))]  # o ç antes de tirar os acentos


def similarity(expected: str, heard: str) -> float:
    return difflib.SequenceMatcher(None, sounds(expected), sounds(heard)).ratio()


def estimate(text: str) -> float:
    """Duração aproximada da fala (medida no XTTS a 1,08x: ~0,36 s por palavra, mais as pausas da pontuação)."""
    pauses = text.count("...") * 0.3 + len(re.findall(r"[,;:]|[.!?]+(?=\s)", text.replace("...", " "))) * 0.12
    return 0.35 + 0.36 * len(words(spell(text))) + pauses


# --- roteiro ----------------------------------------------------------------------------------

INTROS = ["{pago}. Um booster lacrado. E uma fé inabalável.", "{pago}. Um booster. E muita esperança.",
          "{pago} num booster. O que pode dar errado?"]
INTROS_PACKS = ["{pago}. {n} boosters lacrados. E uma fé inabalável.", "{pago}. {n} boosters. O que pode dar errado?"]
INTROS_FREE = ["Um booster lacrado. E uma fé inabalável.", "Um booster. E muita esperança.",
               "Mais um booster. O que pode dar errado?"]
HOPE = ["Calma. As boas vêm no final... é o que dizem.", "Respira. Ainda tem carta.", "Calma... ainda dá tempo de virar."]
OMENS = ["Comum. O universo está avisando.", "Hmm. Isso não cheira bem.", "Mais uma comum. Mau sinal."]
REACTIONS = {
    "Rare": ["Uma rara!", "Opa... uma rara!"],
    "Super_rare": ["Super rara! Agora vai!", "Super rara?! Calma, coração."],
    "Legendary": ["Lendária?! Será que eu me enganei?", "Uma lendária! A profecia treme."],
    "Epic": ["Épica?! Eu preciso sentar."],
    "Enchanted": ["Encantada?! Isso não estava na profecia!"],
    "Iconic": ["Icônica?! Isso não estava no roteiro!"],
}
ENDINGS = {
    "loss": ["Eu avisei.", "Eu avisei... desde o começo.", "A profecia se cumpriu."],
    "profit": ["Tá. Dessa vez eu errei.", "Quem diria... a profecia falhou."],
    "even": ["Empatou. Nem a profecia sabia essa."],
    "unknown": ["E assim termina mais uma abertura."],
}

JOKE_SYSTEM = (
    "Você escreve UMA fala curta de narrador para um vídeo de abertura de booster de Disney Lorcana. Estilo: "
    "narrador de trailer, dramático, engraçado e um pouco trágico, humor brasileiro, de quem pressente que o "
    "booster vai dar errado. Faça trocadilho com o personagem, a versão ou o nome da carta (traduza se a piada "
    "pedir; use os nomes como são conhecidos no Brasil, ex.: Tinker Bell = Sininho, Goofy = Pateta). Nada em "
    "inglês além do nome do personagem: traduza a versão e o nome de cartas que não são personagens. No máximo "
    "9 palavras. Sem números, sem valores, sem falar de lucro ou prejuízo, sem a palavra foil."
)
# piadas já aprovadas (o roteiro do teste); o modelo escreve só para as outras cartas
CURATED = {
    ("Timon", None): "Hakuna matata... sei.",
    ("Dr. Facilier", "Charlatan"): "Doutor Facilier, o charlatão. Pelo menos esse é sincero.",
    ("Scar", "Fiery Usurper"): "Logo de cara, o usurpador. Mau sinal.",
    ("LeFou", "Bumbler"): "LeFou. Em francês, o tolo. Indireta?",
    ("Rapunzel", "Letting Down Her Hair"): "Rapunzel, joga essa trança e me tira daqui.",
    ("Fan the Flames", None): "E essa se chama... Atiçar as Chamas.",
}
JOKE_EXAMPLES = """Exemplos (carta → fala):
- Timon, Grub Rustler (comum) → "Hakuna matata... sei."
- Dr. Facilier, Charlatan (comum) → "Doutor Facilier, o charlatão. Pelo menos esse é sincero."
- Rapunzel, Letting Down Her Hair (incomum) → "Rapunzel, joga essa trança e me tira daqui."
- Fan the Flames (ação, brilhante, última carta) → "E a última se chama... Atiçar as Chamas."
- Scar, Fiery Usurper (comum) → "Logo de cara, o usurpador. Mau sinal."
- LeFou, Bumbler (incomum) → "LeFou. Em francês, o tolo. Indireta?"
"""
BANNED = re.compile(r"\b(lucro|prejuizo|reais|real|dolar|dolares|preco|valor|resumo|total|foil|centavos?)\b")


@dataclass
class Timeline:
    cards: list[dict]  # cartas do scan.json, na ordem em que aparecem
    summary: float  # instante em que o resumo aparece no vídeo com overlay
    end: float  # fim do vídeo com overlay
    packs: int
    paid: float | None  # valor pago, na moeda em que foi pago
    paid_currency: str | None
    result: float | None  # (valor das cartas - pago) / pago
    intro: float = 0.0  # segundos de capa antes do vídeo (os instantes das cartas já contam com ela)

    @property
    def outcome(self) -> str:
        if self.result is None:
            return "unknown"
        return "loss" if self.result < -0.05 else "profit" if self.result > 0.05 else "even"


def timeline(settings: Settings, run, folder: Path) -> Timeline:
    """As cartas e os tempos no vídeo com overlay: a capa vem antes, então cada carta aparece `intro` depois."""
    from .overlay import timing

    scan = load_scan(folder)
    meta = timing(folder / "overlay.mp4")
    intro = meta.get("intro", 0.0)
    end = meta.get("end") or probe(folder / "overlay.mp4").duration
    total = sum(c.get("price_usd") or 0 for c in scan["cards"])
    return Timeline(
        cards=[{**c, "t": c["t"] + intro} for c in scan["cards"]], summary=meta.get("summary") or max(0.0, end - settings.outro_seconds),
        end=end, packs=max((c["pack"] or 1 for c in scan["cards"]), default=1), paid=run["paid"],
        paid_currency=run["paid_currency"], result=(total - run["paid_usd"]) / run["paid_usd"] if run["paid_usd"] else None,
        intro=intro,
    )


def fingerprint(tl: Timeline) -> str:
    """Muda quando muda o que o roteiro automático usa: as cartas, os instantes, o valor pago e o resultado."""
    data = [[c["card_id"], round(c["t"], 1), c["foil"], c["rarity"]] for c in tl.cards]
    data += [tl.paid, tl.paid_currency, tl.outcome, round(tl.summary, 1), round(tl.intro, 2)]
    return hashlib.sha1(json.dumps(data).encode()).hexdigest()[:12]


def clean_joke(text: str, card: dict | None = None) -> str | None:
    """Piada do modelo dentro das regras, ou None (sobra de JSON, outra língua, número, spoiler, longa demais).

    Também cai a piada que repete o inglês da carta (a versão, ou o nome de quem não é personagem): a voz em
    português lê "Grub Rustler" mal, e o Whisper não reconhece.
    """
    text = re.split(r'["”]\s*[}\]]|[{}\[\]\n]', text)[0]
    text = "".join(ch for ch in text if ord(ch) < 0x250 or ch in "…’‘“”–—")
    text = " ".join(text.replace("…", "...").strip(" \"'“”‘’").split())
    n = len(words(text))
    if not 2 <= n <= 12 or re.search(r"\d|\$", text) or BANNED.search(plain(text)):
        return None
    if card is not None:
        english = {w for w in words(card.get("version") or card["name"]) if len(w) > 3}
        if english & set(words(text)):
            return None
    return text if text[-1] in ".!?" else text + "."


def joke_candidates(tl: Timeline, limit: int) -> list[int]:
    """Cartas que rendem piada: depois da abertura, sem reação de raridade; brilhante e personagem primeiro."""
    def score(i: int):
        c = tl.cards[i]
        nxt = tl.cards[i + 1]["t"] if i + 1 < len(tl.cards) else tl.summary
        return c["foil"], bool(c.get("version")), min(nxt - c["t"], 3.0)

    idx = [i for i, c in enumerate(tl.cards) if c["rarity"] not in REACTIONS and c["t"] > 4.0]
    return sorted(sorted(idx, key=score, reverse=True)[:limit])


def write_jokes(settings: Settings, tl: Timeline, indices: list[int], seed: int, progress) -> dict[int, str]:
    """Uma piada por carta: a aprovada, se houver; senão, a do modelo do Ollama (fora do ar = sem piada)."""
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    schema = {"type": "object", "properties": {"fala": {"type": "string"}}, "required": ["fala"]}
    jokes: dict[int, str] = {}
    offline = not settings.narration_writer
    for n, i in enumerate(indices):
        progress(0.02 + 0.08 * n / len(indices), f"Escrevendo o roteiro ({n + 1}/{len(indices)})")
        c = tl.cards[i]
        if ready := curated(c, i == len(tl.cards) - 1):
            jokes[i] = ready
            continue
        if offline:
            continue
        tags = [rarity_label(c["rarity"]).lower()] + (["brilhante"] if c["foil"] else [])
        if i == len(tl.cards) - 1 or tl.cards[i + 1]["pack"] != c["pack"]:
            tags.append("última carta")
        card = f"{c['name']}{', ' + c['version'] if c.get('version') else ''} ({', '.join(tags)})"
        body = {
            "model": settings.narration_writer, "stream": False, "think": False, "format": schema,
            "options": {"temperature": 0.9, "seed": seed + i},
            "messages": [{"role": "system", "content": JOKE_SYSTEM},
                         {"role": "user", "content": f"{JOKE_EXAMPLES}\nCarta: {card}\nResponda em JSON: {{\"fala\": \"...\"}}"}],
        }
        req = urllib.request.Request(f"{host}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            content = json.loads(urllib.request.urlopen(req, timeout=120).read())["message"]["content"]
            joke = clean_joke(str(json.loads(content).get("fala", "")), c)
        except OSError as e:
            print(f"  Ollama indisponível para o roteiro ({e}); só as piadas prontas", flush=True)
            offline = True
            continue
        except (ValueError, KeyError, AttributeError):
            joke = None
        print(f"  piada para {card}: {joke or '(descartada)'}", flush=True)
        if joke:
            jokes[i] = joke
    return jokes


def curated(card: dict, last: bool) -> str | None:
    text = CURATED.get((card["name"], card.get("version") or None)) or CURATED.get((card["name"], None))
    if text and last:
        text = text.replace("E essa se chama", "E a última se chama")
    return text


def plan(tl: Timeline, jokes: dict[int, str], rng: random.Random, max_jokes: int) -> list[dict]:
    """Distribui as falas no tempo: abertura e desfecho sempre; depois raras, esperança, piadas e presságios."""
    if tl.paid:
        pago = money_words(tl.paid, tl.paid_currency or "BRL").capitalize()
        intro = rng.choice(INTROS_PACKS if tl.packs > 1 else INTROS).format(pago=pago, n=spell(str(tl.packs)).capitalize())
    else:
        intro = rng.choice(INTROS_FREE)
    placed: list[tuple[float, float, str, str]] = []  # início, fim, texto, tipo

    def place(t: float, text: str, kind: str, max_delay: float) -> bool:
        dur, start = estimate(text), t
        while start <= t + max_delay:
            clash = next((p for p in placed if start < p[1] + GAP and p[0] < start + dur + GAP), None)
            if clash is None:
                if start >= tl.summary:
                    return False  # depois que o resumo aparece, só o desfecho
                placed.append((start, start + dur, text, kind))
                return True
            start = clash[1] + GAP
        return False

    place(0.2, intro, "abertura", 0.0)
    seen: set[str] = set()
    for c in tl.cards:  # a primeira carta de cada raridade alta ganha reação
        if c["rarity"] in REACTIONS and c["rarity"] not in seen and len(seen) < 3:
            seen.add(c["rarity"])
            place(c["t"] + 0.25, rng.choice(REACTIONS[c["rarity"]]), "reacao", 1.0)
    ranked = sorted(jokes, key=lambda i: (not tl.cards[i]["foil"], tl.cards[i]["t"]))  # a brilhante primeiro
    used = 0
    for i in ranked[:1] if ranked and tl.cards[ranked[0]]["foil"] else []:
        used += place(tl.cards[i]["t"] + 0.25, jokes[i], "piada", 1.0)
    if tl.cards:  # a falsa esperança perto do meio
        middle = sorted(range(len(tl.cards)), key=lambda i: abs(i - len(tl.cards) / 2))
        hope = rng.choice(HOPE)
        any(place(tl.cards[i]["t"] + 0.25, hope, "esperanca", 0.6) for i in middle if tl.cards[i]["t"] > 5)
    for i in ranked[used:]:
        if used < max_jokes and place(tl.cards[i]["t"] + 0.25, jokes[i], "piada", 1.0):
            used += 1
    if not jokes:  # sem piadas: dois presságios genéricos em cartas comuns livres, separados
        last = -math.inf
        for text in rng.sample(OMENS, 2):
            for c in tl.cards:
                if c["rarity"] == "Common" and c["t"] > max(5.0, last + 4.0) and place(c["t"] + 0.25, text, "presagio", 0.8):
                    last = c["t"]
                    break
    # o desfecho vem com o resumo na tela, depois que a última fala termina (como no "Eu avisei" do teste);
    # se isso passar muito do fim do vídeo, a última fala opcional sai
    ending = rng.choice(ENDINGS[tl.outcome])
    while True:
        start = max(tl.summary + 0.7, max((p[1] for p in placed), default=0.0) + GAP)
        last = max(placed, key=lambda p: p[1])
        if start + estimate(ending) <= tl.end + 1.0 or last[3] in ("abertura", "reacao"):
            break
        placed.remove(last)
    placed.append((start, start + estimate(ending), ending, "desfecho"))
    return [{"t": round(p[0], 2), "texto": p[2], "tipo": p[3]} for p in sorted(placed)]


def write_script(settings: Settings, tl: Timeline, seed: int, progress) -> dict:
    rng = random.Random(seed)
    max_jokes = max(2, len(tl.cards) // 3)
    ready = {i for i, c in enumerate(tl.cards) if curated(c, False) and c["rarity"] not in REACTIONS and c["t"] > 4.0}
    jokes = write_jokes(settings, tl, sorted(set(joke_candidates(tl, max_jokes + 2)) | ready), seed, progress)
    lines = plan(tl, jokes, rng, max_jokes)
    approved = {curated(tl.cards[i], i == len(tl.cards) - 1) for i in jokes}
    wrote = any(line["tipo"] == "piada" and line["texto"] not in approved for line in lines)  # o modelo entrou no roteiro?
    return {"source": "auto", "writer": settings.narration_writer if wrote else None, "seed": seed,
            "fingerprint": fingerprint(tl), "intro": tl.intro, "lines": lines}


def script_path(folder: Path) -> Path:
    return folder / "narracao" / "roteiro.json"


def load_script(folder: Path) -> dict | None:
    path = script_path(folder)
    return json.loads(path.read_text()) if path.exists() else None


def save_script(folder: Path, script: dict) -> None:
    path = script_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(script, ensure_ascii=False, indent=1))
    tmp.replace(path)


def clean_lines(lines: list[dict]) -> list[dict]:
    """Falas editadas na página: texto sem espaços sobrando, instante válido, na ordem do vídeo."""
    out = []
    for line in lines:
        text = " ".join(str(line.get("texto", "")).split())[:MAX_CHARS]
        try:
            t = float(line.get("t", 0))
        except (TypeError, ValueError):
            continue
        if text and math.isfinite(t):
            out.append({"t": round(max(0.0, t), 2), "texto": text})
    if not out:
        raise ValueError("O roteiro precisa de pelo menos uma fala.")
    if len(out) > MAX_LINES:
        raise ValueError(f"No máximo {MAX_LINES} falas.")
    return sorted(out, key=lambda line: line["t"])


def edit_script(folder: Path, lines: list[dict]) -> None:
    old = load_script(folder) or {}
    save_script(folder, {**old, "source": "editado", "lines": clean_lines(lines)})


def discard_script(folder: Path) -> None:
    """Pede um roteiro novo: a próxima narração escreve outro, com outra semente (piadas e frases diferentes)."""
    old = load_script(folder) or {}
    save_script(folder, {"source": "auto", "seed": random.randrange(1, 1 << 30), "fingerprint": None,
                         "lines": [], "writer": old.get("writer")})


# --- voz ----------------------------------------------------------------------------------------


def unavailable() -> str | None:
    """Por que não dá para narrar neste ambiente (None = dá)."""
    if any(importlib.util.find_spec(m) is None for m in REQUIRED):
        return "instale a voz com: uv sync --extra narracao"
    return None


def voice_dir() -> Path:
    """Onde o Coqui guarda os modelos (mesma regra do TTS.utils.generic_utils.get_user_data_dir)."""
    if home := os.environ.get("TTS_HOME") or os.environ.get("XDG_DATA_HOME"):
        base = Path(home).expanduser()
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".local" / "share"
    return base / "tts" / XTTS.replace("/", "--")


def load_xtts():
    """O XTTS-v2 (baixa ~1,9 GB na primeira vez, se a licença foi aceita)."""
    if not (voice_dir() / "model.pth").exists() and os.environ.get("COQUI_TOS_AGREED") != "1":
        raise RuntimeError(LICENSE_MSG)
    import torch
    from TTS.api import TTS

    torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
    return TTS(XTTS, progress_bar=False).to("cpu")


class Voice:
    """XTTS-v2 para falar e Whisper para conferir; carregar leva ~20 s, então é uma vez por narração."""

    def __init__(self, settings: Settings):
        import torch
        from faster_whisper import WhisperModel

        self.torch = torch
        self.tts = load_xtts()
        self.sr = self.tts.synthesizer.output_sample_rate
        self.speaker = settings.narration_voice
        if self.speaker not in self.tts.speakers:
            raise RuntimeError(f"A voz {self.speaker!r} não existe no XTTS-v2 (veja as opções com: cardline voz).")
        self.whisper = WhisperModel("small", device="cpu", compute_type="int8")

    def say(self, text: str, seed: int) -> np.ndarray:
        self.torch.manual_seed(seed)
        wav = np.asarray(self.tts.tts(text=text, speaker=self.speaker, language="pt", split_sentences=False, speed=SPEED),
                         np.float32)
        return trim(wav, self.sr)

    def hear(self, wav: np.ndarray) -> str:
        segments, _ = self.whisper.transcribe(resample(wav, self.sr, 16000), language="pt")
        return " ".join(s.text for s in segments).strip()


def trim(wav: np.ndarray, sr: int, thresh: float = 0.012, pad: float = 0.06) -> np.ndarray:
    loud = np.flatnonzero(np.abs(wav) > thresh)
    if not len(loud):
        return wav
    return wav[max(0, loud[0] - int(pad * sr)):loud[-1] + int(pad * sr)]


def resample(wav: np.ndarray, sr: int, target: int) -> np.ndarray:
    if sr == target:
        return wav
    import torch
    import torchaudio.functional as AF

    return AF.resample(torch.from_numpy(np.ascontiguousarray(wav)), sr, target).numpy()


def write_wav(path: Path, wav: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((np.clip(wav, -1, 1) * 32767).astype("<i2").tobytes())


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as f:
        return np.frombuffer(f.readframes(f.getnframes()), "<i2").astype(np.float32) / 32767, f.getframerate()


def line_key(settings: Settings, text: str) -> str:
    return hashlib.sha1(f"{XTTS}|{settings.narration_voice}|{SPEED}|{tts_text(text)}".encode()).hexdigest()[:16]


def voice_lines(settings: Settings, folder: Path, lines: list[dict], progress) -> list[tuple[Path, float] | None]:
    """Grava cada fala (ou reaproveita a do cache); devolve o arquivo e a duração da tomada escolhida.

    Piada ou presságio que a voz não conseguiu dizer direito em nenhuma tomada volta como None (sai do roteiro);
    as outras falas ficam com a melhor tomada.
    """
    cache = folder / "narracao" / "falas"
    cache.mkdir(parents=True, exist_ok=True)
    voice, out = None, []
    for n, line in enumerate(lines):
        key = line_key(settings, line["texto"])
        meta = cache / f"{key}.json"
        best = json.loads(meta.read_text())["best"] if meta.exists() else None
        if best and (cache / best["file"]).exists():  # mesma fala, mesma voz: reaproveita
            out.append(None if dropped(line, best) else (cache / best["file"], best["dur"]))
            continue
        if voice is None:
            progress(0.1, "Carregando a voz")
            voice = Voice(settings)
        slot = lines[n + 1]["t"] - line["t"] - GAP if n + 1 < len(lines) else math.inf
        takes = []
        for k in range(TAKES):
            progress(0.1 + 0.8 * n / len(lines), f"Narrando a fala {n + 1} de {len(lines)}" + (f" (tomada {k + 1})" if k else ""))
            wav = voice.say(tts_text(line["texto"]), seed=int(key[:8], 16) + k)
            heard = voice.hear(wav)
            take = {"file": f"{key}_{k + 1}.wav", "dur": round(len(wav) / voice.sr, 3),
                    "sim": round(similarity(line["texto"], heard), 3), "heard": heard}
            write_wav(cache / take["file"], wav, voice.sr)
            takes.append(take)
            print(f"  fala {n + 1}, tomada {k + 1}: {take['dur']:.2f}s, {take['sim']:.0%} reconhecido «{heard}»", flush=True)
            if take["sim"] >= GOOD and take["dur"] <= slot + 0.4:  # um pouco além: a próxima fala espera
                break
        # a mais fiel ao texto; no empate (5 pontos), a que cabe até a próxima fala e, depois, a mais curta
        best = max(takes, key=lambda t: (round(t["sim"] * 20), t["dur"] <= slot + 0.4, -t["dur"]))
        meta.write_text(json.dumps({"texto": line["texto"], "takes": takes, "best": best}, ensure_ascii=False, indent=1))
        out.append(None if dropped(line, best) else (cache / best["file"], best["dur"]))
    return out


def dropped(line: dict, best: dict) -> bool:
    return line.get("tipo") in ("piada", "presagio") and best["sim"] < 0.7


# --- mixagem ------------------------------------------------------------------------------------


def schedule(lines: list[dict], durations: list[float]) -> list[tuple[float, float]]:
    """Cada fala no seu instante; se a anterior ainda não acabou, ela espera."""
    out, end = [], -math.inf
    for line, dur in zip(lines, durations):
        start = max(line["t"], end + GAP)
        end = start + dur
        out.append((start, end))
    return out


def ducking(n: int, segments: list[tuple[float, float]], sr: int = SR) -> np.ndarray:
    """Ganho do som original: abaixa um pouco antes de cada fala e volta um pouco depois, com rampa."""
    gain = np.ones(n, np.float32)
    for start, end in segments:
        gain[max(0, int((start - ATTACK) * sr)):max(0, min(n, int((end + RELEASE) * sr)))] = 10 ** (DUCK_DB / 20)
    k = max(1, int(RAMP * sr))
    c = np.cumsum(np.pad(gain, (k // 2, k - k // 2), mode="edge"), dtype=np.float64)
    return ((c[k:] - c[:-k]) / k).astype(np.float32)


def mix(overlay: Path, out: Path, clips: list[tuple[Path, float]], segments: list[tuple[float, float]], end: float) -> float:
    """Narração + som original (abaixado) sobre o vídeo; devolve quantos segundos o último quadro ganhou."""
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(overlay), "-vn", "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    orig = np.frombuffer(raw, np.float32).reshape(-1, 2).copy() if raw else np.zeros((int(end * SR), 2), np.float32)
    extra = max(0.0, segments[-1][1] + TAIL - len(orig) / SR) if segments else 0.0
    orig = np.pad(orig, ((0, int(extra * SR)), (0, 0)))
    voice = np.zeros(len(orig), np.float32)
    for (path, _), (start, _) in zip(clips, segments):
        wav, sr = read_wav(path)
        wav = resample(wav, sr, SR)
        wav *= 0.1 / max(1e-6, float(np.sqrt(np.mean(wav ** 2))))  # ~ -20 dBFS: todas as falas no mesmo volume
        a = int(start * SR)
        voice[a:a + len(wav)] += wav[: max(0, len(voice) - a)]
    mixed = orig * ducking(len(orig), segments)[:, None] + voice[:, None]
    mixed *= min(1.0, 0.98 / max(1e-6, float(np.abs(mixed).max())))
    track = out.with_suffix(".wav")
    with wave.open(str(track), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((mixed * 32767).astype("<i2").tobytes())
    video = (["-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={extra:.2f}[v]", "-map", "[v]",
              "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p"] if extra
             else ["-map", "0:v", "-c:v", "copy"])  # sem extensão, a imagem nem é recodificada
    tmp = out.with_name(out.stem + ".part.mp4")
    try:
        subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-i", str(overlay), "-i", str(track), *video, "-map", "1:a",
                        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k", "-ar", str(SR),
                        "-movflags", "+faststart", str(tmp)], check=True, capture_output=True)
        tmp.replace(out)
    finally:
        track.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
    return extra


def follow_intro(folder: Path, script: dict, intro: float) -> dict:
    """A capa mudou depois que o roteiro foi editado: as falas andam junto, para cada uma continuar na mesma carta."""
    if script["source"] != "editado" or script.get("intro", 0.0) == intro:
        return script
    delta = intro - script.get("intro", 0.0)
    script = {**script, "intro": intro,
              "lines": [{**line, "t": round(max(0.0, line["t"] + delta), 2)} for line in script["lines"]]}
    save_script(folder, script)
    return script


def narrate(settings: Settings, run, folder: Path, progress) -> str:
    """Passo "narrar": roteiro (escreve se precisar), voz de cada fala e o vídeo narrado (narrado.mp4)."""
    if reason := unavailable():
        raise RuntimeError(f"Narração indisponível: {reason}")
    tl = timeline(settings, run, folder)
    script = load_script(folder)
    if script:
        script = follow_intro(folder, script, tl.intro)
    if script is None or (script["source"] == "auto" and script.get("fingerprint") != fingerprint(tl)):
        progress(0.01, "Escrevendo o roteiro")
        script = write_script(settings, tl, script["seed"] if script else run["id"], progress)
        save_script(folder, script)
    clips = voice_lines(settings, folder, script["lines"], progress)
    if None in clips:  # a voz não acertou alguma piada: ela sai do roteiro (a página mostra o que ficou)
        script["lines"] = [line for line, clip in zip(script["lines"], clips) if clip]
        save_script(folder, script)
    clips = [clip for clip in clips if clip]
    lines = script["lines"]
    segments = schedule(lines, [dur for _, dur in clips])
    progress(0.92, "Mixando a narração com o vídeo")
    extra = mix(folder / "overlay.mp4", folder / "narrado.mp4", clips, segments, tl.end)
    who = ("roteiro editado" if script["source"] == "editado"
           else f"roteiro com piadas do {script['writer']}" if script.get("writer") else "roteiro automático")
    late = max((start - line["t"] for line, (start, _) in zip(lines, segments)), default=0.0)
    msg = f"{len(lines)} falas · {who}"
    if late > 1.5:
        msg += f" · uma fala atrasou {late:.1f}s (a anterior é longa)"
    return msg + (f" · último quadro +{extra:.1f}s" if extra else "")
