"""providers repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class ProvidersMixin:
    def create_provider(self, name: str, provider_type: str = "openai",
                        base_url: str = "", api_key: str = "",
                        is_default: bool = False,
                        deployment_kind: str = None,
                        model_family: str = "",
                        default_model: str = "",
                        azure_deployment: str = "",
                        api_version: str = "",
                        azure_deployment_type: str = "",
                        inference_region: str = "",
                        hosting: str = "bank_self_hosted",
                        vendor_name: str = "",
                        licence: str = "",
                        max_concurrency: int = 4,
                        timeout_s: float = 60.0,
                        rpm_limit: int = 0,
                        rpd_limit: int = 0,
                        max_input_tokens: int = 0) -> dict:
        """Create an LLM provider entry. API key is encrypted at rest."""
        now = datetime.now(timezone.utc).isoformat()
        pid = str(uuid.uuid4())
        encrypted = _encrypt_setting(api_key) if api_key else ""
        if not deployment_kind:
            if provider_type == "azure_openai":
                deployment_kind = "azure_openai"
            elif provider_type == "anthropic":
                deployment_kind = "anthropic"
            else:
                deployment_kind = "openai_compatible"

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
                 is_default, created_at, updated_at,
                 deployment_kind, model_family, default_model,
                 azure_deployment, api_version, azure_deployment_type,
                 inference_region, hosting, vendor_name, licence,
                 max_concurrency, timeout_s, rpm_limit, rpd_limit, max_input_tokens)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, name, provider_type, base_url, encrypted,
                 int(is_default), now, now,
                 deployment_kind, model_family or "", default_model or "",
                 azure_deployment or "", api_version or "", azure_deployment_type or "",
                 inference_region or "", hosting or "bank_self_hosted", vendor_name or "", licence or "",
                 max_concurrency or 4, timeout_s or 60.0, max(0, int(rpm_limit or 0)), max(0, int(rpd_limit or 0)), max(0, int(max_input_tokens or 0))))
        return self.get_provider(pid) or {
            "id": pid, "name": name, "provider_type": provider_type,
            "base_url": base_url, "is_default": bool(is_default),
            "created_at": now, "updated_at": now,
        }

    def list_providers(self) -> list:
        """List all providers. API keys are NOT returned."""
        with self._read_conn() as c:
            rows = c.fetchall(
                "SELECT * FROM llm_providers ORDER BY is_default DESC, created_at ASC")
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
                "SELECT * FROM llm_providers WHERE id=?", (provider_id,))
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
        """Get provider credentials (decrypted key + base_url + connector settings).
        Internal use only — never expose via API.
        """
        with self._read_conn() as c:
            r = c.fetchone(
                "SELECT * FROM llm_providers WHERE id=?", (provider_id,))
        if not r:
            return None
        rd = _row(r)
        encrypted = rd.get("api_key_encrypted", "")
        api_key = _decrypt_setting(encrypted) if encrypted else ""
        rd["api_key"] = api_key
        return rd

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
                        is_default: bool = None,
                        deployment_kind: str = None,
                        model_family: str = None,
                        default_model: str = None,
                        azure_deployment: str = None,
                        api_version: str = None,
                        azure_deployment_type: str = None,
                        inference_region: str = None,
                        hosting: str = None,
                        vendor_name: str = None,
                        licence: str = None,
                        max_concurrency: int = None,
                        timeout_s: float = None,
                        rpm_limit: int = None,
                        rpd_limit: int = None,
                        max_input_tokens: int = None) -> Optional[dict]:
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
            if deployment_kind is not None:
                rd["deployment_kind"] = deployment_kind
            if model_family is not None:
                rd["model_family"] = model_family
            if default_model is not None:
                rd["default_model"] = default_model
            if azure_deployment is not None:
                rd["azure_deployment"] = azure_deployment
            if api_version is not None:
                rd["api_version"] = api_version
            if azure_deployment_type is not None:
                rd["azure_deployment_type"] = azure_deployment_type
            if inference_region is not None:
                rd["inference_region"] = inference_region
            if hosting is not None:
                rd["hosting"] = hosting
            if vendor_name is not None:
                rd["vendor_name"] = vendor_name
            if licence is not None:
                rd["licence"] = licence
            if max_concurrency is not None:
                rd["max_concurrency"] = max_concurrency
            if timeout_s is not None:
                rd["timeout_s"] = timeout_s
            if rpm_limit is not None:
                rd["rpm_limit"] = rpm_limit
            if rpd_limit is not None:
                rd["rpd_limit"] = rpd_limit
            if max_input_tokens is not None:
                rd["max_input_tokens"] = max_input_tokens

            rd["updated_at"] = now
            c.execute("""UPDATE llm_providers
                SET name=?, provider_type=?, base_url=?, api_key_encrypted=?,
                    is_default=?, updated_at=?,
                    deployment_kind=?, model_family=?, default_model=?,
                    azure_deployment=?, api_version=?, azure_deployment_type=?,
                    inference_region=?, hosting=?, vendor_name=?, licence=?,
                    max_concurrency=?, timeout_s=?, rpm_limit=?, rpd_limit=?, max_input_tokens=?
                WHERE id=?""",
                (rd["name"], rd["provider_type"], rd["base_url"],
                 rd["api_key_encrypted"], rd["is_default"], now,
                 rd.get("deployment_kind", "openai_compatible"),
                 rd.get("model_family", ""),
                 rd.get("default_model", ""),
                 rd.get("azure_deployment", ""),
                 rd.get("api_version", ""),
                 rd.get("azure_deployment_type", ""),
                 rd.get("inference_region", ""),
                 rd.get("hosting", "bank_self_hosted"),
                 rd.get("vendor_name", ""),
                 rd.get("licence", ""),
                 rd.get("max_concurrency", 4),
                 rd.get("timeout_s", 60.0),
                 rd.get("rpm_limit") or 0,
                 rd.get("rpd_limit") or 0,
                 rd.get("max_input_tokens") or 0,
                 provider_id))
        return self.get_provider(provider_id)
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

