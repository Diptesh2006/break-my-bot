import sys
from types import SimpleNamespace

import pytest

from breakmybot import store


def test_libpq_environment_supports_passwords_with_uri_delimiters(monkeypatch):
    monkeypatch.delenv("BREAKMYBOT_DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", "postgres")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGDATABASE", "breakmybot")
    monkeypatch.setenv("PGUSER", "breakmybot")
    password = "local:@/?#%+;= password"
    monkeypatch.setenv("PGPASSWORD", password)
    calls = []

    class Cursor:
        def execute(self, *_args):
            pass

        def fetchall(self):
            return []

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    def connect(**kwargs):
        calls.append(kwargs)
        return Connection()

    monkeypatch.setitem(sys.modules, "psycopg2", SimpleNamespace(connect=connect))

    db = store.connect()

    assert calls == [{"host": "postgres", "port": "5432", "dbname": "breakmybot",
        "user": "breakmybot", "password": password, "connect_timeout": 10}]
    assert isinstance(db, store.PostgresConnection)


def test_incomplete_libpq_environment_fails_with_actionable_message(monkeypatch):
    monkeypatch.delenv("BREAKMYBOT_DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", "postgres")
    for name in ("PGDATABASE", "PGUSER", "PGPASSWORD"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="PGDATABASE, PGUSER, PGPASSWORD"):
        store.connect()
