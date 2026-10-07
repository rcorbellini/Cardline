import importlib.util
import json
import random
import subprocess

import numpy as np
import pytest
from fastapi.testclient import TestClient

from cardline import db, narration, pipeline
from cardline.config import Settings
from cardline.server import create_app
from cardline.video import ffmpeg_exe, probe

needs_num2words = pytest.mark.skipif(importlib.util.find_spec("num2words") is None, reason="extra narracao")

CARDS = [  # o booster de exemplo: instante, nome, versão, raridade, foil
    (1.1, "Magic Golden Flower", None, "Common", False),
    (3.4, "Scar", "Fiery Usurper", "Common", False),
    (6.0, "Timon", "Grub Rustler", "Common", False),
    (8.0, "Dr. Facilier", "Charlatan", "Common", False),
    (10.6, "Megara", "Pulling the Strings", "Common", False),
    (12.6, "Kristoff", "Official Ice Master", "Common", False),
    (14.3, "LeFou", "Bumbler", "Uncommon", False),
    (16.2, "Ransack", None, "Uncommon", False),
    (18.1, "Rapunzel", "Letting Down Her Hair", "Uncommon", False),
    (20.4, "Moana", "Of Motunui", "Rare", False),
    (22.2, "Tinker Bell", "Giant Fairy", "Super_rare", False),
    (24.1, "Fan the Flames", None, "Uncommon", True),
]


def timeline(result=-0.7, paid=40.0, packs=1):
    cards = [{"card_id": f"crd_{i}", "t": t, "name": n, "version": v, "rarity": r, "foil": f, "pack": 1}
             for i, (t, n, v, r, f) in enumerate(CARDS)]
    return narration.Timeline(cards=cards, summary=26.1, end=30.1, packs=packs, paid=paid,
                              paid_currency="BRL" if paid else None, result=result if paid else None)


def no_progress(*_):
    pass


# --- roteiro ------------------------------------------------------------------------------------


@needs_num2words
def test_plan_keeps_the_structure_and_nobody_talks_over_anybody():
    tl = timeline()
    lines = narration.plan(tl, {}, random.Random(1), max_jokes=4)
    kinds = [line["tipo"] for line in lines]
    assert lines[0]["t"] == 0.2 and kinds[0] == "abertura" and lines[0]["texto"].startswith("Quarenta reais")
    assert kinds[-1] == "desfecho" and lines[-1]["texto"] in narration.ENDINGS["loss"]
    assert lines[-1]["t"] >= tl.summary + 0.7  # o desfecho vem com o resumo na tela
    assert all(line["t"] < tl.summary for line in lines[:-1])  # sem spoiler: antes do resumo, só pressentimento
    assert kinds.count("reacao") == 2 and kinds.count("presagio") == 2  # a rara, a super rara; sem piadas, presságios
    spans = [(line["t"], line["t"] + narration.estimate(line["texto"])) for line in lines]
    assert all(a[1] + narration.GAP <= b[0] + 0.01 for a, b in zip(spans, spans[1:]))


@needs_num2words
def test_plan_puts_the_foil_joke_first_and_admits_a_good_booster():
    jokes = {11: "E a última se chama... Atiçar as Chamas.", 2: "Hakuna matata... sei."}
    lines = narration.plan(timeline(result=0.5), jokes, random.Random(2), max_jokes=4)
    texts = [line["texto"] for line in lines]
    assert jokes[11] in texts and jokes[2] in texts
    assert texts[-1] in narration.ENDINGS["profit"] and "presagio" not in [line["tipo"] for line in lines]


@needs_num2words
def test_intro_counts_boosters_and_works_without_paid_value():
    two = narration.plan(timeline(packs=2), {}, random.Random(3), 4)[0]["texto"]
    assert two.split(". ")[1].startswith("Dois boosters")
    free = narration.plan(timeline(paid=None), {}, random.Random(3), 4)
    assert free[0]["texto"] in narration.INTROS_FREE and free[-1]["texto"] in narration.ENDINGS["unknown"]


def test_clean_joke_keeps_good_lines_and_drops_the_rest():
    timon = {"name": "Timon", "version": "Grub Rustler"}
    assert narration.clean_joke("Timão... hakuna matata, sei", timon) == "Timão... hakuna matata, sei."
    assert narration.clean_joke("Goofy, o mosqueteiro… que confusão!”} 替换为： {") == "Goofy, o mosqueteiro... que confusão!"
    for bad in ["Custou 45 reais, hein?", "Prejuízo à vista, meu caro.", "Ok.", "Timon, grub rustler... vai dar ruim.",
                "palavra " * 13]:
        assert narration.clean_joke(bad, timon) is None
    # carta que não é personagem: o nome em inglês tem que ser traduzido
    assert narration.clean_joke("Fan the Flames... que calor!", {"name": "Fan the Flames", "version": None}) is None


