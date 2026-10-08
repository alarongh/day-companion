import importlib.util
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion.configuration import Configuration, prepare, snapshot
from companion.domain import Lesson

SOURCE = Path(__file__).resolve().parents[1] / "cloudflare" / "src" / "cloud_store.py"
spec = importlib.util.spec_from_file_location("cloud_store_test", SOURCE)
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


class Array(list):
    def to_py(self):
        return list(self)


class SQL:
    def __init__(self, db):
        self.db = db

    def exec(self, sql, *values):
        cursor = self.db.execute(sql, values)
        names = [x[0] for x in cursor.description or []]
        rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        return SimpleNamespace(
            columnNames=names, toArray=lambda: Array(rows), one=lambda: SimpleNamespace(**rows[0])
        )


class Storage:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.sql = SQL(self.db)

    def transactionSync(self, callback):
        self.db.commit()
        with self.db:
            return callback()


def test_cloud_adapter_config_claim_private_delete():
    store = cloud.CloudStore(Storage())
    store.db.execute("CREATE TABLE cloud_outbox (uid INTEGER)")
    store.ensure(1)
    store.ensure(2)
    store.replace(1, [Lesson(id="one", title="Math", date=date(2026, 10, 8), start="09:00", end="10:00")])
    store.record_event(1, date(2026, 10, 8), "home", datetime.now(timezone.utc))
    assert store.claim(1, "notice", 1)
    assert not store.claim(1, "notice", 2)
    store.sent(1, "notice")
    assert not store.claim(1, "notice", 1000)
    config = Configuration.model_validate(
        {
            "format": "day-companion/v2",
            "mode": "patch",
            "base_revision": snapshot(store, 1)["base_revision"],
            "profile": {"sleep": {"hours": 8.5}},
        }
    )
    store.apply_config(1, *prepare(store, 1, config)[0])
    assert store.profile(1).sleep.hours == 8.5
    assert len(store.lessons(1)) == 1 and not store.lessons(2)
    store.delete(1)
    assert not store.exists(1) and store.exists(2)
    assert not store.lessons(1) and not store.events(1, date(2026, 10, 8))


def test_cloud_adapter_rolls_back_partial_configuration():
    store = cloud.CloudStore(Storage())
    store.ensure(1)
    p = store.profile(1)
    p.travel = 111
    with pytest.raises(TypeError):
        store.apply_config(1, p, [], [], [{"day": "2026-10-08", "lesson_id": "x", "change": object()}])
    assert store.profile(1).travel == 45
