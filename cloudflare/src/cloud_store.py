"""DB-API adapter over Durable Object SQL; user-facing code uses the original Store."""

from contextlib import contextmanager

from companion.store import Store


class Cursor:
    def __init__(self, cursor, changed=0):
        columns = list(cursor.columnNames)
        rows = cursor.toArray()
        if hasattr(rows, "to_py"):
            rows = rows.to_py()
        self.rows = [tuple(row[k] for k in columns) for row in rows]
        self.rowcount = changed

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class Connection:
    def __init__(self, storage):
        self.storage = storage

    def execute(self, sql, args=()):
        cursor = self.storage.sql.exec(sql, *args)
        result = Cursor(cursor)
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            changed = self.storage.sql.exec("SELECT changes() AS n").one()
            result.rowcount = int(changed["n"] if isinstance(changed, dict) else changed.n)
        return result

    def executemany(self, sql, rows):
        for row in rows:
            self.execute(sql, row)

    def executescript(self, script):
        for sql in script.split(";"):
            if sql.strip() and "journal_mode" not in sql:
                self.execute(sql)


class CloudStore(Store):
    def __init__(self, storage):
        self.storage = storage
        self.db = Connection(storage)
        self.path = "cloudflare:durable-object"
        self.initialize_schema()

    @contextmanager
    def connect(self):
        yield self.db

    def apply_config(self, *args):
        return self.storage.transactionSync(lambda: super(CloudStore, self).apply_config(*args))

    def delete(self, uid):
        def erase():
            for table in ("schedules", "personal", "drafts", "sessions", "overrides", "deliveries", "events"):
                self.db.execute(f"DELETE FROM {table} WHERE uid=?", (uid,))
            super(CloudStore, self).delete(uid)
            self.db.execute("DELETE FROM cloud_outbox WHERE uid=?", (uid,))

        return self.storage.transactionSync(erase)
