"""settings repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class SettingsMixin:
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as c:
            row = c.fetchone("SELECT value FROM settings WHERE key=?", (key,))
            if row is None:
                return default
            value = _row(row)["value"]
            return _decrypt_setting(value) if key in _SENSITIVE_KEYS else value

    def set_setting(self, key: str, value: str) -> None:
        stored = _encrypt_setting(value) if key in _SENSITIVE_KEYS else value
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, stored),
            )

    def get_all_settings(self) -> dict:
        with self._conn() as c:
            rows = c.fetchall("SELECT key, value FROM settings", ())
            result = {}
            for r in rows:
                rd = _row(r)
                k, v = rd["key"], rd["value"]
                result[k] = _decrypt_setting(v) if k in _SENSITIVE_KEYS else v
            return result

    # ── LLM Providers ────────────────────────────────────────────────────────

