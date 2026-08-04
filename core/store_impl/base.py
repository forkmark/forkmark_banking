"""Forkmark database layer — SQLite by default, PostgreSQL via DATABASE_URL.

PostgreSQL support requires psycopg2-binary:
    pip install psycopg2-binary

Set FM_DATABASE_URL=postgresql://user:pass@host:5432/dbname to enable PostgreSQL.
"""

from __future__ import annotations
import base64, hashlib, json, logging, os, re, sqlite3, threading, time, uuid

_log = logging.getLogger("forkmark.store")
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

# ── Argon2id hasher singleton ─────────────────────────────────────────────────
# Instantiated once at import time so verify_api_key and create_api_key never
# pay module-import overhead inside hot request paths.
try:
    from argon2 import PasswordHasher as _PH
    from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
    _ph = _PH()
except ImportError:
    _ph = None  # type: ignore
    VerifyMismatchError = VerificationError = InvalidHashError = Exception  # type: ignore

# ── API key verify cache ───────────────────────────────────────────────────────
# Maps raw_key → (expire_timestamp, ApiKey | None).
# Bounded LRU (max 2048 entries) so long-running servers with key rotation
# don't leak memory indefinitely.
_VERIFY_CACHE_MAX = 2048
_verify_cache: OrderedDict = OrderedDict()
_verify_lock  = threading.Lock()
_VERIFY_TTL   = 60.0  # seconds


def _cache_put(key: str, value: tuple) -> None:
    """Insert/update cache entry, evicting LRU entries over the size cap."""
    with _verify_lock:
        if key in _verify_cache:
            _verify_cache.move_to_end(key)
        _verify_cache[key] = value
        while len(_verify_cache) > _VERIFY_CACHE_MAX:
            _verify_cache.popitem(last=False)  # pop oldest

from core.models import (
    Workflow, WorkflowRun, Branch, StepOutput,
    Comparison, Decision, ApiKey,
    TestSet, TestCase, EvalRun,
    RunStatus, DecisionChoice, ConfidenceLevel, EvalRunStatus, ScoringStatus,
)


# ── Sensitive settings encryption ────────────────────────────────────────────
# Encrypts values like openai_api_key at rest using Fernet if `cryptography`
# is available and FM_SECRET_KEY is set.  Falls back to plaintext with a
# one-time warning.
_SENSITIVE_KEYS = frozenset({"openai_api_key", "provider_api_key"})
_ENC_PREFIX = "enc::"  # marker for encrypted values in the DB

try:
    from cryptography.fernet import Fernet as _Fernet, MultiFernet as _MultiFernet
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC as _PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes as _hashes

    # Stretch FM_SECRET_KEY into the at-rest key with PBKDF2-HMAC-SHA256. A
    # per-value salt isn't stored (the value format is unchanged), so a fixed
    # application salt + high iteration count is used to slow brute force of a
    # weak secret — far stronger than the previous single unsalted SHA-256.
    _FM_KDF_SALT = b"forkmark.fernet.kdf.v1"
    _FM_KDF_ITERS = 200_000

    def _derive_fernet_key(secret: str) -> bytes:
        """PBKDF2-HMAC-SHA256 (200k iters) → 32-byte url-safe-base64 Fernet key."""
        kdf = _PBKDF2HMAC(algorithm=_hashes.SHA256(), length=32,
                          salt=_FM_KDF_SALT, iterations=_FM_KDF_ITERS)
        return base64.urlsafe_b64encode(kdf.derive(secret.encode()))

    def _derive_legacy_fernet_key(secret: str) -> bytes:
        """Legacy unsalted single-SHA-256 derivation. Retained ONLY so values
        encrypted by older versions remain decryptable."""
        return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())

    _fm_secret = os.getenv("FM_SECRET_KEY", "")
    if _fm_secret:
        # Encrypt with the new (PBKDF2) key; decrypt with new OR legacy key.
        # MultiFernet encrypts with the first key and tries all keys to decrypt.
        _fernet = _MultiFernet([
            _Fernet(_derive_fernet_key(_fm_secret)),
            _Fernet(_derive_legacy_fernet_key(_fm_secret)),
        ])
    else:
        _fernet = None
except ImportError:
    _fernet = None


def _encrypt_setting(value: str) -> str:
    """Encrypt a sensitive setting value if Fernet is available."""
    if _fernet is None:
        return value
    return _ENC_PREFIX + _fernet.encrypt(value.encode()).decode()


def _decrypt_setting(value: str) -> str:
    """Decrypt a sensitive setting value if it has the enc:: prefix."""
    if not value.startswith(_ENC_PREFIX):
        return value  # plaintext (legacy or no encryption)
    if _fernet is None:
        return value  # can't decrypt — return raw (will look garbled)
    try:
        return _fernet.decrypt(value[len(_ENC_PREFIX):].encode()).decode()
    except Exception:
        return value  # decryption failed — return as-is


# ── SQLite adapter ────────────────────────────────────────────────────────────

class _SQLiteConn:
    """SQLite connection manager with separate read/write paths.

    Write path: single persistent connection behind a threading.Lock
    (SQLite allows only one writer at a time).

    Read path: thread-local connections so concurrent readers don't block
    each other (WAL mode supports this natively).
    """
    def __init__(self, path: str):
        self._path = path
        self._write_lock = threading.Lock()
        self._write_conn: Optional[sqlite3.Connection] = None
        self._local = threading.local()

    def _get_write_conn(self) -> sqlite3.Connection:
        if self._write_conn is None:
            self._write_conn = sqlite3.connect(self._path, check_same_thread=False)
            self._write_conn.row_factory = sqlite3.Row
            # Bounded wait on a contended lock. Without this the default is 0 —
            # which, combined with WAL and a stray open connection to the same
            # file, can leave a writer (e.g. an executescript migration) blocked
            # indefinitely instead of failing fast. A server must never hang on
            # the DB; 5s then SQLITE_BUSY.
            self._write_conn.execute("PRAGMA busy_timeout=5000")
            self._write_conn.execute("PRAGMA journal_mode=WAL")
            self._write_conn.execute("PRAGMA foreign_keys=ON")
        return self._write_conn

    def _get_read_conn(self) -> sqlite3.Connection:
        """Thread-local read connection — no lock needed, WAL allows concurrent readers."""
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA query_only=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close the write connection and this thread's read connection.

        Lets a caller that is discarding a Store (notably the test suites that
        rebuild the app per test) release the underlying SQLite file handles and
        WAL locks instead of leaking them until garbage collection.
        """
        if self._write_conn is not None:
            try:
                self._write_conn.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._write_conn = None
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._local.conn = None

    @contextmanager
    def connect(self):
        with self._write_lock:
            conn = self._get_write_conn()
            try:
                yield _SQLiteWrapper(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def read_connect(self):
        """Read-only connection — no lock, concurrent readers allowed."""
        conn = self._get_read_conn()
        yield _SQLiteWrapper(conn)


class _SQLiteWrapper:
    def __init__(self, conn):
        self._c = conn

    def execute(self, sql, params=()):
        return self._c.execute(sql, params)

    def executemany(self, sql, params_seq):
        return self._c.executemany(sql, params_seq)

    def executescript(self, sql):
        self._c.executescript(sql)

    def fetchall(self, sql, params=()):
        return self._c.execute(sql, params).fetchall()

    def fetchone(self, sql, params=()):
        return self._c.execute(sql, params).fetchone()


# ── SQL dialect helpers (literal-aware) ───────────────────────────────────────
# ForkMark authors SQL in a SQLite dialect and translates it for psycopg2. The
# two transforms below are literal-aware: they never touch text inside single- or
# double-quoted string/identifier literals (or PostgreSQL dollar-quoted strings),
# which removes the fragility of the previous blind str.replace()/str.split(";").

def _pg_translate(sql: str) -> str:
    """Translate SQLite-style SQL to psycopg2 form.

    psycopg2 performs printf-style parameter interpolation over the *entire*
    query string, so every literal '%' must be doubled to '%%' — including inside
    string literals (a ``LIKE '%foo%'`` must become ``LIKE '%%foo%%'``), otherwise
    psycopg2 misreads it as a format specifier.

    The '?' -> '%s' placeholder conversion, by contrast, is literal-aware: '?' is
    not special to psycopg2, so a '?' that appears inside a '...'/"..." literal is
    left untouched and only bare positional placeholders are converted. This is
    the fragility the previous blind ``str.replace('?', '%s')`` had.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    in_squote = in_dquote = False
    while i < n:
        ch = sql[i]
        # '%' is always escaped for psycopg2, regardless of literal state.
        if ch == "%":
            out.append("%%")
            i += 1
            continue
        if in_squote:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":  # escaped '' inside a literal
                    out.append("'")
                    i += 2
                    continue
                in_squote = False
            i += 1
            continue
        if in_dquote:
            out.append(ch)
            if ch == '"':
                in_dquote = False
            i += 1
            continue
        if ch == "'":
            in_squote = True
            out.append(ch)
        elif ch == '"':
            in_dquote = True
            out.append(ch)
        elif ch == "?":
            out.append("%s")   # bare placeholder (only reached outside literals)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z0-9_]*\$")


