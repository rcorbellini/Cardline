import json

import pytest
from fastapi.testclient import TestClient

from cardline import db, pipeline
from cardline.collection import _renumber, card_uid, load_scan, remove_card, restore_card, save_scan, set_card_foil
from cardline.config import Settings
from cardline.server import create_app


def card(n: int, t: float, uid: bool = True) -> dict:
    c = {"card_id": f"crd_{n}", "name": f"Carta {n}", "version": None, "rarity": "Common", "foil": False,
         "t": t, "crop": f"crops/{n:02d}.jpg", "pack": 1, "slot": n}
    if uid:
        c["uid"] = f"{n:02d}"
    return c


def scan_with(cards: list[dict]) -> dict:
    return {"duration": 30.0, "sets": ["1"], "scanned_at": "2026-10-06T12:00:00-03:00", "cards": cards}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("cardline.money.usd_brl", lambda s: (5.0, "2026-10-06"))
    monkeypatch.setattr("cardline.server.usd_brl", lambda s: (5.0, "2026-10-06"))
    return Settings(root=tmp_path)


def test_remove_moves_the_card_aside_and_renumbers(settings, tmp_path):
    save_scan(tmp_path, scan_with([card(1, 1.0), card(2, 3.0), card(3, 5.0)]))
    remove_card(settings, tmp_path, "02")
    scan = load_scan(tmp_path)
    assert [c["uid"] for c in scan["cards"]] == ["01", "03"]
    assert [c["slot"] for c in scan["cards"]] == [1, 2]
    assert [c["uid"] for c in scan["removed"]] == ["02"]


def test_restore_puts_the_card_back_in_video_order(settings, tmp_path):
    save_scan(tmp_path, scan_with([card(1, 1.0), card(2, 3.0), card(3, 5.0)]))
    remove_card(settings, tmp_path, "02")
    restore_card(settings, tmp_path, "02")
    scan = load_scan(tmp_path)
    assert [c["uid"] for c in scan["cards"]] == ["01", "02", "03"]
    assert scan["removed"] == []


def test_older_scans_identify_cards_by_their_crop(settings, tmp_path):
    save_scan(tmp_path, scan_with([card(1, 1.0, uid=False), card(2, 3.0, uid=False)]))
    assert card_uid(load_scan(tmp_path)["cards"][1]) == "02"
    remove_card(settings, tmp_path, "02")
    assert [card_uid(c) for c in load_scan(tmp_path)["cards"]] == ["01"]


def test_unknown_card_raises(settings, tmp_path):
    save_scan(tmp_path, scan_with([card(1, 1.0)]))
    with pytest.raises(LookupError):
        remove_card(settings, tmp_path, "99")


@pytest.fixture
def run(settings, tmp_path):
    video = tmp_path / "abertura.mp4"
    video.write_bytes(b"fake")
    run_id = pipeline.create_run(settings, video)
    save_scan(settings.runs_dir / str(run_id), scan_with([card(1, 1.0), card(2, 3.0)]))
    con = db.connect(settings.db_path)
    with con:
        con.execute("UPDATE runs SET status = 'done' WHERE id = ?", (run_id,))
        con.execute("UPDATE run_steps SET status = 'done' WHERE run_id = ?", (run_id,))
        con.executemany("INSERT INTO cards(id, set_code, number, name, rarity) VALUES (?, '1', ?, ?, 'Common')",
                        [("crd_1", "1", "Carta 1"), ("crd_2", "2", "Carta 2")])
    return run_id


def test_api_delete_marks_the_rest_of_the_pipeline_pending(settings, run):
    with TestClient(create_app(settings)) as client:
        assert client.delete(f"/api/runs/{run}/cards/02").status_code == 200
        detail = client.get(f"/api/runs/{run}").json()
        assert detail["status"] == "stale" and detail["resume_from"] == "prices"
        assert [c["uid"] for c in detail["cards"]] == ["01"]
        assert [c["uid"] for c in detail["removed"]] == ["02"]
        assert {s["name"]: s["status"] for s in detail["steps"]}["commit"] == "stale"

        assert client.post(f"/api/runs/{run}/cards/02/restore").status_code == 200
        assert [c["uid"] for c in client.get(f"/api/runs/{run}").json()["cards"]] == ["01", "02"]
        assert client.delete(f"/api/runs/{run}/cards/99").status_code == 404


def test_api_refuses_edits_while_the_pipeline_runs(settings, run):
    with TestClient(create_app(settings)) as client:
        # depois da subida: na subida, "rodando" sem processo vivo vira "interrompida"
        con = db.connect(settings.db_path)
        with con:
            con.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run,))
        assert client.delete(f"/api/runs/{run}/cards/01").status_code == 409
    assert json.loads((settings.runs_dir / str(run) / "scan.json").read_text())["cards"][0]["uid"] == "01"


def test_removing_a_duplicate_redoes_the_foil_deduction(settings, tmp_path):
    rar = ["Common"] * 6 + ["Common"] + ["Uncommon"] * 3 + ["Rare", "Rare"] + ["Uncommon"]  # 7ª carta repetida
    cards = [{**card(n, float(n)), "rarity": r} for n, r in enumerate(rar, 1)]
    scan = scan_with(cards)
    _renumber(scan, 12)
    save_scan(tmp_path, scan)
    assert [c["uid"] for c in load_scan(tmp_path)["cards"] if c["foil"]] == ["07"]  # 13 cartas: dedução errada

    remove_card(settings, tmp_path, "07")
    assert [c["uid"] for c in load_scan(tmp_path)["cards"] if c["foil"]] == ["13"]


