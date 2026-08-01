"""flywheel repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class FlywheelMixin:
    def update_test_case_metadata(
        self, tc_id: str, *,
        domain: str = "",
        industry: str = "",
        use_case_type: str = "",
        failure_mode: str = "",
        test_goal: str = "",
    ) -> None:
        """Enrich a test case with domain/use-case metadata for the generation corpus."""
        with self._conn() as c:
            c.execute(
                """UPDATE test_cases
                   SET domain=?, industry=?, use_case_type=?,
                       failure_mode=?, test_goal=?
                   WHERE id=?""",
                (domain, industry, use_case_type, failure_mode, test_goal, tc_id),
            )

    def record_test_case_performance(
        self, *,
        test_case_label: str,
        workflow_id: str,
        eval_run_id: str,
        comparison_id: str = None,
        divergence_score: float = None,
        decision_choice: str = None,
        reviewer_confidence: str = None,
    ) -> None:
        """Upsert a performance record for a (label, eval_run_id) pair.

        Called automatically from create_comparison (divergence) and
        create_decision (human choice + confidence).
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            # Try to update an existing row for this (label, eval_run_id)
            existing = c.fetchone(
                "SELECT id FROM test_case_performance WHERE test_case_label=? AND eval_run_id=?",
                (test_case_label, eval_run_id),
            )
            if existing:
                row_id = _row(existing)["id"]
                if comparison_id is not None:
                    c.execute("UPDATE test_case_performance SET comparison_id=?, divergence_score=? WHERE id=?",
                              (comparison_id, divergence_score, row_id))
                if decision_choice is not None:
                    c.execute("UPDATE test_case_performance SET decision_choice=?, reviewer_confidence=? WHERE id=?",
                              (decision_choice, reviewer_confidence, row_id))
            else:
                c.execute(
                    """INSERT INTO test_case_performance
                       (id, test_case_label, workflow_id, eval_run_id, comparison_id,
                        divergence_score, decision_choice, reviewer_confidence, recorded_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), test_case_label, workflow_id, eval_run_id,
                     comparison_id, divergence_score, decision_choice, reviewer_confidence, now),
                )

    def get_test_case_performance_stats(
        self, test_case_label: str, workflow_id: str = None
    ) -> dict:
        """Aggregate performance history for a test case label.

        Returns: avg_divergence, eval_run_count, decision_breakdown, win_rate_a, win_rate_b
        """
        filters, params = ["test_case_label=?"], [test_case_label]
        if workflow_id:
            filters.append("workflow_id=?"); params.append(workflow_id)
        where = "WHERE " + " AND ".join(filters)
        with self._conn() as c:
            rows = c.fetchall(
                f"""SELECT divergence_score, decision_choice, reviewer_confidence
                    FROM test_case_performance {where}""",
                params,
            )
        parsed = [_row(r) for r in rows]
        scores = [p["divergence_score"] for p in parsed if p["divergence_score"] is not None]
        choices = [p["decision_choice"] for p in parsed if p["decision_choice"]]
        from collections import Counter
        breakdown = dict(Counter(choices))
        total = len(choices)
        return {
            "test_case_label": test_case_label,
            "eval_run_count":  len(rows),
            "avg_divergence":  round(sum(scores) / len(scores), 4) if scores else None,
            "decision_breakdown": breakdown,
            "win_rate_a": round(breakdown.get("a_wins", 0) / total, 3) if total else None,
            "win_rate_b": round(breakdown.get("b_wins", 0) / total, 3) if total else None,
            "decision_rate": round(total / len(rows), 3) if rows else 0,
        }

    def export_test_case_corpus_jsonl(
        self,
        workflow_id: str = None,
        min_eval_runs: int = 1,
        include_performance: bool = True,
    ):
        """Export enriched test cases as JSONL for training an automated test-case generator.

        Each line:
          {label, input, domain, industry, use_case_type, failure_mode, test_goal,
           tags, performance: {avg_divergence, eval_run_count, win_rate_a, win_rate_b, decision_breakdown}}

        Only includes test cases that have been run at least min_eval_runs times.
        """
        import hashlib
        tc_filter  = "WHERE ts.workflow_id=?" if workflow_id else ""
        tc_params  = [workflow_id] if workflow_id else []

        with self._read_conn() as c:
            rows = c.fetchall(
                f"""SELECT tc.id, tc.label, tc.input_data, tc.expected_output,
                           tc.tags, tc.domain, tc.industry, tc.use_case_type,
                           tc.failure_mode, tc.test_goal, ts.workflow_id
                    FROM test_cases tc
                    JOIN test_sets ts ON tc.test_set_id = ts.id
                    {tc_filter}
                    ORDER BY tc.created_at""",
                tc_params,
            )

        # Batch-fetch performance counts to avoid N+1 queries
        perf_counts: Dict[str, int] = {}
        perf_cache: Dict[str, dict] = {}
        if include_performance:
            all_labels = list({_row(r)["label"] for r in rows})
            CHUNK = 900
            with self._read_conn() as c:
                for i in range(0, len(all_labels), CHUNK):
                    chunk = all_labels[i:i + CHUNK]
                    ph = ",".join("?" * len(chunk))
                    cnt_rows = c.fetchall(
                        f"SELECT test_case_label, COUNT(*) AS n "
                        f"FROM test_case_performance WHERE test_case_label IN ({ph}) "
                        f"GROUP BY test_case_label", chunk)
                    for cr in cnt_rows:
                        crd = _row(cr)
                        perf_counts[crd["test_case_label"]] = crd["n"]

        for row in rows:
            r = _row(row)
            label = r["label"]
            wid   = r.get("workflow_id") or workflow_id or ""

            perf = {}
            if include_performance:
                if perf_counts.get(label, 0) < min_eval_runs:
                    continue
                if label not in perf_cache:
                    perf_cache[label] = self.get_test_case_performance_stats(label, wid)
                perf = perf_cache[label]

            input_data = json.loads(r["input_data"]) if r["input_data"] else {}
            tags = json.loads(r["tags"]) if r["tags"] else []

            yield json.dumps({
                "id":            r["id"],
                "label":         label,
                "input":         input_data.get("input", input_data),
                "expected_output": r.get("expected_output") or "",
                "domain":        r.get("domain") or "",
                "industry":      r.get("industry") or "",
                "use_case_type": r.get("use_case_type") or "",
                "failure_mode":  r.get("failure_mode") or "",
                "test_goal":     r.get("test_goal") or "",
                "tags":          tags,
                "performance":   perf,
            })

    # ── Flywheel 2: reviewer profiles ─────────────────────────────────────────

    def upsert_reviewer_profile(
        self, reviewer_id: str, *,
        display_name: str = "",
        role: str = "reviewer",
        expertise_level: str = "intermediate",
        domain_expertise: list = None,
    ) -> dict:
        """Create or update a reviewer profile.

        Roles: domain_expert | ml_engineer | product_manager | end_user | reviewer
        Expertise levels: novice | intermediate | expert
        """
        now = datetime.now(timezone.utc).isoformat()
        domains_json = json.dumps(domain_expertise or [])
        with self._conn() as c:
            existing = c.fetchone(
                "SELECT reviewer_id FROM reviewer_profiles WHERE reviewer_id=?",
                (reviewer_id,),
            )
            if existing:
                c.execute(
                    """UPDATE reviewer_profiles
                       SET display_name=?, role=?, expertise_level=?,
                           domain_expertise=?, updated_at=?
                       WHERE reviewer_id=?""",
                    (display_name, role, expertise_level, domains_json, now, reviewer_id),
                )
            else:
                c.execute(
                    """INSERT INTO reviewer_profiles
                       (reviewer_id, display_name, role, expertise_level,
                        domain_expertise, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (reviewer_id, display_name, role, expertise_level,
                     domains_json, now, now),
                )
        return self.get_reviewer_profile(reviewer_id)

    def get_reviewer_profile(self, reviewer_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.fetchone(
                "SELECT * FROM reviewer_profiles WHERE reviewer_id=?", (reviewer_id,)
            )
        if not row:
            return None
        r = _row(row)
        return {
            "reviewer_id":     r["reviewer_id"],
            "display_name":    r.get("display_name") or "",
            "role":            r.get("role") or "reviewer",
            "expertise_level": r.get("expertise_level") or "intermediate",
            "domain_expertise": json.loads(r.get("domain_expertise") or "[]"),
            "created_at":      r.get("created_at") or "",
            "updated_at":      r.get("updated_at") or "",
        }

    # ── Flywheel 2: data consent ───────────────────────────────────────────────

    def grant_consent(
        self, *,
        scope: str = "global",
        workflow_id: str = None,
        consent_type: str,
        granted_by: str,
        notes: str = "",
        expires_at: str = None,
    ) -> dict:
        """Record that an organisation has opted in to a specific type of data sharing."""
        now = datetime.now(timezone.utc).isoformat()
        cid = str(uuid.uuid4())
        with self._conn() as c:
            c.execute(
                """INSERT INTO data_consent
                   (id, scope, workflow_id, consent_type, granted_by,
                    granted_at, expires_at, is_active, notes)
                   VALUES (?,?,?,?,?,?,?,1,?)""",
                (cid, scope, workflow_id, consent_type, granted_by,
                 now, expires_at, notes),
            )
        return self.get_consent(cid)

    def get_consent(self, consent_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.fetchone("SELECT * FROM data_consent WHERE id=?", (consent_id,))
        return _row(row) if row else None

    def list_consents(self, workflow_id: str = None, active_only: bool = True) -> List[dict]:
        conditions, params = [], []
        if active_only:
            conditions.append("is_active=1")
        if workflow_id:
            conditions.append("(workflow_id=? OR scope='global')")
            params.append(workflow_id)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        with self._conn() as c:
            rows = c.fetchall(
                f"SELECT * FROM data_consent {where} ORDER BY granted_at DESC", params
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        result = []
        for row in rows:
            r = _row(row)
            # Treat expired consents as inactive
            if r.get("expires_at") and r["expires_at"] < now_iso:
                continue
            result.append(r)
        return result

    def revoke_consent(self, consent_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE data_consent SET is_active=0 WHERE id=?", (consent_id,))

    def has_consent(self, workflow_id: str, consent_type: str) -> bool:
        """Return True if an active, unexpired consent record covers this workflow."""
        consents = self.list_consents(workflow_id=workflow_id, active_only=True)
        return any(r["consent_type"] == consent_type for r in consents)

    # ── Flywheel 2: preference corpus export ──────────────────────────────────

    def export_preference_corpus_jsonl(
        self,
        workflow_id: str = None,
        eval_run_id: str = None,
        anonymize: bool = True,
        require_consent: bool = True,
    ):
        """Export the human review decision corpus as JSONL for compliance evidence.

        A structured audit trail of reviewer decisions — includes reviewer
        metadata, confidence, structured rationale, divergence score, and data
        category — suitable for the Human Review Summary section of a model
        validation memo.

        Each line:
          {
            prompt, chosen, rejected,
            rationale_for_choice, rationale_for_rejection,
            confidence, tags, data_category, divergence_score,
            reviewer: {role, expertise_level, domain_expertise},
            provenance_hash  (anonymized cross-record key),
          }

        anonymize=True: replaces raw prompt text with provenance_hash.
        require_consent=True: skips workflows without an active data_consent record.
        """
        import hashlib

        filters = ["d.choice IN ('A', 'B')"]
        params: list = []
        if workflow_id:
            filters.append("d.workflow_id=?"); params.append(workflow_id)
        if eval_run_id:
            filters.append("d.eval_run_id=?"); params.append(eval_run_id)
        filters.append("d.branch_winner_id IS NOT NULL")
        where = "WHERE " + " AND ".join(filters)

        with self._read_conn() as c:
            rows = c.fetchall(
                f"""SELECT d.*, r.input_data AS run_input
                    FROM decisions d
                    LEFT JOIN workflow_runs r ON r.id = d.run_id
                    {where}
                    ORDER BY d.created_at DESC""",
                params,
            )

        # Parse decisions and collect branch IDs for bulk fetch
        parsed_rows = [_row(row) for row in rows]
        branch_ids = set()
        for d in parsed_rows:
            if d.get("branch_winner_id"):
                branch_ids.add(d["branch_winner_id"])
            if d.get("branch_loser_id"):
                branch_ids.add(d["branch_loser_id"])
        steps_by_branch = self.get_step_outputs_bulk(list(branch_ids))

        # Batch consent check — one query per workflow, cached
        consent_cache: Dict[str, bool] = {}
        # Pre-load reviewer profiles (batch)
        reviewer_cache: dict = {}

        for d in parsed_rows:
            # Consent check — skip if require_consent and no active consent
            if require_consent:
                wid = d.get("workflow_id") or ""
                if wid not in consent_cache:
                    consent_cache[wid] = self.has_consent(wid, "training_data")
                if not consent_cache[wid]:
                    continue

            # Build chosen/rejected text from bulk-fetched step outputs
            winner_steps = steps_by_branch.get(d.get("branch_winner_id", ""), [])
            loser_steps = steps_by_branch.get(d.get("branch_loser_id", ""), [])
            d["chosen_text"] = " ".join(s.output_text for s in winner_steps if s.output_text)
            d["rejected_text"] = " ".join(s.output_text for s in loser_steps if s.output_text)

            # Reviewer profile
            rid = d.get("reviewer_id") or ""
            if rid not in reviewer_cache:
                reviewer_cache[rid] = self.get_reviewer_profile(rid) or {}
            rp = reviewer_cache[rid]

            # Prompt text — prefer the rendered prompt the model actually
            # received (winning branch's first step), then the legacy "input"
            # field, then the raw input_data blob.
            first_step = winner_steps[0] if winner_steps else None
            rendered = _extract_prompt_text(
                first_step.input_messages if first_step else None, None)
            if rendered:
                raw_input = rendered
            else:
                raw_input = d.get("run_input") or ""
                try:
                    raw_input = json.loads(raw_input).get("input", raw_input)
                except Exception:
                    pass

            # Provenance hash — deterministic across records, never exposes raw text
            ph = d.get("provenance_hash") or ""
            if not ph:
                ph = hashlib.sha256(
                    f"{d.get('workflow_id','')}:{d.get('test_case_label','')}:{str(raw_input)[:200]}"
                    .encode()
                ).hexdigest()

            yield json.dumps({
                "provenance_hash":        ph,
                "prompt":                 ph if anonymize else raw_input,
                "chosen":                 d.get("chosen_text") or "",
                "rejected":               d.get("rejected_text") or "",
                "rationale_for_choice":   d.get("rationale_for_choice") or "",
                "rationale_for_rejection": d.get("rationale_for_rejection") or "",
                "confidence":             d.get("confidence") or "",
                "tags":                   json.loads(d.get("tags") or "[]"),
                "data_category":          d.get("data_category") or "",
                "divergence_score":       d.get("divergence_score"),
                "reviewer": {
                    "role":             rp.get("role") or "reviewer",
                    "expertise_level":  rp.get("expertise_level") or "intermediate",
                    "domain_expertise": rp.get("domain_expertise") or [],
                },
            })

    # ── Collaboration: comments ─────────────────────────────────────────────

