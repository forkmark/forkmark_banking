"""branches repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class BranchesMixin:
    def create_branch(self, run_id: str, workflow_id: str, name: str, model_id: str,
                      temperature: float = 0.7, system_prompt: str = None,
                      extra_config: dict = None, is_baseline: bool = False) -> Branch:
        now = datetime.now(timezone.utc)
        b = Branch(
            id=str(uuid.uuid4()), run_id=run_id, workflow_id=workflow_id,
            name=name, model_id=model_id, temperature=temperature,
            system_prompt=system_prompt, extra_config=extra_config or {},
            created_at=now, is_baseline=is_baseline,
        )
        with self._conn() as c:
            c.execute("""INSERT INTO branches
                (id,run_id,workflow_id,name,model_id,temperature,system_prompt,
                 extra_config,created_at,is_baseline)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (b.id, b.run_id, b.workflow_id, b.name, b.model_id, b.temperature,
                 b.system_prompt, json.dumps(b.extra_config),
                 b.created_at.isoformat(), int(b.is_baseline)))
        return b

    def get_branch(self, branch_id: str) -> Optional[Branch]:
        with self._conn() as c:
            r = c.fetchone("SELECT * FROM branches WHERE id=?", (branch_id,))
        return Branch.from_row(_row(r)) if r else None

    def list_branches(self, run_id: str) -> List[Branch]:
        with self._conn() as c:
            rows = c.fetchall("SELECT * FROM branches WHERE run_id=? ORDER BY created_at", (run_id,))
        return [Branch.from_row(_row(r)) for r in rows]

    # ── Step Outputs ──────────────────────────────────────────────────────────

    def save_step_output(self, run_id: str, branch_id: str, step_name: str,
                         step_index: int, input_messages: list, output_text: str,
                         model_id: str, temperature: float = 0.7,
                         tokens_input: int = 0, tokens_output: int = 0,
                         latency_ms: int = 0, error: str = None,
                         trace_id: str = None, span_id: str = None,
                         cost_usd: float = None) -> StepOutput:
        # Auto-estimate cost if not provided and tokens are available
        if cost_usd is None and (tokens_input or tokens_output):
            cost_usd = _estimate_cost(model_id, tokens_input, tokens_output)

        now = datetime.now(timezone.utc)
        so = StepOutput(
            id=str(uuid.uuid4()), run_id=run_id, branch_id=branch_id,
            step_name=step_name, step_index=step_index, input_messages=input_messages,
            output_text=output_text, model_id=model_id, temperature=temperature,
            tokens_input=tokens_input, tokens_output=tokens_output,
            latency_ms=latency_ms, created_at=now, error=error,
            trace_id=trace_id, span_id=span_id, cost_usd=cost_usd,
        )
        with self._conn() as c:
            c.execute("""INSERT INTO step_outputs
                (id,run_id,branch_id,step_name,step_index,input_messages,output_text,
                 model_id,temperature,tokens_input,tokens_output,latency_ms,created_at,error,
                 trace_id,span_id,cost_usd)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (so.id, so.run_id, so.branch_id, so.step_name, so.step_index,
                 json.dumps(so.input_messages), so.output_text, so.model_id,
                 so.temperature, so.tokens_input, so.tokens_output,
                 so.latency_ms, so.created_at.isoformat(), so.error,
                 so.trace_id, so.span_id, so.cost_usd))
        return so

    def batch_save_step_outputs(self, steps: List[dict]) -> List[StepOutput]:
        """Insert many step outputs in a single transaction (10x fewer roundtrips)."""
        now = datetime.now(timezone.utc).isoformat()
        records = []
        objects = []
        for s in steps:
            so = StepOutput(
                id=str(uuid.uuid4()),
                run_id=s["run_id"], branch_id=s["branch_id"],
                step_name=s["step_name"], step_index=s.get("step_index", 0),
                input_messages=s.get("input_messages", []),
                output_text=s.get("output_text", ""),
                model_id=s.get("model_id", ""), temperature=s.get("temperature", 0.7),
                tokens_input=s.get("tokens_input", 0), tokens_output=s.get("tokens_output", 0),
                latency_ms=s.get("latency_ms", 0), created_at=datetime.now(timezone.utc),
                error=s.get("error"),
            )
            records.append((
                so.id, so.run_id, so.branch_id, so.step_name, so.step_index,
                json.dumps(so.input_messages), so.output_text, so.model_id,
                so.temperature, so.tokens_input, so.tokens_output,
                so.latency_ms, so.created_at.isoformat(), so.error,
            ))
            objects.append(so)
        with self._conn() as c:
            c.executemany("""INSERT INTO step_outputs
                (id,run_id,branch_id,step_name,step_index,input_messages,output_text,
                 model_id,temperature,tokens_input,tokens_output,latency_ms,created_at,error)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", records)
        return objects

    def get_step_outputs_for_branch(self, branch_id: str) -> List[StepOutput]:
        with self._conn() as c:
            rows = c.fetchall(
                "SELECT * FROM step_outputs WHERE branch_id=? ORDER BY step_index",
                (branch_id,))
        return [StepOutput.from_row(_row(r)) for r in rows]

    def get_step_outputs_for_branches(self, branch_a_id: str, branch_b_id: str) -> List[StepOutput]:
        """Fetch all step outputs for two branches in one query (used by sdk_create_comparison)."""
        with self._conn() as c:
            rows = c.fetchall(
                "SELECT * FROM step_outputs WHERE branch_id IN (?, ?) ORDER BY branch_id, step_index",
                (branch_a_id, branch_b_id))
        return [StepOutput.from_row(_row(r)) for r in rows]

    def get_step_outputs_bulk(self, branch_ids: List[str]) -> Dict[str, List[StepOutput]]:
        """Batch-fetch step outputs for many branches in one query.

        Returns {branch_id: [StepOutput, ...]} dict.
        SQLite has a variable limit (~999), so we chunk large batches.
        """
        result: Dict[str, List[StepOutput]] = {bid: [] for bid in branch_ids}
        if not branch_ids:
            return result
        CHUNK = 900
        with self._conn() as c:
            for i in range(0, len(branch_ids), CHUNK):
                chunk = branch_ids[i:i + CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = c.fetchall(
                    f"SELECT * FROM step_outputs WHERE branch_id IN ({placeholders}) "
                    "ORDER BY branch_id, step_index",
                    chunk)
                for r in rows:
                    so = StepOutput.from_row(_row(r))
                    result.setdefault(so.branch_id, []).append(so)
        return result

    def get_step_outputs_for_run(self, run_id: str) -> List[StepOutput]:
        with self._conn() as c:
            rows = c.fetchall(
                "SELECT * FROM step_outputs WHERE run_id=? ORDER BY branch_id, step_index",
                (run_id,))
        return [StepOutput.from_row(_row(r)) for r in rows]

    def prune_step_outputs(self, older_than_days: int = 30) -> int:
        """Delete step_outputs older than N days. Preserves comparisons and decisions.

        step_outputs are the heaviest table (input_messages + output_text per LLM call).
        Old ones are only needed to re-render the CompareView diff — comparisons already
        cache divergence_score and step_divergence_scores so stats are unaffected.

        Returns the number of rows deleted.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM step_outputs WHERE created_at < ?", (cutoff,)
            )
            # rowcount works on both SQLite and psycopg2
            return getattr(cur, "rowcount", 0)

    # ── Comparisons ───────────────────────────────────────────────────────────

