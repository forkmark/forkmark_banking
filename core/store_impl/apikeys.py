"""apikeys repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class ApiKeysMixin:
    _VALID_ROLES = ("viewer", "reviewer", "admin")

    def create_api_key(self, name: str, role: str = "admin") -> tuple:
        """Create a new API key hashed with argon2id (includes embedded salt).

        role: RBAC role granted to the key — 'viewer' (read-only), 'reviewer'
        (read + record decisions / manage inventory), or 'admin' (full access,
        including key and audit management). Defaults to 'admin' for backward
        compatibility with pre-RBAC deployments.
        """
        if _ph is None:
            raise RuntimeError(
                "argon2-cffi is required for API key management. "
                "Install it: pip install argon2-cffi"
            )
        if role not in self._VALID_ROLES:
            raise ValueError(
                f"invalid role {role!r}; must be one of {self._VALID_ROLES}"
            )
        import secrets
        raw      = "fm_" + secrets.token_urlsafe(32)
        key_hash = _ph.hash(raw)   # argon2id; salt is embedded in the hash string
        now      = datetime.now(timezone.utc)
        ak = ApiKey(
            id=str(uuid.uuid4()), name=name, key_hash=key_hash,
            key_prefix=raw[:8], created_at=now, role=role,
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO api_keys (id,name,key_hash,key_prefix,created_at,role) "
                "VALUES (?,?,?,?,?,?)",
                (ak.id, ak.name, ak.key_hash, ak.key_prefix,
                 ak.created_at.isoformat(), ak.role))
        return ak, raw

    def verify_api_key(self, raw_key: str) -> Optional[ApiKey]:
        """Verify an API key.

        Algorithm:
          1. Check bounded LRU TTL cache — avoids argon2 computation on hot paths.
          2. Look up candidate rows by key_prefix (fast indexed lookup).
          3. Verify with argon2id; auto-upgrade legacy SHA-256 keys on first match.
          4. Cache the result for _VERIFY_TTL seconds; debounce last_used_at DB write.
        """
        import hashlib
        # ── 1. Cache hit ──────────────────────────────────────────────────────
        now_ts = time.time()
        with _verify_lock:
            entry = _verify_cache.get(raw_key)
            if entry and entry[0] > now_ts:
                return entry[1]

        # ── 2. Look up by key_prefix ──────────────────────────────────────────
        key_prefix  = raw_key[:8]
        matched_row = None
        needs_upgrade = False

        with self._conn() as c:
            rows = c.fetchall(
                "SELECT * FROM api_keys WHERE key_prefix=? AND is_active=1", (key_prefix,))

            for raw_row in rows:
                row    = _row(raw_row)
                stored = row["key_hash"]
                try:
                    if stored.startswith("$argon2") and _ph:
                        # argon2id path — uses module-level singleton
                        _ph.verify(stored, raw_key)
                        matched_row = row
                        if _ph.check_needs_rehash(stored):
                            c.execute("UPDATE api_keys SET key_hash=? WHERE id=?",
                                      (_ph.hash(raw_key), row["id"]))
                    else:
                        # Legacy SHA-256 — verify and schedule upgrade
                        if hashlib.sha256(raw_key.encode()).hexdigest() == stored:
                            matched_row = row
                            needs_upgrade = True
                    if matched_row:
                        break
                except (VerifyMismatchError, VerificationError, InvalidHashError):
                    continue
                except Exception:
                    continue

            if matched_row is None:
                _cache_put(raw_key, (now_ts + _VERIFY_TTL, None))
                return None

            # ── 3. Auto-upgrade legacy SHA-256 key to argon2id ────────────────
            if needs_upgrade and _ph:
                c.execute("UPDATE api_keys SET key_hash=? WHERE id=?",
                          (_ph.hash(raw_key), matched_row["id"]))

            # ── 4. Debounce last_used_at write (max 1 write per 60s per key) ──
            last = matched_row.get("last_used_at")
            now  = datetime.now(timezone.utc)
            if not last or (now - datetime.fromisoformat(last)).total_seconds() > 60:
                c.execute("UPDATE api_keys SET last_used_at=? WHERE id=?",
                          (now.isoformat(), matched_row["id"]))

        ak = ApiKey.from_row(matched_row)
        _cache_put(raw_key, (now_ts + _VERIFY_TTL, ak))
        return ak

    def list_api_keys(self, active_only: bool = True) -> List[ApiKey]:
        sql = ("SELECT * FROM api_keys WHERE is_active=1 ORDER BY created_at DESC"
               if active_only else
               "SELECT * FROM api_keys ORDER BY created_at DESC")
        with self._conn() as c:
            rows = c.fetchall(sql)
        return [ApiKey.from_row(_row(r)) for r in rows]

    def revoke_api_key(self, key_id: str):
        with self._conn() as c:
            c.execute("UPDATE api_keys SET is_active=0 WHERE id=?", (key_id,))

    # ── Settings ──────────────────────────────────────────────────────────────

