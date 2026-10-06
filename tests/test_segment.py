from cardline.scan import _segment


def rec(i: int, card: int | None = None, inliers: int = 50) -> dict:
    return {"i": i, "matches": [{"card": card, "inliers": inliers}] if card is not None else []}


def timeline(*spans: tuple) -> list[dict]:
    """spans: (carta, inliers, n_frames); carta None = frame sem match."""
    out = []
    for card, inliers, n in spans:
        out += [rec(len(out), card, inliers) for _ in range(n)]
    return out


def cards_of(events: list[dict]) -> list[int]:
    return [e["card"] for e in events]


def test_each_new_top_card_is_a_reveal():
    records = timeline((None, 0, 5), (7, 60, 10), (None, 0, 3), (3, 80, 10), (9, 40, 6))
    assert cards_of(_segment(records, fps=10)) == [7, 3, 9]


def test_short_flicker_does_not_create_a_card():
    records = timeline((7, 60, 10), (3, 30, 2), (7, 60, 5), (5, 70, 10))
    assert cards_of(_segment(records, fps=10)) == [7, 5]


def test_same_card_again_after_another_is_a_second_copy():
    records = timeline((7, 60, 10), (3, 60, 10), (7, 60, 10))
    assert cards_of(_segment(records, fps=10)) == [7, 3, 7]


def test_start_backfills_to_first_weak_match():
    # carta entrando borrada: matches fracos (abaixo do limiar) antes dos firmes
    records = timeline((None, 0, 3), (7, 8, 4), (None, 0, 2), (7, 60, 10))
    events = _segment(records, fps=10)
    assert cards_of(events) == [7]
    assert events[0]["start"] == 3


def test_backfill_stops_at_previous_card():
    records = timeline((3, 60, 10), (7, 8, 3), (7, 60, 10))
    events = _segment(records, fps=10)
    assert events[1]["start"] == 10
