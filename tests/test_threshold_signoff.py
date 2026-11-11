"""Threshold sign-off: only a named person, through an admin key, recorded in the audit log.

Guards against the failure found in audit round 4, where a sign-off was hard-coded
in source and printed in the memo although nobody had given it.
"""
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_DB_PATH", str(tmp_path / "signoff.db"))
    for m in [m for m in list(sys.modules) if m.startswith(("backend", "core", "ee", "config"))]:
        del sys.modules[m]
    import backend.deps as deps
    from backend.main import app
    client = TestClient(app, raise_server_exceptions=True)
    _, admin = deps.db.create_api_key("admin", role="admin")
    _, reviewer = deps.db.create_api_key("reviewer", role="reviewer")
    yield client, deps.db, admin, reviewer


def _fp(client, key):
    return client.get("/api/safety/thresholds", headers={"X-API-Key": key}).json()["thresholds_fingerprint"]


def test_default_is_not_signed_off(env):
    client, _db, admin, _ = env
    body = client.get("/api/safety/thresholds", headers={"X-API-Key": admin}).json()
    assert body["sign_off"]["signed_off"] is False
    assert body["sign_off"]["signer_name"] is None


def test_signoff_requires_key_admin_role_name_and_matching_fingerprint(env):
    client, _db, admin, reviewer = env
    fp = _fp(client, admin)
    ok = {"signer_name": "Ambar More", "signer_title": "Head of Model Risk", "acknowledged_fingerprint": fp}
    assert client.post("/api/safety/thresholds/sign-off", json=ok).status_code == 401
    assert client.post("/api/safety/thresholds/sign-off", json=ok, headers={"X-API-Key": reviewer}).status_code == 403
    assert client.post("/api/safety/thresholds/sign-off", json={**ok, "signer_name": " "},
                       headers={"X-API-Key": admin}).status_code == 422
    assert client.post("/api/safety/thresholds/sign-off", json={**ok, "acknowledged_fingerprint": "0" * 64},
                       headers={"X-API-Key": admin}).status_code == 422
    after = client.get("/api/safety/thresholds", headers={"X-API-Key": admin}).json()
    assert after["sign_off"]["signed_off"] is False


def test_valid_signoff_is_recorded_and_audited(env):
    client, db, admin, _ = env
    fp = _fp(client, admin)
    r = client.post("/api/safety/thresholds/sign-off", headers={"X-API-Key": admin},
                    json={"signer_name": "Ambar More", "signer_title": "Head of Model Risk",
                          "acknowledged_fingerprint": fp, "notes": "Pilot thresholds"})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["signed_off"] is True and st["signer_name"] == "Ambar More"
    entries = [e for e in db.list_audit_log(limit=50) if getattr(e, "action", None) == "safety_thresholds.signed_off"
               or (isinstance(e, dict) and e.get("action") == "safety_thresholds.signed_off")]
    assert len(entries) == 1
    assert db.verify_audit_chain()["ok"] is True


def test_signoff_goes_stale_when_thresholds_change(env):
    client, db, admin, _ = env
    fp = _fp(client, admin)
    client.post("/api/safety/thresholds/sign-off", headers={"X-API-Key": admin},
                json={"signer_name": "Ambar More", "signer_title": "Head of Model Risk", "acknowledged_fingerprint": fp})
    import core.safety.signoff as so
    changed = {k: dict(v) for k, v in so.DEFAULT_THRESHOLDS.items()}
    changed["HIGH"]["asr_pooled"] = 0.03
    with patch.object(so, "DEFAULT_THRESHOLDS", changed):
        st = so.get_signoff_status(db)
    assert st["signed_off"] is False and st["stale"] is True
    assert st["previous_signer_name"] == "Ambar More"


def test_failed_audit_write_stores_no_signoff(env):
    _client, db, _admin, _ = env
    import core.safety.signoff as so
    with patch.object(db, "add_audit_log", side_effect=RuntimeError("audit disk full")):
        with pytest.raises(RuntimeError):
            so.record_signoff(db, signer_name="Ambar More", signer_title="Head of Model Risk",
                              acknowledged_fingerprint=so.thresholds_fingerprint())
    assert so.get_signoff_status(db)["signed_off"] is False


def test_memo_reports_real_signoff_status():
    from core.compliance_reporter import ValidationEvidence, _generate_findings, _signoff_text
    from core.safety.signoff import not_signed_off
    en, ar = _signoff_text(not_signed_off())
    assert "NOT signed off" in en and "غير معتمدة" in ar
    en, _ = _signoff_text({"signed_off": True, "signer_name": "Ambar More",
                           "signer_title": "Head of Model Risk", "signed_off_at": "2026-11-11T08:00:00+00:00"})
    assert "Ambar More" in en and "2026-11-11" in en


def test_no_signoff_is_hard_coded_in_source():
    pattern = re.compile(r"""["']signed_off["']\s*:\s*True""")
    offenders = []
    for folder in ("core", "backend", "ee", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"Hard-coded sign-off found in: {offenders}"
