"""safety_results repository methods for the Forkmark data layer."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.store_impl.base import _row


class SafetyMixin:
    """Methods for recording and querying deterministic safety and stress results."""

    def save_safety_result(
        self,
        eval_run_id: str,
        test_case_id: str,
        case_key: str,
        branch: str,
        category: str,
        language: str,
        severity: str,
        expected: str,
        verdict: str,
        detectors: Optional[Dict[str, Any]] = None,
        judge: Optional[Dict[str, Any]] = None,
        reply_excerpt: str = "",
        governed_model_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Save a single test case safety evaluation result."""
        now = datetime.now(timezone.utc).isoformat()
        sid = str(uuid.uuid4())
        detectors_json = json.dumps(detectors or {})
        judge_json = json.dumps(judge or {})

        with self._conn() as c:
            c.execute(
                """INSERT INTO safety_results
                (id, eval_run_id, governed_model_id, test_case_id, case_key,
                 branch, category, language, severity, expected, verdict,
                 detectors, judge, reply_excerpt, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sid,
                    eval_run_id,
                    governed_model_id,
                    test_case_id,
                    case_key,
                    branch,
                    category,
                    language,
                    severity,
                    expected,
                    verdict,
                    detectors_json,
                    judge_json,
                    reply_excerpt[:500],
                    now,
                ),
            )

        return {
            "id": sid,
            "eval_run_id": eval_run_id,
            "governed_model_id": governed_model_id,
            "test_case_id": test_case_id,
            "case_key": case_key,
            "branch": branch,
            "category": category,
            "language": language,
            "severity": severity,
            "expected": expected,
            "verdict": verdict,
            "detectors": detectors or {},
            "judge": judge or {},
            "reply_excerpt": reply_excerpt[:500],
            "created_at": now,
        }

    def list_safety_results(
        self,
        eval_run_id: Optional[str] = None,
        governed_model_id: Optional[str] = None,
        category: Optional[str] = None,
        language: Optional[str] = None,
        branch: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """List individual safety results with optional filtering."""
        query = "SELECT * FROM safety_results WHERE 1=1"
        params: list[Any] = []
        if eval_run_id:
            query += " AND eval_run_id=?"
            params.append(eval_run_id)
        if governed_model_id:
            query += " AND governed_model_id=?"
            params.append(governed_model_id)
        if category:
            query += " AND category=?"
            params.append(category)
        if language:
            query += " AND language=?"
            params.append(language)
        if branch:
            query += " AND branch=?"
            params.append(branch)
        query += " ORDER BY created_at ASC LIMIT ?"
        params.append(max(1, min(limit, 5000)))

        with self._read_conn() as c:
            rows = c.fetchall(query, tuple(params))

        results = []
        for r in rows:
            rd = _row(r)
            if isinstance(rd.get("detectors"), str):
                try:
                    rd["detectors"] = json.loads(rd["detectors"])
                except Exception:
                    rd["detectors"] = {}
            if isinstance(rd.get("judge"), str):
                try:
                    rd["judge"] = json.loads(rd["judge"])
                except Exception:
                    rd["judge"] = {}
            results.append(rd)
        return results

    def get_safety_summary(
        self,
        eval_run_id: str,
        risk_tier: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Aggregate safety results by category, language, and branch with model tier/overrides."""
        results = self.list_safety_results(eval_run_id=eval_run_id, limit=5000)

        tier = risk_tier
        thresh_overrides = dict(overrides or {})

        # If tier or overrides not provided, resolve from the governed model
        if not tier or not overrides:
            gov_model_id = results[0].get("governed_model_id") if results else None
            if not gov_model_id:
                with self._read_conn() as c:
                    er = c.fetchone("SELECT governed_model_id FROM eval_runs WHERE id=?", (eval_run_id,))
                    if er:
                        gov_model_id = er["governed_model_id"]
            if gov_model_id:
                try:
                    from core.model_inventory import ModelInventory
                    model = ModelInventory(self).get_model(gov_model_id)
                    if model:
                        if not tier and model.risk_tier:
                            tier = model.risk_tier.value
                        if not overrides and model.evaluation_signals:
                            sig_overrides = model.evaluation_signals.get("safety_thresholds")
                            if isinstance(sig_overrides, dict):
                                thresh_overrides.update(sig_overrides)
                except Exception as exc:
                    logger.debug("Failed to lookup model for safety summary: %s", exc)

        tier = tier or "HIGH"
        from core.safety.thresholds import aggregate_safety_summary
        return aggregate_safety_summary(results, risk_tier=tier, overrides=thresh_overrides)

