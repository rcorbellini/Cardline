import json
import subprocess

import numpy as np
import pytest
from PIL import Image

from cardline import db, narration
from cardline.collection import save_scan
from cardline.config import Settings
from cardline.money import Money
from cardline.overlay import Overlay, render, timing
from cardline.video import ffmpeg_exe, probe


def card(t, name, rarity="Common", price=0.1, pack=1):
    return {"card_id": f"crd_{name}", "t": t, "name": name, "version": None, "rarity": rarity, "foil": False,
            "pack": pack, "set": "1", "price_usd": price, "quad": None}


def booster():
    img = Image.new("RGBA", (300, 520), (0, 0, 0, 0))
    img.paste((200, 40, 60, 255), (20, 20, 280, 500))
    return img


@pytest.fixture
def overlay():
    scan = {"cards": [card(1.0, "Mickey"), card(1.6, "Stitch", "Rare", 0.9)]}
    return Overlay(scan, Money("BRL", 5.0), (360, 640), 12, paid_usd=8.0, icons={"1": booster()},
                   names={"1": "The First Chapter"}, paid=(40.0, "BRL"), intro=3.0)


def test_cover_starts_on_the_first_frame_and_lands_on_the_video_with_the_panel(overlay):
    first = np.random.default_rng(1).integers(0, 255, (640, 360, 3), dtype=np.uint8)
    dim = first.astype(np.float32) * 0.5
    assert np.array_equal(overlay.intro_frame(first.astype(np.float32), dim, 0.0), first)  # nada por cima ainda
    middle = overlay.intro_frame(first.astype(np.float32), dim, 1.5)
    assert np.abs(middle.astype(int) - first).mean() > 20  # booster, painel e fundo escurecido
    # no fim, o booster pousou no ícone do painel: igual ao primeiro quadro do vídeo com o painel desenhado
    video_start = first.copy()
    overlay.draw(video_start, 0.0)
    end = overlay.intro_frame(first.astype(np.float32), dim, 3.0)
    assert np.abs(end.astype(int) - video_start).mean() < 1.5


def test_applause_comes_when_the_running_total_reaches_the_paid_value():
    scan = {"cards": [card(1.0, "A", price=3.0), card(2.0, "B", price=3.0), card(3.0, "C", price=4.0)]}
    paid = lambda usd: Overlay(scan, Money("BRL", 5.0), (360, 640), 12, paid_usd=usd)  # noqa: E731
    from cardline.overlay import COUNT_UP, FLY, POP_IN
    arrive = lambda t: t + POP_IN + FLY  # noqa: E731
    # pagou 5: a primeira carta (3) não paga; a segunda leva a soma a 6, e os 5 são alcançados no meio da contagem
    assert paid(5.0).celebration_time() == pytest.approx(arrive(2.0) + COUNT_UP * 2 / 3)
    assert paid(3.0).celebration_time() == pytest.approx(arrive(1.0) + COUNT_UP)
    assert paid(11.0).celebration_time() is None  # o booster não se pagou: sem aplausos
    assert paid(None).celebration_time() is None


def test_cover_shows_the_value_as_informed(overlay):
    sprites = overlay._cover_sprites()
    assert sprites["images"][0].height > 0.35 * 640  # o booster é o destaque
    no_paid = Overlay({"cards": [card(1.0, "Mickey")]}, Money("BRL", 5.0), (360, 640), 12, icons={}, intro=3.0)
    assert no_paid._cover_sprites()["panel"].height < sprites["panel"].height  # sem valor, só o set