def _split_sql_statements(sql: str) -> List[str]:
    """Split a multi-statement script on semicolons *outside* string literals.

    Respects single-quoted strings (with '' escapes), double-quoted identifiers,
    and PostgreSQL dollar-quoted bodies ($$ ... $$ or $tag$ ... $tag$), so a
    semicolon inside a literal or a function body does not split the statement.
    Returns the non-empty, stripped statements in order.
    """
    stmts: List[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_squote = in_dquote = False
    dollar_tag: Optional[str] = None
    while i < n:
        ch = sql[i]
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue
        if in_squote:
            buf.append(ch)
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_squote = False
            i += 1
            continue
        if in_dquote:
            buf.append(ch)
            if ch == '"':
                in_dquote = False
            i += 1
            continue
        if ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dquote = True
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                dollar_tag = m.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue
        if ch == ";":
            stmts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return [s for s in stmts if s]


# ── JSON storage helpers (dialect-aware) ──────────────────────────────────────
# On PostgreSQL, JSON-bearing columns are stored as JSONB (indexable, queryable);
# on SQLite they remain TEXT. Writes use json.dumps() for both — a JSON string is
# implicitly cast into a JSONB column by PostgreSQL — while reads must accept
# either a str (SQLite/TEXT) or an already-parsed dict/list (psycopg2 returns
# JSONB pre-decoded). ``_json_load`` normalises both.

def _json_col_type(is_postgres: bool) -> str:
    """Return the column type for a JSON payload: JSONB on PG, TEXT on SQLite."""
    return "JSONB" if is_postgres else "TEXT"


def _json_load(value, default=None):
    """Decode a JSON column value from either dialect.

    - None / empty      -> ``default`` (or {})
    - dict / list        -> returned as-is (already parsed by psycopg2 for JSONB)
    - str                -> json.loads(...)
    """
    if default is None:
        default = {}
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


# ── PostgreSQL adapter ────────────────────────────────────────────────────────

class _PostgreSQLConn:
    """psycopg2-backed adapter with the same interface as _SQLiteConn."""

    def __init__(self, url: str):
        self._url = url
        self._pool = None

    def close(self) -> None:
        """Close the connection pool if one was opened."""
        if self._pool is not None:
            try:
                self._pool.closeall()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._pool = None

    def _get_pool(self):
        if self._pool is None:
            try:
                import psycopg2.pool
            except ImportError:
                raise ImportError(
                    "psycopg2-binary is required for PostgreSQL support.\n"
                    "Install it: pip install psycopg2-binary"
                )
            self._pool = psycopg2.pool.ThreadedConnectionPool(
                2,
                max(20, int(os.getenv("FM_BACKGROUND_WORKERS", "10")) + 10),
                self._url,
            )
        return self._pool

    @contextmanager
    def connect(self):
        import psycopg2.extras
        pool = self._get_pool()
        conn = pool.getconn()
        try:
            yield _PGWrapper(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)

    @contextmanager
    def read_connect(self):
        """PG uses connection pool — reads go through the same pool."""
        with self.connect() as c:
            yield c


class _PGWrapper:
    """Wraps a psycopg2 connection with the same interface as _SQLiteWrapper.

    Handles two key differences from SQLite:
      - Parameter placeholders: ? → %s
      - executescript: split on ; and execute each statement individually
    """

    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _pg(sql: str) -> str:
        """Translate SQLite-style SQL to psycopg2 form (literal-aware).

        Delegates to :func:`_pg_translate`, which escapes '%' -> '%%' and
        converts '?' -> '%s' only *outside* string/identifier literals, so a '?'
        or '%' embedded in a string constant (e.g. ``LIKE '20%'``) is preserved
        rather than mangled into a parameter placeholder.
        """
        return _pg_translate(sql)

    def _cursor(self):
        import psycopg2.extras
        return self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql, params=()):
        cur = self._cursor()
        cur.execute(self._pg(sql), params)
        return cur

    def executemany(self, sql, params_seq):
        cur = self._cursor()
        cur.executemany(self._pg(sql), params_seq)
        return cur

    def executescript(self, sql: str):
        """Execute multiple DDL statements atomically.

        Skips PRAGMA (SQLite-only), tolerates 'already exists' errors.
        All statements run in a single transaction — either all succeed or
        all roll back (prevents half-applied migrations).
        """
        cur = self._conn.cursor()
        for stmt in _split_sql_statements(sql):
            if stmt.upper().startswith("PRAGMA"):
                continue
            # Each statement runs inside its own SAVEPOINT. On PostgreSQL a
            # failed statement aborts the entire transaction, so "tolerate the
            # error and keep going" is only possible if the failure is rolled
            # back to a savepoint first — otherwise every later statement dies
            # with InFailedSqlTransaction.
            cur.execute("SAVEPOINT fm_stmt")
            try:
                cur.execute(stmt)
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT fm_stmt")
                msg = str(e).lower()
                if "already exists" not in msg and "duplicate" not in msg:
                    self._conn.rollback()
                    raise
                # Tolerated: the savepoint rollback left the txn usable.
            else:
                cur.execute("RELEASE SAVEPOINT fm_stmt")
        self._conn.commit()

    def fetchall(self, sql, params=()):
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        return rows if rows else []

    def fetchone(self, sql, params=()):
        cur = self.execute(sql, params)
        return cur.fetchone()


