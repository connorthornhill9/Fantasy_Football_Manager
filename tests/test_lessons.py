from ffm.agent import parse_review
from ffm.store import Store


def test_parse_review_tolerates_prose_and_caps():
    text = 'Here you go:\n```json\n{"lessons": ["a", "b", "c", "d"], "suggestions": ["x", "", "y", "z"]}\n```'
    out = parse_review(text)
    assert out == {"lessons": ["a", "b", "c"], "suggestions": ["x", "y"]}
    assert parse_review("no json here") == {"lessons": [], "suggestions": []}
    assert parse_review('{"lessons": null}') == {"lessons": [], "suggestions": []}


def test_lessons_lifecycle_and_cap(tmp_path):
    store = Store(tmp_path / "l.sqlite3")
    ids = [store.add_lesson(1, "lesson", f"lesson {i}") for i in range(12)]
    assert store.kept_lessons() == []  # proposed lessons are not injected
    for i in ids:
        store.set_lesson_status(i, "kept")
    kept = store.kept_lessons()
    assert len(kept) == store.MAX_KEPT_LESSONS and kept[-1]["text"] == "lesson 11"
    store.set_lesson_status(ids[-1], "dismissed")
    assert store.kept_lessons()[-1]["text"] == "lesson 10"
    sid = store.add_lesson(1, "suggestion", "add snap counts", status="kept")
    assert all(l["kind"] == "lesson" for l in store.kept_lessons())
    assert store.lessons(kind="suggestion")[0]["id"] == sid
