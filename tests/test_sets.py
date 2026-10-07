import io
import json
import sqlite3
import sys
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from cardline import catalog, db
from cardline.config import Settings
from cardline.overlay import Overlay
from cardline.server import create_app


def png(color=(200, 30, 60), hole=False) -> bytes:
    """Foto de produto em fundo branco: um retângulo colorido (com um "buraco" branco no meio, se pedido)."""
    im = Image.new("RGB", (120, 200), (255, 255, 255))
    draw = ImageDraw.Draw(im)
    draw.rectangle((30, 40, 89, 159), fill=color)
    if hole:
        draw.rectangle((50, 90, 69, 109), fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=tmp_path)
    con = db.connect(s.db_path)
    for code, name in (("1", "The First Chapter"), ("2", "Rise of the Floodborn"), ("P1", "Promo Set 1")):
        db.upsert_set(con, {"code": code, "id": f"set_{code}", "name": name, "released_at": "2023-08-18"})
    con.commit()
    return s


@pytest.fixture
def tcgcsv(monkeypatch):
    """tcgcsv de mentira: o set 1 tem booster avulso, o 2 só caixa e o P1 nem existe lá."""
    calls = []
    groups = {"results": [{"groupId": 101, "abbreviation": "1"}, {"groupId": 102, "abbreviation": "2"}]}
    products = {
        101: [{"name": "The First Chapter Sleeved Booster Pack", "imageUrl": "https://x/sleeved_200w.jpg"},
              {"name": "The First Chapter Booster Pack", "imageUrl": "https://x/pack_200w.jpg"},
              {"name": "The First Chapter Booster Box", "imageUrl": "https://x/box_200w.jpg"}],
        102: [{"name": "Rise of the Floodborn Booster Box", "imageUrl": "https://x/box2_200w.jpg"}],
    }

    def fetch(url, timeout=60):
        calls.append(url)
        if url.endswith("/groups"):
            return json.dumps(groups).encode()
        if url.endswith("/products"):
            return json.dumps({"results": products[int(url.split("/")[-2])]}).encode()
        return png()

    monkeypatch.setattr(catalog.lorcast, "fetch", fetch)
    return calls


def icon_row(settings, code):
    return db.connect(settings.db_path).execute("SELECT * FROM sets WHERE code = ?", (code,)).fetchone()