@needs_num2words
def test_without_ollama_the_script_still_has_the_approved_jokes(tmp_path, monkeypatch):
    def offline(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(narration.urllib.request, "urlopen", offline)
    script = narration.write_script(Settings(root=tmp_path), timeline(), seed=1, progress=no_progress)
    texts = [line["texto"] for line in script["lines"]]
    assert "Hakuna matata... sei." in texts and "E a última se chama... Atiçar as Chamas." in texts
    assert script["source"] == "auto" and script["writer"] is None


def test_jokes_from_the_model_are_checked(tmp_path, monkeypatch):
    answers = iter(['{"fala": "Megara... puxa as cordas, e a gente cai."}', '{"fala": "Custa 3 reais"}'])

    class Reply:
        def __init__(self, content):
            self.content = content

        def read(self):
            return json.dumps({"message": {"content": self.content}}).encode()

    monkeypatch.setattr(narration.urllib.request, "urlopen", lambda req, timeout: Reply(next(answers)))
    jokes = narration.write_jokes(Settings(root=tmp_path), timeline(), [4, 5], seed=1, progress=no_progress)
    assert jokes == {4: "Megara... puxa as cordas, e a gente cai."}


def test_edited_script_is_cleaned_and_a_new_one_can_be_asked_for(tmp_path):
    narration.edit_script(tmp_path, [{"t": 5, "texto": "  Segunda   fala "}, {"t": "1.5", "texto": "Primeira"},
                                     {"t": 2, "texto": "   "}])
    script = narration.load_script(tmp_path)
    assert script["source"] == "editado"
    assert script["lines"] == [{"t": 1.5, "texto": "Primeira"}, {"t": 5.0, "texto": "Segunda fala"}]
    with pytest.raises(ValueError):
        narration.edit_script(tmp_path, [{"t": 1, "texto": " "}])
    narration.discard_script(tmp_path)
    script = narration.load_script(tmp_path)
    assert script["source"] == "auto" and script["lines"] == [] and script["fingerprint"] is None


# --- voz ----------------------------------------------------------------------------------------


@needs_num2words
def test_voice_text_avoids_spoken_periods_and_spells_values():
    assert narration.tts_text("Calma. As boas vêm no final... é o que dizem.") == "Calma... As boas vêm no final... é o que dizem"
    assert narration.tts_text("Pagou R$ 40,00 e o Dr. Facilier riu.") == "Pagou quarenta reais e o Doutor Facilliê riu"
    assert narration.tts_text("Hakuna matata!") == "Rakuna matata!"
    assert narration.money_words(9.04, "USD") == "nove dólares e quatro centavos"
    assert narration.money_words(1, "BRL") == "um real"


@needs_num2words
def test_whisper_is_checked_by_sound_not_by_spelling():
    assert narration.similarity("Hakuna matata... sei.", "Acuna matata, sei.") == 1
    assert narration.similarity("E a última se chama... Atiçar as Chamas.", "E a última se chama Atissar as Chamas.") == 1
    assert narration.similarity("Quarenta reais. Um booster lacrado.", "40 reais, um búster lacrado") == 1
    assert narration.similarity("Logo de cara, o usurpador. Mau sinal.", "Logo de cara, o usurpador, mal sinal.") == 1
    assert narration.similarity("Uma rara!", "Um rádio.") < 0.5


class FakeVoice:
    sr = 24000
    calls: list[str] = []
    heard = ""

    def __init__(self, settings):
        pass

    def say(self, text, seed):
        FakeVoice.calls.append(text)
        return (np.sin(np.linspace(0, 400, self.sr // 2)) * 0.5).astype(np.float32)

    def hear(self, wav):
        return FakeVoice.heard


def test_lines_are_cached_and_a_joke_the_voice_gets_wrong_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(narration, "Voice", FakeVoice)
    settings = Settings(root=tmp_path)
    lines = [{"t": 0.2, "texto": "Uma rara!", "tipo": "reacao"}, {"t": 3.0, "texto": "Super rara! Agora vai!", "tipo": "piada"}]
    FakeVoice.calls, FakeVoice.heard = [], "Uma rara."
    clips = narration.voice_lines(settings, tmp_path, lines, no_progress)
    assert clips[0][0].exists() and clips[0][1] == pytest.approx(0.5, abs=0.01)
    assert clips[1] is None  # 3 tomadas e a voz não disse a piada: ela sai
    assert FakeVoice.calls == ["Uma rara!"] + ["Super rara! Agora vai!"] * 3
    assert narration.voice_lines(settings, tmp_path, lines, no_progress) == clips
    assert len(FakeVoice.calls) == 4  # tudo do cache


def test_schedule_waits_for_the_previous_line_and_ducking_only_lowers_during_speech():
    timing = narration.schedule([{"t": 0.0}, {"t": 1.0}, {"t": 5.0}], [2.0, 1.0, 1.0])
    assert [x for pair in timing for x in pair] == pytest.approx([0, 2, 2.12, 3.12, 5, 6])
    gain = narration.ducking(10_000, [(2.0, 4.0)], sr=1000)
    assert gain[500] == pytest.approx(1) and gain[9000] == pytest.approx(1)
    assert gain[3000] == pytest.approx(10 ** (narration.DUCK_DB / 20), rel=1e-3)


def test_mix_puts_the_voice_over_the_video_and_holds_the_last_frame(tmp_path):
    pytest.importorskip("torchaudio")
    video = tmp_path / "overlay.mp4"
    subprocess.run([ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(video)], check=True)
    clip = tmp_path / "fala.wav"
    narration.write_wav(clip, (np.sin(np.linspace(0, 2000, 24000)) * 0.3).astype(np.float32), 24000)
    out = tmp_path / "narrado.mp4"
    extra = narration.mix(video, out, [(clip, 1.0)], [(1.5, 2.5)], 2.0)
    assert extra == pytest.approx(2.5 + narration.TAIL - 2.0, abs=0.05)  # a fala acaba depois do vídeo: ele espera
    info = probe(out)
    assert info.has_audio and info.duration == pytest.approx(2.0 + extra, abs=0.25)


# --- pipeline e página --------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("cardline.money.usd_brl", lambda s: (5.0, "2026-10-06"))
    monkeypatch.setattr("cardline.server.usd_brl", lambda s: (5.0, "2026-10-06"))
    return Settings(root=tmp_path)


def finished_run(settings, name="abertura.mp4", **kwargs):
    video = settings.root / name
    video.write_bytes(name.encode())
    run_id = pipeline.create_run(settings, video, **kwargs)
    con = db.connect(settings.db_path)
    with con:
        con.execute("UPDATE runs SET status = 'done' WHERE id = ?", (run_id,))
        con.execute("UPDATE run_steps SET status = 'done' WHERE run_id = ?", (run_id,))
    return run_id


def run_row(settings, run_id):
    return db.connect(settings.db_path).execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def test_narration_needs_the_overlay_and_the_toggle_marks_the_step(settings):
    no_video = finished_run(settings, "a.mp4", narration=True, overlay=False)
    assert json.loads(run_row(settings, no_video)["options"])["narration"] is False
    with pytest.raises(ValueError):
        pipeline.update_narration(settings, no_video, True)

    run_id = finished_run(settings, "b.mp4")
    pipeline.update_narration(settings, run_id, True)
    run = run_row(settings, run_id)
    assert run["status"] == "stale" and run["resume_from"] == "narrate"
    narrated = settings.root / run["dir"] / "narrado.mp4"
    narrated.write_bytes(b"video")
    pipeline.update_narration(settings, run_id, False)
    run = run_row(settings, run_id)
    assert run["status"] == "done" and run["resume_from"] is None and not narrated.exists()
    step = db.connect(settings.db_path).execute(
        "SELECT status, message FROM run_steps WHERE run_id = ? AND name = 'narrate'", (run_id,)).fetchone()
    assert tuple(step) == ("skipped", "desligada")


def test_a_later_change_does_not_hide_an_earlier_edit(settings):
    run_id = finished_run(settings)
    pipeline.mark_stale(settings, run_id, "prices")  # carta removida
    pipeline.mark_stale(settings, run_id, "overlay")  # depois, a moeda do vídeo
    assert run_row(settings, run_id)["resume_from"] == "prices"


def test_old_openings_get_the_narration_step(tmp_path):
    path = tmp_path / "v4.db"
    con = db.connect(path)
    with con:
        con.execute("INSERT INTO runs(id, kind, video, video_name, video_sha1, dir, status, created_at)"
                    " VALUES (1, 'abertura', 'v.mp4', 'v.mp4', 'x', 'runs/1', 'done', 'now')")
        con.execute("INSERT INTO run_steps(run_id, name, status) VALUES (1, 'overlay', 'done')")
        con.execute("PRAGMA user_version = 4")
    con.close()
    step = db.connect(path).execute("SELECT status, message FROM run_steps WHERE run_id = 1 AND name = 'narrate'").fetchone()
    assert tuple(step) == ("skipped", "desligada")


def test_script_routes(settings):
    run_id = finished_run(settings)
    with TestClient(create_app(settings)) as client:
        assert "unavailable" in client.get("/api/meta").json()["narration"]
        assert client.get(f"/api/runs/{run_id}").json()["narration"] == {"enabled": False, "lines": [], "source": None,
                                                                          "writer": None}
        lines = {"lines": [{"t": 1, "texto": "Uma rara!"}]}
        assert client.put(f"/api/runs/{run_id}/narration", json=lines).status_code == 409  # narração desligada

        assert client.patch(f"/api/runs/{run_id}", json={"narration": True}).status_code == 200
        assert client.put(f"/api/runs/{run_id}/narration", json=lines).status_code == 200
        detail = client.get(f"/api/runs/{run_id}").json()
        assert detail["status"] == "stale" and detail["resume_from"] == "narrate"
        assert detail["narration"]["source"] == "editado" and detail["narration"]["lines"] == [{"t": 1.0, "texto": "Uma rara!"}]
        assert client.put(f"/api/runs/{run_id}/narration", json={"lines": [{"t": 1, "texto": " "}]}).status_code == 400

        assert client.post(f"/api/runs/{run_id}/narration/new").status_code == 200
        assert client.get(f"/api/runs/{run_id}").json()["narration"]["lines"] == []
