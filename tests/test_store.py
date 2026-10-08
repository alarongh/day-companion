from datetime import date

from companion.domain import Lesson
from companion.store import Store


def test_isolation_persistence_and_cascading_delete(tmp_path):
    path = tmp_path / "db.sqlite3"
    s = Store(path)
    for uid in (1, 2):
        s.ensure(uid)
    a = Lesson(title="Секрет пользователя 1", date=date(2026, 10, 7), start="09:00", end="10:30")
    s.replace(1, [a])
    s.draft(1, [a], {}, "nonce")
    s.override(1, a.date, a.id, {"room": "999"})
    assert not s.lessons(2)
    assert not s.get_draft(2, "nonce")
    assert not s.overrides(2, a.date)
    assert Store(path).lessons(1)[0].title == a.title
    assert s.claim(1, "key", 100)
    assert not s.claim(1, "key", 101)
    s.sent(1, "key")
    assert not Store(path).claim(1, "key", 1000)
    s.delete(1)
    assert not s.exists(1)
    assert s.exists(2)
    with s.connect() as db:
        for table in ("schedules", "personal", "drafts", "overrides", "deliveries", "sessions"):
            assert db.execute(f"SELECT count(*) FROM {table} WHERE uid=1").fetchone()[0] == 0


def test_delivery_lease_recovers_after_restart(tmp_path):
    s = Store(tmp_path / "db.sqlite3")
    s.ensure(1)
    assert s.claim(1, "key", 100)
    assert not s.claim(1, "key", 219)
    assert s.claim(1, "key", 221)


def test_process_lock_is_released(tmp_path):
    import pytest

    from companion.runtime import process_lock

    path = tmp_path / "bot.lock"
    with process_lock(path):
        with pytest.raises(SystemExit, match="уже запущен"):
            with process_lock(path):
                pass
    with process_lock(path):
        pass