def test_cutout_removes_only_the_white_around_the_product():
    img = catalog.cutout(png(hole=True))
    assert img.mode == "RGBA"
    assert abs(img.width - 60) <= 2 and abs(img.height - 120) <= 2  # recortado no retângulo
    assert img.getpixel((img.width // 2, img.height // 2))[3] == 255  # branco dentro da arte fica
    assert img.getpixel((img.width // 2, 5))[:3] == (200, 30, 60)


def test_booster_pack_skips_sleeved_boxes_and_displays():
    products = [{"name": "X Sleeved Booster Pack"}, {"name": "X Booster Display"}, {"name": "X Booster Pack"}]
    assert catalog.booster_pack(products) == {"name": "X Booster Pack"}
    assert catalog.booster_pack([{"name": "X Booster Box"}]) is None


def test_sync_downloads_the_booster_photo_once(settings, tcgcsv):
    con = db.connect(settings.db_path)
    assert catalog.sync_set_icons(settings, con) == 1
    row = icon_row(settings, "1")
    assert row["icon_source"] == "tcgplayer" and row["tcg_group_id"] == 101
    assert (settings.root / row["icon"]).exists()
    assert "https://x/pack_in_1000x1000.jpg" in tcgcsv  # a foto grande, não a miniatura
    assert icon_row(settings, "2")["icon"] is None  # sem booster avulso: a página usa o selo
    assert icon_row(settings, "P1")["icon"] is None

    tcgcsv.clear()
    assert catalog.sync_set_icons(settings, con) == 0
    assert not any("1000x1000" in url for url in tcgcsv)


def test_manual_icon_survives_sync_and_reset_brings_back_the_booster(settings, tcgcsv):
    catalog.save_manual_icon(settings, "1", png((10, 200, 10)))
    assert icon_row(settings, "1")["icon_source"] == "manual"
    catalog.sync_set_icons(settings, db.connect(settings.db_path), force=True)
    assert icon_row(settings, "1")["icon_source"] == "manual"

    catalog.reset_icon(settings, "1")
    assert icon_row(settings, "1")["icon_source"] == "tcgplayer"
    catalog.reset_icon(settings, "P1")  # sem foto automática: volta ao selo
    assert icon_row(settings, "P1")["icon"] is None


def test_icons_saved_by_the_old_version_are_downloaded_again_in_full_size(settings, tcgcsv):
    con = db.connect(settings.db_path)
    catalog.sync_set_icons(settings, con)
    old = catalog.icon_path(settings, "1")
    Image.new("RGBA", (345, catalog.LEGACY_MAX), (255, 0, 0, 255)).save(old)  # como a versão antiga guardava
    catalog.thumb_path(settings, "1").unlink()
    tcgcsv.clear()
    assert catalog.sync_set_icons(settings, con) == 1
    assert max(Image.open(old).size) != catalog.LEGACY_MAX and catalog.thumb_path(settings, "1").exists()


def test_sync_replaces_a_manual_icon_whose_file_is_gone(settings, tcgcsv):
    catalog.save_manual_icon(settings, "1", png((10, 200, 10)))
    catalog.icon_path(settings, "1").unlink()  # ex.: apagaram data/cache
    assert catalog.sync_set_icons(settings, db.connect(settings.db_path)) == 1
    assert icon_row(settings, "1")["icon_source"] == "tcgplayer"


def test_manual_icon_needs_a_known_set_and_an_image(settings):
    with pytest.raises(LookupError):
        catalog.save_manual_icon(settings, "99", png())
    with pytest.raises(ValueError):
        catalog.save_manual_icon(settings, "1", b"isso nao e imagem")


def test_a_failed_download_skips_the_set_but_reset_reports_it(settings, tcgcsv, monkeypatch):
    fake = catalog.lorcast.fetch

    def flaky(url, timeout=60):
        if "1000x1000" in url:
            raise OSError("rede caiu")
        return fake(url, timeout)

    monkeypatch.setattr(catalog.lorcast, "fetch", flaky)
    assert catalog.sync_set_icons(settings, db.connect(settings.db_path)) == 0
    with pytest.raises(OSError):
        catalog.reset_icon(settings, "1")


def test_existing_database_gets_the_icon_columns(tmp_path):
    path = tmp_path / "antigo.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE sets (code TEXT PRIMARY KEY, id TEXT NOT NULL, name TEXT NOT NULL, released_at TEXT)")
    old.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, video TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'abertura')")
    old.execute("INSERT INTO sets VALUES ('1', 'set_1', 'The First Chapter', '2023-08-18')")
    old.execute("PRAGMA user_version = 3")
    old.commit()
    old.close()
    row = db.connect(path).execute("SELECT * FROM sets").fetchone()
    assert row["name"] == "The First Chapter" and row["icon"] is None and row["tcg_group_id"] is None


def test_hud_makes_room_for_the_set_icon():
    o = Overlay.__new__(Overlay)  # só o necessário para desenhar o painel
    o.u, o.pack_size, o.cards = 1.0, 12, [{"pack": 1}]
    widths = []
    for pack_set, icons in (({}, {}), ({1: "1"}, {"1": Image.new("RGBA", (300, 500), (255, 0, 0, 255))}),
                            ({1: "Coconut"}, {})):
        o.pack_set, o.icons, o._icon_cache, o._hud_cache = pack_set, icons, {}, {}
        widths.append(o._hud(1, 3, "US$ 2,00", (None,) * 12, None).width)
    plain, photo, badge = widths
    assert plain < photo < badge  # a foto (estreita) e o selo hexagonal empurram o texto para a direita


# --- página -------------------------------------------------------------------------------------


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr("cardline.server.usd_brl", lambda s: (5.0, "2026-10-06"))
    with TestClient(create_app(settings)) as c:
        yield c


def test_sets_api_lists_sets_and_serves_uploaded_icons(client):
    sets = {s["code"]: s for s in client.get("/api/sets").json()}
    assert set(sets) == {"1", "2", "P1"}
    assert sets["1"]["booster"] and not sets["P1"]["booster"]
    assert sets["1"]["icon"] is None and not sets["1"]["recognized"]

    assert client.post("/api/sets/1/icon", content=png()).status_code == 200
    one = next(s for s in client.get("/api/sets").json() if s["code"] == "1")
    assert one["icon"].startswith("/set-icons/1.thumb.webp?v=") and one["icon_source"] == "manual"
    image = client.get(one["icon"])  # a página recebe a miniatura; o vídeo usa o ícone em resolução cheia
    assert image.status_code == 200 and image.headers["content-type"] == "image/webp"
    meta = {s["code"]: s for s in client.get("/api/meta").json()["sets"]}
    assert meta["1"]["icon"] == one["icon"] and meta["P1"]["icon"] is None

    assert client.post("/api/sets/99/icon", content=png()).status_code == 404
    assert client.post("/api/sets/1/icon", content=b"lixo").status_code == 400


def test_reset_icon_endpoint(client, tcgcsv, monkeypatch):
    client.post("/api/sets/1/icon", content=png())
    assert client.delete("/api/sets/1/icon").status_code == 200
    assert next(s for s in client.get("/api/sets").json() if s["code"] == "1")["icon_source"] == "tcgplayer"

    def offline(url, timeout=60):
        raise OSError("sem internet")

    monkeypatch.setattr(catalog.lorcast, "fetch", offline)
    assert client.delete("/api/sets/1/icon").status_code == 502


def test_sync_runs_one_at_a_time_in_the_background(client, monkeypatch):
    real_popen = __import__("subprocess").Popen

    def fake_popen(args, **kw):  # no lugar do `cardline sync` de verdade (que usa a rede)
        script = "import time; print('Catálogo: 3 sets', flush=True); time.sleep(0.5); print('Sync concluído.')"
        return real_popen([sys.executable, "-c", script], **kw)

    monkeypatch.setattr("cardline.server.subprocess.Popen", fake_popen)
    assert client.post("/api/sets/sync").json()["running"] is True
    assert client.post("/api/sets/sync").status_code == 409
    for _ in range(100):
        status = client.get("/api/sets/sync").json()
        if not status["running"]:
            break
        time.sleep(0.05)
    assert status["ok"] is True and status["finished_at"]
    assert status["log"].splitlines()[-1] == "Sync concluído."