def test_render_puts_the_cover_before_the_video_and_delays_the_sound(tmp_path):
    settings = Settings(root=tmp_path, output_short_side=360, intro_seconds=1.0, outro_seconds=1.0)
    source = tmp_path / "abertura.mp4"
    subprocess.run([ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=30:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(source)], check=True)
    con = db.connect(settings.db_path)
    db.upsert_set(con, {"code": "1", "id": "set_1", "name": "The First Chapter", "released_at": "2023-08-18"})
    con.commit()
    scan = {"video": "abertura.mp4", "cards": [card(0.5, "Mickey"), card(1.2, "Stitch", "Rare", 0.9)]}
    out = tmp_path / "overlay.mp4"
    render(settings, scan, out, Money("BRL", 5.0), lambda *a: None, paid_usd=8.0, paid=40.0, paid_currency="BRL")

    meta = timing(out)
    assert meta["intro"] == 1.0 and meta["end"] == pytest.approx(meta["summary"] + 1.0)
    assert meta["sounds"] == [1.5, 2.2]  # o "ka-ching" de cada carta, contando a capa
    assert meta["celebration"] is None  # as cartas valem US$ 1,00 e o booster custou US$ 8: sem aplausos
    assert probe(out).duration == pytest.approx(meta["end"], abs=0.15)
    assert out.with_suffix(".jpg").exists()  # a capa vira a imagem do vídeo na página
    pcm = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(out), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, np.float32)
    rms = lambda a, b: float(np.sqrt(np.mean(audio[int(a * 16000):int(b * 16000)] ** 2)))  # noqa: E731
    assert rms(0.0, 0.9) < 0.01  # silêncio durante a capa
    assert rms(1.3, 2.7) > 0.05  # o som original começa junto com o vídeo


def test_narration_counts_the_cover_and_edited_scripts_follow_it(tmp_path):
    save_scan(tmp_path, {"video": "v.mp4", "cards": [card(1.1, "Mickey"), card(3.4, "Stitch", "Rare")]})
    (tmp_path / "overlay.json").write_text(json.dumps({"intro": 3.0, "summary": 29.1, "end": 33.1}))
    run = {"paid": 40.0, "paid_currency": "BRL", "paid_usd": 8.0}
    tl = narration.timeline(Settings(root=tmp_path), run, tmp_path)
    assert [c["t"] for c in tl.cards] == pytest.approx([4.1, 6.4]) and tl.intro == 3.0 and tl.summary == 29.1

    narration.edit_script(tmp_path, [{"t": 0.2, "texto": "Abertura"}, {"t": 3.6, "texto": "Uma rara!"}])
    script = narration.follow_intro(tmp_path, narration.load_script(tmp_path), 3.0)  # editado antes da capa existir
    assert [line["t"] for line in script["lines"]] == [3.2, 6.6]
    assert narration.load_script(tmp_path)["intro"] == 3.0
    assert narration.follow_intro(tmp_path, script, 3.0) == script  # já está em dia


def test_logo_is_stamped_semi_transparent_in_the_corner():
    scan = {"cards": [card(1.0, "Mickey")]}
    red = Image.new("RGBA", (400, 400), (255, 0, 0, 255))
    ov = Overlay(scan, Money("USD", 1.0), (1080, 1920), 12, logo=red, logo_corner="top-right", logo_opacity=0.5)
    frame = np.zeros((1920, 1080, 3), np.uint8)
    ov.stamp(frame)
    x, y = ov.logo_xy
    assert ov.logo.shape[:2] == (110, 110) and (x, y) == (1080 - 20 - 110, 20)  # 110 px num vídeo de 1080, no canto
    assert tuple(frame[y + 55, x + 55]) == (127, 0, 0)  # metade da opacidade sobre o preto
    assert not frame[:, :x].any() and not frame[y + 110:].any()  # o resto do quadro fica igual
    hud, cx, cy, _, _ = ov._hud_at(2.0)
    panel = hud.getchannel("A").point(lambda a: 255 if a > 128 else 0).getbbox()  # o painel, sem a sombra
    assert cx - hud.width / 2 + panel[2] <= x  # não cobre o painel do topo


def test_logos_are_kept_as_png_by_content(tmp_path):
    from cardline import logo

    s = Settings(root=tmp_path)
    buf = __import__("io").BytesIO()
    Image.new("RGB", (1600, 800), (10, 20, 30)).save(buf, "JPEG")
    name = logo.save(s, buf.getvalue())
    assert name.endswith(".png") and logo.load(s, name).size == (600, 300)  # no máximo 600 de lado, com transparência
    assert logo.save(s, buf.getvalue()) == name  # o mesmo arquivo não duplica
    assert logo.path(s, "../cardline.db") is None and logo.default(s) is None
    with pytest.raises(ValueError):
        logo.save(s, b"isto nao e imagem")