# ── Row helper ────────────────────────────────────────────────────────────────

def _row(r) -> dict:
    """Normalise sqlite3.Row / psycopg2 RealDictRow / plain dict → dict."""
    if r is None:
        return {}
    return dict(r)


# ── Training-data prompt reconstruction ───────────────────────────────────────
# Fine-tuning exports must use the prompt the model *actually received* — the
# rendered system/user turns stored on each step's input_messages — not the raw
# input-variable dict. These helpers reconstruct that, with a safe fallback to
# the legacy json.dumps(input_data) for old rows that have no input_messages.

def _prompt_messages(input_messages) -> List[dict]:
    """Return the non-assistant turns (system/user/...) from a step's
    input_messages, in order. These are the messages that elicited the output."""
    out: List[dict] = []
    for m in (input_messages or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role == "assistant" or content in (None, ""):
            continue
        out.append({"role": role or "user", "content": content})
    return out


def _extract_prompt_text(input_messages, input_data) -> str:
    """Flatten the real input the model received into a single prompt string.

    Joins the system/user turn contents in order. Falls back to the legacy
    json.dumps(input_data) only when no usable input_messages exist (older rows),
    so historical exports remain reproducible.
    """
    msgs = _prompt_messages(input_messages)
    if msgs:
        return "\n\n".join(m["content"] for m in msgs).strip()
    return json.dumps(input_data) if input_data else ""


# ── Cost estimation ───────────────────────────────────────────────────────────

# Prices in USD per 1M tokens — updated periodically.
# Override or extend via FM_COST_TABLE_JSON env var (JSON dict).
_DEFAULT_PRICES: Dict[str, Dict[str, float]] = {
    # OpenAI
    "gpt-4o":            {"input": 2.50, "output": 10.00},
    "gpt-4o-mini":       {"input": 0.15, "output": 0.60},
    "gpt-4-turbo":       {"input": 10.00, "output": 30.00},
    "gpt-4":             {"input": 30.00, "output": 60.00},
    "gpt-3.5-turbo":     {"input": 0.50, "output": 1.50},
    "o1":                {"input": 15.00, "output": 60.00},
    "o1-mini":           {"input": 3.00, "output": 12.00},
    "o3-mini":           {"input": 1.10, "output": 4.40},
    # Anthropic
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku":  {"input": 0.80, "output": 4.00},
    "claude-3-opus":     {"input": 15.00, "output": 75.00},
    "claude-3-sonnet":   {"input": 3.00, "output": 15.00},
    "claude-3-haiku":    {"input": 0.25, "output": 1.25},
    # Google
    "gemini-1.5-pro":    {"input": 1.25, "output": 5.00},
    "gemini-1.5-flash":  {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash":  {"input": 0.10, "output": 0.40},
    # Meta (self-hosted prices are $0 but we estimate typical API provider rates)
    "llama-3.1-70b":     {"input": 0.59, "output": 0.79},
    "llama-3.1-8b":      {"input": 0.10, "output": 0.10},
}

_price_cache: Optional[Dict[str, Dict[str, float]]] = None
_price_cache_env: Optional[str] = None   # tracks FM_COST_TABLE_JSON value


def update_pricing_table(new_prices: Dict[str, Dict[str, float]]):
    """Update in-memory price table from an external source (e.g. LiteLLM).
    Expects prices in USD per 1M tokens.
    """
    global _price_cache
    _DEFAULT_PRICES.update(new_prices)
    _price_cache = None  # invalidate cache


# ── On-disk price cache ───────────────────────────────────────────────────────
# Persist the (LiteLLM-synced) price table so prices survive restarts and are
# available with zero network calls — important for offline/air-gapped self-hosts
# whose whole reason for self-hosting is "no bytes leave the network".
_PRICE_CACHE_FILE = Path(
    os.getenv("FM_HOME", str(Path.home() / ".forkmark"))) / "price_table.json"


def save_pricing_cache(prices: Optional[Dict[str, Dict[str, float]]] = None) -> None:
    """Write the current (post-merge) price table to disk. Best-effort."""
    try:
        data = prices if prices is not None else _DEFAULT_PRICES
        _PRICE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PRICE_CACHE_FILE.write_text(json.dumps(data))
    except Exception:
        _log.debug("Could not write price cache", exc_info=True)


def load_cached_pricing() -> bool:
    """Load the on-disk price cache into the table if present. Returns True when
    loaded — lets the server start with last-known prices and no network call."""
    try:
        if _PRICE_CACHE_FILE.is_file():
            data = json.loads(_PRICE_CACHE_FILE.read_text())
            if isinstance(data, dict) and data:
                update_pricing_table(data)
                return True
    except Exception:
        _log.debug("Could not read price cache", exc_info=True)
    return False


def _get_price_table() -> Dict[str, Dict[str, float]]:
    """Return the model price table, optionally extended via env var.

    Cached — only rebuilt when update_pricing_table() is called or
    FM_COST_TABLE_JSON env var changes.
    """
    global _price_cache, _price_cache_env
    current_env = os.getenv("FM_COST_TABLE_JSON")
    if _price_cache is not None and _price_cache_env == current_env:
        return _price_cache
    prices = dict(_DEFAULT_PRICES)
    if current_env:
        try:
            prices.update(json.loads(current_env))
        except (json.JSONDecodeError, TypeError):
            pass
    _price_cache = prices
    _price_cache_env = current_env
    return prices

# Compiled once at module level — avoids re.compile() on every _estimate_cost call.
_VERSION_SUFFIX_RE = re.compile(
    r'(-\d{4}(-\d{2}(-\d{2})?)?|-preview|-latest|-turbo|-instruct'
    r'|-vision|-mini|-nano|-pro|-ultra|-flash|-exp|-beta|:\d+)$'
)


def _estimate_cost(model_id: str, tokens_input: int, tokens_output: int) -> Optional[float]:
    """Estimate USD cost for a step from token counts and model pricing.

    Returns None if the model is not in the price table.
    Uses prefix matching so 'gpt-4o-2024-08-06' matches 'gpt-4o'.
    """
    prices = _get_price_table()

    # Try exact match first, then prefix match (longest prefix wins)
    price = prices.get(model_id)
    if not price:
        # Prefix match — only accept known version/date-style suffixes to avoid
        # false matches on fine-tuned IDs (e.g. "gpt-4o-mini-ft-acme" must NOT
        # match "gpt-4o-mini").  Accepted suffixes: date stamps, -preview,
        # -latest, -turbo, -instruct, -vision, -mini, -nano, -pro, -ultra,
        # -flash, -exp, -beta, and snapshot tags like ":20240801".
        candidates = [
            (k, v) for k, v in prices.items()
            if model_id.startswith(k) and _VERSION_SUFFIX_RE.match(model_id[len(k):])
        ]
        if candidates:
            # Pick longest matching prefix for specificity
            candidates.sort(key=lambda x: len(x[0]), reverse=True)
            price = candidates[0][1]

    if not price:
        return None

    cost = (tokens_input * price["input"] + tokens_output * price["output"]) / 1_000_000
    return round(cost, 8)  # sub-cent precision


# ── Database ──────────────────────────────────────────────────────────────────


def _is_pg(c) -> bool:
    """True when ``c`` is the PostgreSQL connection wrapper."""
    return type(c).__name__ == "_PGWrapper"


# ── Audit-log hash chaining (tamper-evidence) ──────────────────────────────────
# Each audit entry stores a SHA-256 over its own fields plus the previous entry's
# hash, forming a chain. Editing, deleting, or reordering any entry breaks the
# chain from that point on, so a privileged operator (e.g. a DBA) cannot silently
# alter the supervisory record — ``verify_audit_chain()`` detects it.
_AUDIT_GENESIS_HASH = "0" * 64


def _audit_canonical_detail(detail) -> str:
    """Canonical JSON for the audit ``detail`` payload, stable across SQLite
    (stored as TEXT) and PostgreSQL (JSONB, read back as a dict) so the hash
    reproduces identically on either dialect."""
    if isinstance(detail, str):
        try:
            detail = json.loads(detail or "{}")
        except (ValueError, TypeError):
            detail = {}
    return json.dumps(detail or {}, sort_keys=True, separators=(",", ":"))


def _audit_entry_hash(prev_hash, entry_id, ts, actor, actor_role, action,
                      resource_type, resource_id, detail, ip) -> str:
    """Deterministic SHA-256 chaining one audit entry to ``prev_hash``.

    ``detail`` may be a dict or a JSON string; it is canonicalised first so the
    write-time and verify-time hashes match regardless of storage dialect.
    """
    payload = "\x1f".join([
        prev_hash or _AUDIT_GENESIS_HASH,
        str(entry_id or ""), str(ts or ""), str(actor or ""),
        str(actor_role or ""), str(action or ""), str(resource_type or ""),
        str(resource_id or ""), _audit_canonical_detail(detail), str(ip or ""),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _column_exists(c, table: str, col: str) -> bool:
    """Whether ``table.col`` is already present, without provoking an error.

    Asking first matters on PostgreSQL. A statement that fails there aborts the
    entire surrounding transaction, and every later statement in it raises
    InFailedSqlTransaction. Catching the ALTER's "already exists" error after
    the fact is therefore not enough — by then the transaction is unusable, and
    the *next* migration statement dies with an unrelated message. SQLite has no
    such behaviour, which is why this only ever surfaced against Postgres.
    """
    if _is_pg(c):
        rows = c.fetchall(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = ? AND column_name = ?",
            (table, col),
        )
        return bool(rows)
    rows = c.fetchall(f"PRAGMA table_info({table})")
    names = set()
    for r in rows:
        try:
            names.add(r["name"])
        except (TypeError, IndexError, KeyError):
            if len(r) > 1:
                names.add(r[1])
    return col in names


def _add_column(c, table: str, col: str, typedef: str):
    """Add a column when it isn't already there.

    Several columns are created by ``_init()``'s CREATE TABLE *and* re-added by
    an older migration. That is harmless on SQLite but fatal on PostgreSQL, so
    the existence check above runs first. The try/except is retained only as a
    backstop for races and for dialects where the catalog lookup is unavailable.
    """
    if _column_exists(c, table, col):
        return
    try:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
    except Exception as e:
        msg = str(e).lower()
        if "duplicate column" not in msg and "already exists" not in msg:
            raise


def _migration_v1(c):
    """v1: Add eval_run support columns."""
    _add_column(c, "workflows",     "eval_run_count",        "INTEGER DEFAULT 0")
    _add_column(c, "workflow_runs", "eval_run_id",            "TEXT")
    _add_column(c, "workflow_runs", "test_case_label",        "TEXT DEFAULT ''")
    _add_column(c, "comparisons",   "eval_run_id",            "TEXT")
    _add_column(c, "comparisons",   "test_case_label",        "TEXT DEFAULT ''")
    _add_column(c, "comparisons",   "divergence_score",       "REAL")
    _add_column(c, "comparisons",   "step_divergence_scores", "TEXT DEFAULT '{}'")
    _add_column(c, "decisions",     "eval_run_id",            "TEXT")
    _add_column(c, "decisions",     "updated_at",             "TEXT")


def _migration_v2(c):
    """v2: Add async scoring, eval results, test set versioning, OTel tracing."""
    _add_column(c, "comparisons",   "eval_results",    "TEXT DEFAULT '{}'")
    _add_column(c, "comparisons",   "scoring_status",  "TEXT DEFAULT 'completed'")
    _add_column(c, "test_sets",     "version",         "INTEGER DEFAULT 1")
    _add_column(c, "test_sets",     "is_frozen",       "INTEGER DEFAULT 0")
    _add_column(c, "step_outputs",  "trace_id",        "TEXT")
    _add_column(c, "step_outputs",  "span_id",         "TEXT")


def _migration_v3(c):
    """v3: Add expected_output to test_cases and cost_usd to step_outputs."""
    _add_column(c, "test_cases",    "expected_output",  "TEXT")
    _add_column(c, "step_outputs",  "cost_usd",         "REAL")


def _migration_v4(c):
    """v4 — Flywheel 1: enrich test_cases with domain/industry/use-case metadata.

    New columns on test_cases:
      domain         – high-level domain (e.g. 'customer_support', 'legal', 'healthcare')
      industry       – vertical (e.g. 'ecommerce', 'finserv', 'healthcare')
      use_case_type  – 'safety' | 'edge_case' | 'regression' | 'happy_path' | 'adversarial'
      failure_mode   – what failure the test is designed to catch (free text)
      test_goal      – what quality signal this case measures (free text)

    Also creates the test_case_performance table for cases where
    the DDL ran before this migration was added (existing DBs).
    """
    _add_column(c, "test_cases", "domain",        "TEXT NOT NULL DEFAULT ''")
    _add_column(c, "test_cases", "industry",      "TEXT NOT NULL DEFAULT ''")
    _add_column(c, "test_cases", "use_case_type", "TEXT NOT NULL DEFAULT ''")
    _add_column(c, "test_cases", "failure_mode",  "TEXT NOT NULL DEFAULT ''")
    _add_column(c, "test_cases", "test_goal",     "TEXT NOT NULL DEFAULT ''")
    # Create flywheel tables if not yet present (idempotent via IF NOT EXISTS)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS test_case_performance (
            id               TEXT PRIMARY KEY,
            test_case_label  TEXT NOT NULL,
            workflow_id      TEXT NOT NULL,
            eval_run_id      TEXT NOT NULL,
            comparison_id    TEXT,
            divergence_score REAL,
            decision_choice  TEXT,
            reviewer_confidence TEXT,
            recorded_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tc_perf_label
            ON test_case_performance(test_case_label, workflow_id);
        CREATE INDEX IF NOT EXISTS idx_tc_perf_eval_run
            ON test_case_performance(eval_run_id);
    """)


def _migration_v5(c):
    """v5 — Flywheel 2: enrich decisions with provenance + category; add
    reviewer_profiles and data_consent tables.

    New columns on decisions:
      provenance_hash – SHA-256(workflow_id:label:input_snippet) for
                        cross-customer correlation without exposing raw text
      data_category   – auto-classified category tag (safety, legal, billing, …)
    """
    _add_column(c, "decisions", "provenance_hash", "TEXT NOT NULL DEFAULT ''")
    _add_column(c, "decisions", "data_category",   "TEXT NOT NULL DEFAULT ''")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS reviewer_profiles (
            reviewer_id      TEXT PRIMARY KEY,
            display_name     TEXT NOT NULL DEFAULT '',
            role             TEXT NOT NULL DEFAULT 'reviewer',
            expertise_level  TEXT NOT NULL DEFAULT 'intermediate',
            domain_expertise TEXT NOT NULL DEFAULT '[]',
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS data_consent (
            id           TEXT PRIMARY KEY,
            scope        TEXT NOT NULL DEFAULT 'global',
            workflow_id  TEXT,
            consent_type TEXT NOT NULL,
            granted_by   TEXT NOT NULL,
            granted_at   TEXT NOT NULL,
            expires_at   TEXT,
            is_active    INTEGER NOT NULL DEFAULT 1,
            notes        TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_consent_scope
            ON data_consent(scope, workflow_id, consent_type, is_active);
    """)


def _migration_v6(c):
    """v6 — Collaboration: comments, review assignments, review status tracking.

    New tables:
      comments           – threaded comments on comparisons/decisions
      review_assignments – assign comparisons to reviewers with status tracking
    New columns:
      comparisons.review_status   – pending/assigned/reviewed/skipped
      comparisons.assigned_to     – reviewer ID for queue management
    """
    _add_column(c, "comparisons", "review_status", "TEXT NOT NULL DEFAULT 'pending'")
    _add_column(c, "comparisons", "assigned_to", "TEXT NOT NULL DEFAULT ''")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS comments (
            id              TEXT PRIMARY KEY,
            comparison_id   TEXT NOT NULL,
            author_id       TEXT NOT NULL,
            author_name     TEXT NOT NULL DEFAULT '',
            body            TEXT NOT NULL,
            parent_id       TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            is_resolved     INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_comments_comparison
            ON comments(comparison_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_comments_parent
            ON comments(parent_id);

        CREATE TABLE IF NOT EXISTS review_assignments (
            id              TEXT PRIMARY KEY,
            eval_run_id     TEXT NOT NULL,
            comparison_id   TEXT NOT NULL,
            reviewer_id     TEXT NOT NULL,
            assigned_by     TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'pending',
            assigned_at     TEXT NOT NULL,
            completed_at    TEXT,
            notes           TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_assignments_reviewer
            ON review_assignments(reviewer_id, status);
        CREATE INDEX IF NOT EXISTS idx_assignments_eval_run
            ON review_assignments(eval_run_id, status);
        CREATE INDEX IF NOT EXISTS idx_assignments_comparison
            ON review_assignments(comparison_id);
    """)


# ── Migration strategy ─────────────────────────────────────────────────────
#
# Forkmark uses TWO complementary migration systems:
#
#   1. store.py inline DDL (below) — workspace-level tables (workflows, runs,
#      branches, step_outputs, comparisons, decisions, test_sets, eval_runs).
#      Runs on BOTH SQLite (dev) and PostgreSQL (prod).  Applied automatically
#      on Database.__init__ via _migrate().
#
#   2. Alembic migrations (migrations/versions/) — multi-tenant control plane
#      tables (organizations, workspaces, users, workspace_memberships,
#      api_keys_v2, audit_log).  PostgreSQL ONLY.  Run via `alembic upgrade head`.
#
# These are NOT duplicates: (1) handles data that lives *inside* each workspace
# schema, while (2) handles data in the shared public schema.
#
# Future workspace-level schema changes should be added as new entries in
# _MIGRATIONS below.  Control-plane changes go in migrations/versions/.
# ───────────────────────────────────────────────────────────────────────────

def _migration_v7(c):
    """v7 — Provider registry: dedicated table for LLM providers with encrypted keys.

    Also adds provider_id columns to branches and eval_runs so each branch
    can independently track which provider was used.
    """
    c.executescript("""
        CREATE TABLE IF NOT EXISTS llm_providers (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            provider_type   TEXT NOT NULL DEFAULT 'openai',
            base_url        TEXT NOT NULL DEFAULT '',
            api_key_encrypted TEXT NOT NULL DEFAULT '',
            is_default      INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_providers_default
            ON llm_providers(is_default);
    """)
    _add_column(c, "branches", "provider_id", "TEXT")


# Ordered list of (version, description, function)
def _migration_v8(c):
    """v8 — Agent comparison: trace_events tree + trajectory_outcomes.

    No inbound FKs from core tables — fully additive, safe to roll back
    by dropping these two tables.
    """
    c.executescript("""
        CREATE TABLE IF NOT EXISTS trace_events (
            id TEXT PRIMARY KEY,
            branch_id TEXT NOT NULL REFERENCES branches(id),
            run_id TEXT NOT NULL REFERENCES workflow_runs(id),
            parent_event_id TEXT REFERENCES trace_events(id),
            event_type TEXT NOT NULL DEFAULT 'tool_call',
            event_index INTEGER NOT NULL DEFAULT 0,
            name TEXT NOT NULL DEFAULT '',
            input_data TEXT DEFAULT '{}',
            output_data TEXT DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'completed',
            latency_ms INTEGER DEFAULT 0,
            tokens_input INTEGER DEFAULT 0,
            tokens_output INTEGER DEFAULT 0,
            cost_usd REAL,
            metadata TEXT DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trace_events_branch
            ON trace_events(branch_id);
        CREATE INDEX IF NOT EXISTS idx_trace_events_run
            ON trace_events(run_id);
        CREATE INDEX IF NOT EXISTS idx_trace_events_parent
            ON trace_events(parent_event_id);

        CREATE TABLE IF NOT EXISTS trajectory_outcomes (
            id TEXT PRIMARY KEY,
            comparison_id TEXT NOT NULL REFERENCES comparisons(id),
            run_id TEXT NOT NULL REFERENCES workflow_runs(id),
            workflow_id TEXT NOT NULL,
            tool_sequence_score REAL DEFAULT 0.0,
            outcome_equivalence_score REAL DEFAULT 0.0,
            efficiency_score REAL DEFAULT 0.0,
            trajectory_score REAL DEFAULT 0.0,
            tool_sequence_detail TEXT DEFAULT '{}',
            outcome_detail TEXT DEFAULT '{}',
            efficiency_detail TEXT DEFAULT '{}',
            branch_a_tool_count INTEGER DEFAULT 0,
            branch_b_tool_count INTEGER DEFAULT 0,
            branch_a_depth INTEGER DEFAULT 0,
            branch_b_depth INTEGER DEFAULT 0,
            branch_a_total_latency_ms INTEGER DEFAULT 0,
            branch_b_total_latency_ms INTEGER DEFAULT 0,
            branch_a_total_cost_usd REAL DEFAULT 0.0,
            branch_b_total_cost_usd REAL DEFAULT 0.0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trajectory_outcomes_comparison
            ON trajectory_outcomes(comparison_id);
        CREATE INDEX IF NOT EXISTS idx_trajectory_outcomes_workflow
            ON trajectory_outcomes(workflow_id);
    """)
    # Add run_type column to workflow_runs for distinguishing agent runs
    _add_column(c, "workflow_runs", "run_type", "TEXT DEFAULT 'standard'")


def _migration_v9(c):
    """v9 — Model inventory: model_inventory table for MRM governance.

    Backs core.model_inventory.ModelInventory. Fully additive with no inbound FKs
    from core tables, so it is safe to roll back by dropping the table. A matching
    Alembic revision (migrations/versions/0009_add_model_inventory.py) is provided
    for teams that manage the shared PostgreSQL schema with Alembic.
    """
    c.executescript("""
        CREATE TABLE IF NOT EXISTS model_inventory (
            model_id              TEXT PRIMARY KEY,
            display_name          TEXT NOT NULL,
            provider              TEXT NOT NULL DEFAULT '',
            version               TEXT NOT NULL DEFAULT '',
            use_case              TEXT NOT NULL DEFAULT '',
            risk_tier             TEXT NOT NULL DEFAULT 'MEDIUM',
            regulatory_frameworks TEXT NOT NULL DEFAULT '[]',
            deployed_at           TEXT NOT NULL,
            last_validated_at     TEXT,
            next_validation_due   TEXT,
            owner_team            TEXT NOT NULL DEFAULT '',
            documentation_url     TEXT NOT NULL DEFAULT '',
            status                TEXT NOT NULL DEFAULT 'ACTIVE',
            present_artifacts     TEXT NOT NULL DEFAULT '[]',
            evaluation_signals    TEXT NOT NULL DEFAULT '{}',
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_model_inventory_status
            ON model_inventory(status);
        CREATE INDEX IF NOT EXISTS idx_model_inventory_due
            ON model_inventory(next_validation_due);
    """)


def _migration_v10(c):
    """v10 — Compliance report history.

    Records metadata for each validation memo generated per model, so the
    /compliance/reports/{model_id}/history endpoint can list prior reports.
    Fully additive; safe to roll back by dropping the table.
    """
    c.executescript("""
        CREATE TABLE IF NOT EXISTS compliance_reports (
            id                TEXT PRIMARY KEY,
            model_id          TEXT NOT NULL,
            framework         TEXT NOT NULL,
            generated_at      TEXT NOT NULL,
            findings_count    INTEGER NOT NULL DEFAULT 0,
            coverage_complete INTEGER NOT NULL DEFAULT 0,
            format            TEXT NOT NULL DEFAULT 'json'
        );
        CREATE INDEX IF NOT EXISTS idx_compliance_reports_model
            ON compliance_reports(model_id);
    """)


def _migration_v11(c):
    """v11 — Governance controls: API-key roles + immutable audit log.

    Adds role-based access control to API keys (default 'admin' so existing keys
    keep full access — backward compatible) and an append-only audit_log table
    recording who did what and when. Both close model-risk-management control
    gaps (segregation of duties; auditability of supervisory records). Fully
    additive and safe to roll back by dropping the audit_log table and ignoring
    the role column.
    """
    _add_column(c, "api_keys", "role", "TEXT NOT NULL DEFAULT 'admin'")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id            TEXT PRIMARY KEY,
            ts            TEXT NOT NULL,
            actor         TEXT NOT NULL DEFAULT 'system',
            actor_role    TEXT NOT NULL DEFAULT '',
            action        TEXT NOT NULL,
            resource_type TEXT NOT NULL DEFAULT '',
            resource_id   TEXT NOT NULL DEFAULT '',
            detail        TEXT NOT NULL DEFAULT '{}',
            ip            TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_audit_log_ts
            ON audit_log(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_log_resource
            ON audit_log(resource_type, resource_id);
        CREATE INDEX IF NOT EXISTS idx_audit_log_action
            ON audit_log(action);
    """)


def _migration_v12(c):
    """v12 — Store the audit-log ``detail`` payload as JSONB on PostgreSQL.

    JSONB makes the audit detail indexable and queryable (e.g. filter by a nested
    key) on production Postgres deployments. On SQLite this is a no-op — the
    column stays TEXT — so development is unaffected. Reads go through
    ``_json_load``, which accepts either a JSON string (SQLite/TEXT) or a decoded
    dict (psycopg2 returns JSONB pre-parsed), so the application code is
    dialect-agnostic.

    Only this single, centrally-read column is converted here. Extending JSONB to
    the larger entity tables (comparisons, workflow_runs, ...) is a deliberate
    follow-up: it requires every read path for those columns to go through
    ``_json_load`` first, and should be validated against a live PostgreSQL in CI.
    """
    if not _is_pg(c):
        return  # SQLite keeps JSON as TEXT — nothing to do.
    # Look before leaping: on PostgreSQL a failed statement aborts the whole
    # transaction, so a swallowed exception here would break every statement
    # that follows in this migration.
    rows = c.fetchall(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'audit_log' AND column_name = 'detail'"
    )
    if not rows:
        return  # table or column absent — nothing to convert
    row = rows[0]
    try:
        current = row["data_type"]
    except (TypeError, IndexError, KeyError):
        current = row[0]
    if str(current).lower() == "jsonb":
        return  # already converted

    # The column is declared TEXT NOT NULL DEFAULT '{}'. PostgreSQL will convert
    # the stored *data* via USING, but it refuses to cast the column DEFAULT
    # ('{}'::text) to jsonb and fails with DatatypeMismatch. Drop the default,
    # change the type, then restore the default with the correct type.
    c.execute("ALTER TABLE audit_log ALTER COLUMN detail DROP DEFAULT")
    c.execute(
        "ALTER TABLE audit_log ALTER COLUMN detail TYPE JSONB "
        "USING detail::jsonb"
    )
    c.execute("ALTER TABLE audit_log ALTER COLUMN detail SET DEFAULT '{}'::jsonb")


def _migration_v13(c):
    """v13 — Link a validation (eval) run to a governed model.

    Adds ``eval_runs.governed_model_id`` so a governed AI model's evaluation
    evidence (comparisons, decisions, evaluator results) rolls up to it — the
    keystone that lets ForkMark auto-assemble an evidence-backed validation memo.
    Nullable and backward compatible; existing rows stay NULL.
    """
    try:
        c.execute("ALTER TABLE eval_runs ADD COLUMN governed_model_id TEXT")
    except Exception:  # pragma: no cover - column already exists
        pass


def _migration_v14(c):
    """v14 — Model-level evaluation signals.

    Adds ``model_inventory.evaluation_signals`` (JSON): observed signals for a model
    such as per-group fairness scores, ingested from validation runs and used to
    auto-assemble the validation memo. Nullable/defaulted; backward compatible.
    """
    try:
        c.execute("ALTER TABLE model_inventory ADD COLUMN evaluation_signals TEXT DEFAULT '{}'")
    except Exception:  # pragma: no cover - column already exists
        pass


def _migration_v15(c):
    """v15 — Tamper-evidence for the audit log (hash chaining).

    Adds a per-entry SHA-256 (``entry_hash``) that chains to the previous entry's
    hash (``prev_hash``), ordered by a monotonic ``seq``. Any edit, delete, or
    reorder of a supervisory audit record breaks the chain, which
    ``verify_audit_chain()`` detects — closing the gap an append-only convention
    alone leaves (nothing stopped a privileged DB operator editing a row).
    Existing rows are backfilled into a valid chain in ``ts`` order. Additive and
    backward compatible.
    """
    _add_column(c, "audit_log", "seq", "INTEGER")
    _add_column(c, "audit_log", "prev_hash", "TEXT NOT NULL DEFAULT ''")
    _add_column(c, "audit_log", "entry_hash", "TEXT NOT NULL DEFAULT ''")
    # Backfill existing rows into a valid chain, ordered deterministically.
    rows = c.fetchall(
        "SELECT id, ts, actor, actor_role, action, resource_type, "
        "resource_id, detail, ip FROM audit_log ORDER BY ts ASC, id ASC"
    )
    prev = _AUDIT_GENESIS_HASH
    for i, raw in enumerate(rows, start=1):
        r = _row(raw)
        h = _audit_entry_hash(prev, r.get("id"), r.get("ts"), r.get("actor"),
                              r.get("actor_role"), r.get("action"),
                              r.get("resource_type"), r.get("resource_id"),
                              r.get("detail"), r.get("ip"))
        c.execute(
            "UPDATE audit_log SET seq=?, prev_hash=?, entry_hash=? WHERE id=?",
            (i, prev, h, r.get("id")),
        )
        prev = h
    # A unique index on ``seq`` is defence-in-depth — add_audit_log already
    # assigns seq = max(seq)+1, so duplicates should not occur. Create it
    # best-effort. On PostgreSQL a failed statement poisons the entire migration
    # transaction (every later statement then raises InFailedSqlTransaction), so
    # the attempt is fenced with a SAVEPOINT and rolled back to it on any error —
    # the same pattern the executescript path uses. If the index cannot be
    # created, application-level assignment remains the guarantee.
    if _is_pg(c):
        c.execute("SAVEPOINT fm_v15_seq_idx")
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_log_seq "
                      "ON audit_log(seq)")
        except Exception:
            c.execute("ROLLBACK TO SAVEPOINT fm_v15_seq_idx")
        else:
            c.execute("RELEASE SAVEPOINT fm_v15_seq_idx")
    else:
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_log_seq "
                      "ON audit_log(seq)")
        except Exception:  # pragma: no cover - SQLite does not poison the txn
            pass


_MIGRATIONS = [
    (1, "eval_run support columns",                          _migration_v1),
    (2, "async scoring, eval results, OTel, versioning",     _migration_v2),
    (3, "expected_output on test_cases, cost_usd on step_outputs", _migration_v3),
    (4, "flywheel-1: test_case domain metadata + performance corpus", _migration_v4),
    (5, "flywheel-2: decision provenance + reviewer_profiles + data_consent", _migration_v5),
    (6, "collaboration: comments + review assignments + review status", _migration_v6),
    (7, "provider registry: llm_providers table + branch provider_id", _migration_v7),
    (8, "agent comparison: trace_events + trajectory_outcomes", _migration_v8),
    (9, "model inventory: model_inventory table", _migration_v9),
    (10, "compliance report history", _migration_v10),
    (11, "governance controls: api-key roles + immutable audit_log", _migration_v11),
    (12, "postgres: audit_log.detail as JSONB (no-op on SQLite)", _migration_v12),
    (13, "link eval runs to a governed model", _migration_v13),
    (14, "model evaluation_signals column", _migration_v14),
    (15, "audit_log tamper-evidence: hash chaining", _migration_v15),
]



class DatabaseBase:
    """Connection management, schema init, and migrations shared by all repository mixins."""
    def __init__(self, db_path: str, database_url: str = ""):
        if database_url:
            self._adapter = _PostgreSQLConn(database_url)
        else:
            self._adapter = _SQLiteConn(str(db_path))
        self._init()
        self._migrate()

    @contextmanager
    def _conn(self):
        with self._adapter.connect() as c:
            yield c

    @contextmanager
    def _read_conn(self):
        """Read-only connection — allows concurrent readers on SQLite WAL."""
        with self._adapter.read_connect() as c:
            yield c

    def close(self) -> None:
        """Release the backing connection(s). Safe to call more than once."""
        closer = getattr(self._adapter, "close", None)
        if callable(closer):
            closer()

    @property
    def is_postgres(self) -> bool:
        """True when the backing store is PostgreSQL (drives JSONB column typing)."""
        return isinstance(self._adapter, _PostgreSQLConn)

    def _init(self):
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                run_count INTEGER DEFAULT 0,
                decision_count INTEGER DEFAULT 0,
                eval_run_count INTEGER DEFAULT 0,
                tags TEXT DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS test_sets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                workflow_id TEXT,
                created_at TEXT NOT NULL,
                version INTEGER DEFAULT 1,
                is_frozen INTEGER DEFAULT 0,
                FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS test_cases (
                id TEXT PRIMARY KEY,
                test_set_id TEXT NOT NULL,
                label TEXT NOT NULL,
                input_data TEXT DEFAULT '{}',
                expected_output TEXT,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                FOREIGN KEY (test_set_id) REFERENCES test_sets(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS eval_runs (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                test_set_id TEXT,
                governed_model_id TEXT,
                branch_a_config TEXT DEFAULT '{}',
                branch_b_config TEXT DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                total_cases INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                input_data TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}',
                sdk_key_prefix TEXT DEFAULT '',
                eval_run_id TEXT,
                test_case_label TEXT DEFAULT '',
                FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE,
                FOREIGN KEY (eval_run_id) REFERENCES eval_runs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS branches (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                name TEXT NOT NULL,
                model_id TEXT NOT NULL,
                temperature REAL DEFAULT 0.7,
                system_prompt TEXT,
                extra_config TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                is_baseline INTEGER DEFAULT 0,
                FOREIGN KEY (run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS step_outputs (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                step_name TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                input_messages TEXT DEFAULT '[]',
                output_text TEXT NOT NULL,
                model_id TEXT NOT NULL,
                temperature REAL DEFAULT 0.7,
                tokens_input INTEGER DEFAULT 0,
                tokens_output INTEGER DEFAULT 0,
                latency_ms INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                error TEXT,
                trace_id TEXT,
                span_id TEXT,
                cost_usd REAL,
                FOREIGN KEY (branch_id) REFERENCES branches(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS comparisons (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                branch_a_id TEXT NOT NULL,
                branch_b_id TEXT NOT NULL,
                step_names TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                decided INTEGER DEFAULT 0,
                decision_id TEXT,
                eval_run_id TEXT,
                test_case_label TEXT DEFAULT '',
                divergence_score REAL,
                step_divergence_scores TEXT DEFAULT '{}',
                eval_results TEXT DEFAULT '{}',
                scoring_status TEXT DEFAULT 'completed',
                FOREIGN KEY (run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (eval_run_id) REFERENCES eval_runs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY,
                comparison_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                reviewer_id TEXT NOT NULL,
                choice TEXT NOT NULL,
                confidence TEXT NOT NULL,
                rationale_for_choice TEXT NOT NULL,
                rationale_for_rejection TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                branch_winner_id TEXT,
                branch_loser_id TEXT,
                divergence_score REAL DEFAULT 0.0,
                divergence_summary TEXT,
                eval_run_id TEXT,
                FOREIGN KEY (comparison_id) REFERENCES comparisons(id) ON DELETE CASCADE,
                FOREIGN KEY (eval_run_id) REFERENCES eval_runs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                key_prefix TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                is_active INTEGER DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_runs_workflow ON workflow_runs(workflow_id);
            CREATE INDEX IF NOT EXISTS idx_runs_eval_run ON workflow_runs(eval_run_id);
            CREATE INDEX IF NOT EXISTS idx_branches_run ON branches(run_id);
            CREATE INDEX IF NOT EXISTS idx_steps_branch ON step_outputs(branch_id);
            CREATE INDEX IF NOT EXISTS idx_steps_run ON step_outputs(run_id);
            CREATE INDEX IF NOT EXISTS idx_comparisons_run ON comparisons(run_id);
            CREATE INDEX IF NOT EXISTS idx_comparisons_eval_run ON comparisons(eval_run_id);
            CREATE INDEX IF NOT EXISTS idx_decisions_workflow ON decisions(workflow_id);
            CREATE INDEX IF NOT EXISTS idx_decisions_eval_run ON decisions(eval_run_id);
            CREATE INDEX IF NOT EXISTS idx_decisions_comparison ON decisions(comparison_id);
            CREATE INDEX IF NOT EXISTS idx_test_cases_set ON test_cases(test_set_id);
            CREATE INDEX IF NOT EXISTS idx_eval_runs_workflow ON eval_runs(workflow_id);
            CREATE INDEX IF NOT EXISTS idx_comparisons_workflow ON comparisons(workflow_id);

            -- Composite index for eval run stats query (eval_run_id + divergence ordering)
            CREATE INDEX IF NOT EXISTS idx_comparisons_eval_div
                ON comparisons(eval_run_id, divergence_score DESC);

            -- Index for list_decisions ORDER BY created_at DESC
            CREATE INDEX IF NOT EXISTS idx_decisions_created
                ON decisions(created_at DESC);

            -- Index for API key lookup by prefix (needed for argon2 verification)
            CREATE INDEX IF NOT EXISTS idx_api_keys_prefix
                ON api_keys(key_prefix);

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- ── Provider registry ────────────────────────────────────────
            -- Stores LLM provider configurations with encrypted API keys.
            -- Each branch can optionally reference a provider_id for
            -- per-branch credential resolution.
            CREATE TABLE IF NOT EXISTS llm_providers (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                provider_type   TEXT NOT NULL DEFAULT 'openai',
                base_url        TEXT NOT NULL DEFAULT '',
                api_key_encrypted TEXT NOT NULL DEFAULT '',
                is_default      INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_providers_default
                ON llm_providers(is_default);

            -- ── Flywheel 1: test-case performance corpus ──────────────────────
            -- One row per (test_case_label, eval_run_id).
            -- Populated automatically when comparisons are created and decisions recorded.
            -- Powers automated test-case generation: tracks which inputs surface
            -- divergence, which models win, and reviewer confidence over time.
            CREATE TABLE IF NOT EXISTS test_case_performance (
                id               TEXT PRIMARY KEY,
                test_case_label  TEXT NOT NULL,
                workflow_id      TEXT NOT NULL,
                eval_run_id      TEXT NOT NULL,
                comparison_id    TEXT,
                divergence_score REAL,
                decision_choice  TEXT,           -- a_wins | b_wins | tie | skip
                reviewer_confidence TEXT,        -- low | medium | high
                recorded_at      TEXT NOT NULL,
                FOREIGN KEY (workflow_id)   REFERENCES workflows(id) ON DELETE CASCADE,
                FOREIGN KEY (eval_run_id)   REFERENCES eval_runs(id) ON DELETE CASCADE
            );

            -- ── Flywheel 2a: reviewer identity & expertise ────────────────────
            -- Enriches preference exports with reviewer quality metadata.
            -- AI companies pay more for data with auditable reviewer provenance.
            CREATE TABLE IF NOT EXISTS reviewer_profiles (
                reviewer_id      TEXT PRIMARY KEY,
                display_name     TEXT NOT NULL DEFAULT '',
                role             TEXT NOT NULL DEFAULT 'reviewer',
                expertise_level  TEXT NOT NULL DEFAULT 'intermediate',
                domain_expertise TEXT NOT NULL DEFAULT '[]',  -- JSON array of domain strings
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            );

            -- ── Flywheel 2b: data-sharing consent ────────────────────────────
            -- Each customer must explicitly opt in before their preference data
            -- is included in anonymized B2B exports.
            CREATE TABLE IF NOT EXISTS data_consent (
                id           TEXT PRIMARY KEY,
                scope        TEXT NOT NULL DEFAULT 'global', -- global | workflow
                workflow_id  TEXT,                           -- NULL when scope='global'
                consent_type TEXT NOT NULL,                  -- training_data | anonymized_export | aggregated_stats
                granted_by   TEXT NOT NULL,
                granted_at   TEXT NOT NULL,
                expires_at   TEXT,                          -- NULL = no expiry
                is_active    INTEGER NOT NULL DEFAULT 1,
                notes        TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_tc_perf_label
                ON test_case_performance(test_case_label, workflow_id);
            CREATE INDEX IF NOT EXISTS idx_tc_perf_eval_run
                ON test_case_performance(eval_run_id);
            CREATE INDEX IF NOT EXISTS idx_consent_scope
                ON data_consent(scope, workflow_id, consent_type, is_active);
            """)

    def _migrate(self):
        """Run versioned schema migrations.

        Uses a `schema_version` table to track which migrations have been applied.
        Each migration function is numbered and runs exactly once in order.
        New migrations should be appended to the _MIGRATIONS list.
        """
        # Ensure schema_version table exists
        with self._conn() as c:
            try:
                c.execute("""CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )""")
            except Exception:
                pass  # Table might already exist in some form

        # Get current version
        with self._conn() as c:
            row = c.fetchone("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
            current_version = _row(row).get("v", 0)

        # Run pending migrations
        for version, description, fn in _MIGRATIONS:
            if version <= current_version:
                continue
            try:
                with self._conn() as c:
                    fn(c)
                    c.execute(
                        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                        (version, datetime.now(timezone.utc).isoformat()),
                    )
                import logging
                logging.getLogger("forkmark.migrations").info(
                    f"Applied migration v{version}: {description}"
                )
            except Exception as e:
                msg = str(e).lower()
                # Tolerate "already exists" / "duplicate column" from re-runs
                if "already exists" in msg or "duplicate column" in msg:
                    # Record it as applied so we don't retry
                    with self._conn() as c:
                        try:
                            c.execute(
                                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                                (version, datetime.now(timezone.utc).isoformat()),
                            )
                        except Exception:
                            pass
                else:
                    raise




    # ── Stats (SQL-based, no Python counting) ─────────────────────────────────



# Re-export every module-level name (incl. underscore helpers) so the
# domain mixins can do `from core.store_impl.base import *`.
__all__ = [k for k in list(globals().keys()) if not k.startswith('__')]
