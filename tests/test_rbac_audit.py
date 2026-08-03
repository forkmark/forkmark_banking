"""Tests for RBAC (API-key roles) and the immutable audit log.

These exercise the governance controls added to the OSS edition: role-based
segregation of duties on write/admin endpoints, and an append-only audit trail
for supervisory mutations (model inventory, key management, memo generation).
"""
from __future__ import annotations

import importlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.deps import db

client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _bind_current_app():
    """Bind ``client`` and ``db`` to the *current* app instance for every test.

    Other suites reload ``backend.*``/``core.store`` in place to rebind the DB
    path; that reassigns ``backend.deps.db`` while this module's collection-time
    ``db`` binding goes stale. The app's auth reads ``backend.deps.db`` at request
    time, so a key created via the stale ``db`` isn't found by the app — surfacing
    as a spurious 401 only in a full single-process run. Pulling both the client
    and ``db`` from the current ``sys.modules`` keeps them consistent, matching the
    self-resetting pattern the other suites already use.
    """
    global client, db
    deps = importlib.import_module("backend.deps")
    main = importlib.import_module("backend.main")
    db = deps.db
    client = TestClient(main.app)
    yield


def _mid() -> str:
    return f"m-{uuid.uuid4().hex[:10]}"


def _payload(model_id: str) -> dict:
    return {
        "model_id": model_id,
        "display_name": f"Model {model_id}",
        "provider": "anthropic",
        "version": "1.0",
        "use_case": "credit adjudication",
        "risk_tier": "HIGH",
        "regulatory_frameworks": ["cbuae_mms"],
        "deployed_at": "2025-01-01T00:00:00+00:00",
        "owner_team": "Model Risk",
        "present_artifacts": [],
    }


# ── Roles ─────────────────────────────────────────────────────────────────────


def test_api_key_roles_persist_and_validate() -> None:
    ak, raw = db.create_api_key("viewer-key", role="viewer")
    assert ak.role == "viewer"
    assert db.verify_api_key(raw).role == "viewer"
    # New keys default to least-privilege 'viewer'; elevated roles must be
    # requested explicitly.
    ak2, _ = db.create_api_key("defaulted-key")
    assert ak2.role == "viewer"
    with pytest.raises(ValueError):
        db.create_api_key("bad", role="superuser")


def test_rbac_blocks_viewer_writes_but_allows_reads() -> None:
    _, viewer = db.create_api_key("viewer-w", role="viewer")
    _, reviewer = db.create_api_key("reviewer-w", role="reviewer")
    payload = _payload(_mid())

    # A viewer key can read...
    assert client.get("/api/inventory/models",
                      headers={"X-API-Key": viewer}).status_code == 200
    # ...but cannot write (segregation of duties).
    blocked = client.post("/api/inventory/models", json=payload,
                          headers={"X-API-Key": viewer})
    assert blocked.status_code == 403

    # A reviewer key can write.
    allowed = client.post("/api/inventory/models", json=payload,
                          headers={"X-API-Key": reviewer})
    assert allowed.status_code == 201


def test_key_management_is_admin_only() -> None:
    _, reviewer = db.create_api_key("reviewer-k", role="reviewer")
    _, admin = db.create_api_key("admin-k", role="admin")
    # Reviewer cannot list keys; admin can.
    assert client.get("/api/keys", headers={"X-API-Key": reviewer}).status_code == 403
    assert client.get("/api/keys", headers={"X-API-Key": admin}).status_code == 200


def test_new_keys_default_to_viewer_via_api() -> None:
    # An admin minting a key without naming a role gets least privilege back.
    _, admin = db.create_api_key("admin-mint", role="admin")
    r = client.post("/api/keys", json={"name": "defaulted-via-api"},
                    headers={"X-API-Key": admin})
    assert r.status_code == 201
    assert r.json()["role"] == "viewer"
    # An admin can still request an elevated role explicitly.
    r2 = client.post("/api/keys", json={"name": "explicit-reviewer",
                                        "role": "reviewer"},
                     headers={"X-API-Key": admin})
    assert r2.status_code == 201
    assert r2.json()["role"] == "reviewer"


# ── Audit log ─────────────────────────────────────────────────────────────────


def test_model_mutations_are_audited_and_log_is_admin_only() -> None:
    _, admin = db.create_api_key("admin-a", role="admin")
    _, viewer = db.create_api_key("viewer-a", role="viewer")
    mid = _mid()

    created = client.post("/api/inventory/models", json=_payload(mid),
                          headers={"X-API-Key": admin})
    assert created.status_code == 201

    # The audit log is admin-gated.
    assert client.get("/api/audit/log",
                      headers={"X-API-Key": viewer}).status_code == 403

    r = client.get("/api/audit/log?action=model.create",
                   headers={"X-API-Key": admin})
    assert r.status_code == 200
    entries = r.json()
    match = [e for e in entries if e["resource_id"] == mid]
    assert match, "model.create should have produced an audit entry"
    entry = match[0]
    assert entry["action"] == "model.create"
    assert entry["resource_type"] == "model"
    assert entry["actor_role"] == "admin"
    assert entry["detail"].get("risk_tier") == "HIGH"


def test_audit_log_is_append_only_via_store() -> None:
    before = len(db.list_audit_log(limit=1000))
    db.add_audit_log("test.event", actor="unit", resource_type="test",
                     resource_id="x1", detail={"k": "v"})
    after = db.list_audit_log(limit=1000)
    assert len(after) == before + 1
    newest = after[0]
    assert newest["action"] == "test.event"
    assert newest["detail"] == {"k": "v"}


# ── Audit-log tamper-evidence (hash chain) ─────────────────────────────────────


def _isolated_db():
    """A fresh, empty Database on a temp path. The audit-chain invariant is
    whole-table, so these tests must not share the process-wide DB (whose log
    accumulates rows from other tests and prior runs, and whose tamper case would
    otherwise leak a permanently broken row into everyone else's view)."""
    import tempfile

    from core.store import Database

    return Database(tempfile.mktemp(suffix=".db"))


def test_audit_chain_verifies_after_appends() -> None:
    fdb = _isolated_db()
    for i in range(5):
        fdb.add_audit_log("test.chain", actor="unit", resource_type="test",
                          resource_id=f"c{i}", detail={"i": i})
    result = fdb.verify_audit_chain()
    assert result["ok"] is True
    assert result["broken_at"] is None
    assert result["entries"] == 5
    assert result["checked"] == 5


def test_audit_chain_detects_tampering() -> None:
    fdb = _isolated_db()
    entry_id = fdb.add_audit_log("test.tamper", actor="unit",
                                 resource_type="test", resource_id="victim",
                                 detail={"amount": 100})
    # A normal append leaves the chain intact.
    assert fdb.verify_audit_chain()["ok"] is True
    # Simulate an out-of-band edit by a privileged operator (bypassing the API).
    with fdb._conn() as c:
        c.execute("UPDATE audit_log SET detail=? WHERE id=?",
                  (json.dumps({"amount": 999}), entry_id))
    result = fdb.verify_audit_chain()
    assert result["ok"] is False
    assert result["broken_at"]["id"] == entry_id


def test_audit_verify_endpoint_is_admin_only() -> None:
    _, admin = db.create_api_key("verify-admin", role="admin")
    _, viewer = db.create_api_key("verify-viewer", role="viewer")
    assert client.get("/api/audit/verify",
                      headers={"X-API-Key": viewer}).status_code == 403
    r = client.get("/api/audit/verify", headers={"X-API-Key": admin})
    assert r.status_code == 200
    assert "ok" in r.json()
