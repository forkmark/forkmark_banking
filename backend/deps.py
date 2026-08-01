"""Shared dependencies for route modules.

All route modules import from here to access db, config, auth functions,
and common utilities. This avoids circular imports and keeps routes clean.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections import deque
from typing import Dict, Optional

from fastapi import Header, HTTPException, Request, Depends, Query

from config import config
from core.store import Database
from core.models import RunStatus, DecisionChoice, ConfidenceLevel, EvalRunStatus
from core.comparator import divergence_score, inline_diff, summarize_divergence, scorer_name
from core.background import async_enqueue_scoring


# ── Database ─────────────────────────────────────────────────────────────────

db = Database(str(config.DB_PATH), database_url=config.DATABASE_URL)

# ── Redis (optional) ─────────────────────────────────────────────────────────

import redis
redis_client = redis.from_url(config.REDIS_URL) if hasattr(config, 'REDIS_URL') and config.REDIS_URL else None


# ── Inline diff LRU cache ────────────────────────────────────────────────────

_inline_diff_cache: Dict[str, tuple] = {}
_INLINE_DIFF_MAX = 512


def cached_inline_diff(comp_id: str, text_a: str, text_b: str) -> tuple:
    """Cached wrapper — uses Redis if available, else local in-memory LRU cache."""
    cache_key = None
    if redis_client:
        try:
            cache_key = f"diff:{comp_id}:{hashlib.md5((text_a+text_b).encode()).hexdigest()[:8]}"
            cached = redis_client.get(cache_key)
            if cached:
                return tuple(json.loads(cached))
        except Exception:
            pass

    if comp_id in _inline_diff_cache:
        return _inline_diff_cache[comp_id]

    result = tuple(inline_diff(text_a, text_b))

    if redis_client and cache_key:
        try:
            redis_client.setex(cache_key, 86400, json.dumps(result))
        except Exception:
            pass

    if len(_inline_diff_cache) >= _INLINE_DIFF_MAX:
        _inline_diff_cache.pop(next(iter(_inline_diff_cache)))
    _inline_diff_cache[comp_id] = result
    return result


# ── Rate limiting ────────────────────────────────────────────────────────────

_RATE_WINDOW = 60.0
_RATE_LIMIT = int(os.getenv("FM_RATE_LIMIT", "1000"))
_MAX_RATE_KEYS = 4096


class _RateBucket:
    __slots__ = ('lock', 'timestamps')
    def __init__(self):
        self.lock = threading.Lock()
        self.timestamps: deque = deque()


_rate_buckets: dict = {}
_rate_meta_lock = threading.Lock()


def _check_rate(key_id: str) -> bool:
    """Return True if request is allowed; False if rate limit exceeded."""
    if redis_client:
        try:
            now = time.time()
            cutoff = now - _RATE_WINDOW
            redis_key = f"rate:{key_id}"
            member = str(uuid.uuid4())

            pipe = redis_client.pipeline()
            pipe.zremrangebyscore(redis_key, 0, cutoff)
            pipe.zadd(redis_key, {member: now})
            pipe.zcard(redis_key)
            pipe.expire(redis_key, int(_RATE_WINDOW + 5))
            results = pipe.execute()

            count = results[2]
            return count <= _RATE_LIMIT
        except Exception:
            pass

    bucket = _rate_buckets.get(key_id)
    if bucket is None:
        with _rate_meta_lock:
            bucket = _rate_buckets.get(key_id)
            if bucket is None:
                if len(_rate_buckets) >= _MAX_RATE_KEYS:
                    now_t = time.time()
                    cutoff_t = now_t - _RATE_WINDOW
                    for k, b in list(_rate_buckets.items()):
                        with b.lock:
                            while b.timestamps and b.timestamps[0] < cutoff_t:
                                b.timestamps.popleft()
                            if not b.timestamps:
                                _rate_buckets.pop(k, None)
                    if len(_rate_buckets) >= _MAX_RATE_KEYS:
                        to_remove = list(_rate_buckets.keys())[:_MAX_RATE_KEYS // 10]
                        for k in to_remove:
                            _rate_buckets.pop(k, None)
                bucket = _RateBucket()
                _rate_buckets[key_id] = bucket

    now = time.time()
    cutoff = now - _RATE_WINDOW
    with bucket.lock:
        q = bucket.timestamps
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= _RATE_LIMIT:
            return False
        q.append(now)
        return True


# ── Auth dependencies ────────────────────────────────────────────────────────

# ── Role-based access control ────────────────────────────────────────────────
# Ordered privilege ranks. A key's role must rank at or above the level an
# endpoint requires. Roles are additive: admin ⊇ reviewer ⊇ viewer.
#   viewer   — read-only (dashboards, coverage, history, exports)
#   reviewer — read + record decisions, manage the model inventory, generate memos
#   admin    — full access, including API-key and audit-log management
# Keys created before RBAC default to 'admin', so existing deployments are
# unaffected. RBAC is enforced only when a key is actually presented; when
# FM_REQUIRE_UI_AUTH is off and no key is sent, endpoints stay open (dev/test).
_ROLE_RANK = {"viewer": 1, "reviewer": 2, "admin": 3}


def _role_rank(role: str) -> int:
    return _ROLE_RANK.get((role or "").lower(), 0)


def principal(x_api_key: Optional[str]) -> tuple:
    """Resolve (actor, role) for audit logging from a presented key.

    Returns a short, non-secret actor label (the key prefix) and its role, or
    ("system", "") when no key is presented. Never raises — verification is
    cached, so this is cheap to call in a route handler.
    """
    if not x_api_key:
        return "system", ""
    ak = db.verify_api_key(x_api_key)
    if ak is None:
        return "unknown", ""
    return ak.key_prefix, ak.role


def require_key(x_api_key: str = Header(None, alias="X-API-Key")) -> str:
    """Hard auth — always required. Used on read-only SDK endpoints."""
    if not x_api_key:
        raise HTTPException(401, "X-API-Key header required")
    ak = db.verify_api_key(x_api_key)
    if not ak:
        raise HTTPException(401, "Invalid or revoked API key")
    if not _check_rate(ak.id):
        raise HTTPException(429, "Rate limit exceeded — max 1000 requests/minute per key")
    return x_api_key


def require_key_write(x_api_key: str = Header(None, alias="X-API-Key")) -> str:
    """Hard auth with an RBAC floor — reviewer role or higher.

    Used on every SDK endpoint that writes evaluation data. Without this a
    'viewer' key could ingest runs, branches, steps and comparisons, which
    would make read-only keys not actually read-only.
    """
    key = require_key(x_api_key)
    ak = db.verify_api_key(key)
    if _role_rank(ak.role) < _ROLE_RANK["reviewer"]:
        raise HTTPException(
            403,
            f"Insufficient role: 'reviewer' or higher required to ingest "
            f"evaluation data (this key is '{ak.role}').",
        )
    return key


def _ui_auth(request: Request, x_api_key: Optional[str], min_role: str) -> Optional[str]:
    """Shared conditional-auth core for UI endpoints with an RBAC floor.

    When a key is presented it must be valid and rank at or above ``min_role``.
    When no key is presented, access is allowed only if FM_REQUIRE_UI_AUTH is off
    (development / open mode); otherwise a key is required.
    """
    rate_id = x_api_key or (request.client.host if request.client else "unknown")
    if not _check_rate(f"ui:{rate_id}"):
        raise HTTPException(429, "Rate limit exceeded")
    if x_api_key:
        ak = db.verify_api_key(x_api_key)
        if not ak:
            raise HTTPException(401, "Invalid or revoked API key")
        if _role_rank(ak.role) < _ROLE_RANK[min_role]:
            raise HTTPException(
                403,
                f"Insufficient role: '{min_role}' or higher required "
                f"(this key is '{ak.role}').",
            )
        return x_api_key
    if config.REQUIRE_UI_AUTH:
        raise HTTPException(401, "X-API-Key required (FM_REQUIRE_UI_AUTH is enabled)")
    return None


def ui_read_auth(request: Request,
                 x_api_key: str = Header(None, alias="X-API-Key")) -> Optional[str]:
    """Conditional auth for UI read endpoints (viewer role or higher)."""
    return _ui_auth(request, x_api_key, "viewer")


def ui_write_auth(request: Request,
                  x_api_key: str = Header(None, alias="X-API-Key")) -> Optional[str]:
    """Conditional auth for UI write endpoints (reviewer role or higher).

    Enforces segregation of duties: a 'viewer' key can read but cannot record
    decisions, mutate the inventory, or generate memos.
    """
    return _ui_auth(request, x_api_key, "reviewer")


def ui_admin_auth(request: Request,
                  x_api_key: str = Header(None, alias="X-API-Key")) -> Optional[str]:
    """Conditional auth for administrative endpoints (admin role only):
    API-key management, audit-log access, and destructive maintenance."""
    return _ui_auth(request, x_api_key, "admin")


# ── Stats cache ──────────────────────────────────────────────────────────────

STATS_CACHE_TTL = 15
stats_local_cache: dict = {}
