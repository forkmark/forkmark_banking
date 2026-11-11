"""Threshold sign-off: a named validator approves the exact safety thresholds in force.

A sign-off is valid only for the thresholds it was given on. If DEFAULT_THRESHOLDS
change, the stored fingerprint no longer matches and the status reverts to
"not signed off" until someone signs again. Every sign-off is written to the
tamper-evident audit log in the same transaction as the record itself.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from core.safety.thresholds import DEFAULT_THRESHOLDS

SETTING_KEY = "safety_threshold_signoff"


def thresholds_fingerprint(thresholds: Optional[dict] = None) -> str:
    """SHA-256 over a canonical JSON encoding of the thresholds."""
    payload = json.dumps(thresholds if thresholds is not None else DEFAULT_THRESHOLDS,
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def not_signed_off(reason: str = "No validator has signed off the safety thresholds.") -> dict[str, Any]:
    return {
        "signed_off": False,
        "stale": False,
        "signer_name": None,
        "signer_title": None,
        "signed_off_at": None,
        "recorded_by_key": None,
        "thresholds_fingerprint": thresholds_fingerprint(),
        "notes": None,
        "reason": reason,
    }


def get_signoff_status(db: Any) -> dict[str, Any]:
    """Current sign-off status. Never invents a signer: missing or stale means not signed off."""
    raw = db.get_setting(SETTING_KEY)
    if not raw:
        return not_signed_off()
    try:
        rec = json.loads(raw)
    except (TypeError, ValueError):
        return not_signed_off("Stored sign-off record is unreadable.")
    current = thresholds_fingerprint()
    if rec.get("thresholds_fingerprint") != current:
        status = not_signed_off("Thresholds changed after the last sign-off; a new sign-off is required.")
        status.update({"stale": True, "previous_signer_name": rec.get("signer_name"),
                       "previous_signed_off_at": rec.get("signed_off_at")})
        return status
    return {
        "signed_off": bool(rec.get("signer_name")) and rec.get("thresholds_fingerprint") == current,
        "stale": False,
        "signer_name": rec.get("signer_name"),
        "signer_title": rec.get("signer_title"),
        "signed_off_at": rec.get("signed_off_at"),
        "recorded_by_key": rec.get("recorded_by_key"),
        "thresholds_fingerprint": current,
        "notes": rec.get("notes"),
        "reason": None,
    }


def record_signoff(db: Any, *, signer_name: str, signer_title: str, acknowledged_fingerprint: str,
                   notes: str = "", actor: str = "system", actor_role: str = "") -> dict[str, Any]:
    """Store a sign-off and its audit entry atomically. Raises ValueError on bad input."""
    signer_name = (signer_name or "").strip()
    signer_title = (signer_title or "").strip()
    if len(signer_name) < 2:
        raise ValueError("signer_name is required: the person giving the sign-off, by name.")
    if not signer_title:
        raise ValueError("signer_title is required.")
    current = thresholds_fingerprint()
    if acknowledged_fingerprint != current:
        raise ValueError("acknowledged_fingerprint does not match the thresholds in force; "
                         "fetch GET /api/safety/thresholds and sign off the values shown.")
    rec = {
        "signer_name": signer_name,
        "signer_title": signer_title,
        "signed_off_at": datetime.now(timezone.utc).isoformat(),
        "recorded_by_key": actor,
        "thresholds_fingerprint": current,
        "thresholds": DEFAULT_THRESHOLDS,
        "notes": (notes or "").strip() or None,
    }
    with db._conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SETTING_KEY, json.dumps(rec, sort_keys=True)),
        )
        db.add_audit_log(
            "safety_thresholds.signed_off",
            actor=actor, actor_role=actor_role,
            resource_type="safety_thresholds", resource_id=current[:16],
            detail={"signer_name": signer_name, "signer_title": signer_title,
                    "thresholds_fingerprint": current, "notes": rec["notes"]},
            conn=c,
        )
    return get_signoff_status(db)
