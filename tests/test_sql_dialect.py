"""Unit tests for the literal-aware SQL dialect helpers and JSON adapters.

These cover the hardened PostgreSQL translation and multi-statement splitting
(previously naive str.replace / str.split(';')) plus the dialect-agnostic JSON
column helpers used for JSONB-on-Postgres support.
"""
from __future__ import annotations

from core.store_impl.base import (
    _json_col_type,
    _json_load,
    _pg_translate,
    _split_sql_statements,
)


# ── PostgreSQL placeholder translation ────────────────────────────────────────


def test_pg_translate_converts_placeholders_and_escapes_percent() -> None:
    assert _pg_translate("SELECT * FROM t WHERE id = ?") == "SELECT * FROM t WHERE id = %s"
    # A LIKE percent outside a literal is escaped for psycopg2.
    assert _pg_translate("x % y") == "x %% y"
    assert _pg_translate("a=? AND b=?") == "a=%s AND b=%s"


def test_pg_translate_is_literal_aware_for_placeholders() -> None:
    # A '?' inside a string literal must NOT be treated as a placeholder — this is
    # exactly what the old blind str.replace('?', '%s') got wrong.
    assert _pg_translate("SELECT '?' AS q") == "SELECT '?' AS q"
    # '%' is still escaped everywhere (psycopg2 requirement), but the literal '?'
    # stays literal while the bare placeholder becomes %s.
    assert _pg_translate("WHERE c LIKE '20%' AND id = ?") == "WHERE c LIKE '20%%' AND id = %s"
    # Escaped single quote inside a literal is handled.
    assert _pg_translate("SELECT 'O''Brien' , ?") == "SELECT 'O''Brien' , %s"


# ── Multi-statement splitting ─────────────────────────────────────────────────


def test_split_statements_basic() -> None:
    stmts = _split_sql_statements("CREATE TABLE a(x); CREATE INDEX i ON a(x);")
    assert stmts == ["CREATE TABLE a(x)", "CREATE INDEX i ON a(x)"]


def test_split_statements_ignores_semicolons_in_literals() -> None:
    sql = "INSERT INTO t VALUES ('a;b'); INSERT INTO t VALUES ('c');"
    assert _split_sql_statements(sql) == [
        "INSERT INTO t VALUES ('a;b')",
        "INSERT INTO t VALUES ('c')",
    ]


def test_split_statements_respects_dollar_quoting() -> None:
    sql = (
        "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql;"
        " SELECT 1;"
    )
    stmts = _split_sql_statements(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE FUNCTION")
    assert "RETURN 1; END;" in stmts[0]  # inner semicolons preserved
    assert stmts[1] == "SELECT 1"


# ── JSON column helpers ───────────────────────────────────────────────────────


def test_json_col_type_is_dialect_aware() -> None:
    assert _json_col_type(True) == "JSONB"
    assert _json_col_type(False) == "TEXT"


def test_json_load_accepts_str_dict_and_none() -> None:
    assert _json_load('{"a": 1}') == {"a": 1}        # SQLite / TEXT
    assert _json_load({"a": 1}) == {"a": 1}          # Postgres / JSONB (pre-parsed)
    assert _json_load([1, 2]) == [1, 2]
    assert _json_load(None) == {}                     # default
    assert _json_load("", default=[]) == []
    assert _json_load("not json", default={"fallback": True}) == {"fallback": True}
