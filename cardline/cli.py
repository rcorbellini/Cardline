"""cardline: identifica as cartas abertas num vídeo, precifica, cataloga e gera o vídeo com overlay.

Fluxo comum:  cardline sync           (uma vez; depois só para atualizar o catálogo)
              cardline serve          (página com a coleção, as pipelines e o upload de vídeos)
         ou   cardline process videos/abertura.mp4 --paid 35
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import Settings, load_settings


def _sets_arg(value: str | None) -> list[str] | None:
    return [s.strip() for s in value.split(",") if s.strip()] if value else None


def cmd_sync(s: Settings, a: argparse.Namespace) -> None:
    from .catalog import sync

    sync(s, _sets_arg(a.sets), images=not a.no_images)


def cmd_serve(s: Settings, a: argparse.Namespace) -> None:
    from .server import serve

    serve(s, a.host, a.port)


def cmd_process(s: Settings, a: argparse.Namespace) -> None:
    from .pipeline import DuplicateVideo, create_run, execute

    try:
        run_id = create_run(
            s, Path(a.video), paid=a.paid, paid_currency=a.paid_currency, set_hint=a.set,
            overlay=not a.no_overlay, verify=a.verify, currency=a.currency,
            kind="cadastro" if a.cadastro else "abertura", narration=a.narrar,
        )
    except DuplicateVideo as e:
        raise SystemExit(f"{e} Para reprocessar: cardline run {e.run_id} --from scan") from e
    print(f"Pipeline #{run_id}")
    if not execute(s, run_id):
        raise SystemExit(1)
    print(f"Pronto: runs/{run_id}/ · coleção atualizada")


def cmd_run(s: Settings, a: argparse.Namespace) -> None:
    from .pipeline import execute

    if not execute(s, a.id, a.from_step):
        raise SystemExit(1)


def cmd_list(s: Settings, a: argparse.Namespace) -> None:
    from . import db
    from .collection import load_scan

    con = db.connect(s.db_path)
    for r in con.execute("SELECT * FROM runs ORDER BY id"):
        folder = s.root / r["dir"]
        cards = load_scan(folder)["cards"] if (folder / "scan.json").exists() else []
        value = sum(c.get("price_usd") or 0 for c in cards)
        paid = f"pago {r['paid']:.2f} {r['paid_currency']}" if r["paid"] else "sem valor pago"
        print(f"#{r['id']:<4} {r['kind']:<9} {r['status']:<11} {r['created_at'][:16]}  {r['video_name']:<34} "
              f"{len(cards):>3} cartas  US$ {value:7.2f}  {paid}")


def cmd_edit(s: Settings, a: argparse.Namespace) -> None:
    from . import db
    from .collection import edit_scan
    from .pipeline import mark_stale

    run = db.connect(s.db_path).execute("SELECT * FROM runs WHERE id = ?", (a.id,)).fetchone()
    if run is None:
        raise SystemExit(f"Pipeline #{a.id} não existe.")
    print(edit_scan(s, s.root / run["dir"], a.n, card_ref=a.card, foil=a.foil, remove=a.remove, add_at=a.at))
    mark_stale(s, a.id, "prices")
    print(f"Preço, coleção e vídeo ficaram desatualizados. Para aplicar: cardline run {a.id}"
          " (ou 'Rodar o resto' na página)")


def cmd_voice(s: Settings, a: argparse.Namespace) -> None:
    from .narration import load_xtts, make_samples, unavailable

    if reason := unavailable():
        raise SystemExit(f"Narração indisponível: {reason}")
    if a.amostras:
        found = make_samples(s, lambda n, total, name: print(f"  {n + 1}/{total} {name}", flush=True))
        print(f"{len(found)} amostras em {s.cache_dir / 'vozes'}")
        return
    try:
        tts = load_xtts()
    except RuntimeError as e:
        raise SystemExit(str(e)) from e
    print(f"Voz pronta. Em uso: {s.narration_voice} (narration_voice no cardline.toml)")
    print("Vozes do XTTS-v2:", ", ".join(tts.speakers))


def cmd_add(s: Settings, a: argparse.Namespace) -> None:
    from .collection import add

    add(s, a.card, a.foil, a.qty)


def cmd_google(s: Settings, a: argparse.Namespace) -> None:
    """Salva o cliente OAuth do Google: o mesmo serve para entrar na página e para conectar o YouTube."""
    from getpass import getpass

    from . import youtube

    client_id = a.client_id or input("ID do cliente (…apps.googleusercontent.com): ")
    try:
        youtube.save_client(s, client_id, getpass("Chave secreta do cliente (não aparece ao digitar): "))
    except ValueError as e:
        raise SystemExit(str(e)) from e
    print(f"Cliente salvo em {youtube.folder(s) / 'client.json'} (fora do git). Já dá para entrar na página.")


def main(argv: list[str] | None = None) -> None:
    from .pipeline import STEP_NAMES

    p = argparse.ArgumentParser(prog="cardline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="comando")

    sp = sub.add_parser("sync", help="atualiza catálogo e preços; baixa imagens, indexa os sets e busca os ícones")
    sp.add_argument("--sets", help="sets a indexar, ex.: 1,2,5 (padrão: todos os sets de booster)")
    sp.add_argument("--no-images", action="store_true", help="só catálogo e preços (rápido)")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("serve", help="abre a página local: coleção, pipelines e upload de vídeos")
    sp.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 para acessar de outro aparelho na rede")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("process", help="cria uma pipeline para um vídeo e roda tudo (sem o servidor)")
    sp.add_argument("video")
    sp.add_argument("--cadastro", action="store_true",
                    help="cadastro de coleção: só identifica e registra as cartas (sem valor pago nem vídeo)")
    sp.add_argument("--paid", type=float, help="valor pago pelos boosters do vídeo")
    sp.add_argument("--paid-currency", default="BRL", choices=["BRL", "USD"], help="moeda do valor pago (padrão BRL)")
    sp.add_argument("--set", help="set(s) do booster, ex.: 1 (padrão: detecta automaticamente)")
    sp.add_argument("--no-overlay", action="store_true", help="não gera o vídeo com overlay")
    sp.add_argument("--verify", action=argparse.BooleanOptionalAction, default=None,
                    help="confere as cartas com o modelo de visão do Ollama (padrão: verify_model do cardline.toml)")
    sp.add_argument("--currency", choices=["USD", "BRL"], help="moeda do overlay (padrão: cardline.toml)")
    sp.add_argument("--narrar", action="store_true", help="narra o vídeo com overlay (precisa do extra narracao)")
    sp.set_defaults(func=cmd_process)

    sp = sub.add_parser("run", help="executa uma pipeline existente, de onde parou ou a partir de um passo")
    sp.add_argument("id", type=int)
    sp.add_argument("--from", dest="from_step", choices=STEP_NAMES, help="passo inicial")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("list", help="lista as pipelines")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("edit", help="corrige uma carta identificada (depois rode `cardline run ID`)",
                        description="Exemplos:\n"
                                    "  cardline edit 4 3 --card 1/24        carta #3 da pipeline 4 é a 1/24\n"
                                    "  cardline edit 4 12 --foil            carta #12 é foil\n"
                                    "  cardline edit 4 5 --remove           #5 não é uma carta aberta\n"
                                    "  cardline edit 4 --card 1/55 --at 9.2 faltou uma carta aos 9,2 s",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("id", type=int, help="número da pipeline")
    sp.add_argument("n", nargs="?", type=int, help="número da carta (#) na pipeline")
    sp.add_argument("--card", help="carta certa: SET/NÚM (ex.: 1/169), id do Lorcast ou nome")
    sp.add_argument("--foil", action=argparse.BooleanOptionalAction, default=None, help="marca/desmarca como foil")
    sp.add_argument("--remove", action="store_true", help="remove a carta")
    sp.add_argument("--at", type=float, help="insere --card como carta nova nesse instante do vídeo (segundos)")
    sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser("voz", help="prepara a voz da narração (baixa na primeira vez) e lista as vozes")
    sp.add_argument("--amostras", action="store_true", help="grava uma amostra de cada voz, para ouvir na página")
    sp.set_defaults(func=cmd_voice)

    sp = sub.add_parser("add", help="adiciona à coleção uma carta que não veio de vídeo")
    sp.add_argument("card", help="SET/NÚM (ex.: 1/169), id do Lorcast ou nome")
    sp.add_argument("--foil", action="store_true")
    sp.add_argument("--qty", type=int, default=1)
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("google", help="configura o cliente OAuth do Google (entrar na página e conectar o YouTube)")
    sp.add_argument("client_id", nargs="?", help="ID do cliente (a chave secreta é pedida sem aparecer na tela)")
    sp.set_defaults(func=cmd_google)

    a = p.parse_args(argv)
    a.func(load_settings(), a)


if __name__ == "__main__":
    main()
