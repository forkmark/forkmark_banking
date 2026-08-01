"""providers repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class ProvidersMixin:
    def create_provider(self, name: str, provider_type: str = "openai",
                        base_url: str = "", api_key: str = "",
                        is_default: bool = False) -> dict:
        """Create an LLM provider entry. API key is encrypted at rest."""
        now = datetime.now(timezone.utc).isoformat()
        pid = str(uuid.uuid4())
        encrypted = _encrypt_setting(api_key) if api_key else ""
        with self._conn() as c:
            # If this is the first provider or is_default, clear other defaults
            if is_default:
                c.execute("UPDATE llm_providers SET is_default=0")
            # If no providers exist yet, make this one the default
            existing = c.fetchone("SELECT COUNT(*) AS cnt FROM llm_providers")
            cnt = _row(existing).get("cnt", 0)
            if cnt == 0:
                is_default = True
            c.execute("""INSERT INTO llm_providers
                (id, name, provider_type, base_url, api_key_encrypted,
                 is_default, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (pid, name, provider_type, base_url, encrypted,
                 int(is_default), now, now))
        return {
            "id": pid, "name": name, "provider_type": provider_type,
            "base_url": base_url, "is_default": bool(is_default),
            "created_at": now, "updated_at": now,
        }

    def list_providers(self) -> list:
        """List all providers. API keys are NOT returned."""
        with self._read_conn() as c:
            rows = c.fetchall(
                "SELECT id, name, provider_type, base_url, api_key_encrypted, "
                "is_default, created_at, updated_at "
                "FROM llm_providers ORDER BY is_default DESC, created_at ASC")
        result = []
        for r in rows:
            rd = _row(r)
            # Mask the key — never return raw
            encrypted = rd.pop("api_key_encrypted", "")
            has_key = bool(encrypted)
            masked = ""
            if has_key:
                try:
                    raw = _decrypt_setting(encrypted)
                    masked = raw[:4] + "..." + raw[-4:] if len(raw) > 8 else "***"
                except Exception:
                    masked = "***"
            rd["api_key_set"] = has_key
            rd["api_key_masked"] = masked
            rd["is_default"] = bool(rd.get("is_default"))
            result.append(rd)
        return result

    def get_provider(self, provider_id: str) -> Optional[dict]:
        """Get a single provider (without raw key)."""
        with self._read_conn() as c:
            r = c.fetchone(
                "SELECT id, name, provider_type, base_url, api_key_encrypted, "
                "is_default, created_at, updated_at "
                "FROM llm_providers WHERE id=?", (provider_id,))
        if not r:
            return None
        rd = _row(r)
        encrypted = rd.pop("api_key_encrypted", "")
        has_key = bool(encrypted)
        masked = ""
        if has_key:
            try:
                raw = _decrypt_setting(encrypted)
                masked = raw[:4] + "..." + raw[-4:] if len(raw) > 8 else "***"
            except Exception:
                masked = "***"
        rd["api_key_set"] = has_key
        rd["api_key_masked"] = masked
        rd["is_default"] = bool(rd.get("is_default"))
        return rd

    def get_provider_credentials(self, provider_id: str) -> Optional[dict]:
        """Get provider credentials (decrypted key + base_url) for LLM calls.
        Internal use only — never expose via API.
        """
        with self._read_conn() as c:
            r = c.fetchone(
                "SELECT base_url, api_key_encrypted, provider_type "
                "FROM llm_providers WHERE id=?", (provider_id,))
        if not r:
            return None
        rd = _row(r)
        encrypted = rd.get("api_key_encrypted", "")
        api_key = _decrypt_setting(encrypted) if encrypted else ""
        return {
            "api_key": api_key,
            "base_url": rd.get("base_url", ""),
            "provider_type": rd.get("provider_type", "openai"),
        }

    def get_default_provider_credentials(self) -> Optional[dict]:
        """Get the default provider's credentials. Falls back to legacy settings."""
        with self._read_conn() as c:
            r = c.fetchone(
                "SELECT id, base_url, api_key_encrypted, provider_type "
                "FROM llm_providers WHERE is_default=1 LIMIT 1")
        if r:
            rd = _row(r)
            encrypted = rd.get("api_key_encrypted", "")
            api_key = _decrypt_setting(encrypted) if encrypted else ""
            if api_key:
                return {
                    "api_key": api_key,
                    "base_url": rd.get("base_url", ""),
                    "provider_type": rd.get("provider_type", "openai"),
                    "provider_id": rd.get("id"),
                }
        # Fallback to legacy settings
        return None

    def update_provider(self, provider_id: str, *,
                        name: str = None, provider_type: str = None,
                        base_url: str = None, api_key: str = None,
                        is_default: bool = None) -> Optional[dict]:
        """Update a provider. Pass api_key="" to keep the existing key."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            existing = c.fetchone("SELECT * FROM llm_providers WHERE id=?",
                                  (provider_id,))
            if not existing:
                return None
            rd = _row(existing)
            if name is not None:
                rd["name"] = name
            if provider_type is not None:
                rd["provider_type"] = provider_type
            if base_url is not None:
                rd["base_url"] = base_url
            if api_key is not None and api_key != "":
                rd["api_key_encrypted"] = _encrypt_setting(api_key)
            if is_default is True:
                c.execute("UPDATE llm_providers SET is_default=0")
                rd["is_default"] = 1
            rd["updated_at"] = now
            c.execute("""UPDATE llm_providers
                SET name=?, provider_type=?, base_url=?, api_key_encrypted=?,
                    is_default=?, updated_at=?
                WHERE id=?""",
                (rd["name"], rd["provider_type"], rd["base_url"],
                 rd["api_key_encrypted"], rd["is_default"], now, provider_id))
        return self.get_provider(provider_id)

    def delete_provider(self, provider_id: str) -> bool:
        """Delete a provider. Cannot delete the last default provider."""
        with self._conn() as c:
            r = c.fetchone("SELECT is_default FROM llm_providers WHERE id=?",
                           (provider_id,))
            if not r:
                return False
            rd = _row(r)
            if rd.get("is_default"):
                # Check if there are other providers
                cnt = c.fetchone(
                    "SELECT COUNT(*) AS cnt FROM llm_providers WHERE id != ?",
                    (provider_id,))
                if _row(cnt).get("cnt", 0) > 0:
                    # Promote the oldest remaining provider to default
                    c.execute("""UPDATE llm_providers SET is_default=1
                        WHERE id = (SELECT id FROM llm_providers
                                    WHERE id != ? ORDER BY created_at ASC LIMIT 1)""",
                        (provider_id,))
            c.execute("DELETE FROM llm_providers WHERE id=?", (provider_id,))
        return True

    def migrate_legacy_provider(self) -> Optional[str]:
        """Auto-migrate legacy openai_api_key setting to a provider entry.

        Called on first provider list/access. If llm_providers table is empty
        and openai_api_key exists in settings, creates a "Default" provider.
        Returns the new provider_id or None.
        """
        with self._conn() as c:
            cnt = c.fetchone("SELECT COUNT(*) AS cnt FROM llm_providers")
            if _row(cnt).get("cnt", 0) > 0:
                return None  # Already have providers
            # Check for legacy key
            row = c.fetchone("SELECT value FROM settings WHERE key='openai_api_key'")
            if not row:
                return None
            raw_key = _row(row).get("value", "")
            if not raw_key:
                return None
            # Decrypt if needed
            decrypted = _decrypt_setting(raw_key)
            if not decrypted:
                return None
            # Get legacy base_url
            url_row = c.fetchone("SELECT value FROM settings WHERE key='openai_base_url'")
            base_url = _row(url_row).get("value", "") if url_row else ""
            # Determine provider type from base_url
            ptype = "openai"
            if base_url:
                lower = base_url.lower()
                if "openrouter" in lower:
                    ptype = "openrouter"
                elif "anthropic" in lower:
                    ptype = "anthropic"
                elif "localhost" in lower or "127.0.0.1" in lower:
                    ptype = "ollama"
            now = datetime.now(timezone.utc).isoformat()
            pid = str(uuid.uuid4())
            encrypted = _encrypt_setting(decrypted)
            c.execute("""INSERT INTO llm_providers
                (id, name, provider_type, base_url, api_key_encrypted,
                 is_default, created_at, updated_at)
                VALUES (?,?,?,?,?,1,?,?)""",
                (pid, "Default (migrated)", ptype, base_url, encrypted, now, now))
        return pid

    # ── Flywheel 1: test-case metadata ────────────────────────────────────────