def test_manual_foil_choice_survives_edits(settings, tmp_path):
    cards = [card(n, float(n)) for n in range(1, 4)]
    cards[0].update(foil=True, foil_reason="manual")
    save_scan(tmp_path, scan_with(cards))
    remove_card(settings, tmp_path, "03")
    assert load_scan(tmp_path)["cards"][0]["foil"] is True


def test_marking_a_card_foil_takes_the_foil_from_the_deduced_one(settings, tmp_path):
    rar = ["Common"] * 6 + ["Uncommon"] * 3 + ["Rare", "Rare"] + ["Uncommon"]
    cards = [{**card(n, float(n)), "rarity": r, "price_usd": 0.1, "price_key": f"crd_{n}:0"} for n, r in enumerate(rar, 1)]
    scan = scan_with(cards)
    _renumber(scan, 12)
    save_scan(tmp_path, scan)
    assert [c["uid"] for c in load_scan(tmp_path)["cards"] if c["foil"]] == ["12"]  # deduzida pelo booster

    assert set_card_foil(settings, tmp_path, "01", True) is True
    after = {c["uid"]: c for c in load_scan(tmp_path)["cards"]}
    assert [uid for uid, c in after.items() if c["foil"]] == ["01"]
    assert after["01"]["foil_reason"] == "manual"
    assert "price_usd" not in after["01"] and "price_usd" not in after["12"]  # repreço pelo dia da abertura
    assert after["05"]["price_usd"] == 0.1  # as outras mantêm o preço da abertura


def test_foil_only_rarities_stay_foil(settings, tmp_path):
    save_scan(tmp_path, scan_with([{**card(1, 1.0), "rarity": "Enchanted", "foil": True}]))
    with pytest.raises(ValueError, match="sempre foil"):
        set_card_foil(settings, tmp_path, "01", False)


def test_api_edit_card_foil(settings, run):
    with TestClient(create_app(settings)) as client:
        r = client.patch(f"/api/runs/{run}/cards/02", json={"foil": True})
        assert r.status_code == 200 and r.json()["changed"] is True
        detail = client.get(f"/api/runs/{run}").json()
        assert detail["status"] == "stale" and detail["resume_from"] == "prices"
        assert [c["uid"] for c in detail["cards"] if c["foil"]] == ["02"]
        assert client.patch(f"/api/runs/{run}/cards/02", json={}).status_code == 400
        assert client.patch(f"/api/runs/{run}/cards/99", json={"foil": True}).status_code == 404


def test_cadastro_has_no_boosters_nor_deduced_foil(settings, tmp_path):
    rar = ["Common"] * 6 + ["Uncommon"] * 3 + ["Rare", "Rare"] + ["Uncommon"]
    scan = {**scan_with([{**card(n, float(n)), "rarity": r} for n, r in enumerate(rar, 1)]), "kind": "cadastro"}
    _renumber(scan, 12)
    assert all(c["pack"] is None for c in scan["cards"])
    assert [c["slot"] for c in scan["cards"]] == list(range(1, 13))
    assert not any(c["foil"] for c in scan["cards"])


def test_marking_foil_in_cadastro_does_not_touch_other_cards(settings, tmp_path):
    cards = [{**card(n, float(n)), "foil": n == 2, "foil_reason": "manual" if n == 2 else None} for n in range(1, 4)]
    save_scan(tmp_path, {**scan_with(cards), "kind": "cadastro"})
    set_card_foil(settings, tmp_path, "01", True)
    assert [c["uid"] for c in load_scan(tmp_path)["cards"] if c["foil"]] == ["01", "02"]


def test_api_rejects_unknown_kind_and_paid_on_cadastro(settings, tmp_path):
    video = tmp_path / "cadastro.mp4"
    video.write_bytes(b"outro video")
    run_id = pipeline.create_run(settings, video, kind="cadastro")
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/runs?filename=x.mp4&kind=troca", content=b"x").status_code == 400
        assert client.patch(f"/api/runs/{run_id}", json={"paid": 10}).status_code == 400
        assert [s["name"] for s in client.get(f"/api/runs/{run_id}").json()["steps"]] == ["scan", "verify", "prices", "commit"]


def test_api_detail_of_a_cadastro_without_boosters(settings, tmp_path):
    video = tmp_path / "cadastro.mp4"
    video.write_bytes(b"mais um video")
    run_id = pipeline.create_run(settings, video, kind="cadastro")
    cards = [{**card(n, float(n)), "pack": None, "slot": n} for n in (1, 2)]
    save_scan(settings.runs_dir / str(run_id), {**scan_with(cards), "kind": "cadastro"})
    con = db.connect(settings.db_path)
    with con:
        con.executemany("INSERT INTO cards(id, set_code, number, name, rarity) VALUES (?, '1', ?, ?, 'Common')",
                        [("crd_1", "1", "Carta 1"), ("crd_2", "2", "Carta 2")])
    with TestClient(create_app(settings)) as client:
        detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["packs"] == 0 and detail["n_cards"] == 2
