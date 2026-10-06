import pytest

from cardline import db, pipeline
from cardline.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("cardline.money.usd_brl", lambda s: (5.0, "2026-10-06"))
    return Settings(root=tmp_path)


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "abertura.mp4"
    path.write_bytes(b"not really a video")
    return path


@pytest.fixture
def fake_steps(monkeypatch):
    """Passos de mentira que só registram a ordem em que rodaram."""
    calls = []
    fail = set()

    def make(name):
        def step(ctx):
            calls.append(name)
            if name in fail:
                raise RuntimeError(f"{name} quebrou")
            return f"{name} ok"
        return step

    monkeypatch.setattr(pipeline, "STEP_FUNCS", {n: make(n) for n in pipeline.STEP_NAMES})
    return calls, fail


def steps(settings, run_id):
    con = db.connect(settings.db_path)
    return {r["name"]: r["status"] for r in con.execute("SELECT * FROM run_steps WHERE run_id = ?", (run_id,))}


def run_row(settings, run_id):
    return db.connect(settings.db_path).execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def test_create_run_converts_paid_and_rejects_the_same_video(settings, video):
    run_id = pipeline.create_run(settings, video, paid=35.0, paid_currency="BRL")
    run = run_row(settings, run_id)
    assert run["status"] == "queued" and run["paid_usd"] == pytest.approx(7.0)
    assert set(steps(settings, run_id).values()) == {"pending"}
    with pytest.raises(pipeline.DuplicateVideo):
        pipeline.create_run(settings, video)


def test_upload_is_moved_into_the_run_folder(settings, video):
    run_id = pipeline.create_run(settings, video, video_name="Minha Abertura (1).mp4", move=True)
    run = run_row(settings, run_id)
    assert not video.exists()
    assert (settings.root / run["video"]).exists()
    assert run["video"].startswith(f"runs/{run_id}/")


def test_failure_stops_and_resume_continues_from_the_failed_step(settings, video, fake_steps):
    calls, fail = fake_steps
    run_id = pipeline.create_run(settings, video)
    fail.add("commit")
    assert pipeline.execute(settings, run_id) is False
    assert calls == ["scan", "verify", "prices", "commit"]
    assert run_row(settings, run_id)["status"] == "failed"
    assert run_row(settings, run_id)["resume_from"] == "commit"

    fail.clear()
    calls.clear()
    assert pipeline.execute(settings, run_id) is True
    assert calls == ["commit", "overlay"]
    assert set(steps(settings, run_id).values()) == {"done"}


def test_editing_marks_the_rest_stale_and_rerun_only_redoes_it(settings, video, fake_steps):
    calls, _ = fake_steps
    run_id = pipeline.create_run(settings, video)
    pipeline.execute(settings, run_id)
    pipeline.mark_stale(settings, run_id, "prices")
    assert run_row(settings, run_id)["status"] == "stale"
    assert steps(settings, run_id) == {"scan": "done", "verify": "done", "prices": "stale", "commit": "stale", "overlay": "stale"}

    calls.clear()
    pipeline.execute(settings, run_id)
    assert calls == ["prices", "commit", "overlay"]


def test_enqueue_refuses_a_run_already_in_the_queue(settings, video):
    run_id = pipeline.create_run(settings, video)
    with pytest.raises(RuntimeError):
        pipeline.enqueue(settings, run_id)


def test_delete_removes_cards_and_folder_but_keeps_external_video(settings, video, fake_steps):
    run_id = pipeline.create_run(settings, video)
    con = db.connect(settings.db_path)
    con.execute("INSERT INTO cards(id, set_code, number, name) VALUES ('crd_x', '1', '1', 'X')")
    con.execute("INSERT INTO collection(card_id, run_id, added_at) VALUES ('crd_x', ?, 'now')", (run_id,))
    con.commit()
    folder = settings.runs_dir / str(run_id)
    pipeline.delete_run(settings, run_id)
    assert con.execute("SELECT COUNT(*) FROM collection").fetchone()[0] == 0
    assert not folder.exists()
    assert video.exists()


def test_cadastro_has_no_paid_value_and_no_video_step(settings, video, fake_steps):
    calls, _ = fake_steps
    run_id = pipeline.create_run(settings, video, kind="cadastro", paid=35.0)
    run = run_row(settings, run_id)
    assert run["kind"] == "cadastro" and run["paid"] is None
    assert set(steps(settings, run_id)) == {"scan", "verify", "prices", "commit"}
    assert pipeline.execute(settings, run_id) is True
    assert calls == ["scan", "verify", "prices", "commit"]
    with pytest.raises(ValueError):
        pipeline.update_paid(settings, run_id, 10.0, "BRL")


def test_unknown_kind_is_rejected(settings, video):
    with pytest.raises(ValueError):
        pipeline.create_run(settings, video, kind="troca")


def test_existing_database_gets_the_kind_column(tmp_path):
    import sqlite3
    path = tmp_path / "antigo.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, video TEXT NOT NULL)")
    old.execute("INSERT INTO runs(id, video) VALUES (1, 'v.mp4')")
    old.execute("PRAGMA user_version = 2")
    old.commit()
    old.close()
    con = db.connect(path)
    assert con.execute("SELECT kind FROM runs WHERE id = 1").fetchone()[0] == "abertura"
    assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
