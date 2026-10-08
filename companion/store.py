from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

from .domain import Lesson, Profile


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize_schema()

    def initialize_schema(self):
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (uid INTEGER PRIMARY KEY, profile TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS schedules (uid INTEGER PRIMARY KEY REFERENCES users(uid) ON DELETE CASCADE, content TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS personal (uid INTEGER PRIMARY KEY REFERENCES users(uid) ON DELETE CASCADE, content TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS drafts (uid INTEGER PRIMARY KEY REFERENCES users(uid) ON DELETE CASCADE, content TEXT NOT NULL, meta TEXT NOT NULL, nonce TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (uid INTEGER PRIMARY KEY REFERENCES users(uid) ON DELETE CASCADE, content TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS overrides (uid INTEGER REFERENCES users(uid) ON DELETE CASCADE, day TEXT, lesson_id TEXT, content TEXT NOT NULL, PRIMARY KEY(uid,day,lesson_id));
                CREATE TABLE IF NOT EXISTS deliveries (uid INTEGER REFERENCES users(uid) ON DELETE CASCADE, key TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(uid,key));
                CREATE TABLE IF NOT EXISTS events (uid INTEGER REFERENCES users(uid) ON DELETE CASCADE, day TEXT, name TEXT, happened TEXT NOT NULL, PRIMARY KEY(uid,day,name));
            """)

    @contextmanager
    def connect(self):
        import sqlite3

        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def ensure(self, uid):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO users(uid,profile) VALUES (?,?)", (uid, Profile().model_dump_json())
            )

    def exists(self, uid):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone() is not None

    def profile(self, uid):
        with self.connect() as db:
            row = db.execute("SELECT profile FROM users WHERE uid=?", (uid,)).fetchone()
        if row is None:
            raise ValueError("Сначала нажмите /start")
        return Profile.model_validate_json(row[0])

    def save_profile(self, uid, p):
        p = Profile.model_validate(p.model_dump())
        with self.connect() as db:
            db.execute(
                "UPDATE users SET profile=?,revision=revision+1 WHERE uid=?", (p.model_dump_json(), uid)
            )

    def users(self):
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT uid FROM users")]

    def revision(self, uid):
        with self.connect() as db:
            r = db.execute("SELECT revision FROM users WHERE uid=?", (uid,)).fetchone()
            return r[0] if r else -1

    def lessons(self, uid):
        return self.base_lessons(uid) + self.personal(uid)

    def base_lessons(self, uid):
        with self.connect() as db:
            r = db.execute("SELECT content FROM schedules WHERE uid=?", (uid,)).fetchone()
        main = [Lesson.model_validate(x) for x in json.loads(r[0])] if r else []
        return main

    def record_event(self, uid, day, name, happened):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?)", (uid, str(day), name, happened.isoformat())
            )

    def events(self, uid, day):
        with self.connect() as db:
            return dict(db.execute("SELECT name,happened FROM events WHERE uid=? AND day=?", (uid, str(day))))

    def apply_config(self, uid, p, lessons, personal, overrides):
        # One transaction: profile and all timetable changes either commit together or roll back.
        p = Profile.model_validate(p.model_dump())
        with self.connect() as db:
            db.execute(
                "UPDATE users SET profile=?,revision=revision+1 WHERE uid=?", (p.model_dump_json(), uid)
            )
            for table, values in [("schedules", lessons), ("personal", personal)]:
                content = json.dumps([x.model_dump(mode="json") for x in values], ensure_ascii=False)
                db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?)", (uid, content))
            db.execute("DELETE FROM overrides WHERE uid=?", (uid,))
            db.executemany(
                "INSERT INTO overrides VALUES (?,?,?,?)",
                [
                    (uid, x["day"], x["lesson_id"], json.dumps(x["change"], ensure_ascii=False))
                    for x in overrides
                ],
            )
            db.execute("DELETE FROM drafts WHERE uid=?", (uid,))

    def personal(self, uid):
        with self.connect() as db:
            r = db.execute("SELECT content FROM personal WHERE uid=?", (uid,)).fetchone()
        return [Lesson.model_validate(x) for x in json.loads(r[0])] if r else []

    def add_personal(self, uid, lessons):
        existing = self.personal(uid)
        known = {x.id for x in self.lessons(uid)}
        if any(x.id in known for x in lessons):
            raise ValueError("Это событие уже есть в расписании")
        content = json.dumps([x.model_dump(mode="json") for x in existing + lessons], ensure_ascii=False)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO personal VALUES (?,?)", (uid, content))
            db.execute("UPDATE users SET revision=revision+1 WHERE uid=?", (uid,))

    def replace(self, uid, lessons):
        content = json.dumps([x.model_dump(mode="json") for x in lessons], ensure_ascii=False)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO schedules VALUES (?,?)", (uid, content))
            db.execute("UPDATE users SET revision=revision+1 WHERE uid=?", (uid,))

    def draft(self, uid, lessons, meta, nonce):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO drafts VALUES (?,?,?,?)",
                (
                    uid,
                    json.dumps([x.model_dump(mode="json") for x in lessons], ensure_ascii=False),
                    json.dumps(meta, ensure_ascii=False),
                    nonce,
                ),
            )

    def get_draft(self, uid, nonce):
        with self.connect() as db:
            r = db.execute("SELECT content,meta FROM drafts WHERE uid=? AND nonce=?", (uid, nonce)).fetchone()
        return ([Lesson.model_validate(x) for x in json.loads(r[0])], json.loads(r[1])) if r else None

    def clear_draft(self, uid):
        with self.connect() as db:
            db.execute("DELETE FROM drafts WHERE uid=?", (uid,))

    def set_session(self, uid, data):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?)", (uid, json.dumps(data, ensure_ascii=False))
            )

    def session(self, uid):
        with self.connect() as db:
            r = db.execute("SELECT content FROM sessions WHERE uid=?", (uid,)).fetchone()
        return json.loads(r[0]) if r else {}

    def override(self, uid, day, lesson_id, content):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO overrides VALUES (?,?,?,?)",
                (uid, str(day), lesson_id, json.dumps(content, ensure_ascii=False)),
            )
            db.execute("UPDATE users SET revision=revision+1 WHERE uid=?", (uid,))

    def overrides(self, uid, day):
        with self.connect() as db:
            return {
                r[0]: json.loads(r[1])
                for r in db.execute(
                    "SELECT lesson_id,content FROM overrides WHERE uid=? AND day=?", (uid, str(day))
                )
            }

    def claim(self, uid, key, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO deliveries(uid,key,status) VALUES (?,?,'pending')", (uid, key))
            r = db.execute(
                "UPDATE deliveries SET status='sending', attempts=attempts+1, next_at=? WHERE uid=? AND key=? AND status<>'sent' AND next_at<=?",
                (now + 120, uid, key, now),
            )
            return r.rowcount == 1

    def sent(self, uid, key):
        with self.connect() as db:
            db.execute("UPDATE deliveries SET status='sent' WHERE uid=? AND key=?", (uid, key))

    def retry(self, uid, key, seconds=60):
        with self.connect() as db:
            db.execute(
                "UPDATE deliveries SET status='pending',next_at=? WHERE uid=? AND key=?",
                (time.time() + seconds, uid, key),
            )

    def delete(self, uid):
        with self.connect() as db:
            db.execute("DELETE FROM users WHERE uid=?", (uid,))

    def export(self, uid):
        with self.connect() as db:
            events = [
                dict(day=r[0], name=r[1], happened=r[2])
                for r in db.execute("SELECT day,name,happened FROM events WHERE uid=?", (uid,))
            ]
            overrides = [
                dict(day=r[0], lesson_id=r[1], change=json.loads(r[2]))
                for r in db.execute("SELECT day,lesson_id,content FROM overrides WHERE uid=?", (uid,))
            ]
        return {
            "profile": self.profile(uid).model_dump(mode="json"),
            "lessons": [x.model_dump(mode="json") for x in self.lessons(uid)],
            "overrides": overrides,
            "events": events,
        }
