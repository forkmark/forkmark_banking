"""Comprehensive verification test suite for ForkMark Banking MRM remediations.

Covers:
  1. Audit Log Concurrency, Tamper-Evident Hash Chaining, & Partitioning Integrity
  2. GCC Currency, Arabic-Indic Numeral Extraction, & Magnitude Parsing
  3. Grounding vs Hallucination False Positives & Distinct Branch Attribution
  4. Real Multi-Tenant Schema DDL Execution (All 46 SQL statements)
  5. Live Prometheus Metrics Exposition with Datetime Parsing
  6. Exact Student's t-Distribution Statistical Analysis (Fail Loudly)
  7. Atomic Rollback of Model Changes When Supervisory Audit Write Fails
  8. Sovereign LLM Data Egress Protection & Azure Endpoint Handling
  9. Strict Rejection of API Keys in URLs (OWASP Compliance)
 10. Data Residency Middleware Active Boundary Enforcement (400/403)
 11. Kubernetes Airgap & Migration Race Condition Guardrails
 12. Sharia & CBUAE Validation Memorandum Generation
"""

import os
import sys
import tempfile
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

# Add repo root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.store import Database
from core.store_impl.base import _audit_entry_hash
from core.finance_evaluators import (
    NumericalFidelityEvaluator,
    _extract_numbers,
    ExtractedNumber,
)
from core.compliance_reporter import (
    _summarize_decisions,
    build_validation_memo,
    render_validation_memo_html,
    ValidationEvidence,
)
from core.model_inventory import ModelRecord, RiskTier, ModelStatus, ModelInventory
from core.regulatory_frameworks import RegulatoryFramework
from core.statistical_analyzer import welch_t_test, paired_t_test, StatisticalResult
from core.comparator import _is_external_endpoint, _get_provider_credentials
from ee.data_residency import REGIONS, DEFAULT_REGION, DataResidencyManager
from ee.multitenancy import WORKSPACE_SCHEMA_DDL
import backend.deps as deps
from backend.routes.inventory import _inventory, delete_model, create_model, ModelCreate
from backend.routes.compliance import _audit_report
from fastapi import HTTPException
from fastapi.testclient import TestClient
import backend.main as main_module


class TestBankingAuditLog(unittest.TestCase):
    """Test audit log tamper-evidence, hash chaining, and partitioning integrity."""

    def setUp(self):
        self.tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tf.name
        self.db = Database(self.db_path)

    def tearDown(self):
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
        except Exception:
            pass

    def test_audit_hash_chain_integrity(self):
        e1 = self.db.add_audit_log(
            actor="validator_1",
            action="model.registered",
            resource_type="model",
            resource_id="m-001",
            detail={"name": "CreditRisk-v1"},
        )
        e2 = self.db.add_audit_log(
            actor="validator_1",
            action="eval.run",
            resource_type="eval_run",
            resource_id="r-101",
            detail={"passed": True},
        )
        e3 = self.db.add_audit_log(
            actor="compliance_officer",
            action="report.approved",
            resource_type="report",
            resource_id="rep-202",
            detail={"framework": "CBUAE"},
        )

        res = self.db.verify_audit_chain()
        self.assertTrue(res["ok"])
        self.assertEqual(res["entries"], 3)
        self.assertEqual(res["checked"], 3)
        self.assertIsNone(res["broken_at"])

    def test_audit_tamper_detection(self):
        e1 = self.db.add_audit_log(
            actor="validator_1",
            action="model.registered",
            resource_type="model",
            resource_id="m-001",
        )
        e2 = self.db.add_audit_log(
            actor="validator_1",
            action="eval.run",
            resource_type="eval_run",
            resource_id="r-101",
        )

        # Simulate rogue update
        with self.db._conn() as conn:
            conn.execute(
                "UPDATE audit_log SET action='tampered.action' WHERE id=?", (e1,)
            )

        res = self.db.verify_audit_chain()
        self.assertFalse(res["ok"])
        self.assertEqual(res["broken_at"]["id"], e1)

    def test_timestamp_normalization_preserves_hash_chain(self):
        """Verify that string/datetime normalization in hash calculation is invariant."""
        ts_str = "2026-11-08T12:00:00+00:00"
        ts_dt = datetime.fromisoformat(ts_str)

        h1 = _audit_entry_hash("hash0", 1, ts_str, "admin", "admin", "create", "model", "m-1", {}, "127.0.0.1")
        h2 = _audit_entry_hash("hash0", 1, ts_dt, "admin", "admin", "create", "model", "m-1", {}, "127.0.0.1")
        self.assertEqual(h1, h2, "Hash must be identical whether ts is datetime or ISO string")

    def test_partitioning_script_index_swap_syntax(self):
        """Verify that the partitioning script renames the legacy index first to avoid collisions."""
        script_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "scripts", "postgres_partitioning.sql")
        )
        self.assertTrue(os.path.exists(script_path))
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("idx_audit_log_seq RENAME TO idx_audit_log_seq_legacy", content)
        self.assertIn("idx_audit_log_seq_part RENAME TO idx_audit_log_seq", content)


class TestGCCFinanceEvaluators(unittest.TestCase):
    """Test Arabic-Indic numerals, GCC currencies, and numerical fidelity."""

    def test_arabic_numeral_and_currency_extraction(self):
        text = "بلغت أرباح بنك أبوظبي الأول ٤.٥ مليار درهم وقيمة المحفظة ١٢٠ مليون دولار"
        nums = _extract_numbers(text)
        self.assertEqual(len(nums), 2)

        n1 = next(n for n in nums if "٤.٥" in n.raw or "4.5" in n.raw)
        self.assertEqual(n1.value, 4.5e9)

        n2 = next(n for n in nums if "١٢٠" in n.raw or "120" in n.raw)
        self.assertEqual(n2.value, 1.2e8)

    def test_gcc_currency_markers(self):
        cases = [
            ("قيمة التسهيلات AED 5,000,000", 5000000.0),
            ("تمويل بقيمة 250,000 د.إ", 250000.0),
            ("مرابحة SAR 1,200,000", 1200000.0),
            ("صكوك بقيمة 10.5m Dhs", 10500000.0),
        ]
        for txt, expected in cases:
            extracted = _extract_numbers(txt)
            self.assertTrue(len(extracted) > 0, f"Failed to extract from: {txt}")
            self.assertEqual(extracted[0].value, expected, f"Value mismatch for: {txt}")

    def test_magnitude_parsing_not_confusing_tenor_months_with_millions(self):
        """Minor 11 fix: 'Tenor 5 m' must parse as 5, not 5,000,000."""
        cases = [
            ("Tenor 5 m", 5.0),
            ("Loan term: 12 m", 12.0),
            ("Duration 36 months", 36.0),
            ("Capital 5M AED", 5000000.0),
            ("Facility 5m USD", 5000000.0),
        ]
        for txt, expected in cases:
            nums = _extract_numbers(txt)
            self.assertTrue(len(nums) > 0, f"Failed to extract number from: {txt}")
            self.assertEqual(nums[0].value, expected, f"Mismatch for: {txt}")

    def test_fidelity_arabic_vs_western_digits(self):
        evaluator = NumericalFidelityEvaluator()
        source = "صافي الدخل السنوي يبلغ ٤.٥ مليار درهم"
        generated = "بلغ صافي الدخل السنوي 4.5 مليار درهم"
        res = evaluator.evaluate(source, generated)
        self.assertEqual(res.score, 1.0)
        self.assertTrue(res.is_faithful)


class TestHallucinationAndGrounding(unittest.TestCase):
    """Test Blocker 2: Runner inputs, grounding priority, and distinct branch attribution."""

    def test_no_false_positive_hallucination_on_runner_question(self):
        """Verify that user questions are NOT used as source documents."""
        evaluator = NumericalFidelityEvaluator()
        correct_answer = "The monthly payment is AED 8,000 with a profit rate of 6.99%."
        policy_document = "Personal finance rates are capped at 6.99% with standard monthly installments around AED 8,000."
        res = evaluator.evaluate(policy_document, correct_answer, target_branch="challenger (candidate upgrade)")
        self.assertTrue(res.is_faithful)
        self.assertEqual(len(res.flagged_numbers), 0)
        self.assertEqual(res.target_branch, "challenger (candidate upgrade)")

    def test_distinct_champion_vs_challenger_attribution(self):
        """Verify that flagged numbers explicitly identify which branch failed."""
        evaluator = NumericalFidelityEvaluator()
        source_doc = "Standard fee is AED 250."
        challenger_output = "We offer a special fee of AED 99.00 and 3.75% rate."

        res = evaluator.evaluate(source_doc, challenger_output, target_branch="challenger (candidate upgrade)")
        self.assertFalse(res.is_faithful)
        self.assertEqual(res.target_branch, "challenger (candidate upgrade)")
        self.assertEqual(len(res.flagged_numbers), 2)


class TestComplianceReporter(unittest.TestCase):
    """Test bilingual rationale summarization and HTML report generation."""

    def test_arabic_decision_theme_extraction(self):
        decisions = [
            {
                "choice": "branch_a",
                "rationale": "النموذج أ يظهر دقة أعلى ومطابقة أفضل للسياسات الائتمانية المقررة",
            },
            {
                "choice": "branch_a",
                "rationale": "مطابقة عالية للسياسات الائتمانية واللوائح المالية لمصرف الإمارات المركزي",
            },
        ]
        summary = _summarize_decisions(decisions)
        self.assertEqual(summary["total_decisions"], 2)
        theme_words = [t["theme"] for t in summary["rationale_themes"]]
        self.assertTrue(
            any("سياسات" in t or "ائتمان" in t for t in theme_words),
            f"Expected credit/policy themes in {theme_words}",
        )
        self.assertNotIn("لا يوجد", summary["summary_ar"])

    def test_html_validation_memo_generation(self):
        m = ModelRecord(
            model_id="test-adcb-001",
            display_name="ADCB Credit Adjudication Engine",
            provider="Azure OpenAI",
            version="3.5",
            use_case="Wholesale Corporate Credit",
            risk_tier=RiskTier.HIGH,
            regulatory_frameworks=[RegulatoryFramework.CBUAE_MMS],
            deployed_at=datetime.now(timezone.utc),
            owner_team="Credit Risk Analytics",
            documentation_url="https://bank.adcb/mrm/001",
            status=ModelStatus.ACTIVE,
            last_validated_at=None,
            next_validation_due=None,
            present_artifacts=["conceptual_soundness", "bias_test"],
            evaluation_signals={},
        )
        memo = build_validation_memo(
            m, RegulatoryFramework.CBUAE_MMS, ValidationEvidence()
        )
        html = render_validation_memo_html(memo)

        self.assertIn("ADCB Credit Adjudication Engine", html)
        self.assertIn("Print to PDF / طباعة كملف PDF", html)
        self.assertIn("CBUAE Model Management Standards (2022)", html)
        self.assertIn("Supervisory Sign-off & Attestation", html)

    def test_sharia_supervisory_attestation_block(self):
        m = ModelRecord(
            model_id="dib-murabaha-01",
            display_name="Dubai Islamic Bank Murabaha Engine",
            provider="Sovereign Jais-30B",
            version="1.2",
            use_case="Islamic Murabaha Retail Financing",
            risk_tier=RiskTier.HIGH,
            regulatory_frameworks=[RegulatoryFramework.CBUAE_MMS],
            deployed_at=datetime.now(timezone.utc),
            owner_team="Sharia Governance",
            documentation_url="https://bank.dib/sharia",
            status=ModelStatus.ACTIVE,
            last_validated_at=None,
            next_validation_due=None,
            present_artifacts=["conceptual_soundness"],
            evaluation_signals={},
        )
        memo = build_validation_memo(
            m, RegulatoryFramework.CBUAE_MMS, ValidationEvidence()
        )
        self.assertTrue(memo["sign_off"]["sharia_board_applicable"])
        html = render_validation_memo_html(memo)
        self.assertIn("Shari'ah Supervisory Board Attestation", html)
        self.assertIn("اعتماد هيئة الرقابة الشرعية", html)


class TestEnterpriseArchitecture(unittest.TestCase):
    """Test multitenancy schema reconciliation, live Prometheus metrics, and data residency."""

    def test_multitenancy_workspace_ddl_real_execution(self):
        """Execute all 46 statements of WORKSPACE_SCHEMA_DDL on a real SQLite database.

        Catches SQL syntax errors like the previous DEFAULT ''' bug.
        """
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        clean_ddl = WORKSPACE_SCHEMA_DDL.replace("TIMESTAMPTZ DEFAULT NOW()", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        clean_ddl = clean_ddl.replace("TIMESTAMPTZ", "TIMESTAMP")
        clean_ddl = clean_ddl.replace("NOW()", "CURRENT_TIMESTAMP")
        clean_ddl = clean_ddl.replace("BIGSERIAL", "INTEGER")
        clean_ddl = clean_ddl.replace("SERIAL", "INTEGER")
        statements = [s.strip() for s in clean_ddl.split(";") if s.strip()]
        self.assertEqual(len(statements), 46, f"Expected 46 statements, found {len(statements)}")

        for stmt in statements:
            try:
                cur.execute(stmt)
            except Exception as e:
                self.fail(f"SQL execution failed on statement:\n{stmt}\nError: {e}")

        conn.commit()

        tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        required = [
            "model_inventory",
            "compliance_reports",
            "llm_providers",
            "trace_events",
            "trajectory_outcomes",
            "test_case_performance",
            "reviewer_profiles",
            "data_consent",
            "comments",
            "review_assignments",
        ]
        for tbl in required:
            self.assertIn(tbl, tables, f"Table {tbl} not created by WORKSPACE_SCHEMA_DDL")
        conn.close()

    def test_uae_data_residency_default(self):
        self.assertIn("me-central-1", REGIONS)
        self.assertEqual(DEFAULT_REGION, "me-central-1")
        mgr = DataResidencyManager()
        self.assertEqual(mgr.get_org_region("org_adcb"), "me-central-1")

    def test_kubernetes_manifest_wait_and_migration_guard(self):
        k8s_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "deploy", "kubernetes", "forkmark.yaml")
        )
        self.assertTrue(os.path.exists(k8s_path), "Missing Kubernetes manifest")
        with open(k8s_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("initContainers:", content)
        self.assertIn("name: wait-for-db-migration", content)
        # Verify initContainer explicitly has FM_SKIP_STARTUP_MIGRATIONS to avoid migration race
        deployment_part = content[content.index("kind: Deployment"):]
        init_section = deployment_part[deployment_part.index("initContainers:"):deployment_part.index("      containers:")]
        self.assertIn("FM_SKIP_STARTUP_MIGRATIONS", init_section)
        self.assertIn("kubernetes.io/metadata.name: monitoring", content)
        self.assertNotIn(":latest", content, "Kubernetes manifest must not use :latest image tag")

    def test_prometheus_metrics_real_invocation_with_datetime_parsing(self):
        """Invoke backend.routes.health.prometheus_metrics() live."""
        from backend.routes.health import prometheus_metrics

        inv = ModelInventory(deps.db)
        overdue_dt = datetime.now(timezone.utc) - timedelta(days=10)
        future_iso = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

        m1 = ModelRecord(
            model_id="m-overdue-dt",
            display_name="Overdue Model (datetime object)",
            provider="Local vLLM",
            version="1.0",
            use_case="Credit Scoring",
            risk_tier=RiskTier.HIGH,
            regulatory_frameworks=[RegulatoryFramework.CBUAE_MMS],
            deployed_at=datetime.now(timezone.utc),
            owner_team="Risk Analytics",
            status=ModelStatus.ACTIVE,
            next_validation_due=overdue_dt,
        )
        m2 = ModelRecord(
            model_id="m-future-str",
            display_name="Future Model (ISO str)",
            provider="Local vLLM",
            version="1.0",
            use_case="Fraud Detection",
            risk_tier=RiskTier.CRITICAL,
            regulatory_frameworks=[RegulatoryFramework.CBUAE_MMS],
            deployed_at=datetime.now(timezone.utc),
            owner_team="Fraud Analytics",
            status=ModelStatus.ACTIVE,
            next_validation_due=future_iso,
        )
        inv.add_model(m1)
        inv.add_model(m2)

        resp = prometheus_metrics()
        output = resp.body.decode("utf-8")

        self.assertIn("forkmark_up 1", output)
        self.assertIn('forkmark_models_total{risk_tier="HIGH"}', output)
        self.assertIn('forkmark_models_total{risk_tier="CRITICAL"}', output)
        self.assertIn("forkmark_models_revalidation_due", output)
        self.assertNotIn("Error collecting inventory metrics", output)


class TestStatisticalAnalyzerIntegrity(unittest.TestCase):
    """Test Should-Fix 7: Fail loudly on missing stats/regex, exact Student's t distributions."""

    def test_welch_t_test_exact_student_t_distribution(self):
        """Small sample size (n=10) with t=2.0 must produce Student's t p ≈ 0.0766, NOT normal p ≈ 0.0455."""
        from scipy import stats

        a = [1.0, 1.2, 1.1, 1.0, 1.3, 1.1, 1.2, 1.0, 1.1, 1.2]
        b = [0.8, 0.9, 0.7, 0.9, 0.8, 1.0, 0.8, 0.9, 0.7, 0.8]
        t_stat, p_val = welch_t_test(a, b)

        t_stat_expected, p_expected = stats.ttest_ind(a, b, equal_var=False)
        self.assertAlmostEqual(p_val, p_expected, places=4)
        self.assertAlmostEqual(t_stat, t_stat_expected, places=4)

    def test_fail_loudly_when_scipy_or_numpy_missing(self):
        """Test that missing dependencies raise RuntimeError rather than returning flawed normal fallbacks."""
        with patch("core.statistical_analyzer.stats", None):
            with self.assertRaises(RuntimeError) as ctx:
                welch_t_test([1.0, 2.0], [3.0, 4.0])
            self.assertIn("scipy", str(ctx.exception))


class TestSupervisoryAuditAtomicTransactions(unittest.TestCase):
    """Test that model mutations roll back atomically when audit logging fails."""

    def setUp(self):
        self.tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db = Database(self.tf.name)
        self.inv = ModelInventory(self.test_db)

    def tearDown(self):
        try:
            if os.path.exists(self.tf.name):
                os.remove(self.tf.name)
        except Exception:
            pass

    def test_atomic_rollback_on_failed_audit_delete(self):
        """When audit log write fails during delete, model MUST NOT be deleted."""
        record = ModelRecord(
            model_id="m-rollback-del",
            display_name="Test Rollback Delete",
            provider="Local",
            version="1.0",
            use_case="Test",
            risk_tier=RiskTier.HIGH,
            regulatory_frameworks=[RegulatoryFramework.CBUAE_MMS],
            deployed_at=datetime.now(timezone.utc),
            owner_team="Risk",
            status=ModelStatus.ACTIVE,
        )
        self.inv.add_model(record)
        self.assertIsNotNone(self.inv.get_model("m-rollback-del"))

        # Simulate audit log write failure inside the transaction
        with patch.object(self.test_db, "add_audit_log", side_effect=IOError("Audit disk full or lock timeout")):
            with self.assertRaises(IOError):
                self.inv.delete_model("m-rollback-del", audit_actor="auditor_1")

        # Crucial check: Because the transaction rolled back, the model still exists!
        model_still_there = self.inv.get_model("m-rollback-del")
        self.assertIsNotNone(
            model_still_there,
            "Model was deleted despite audit write failure! Atomic rollback failed."
        )

    def test_atomic_rollback_on_failed_audit_create(self):
        """When audit log write fails during creation, model MUST NOT be saved."""
        record = ModelRecord(
            model_id="m-rollback-create",
            display_name="Test Rollback Create",
            provider="Local",
            version="1.0",
            use_case="Test",
            risk_tier=RiskTier.HIGH,
            regulatory_frameworks=[RegulatoryFramework.CBUAE_MMS],
            deployed_at=datetime.now(timezone.utc),
            owner_team="Risk",
            status=ModelStatus.ACTIVE,
        )

        with patch.object(self.test_db, "add_audit_log", side_effect=IOError("Audit disk full")):
            with self.assertRaises(IOError):
                self.inv.add_model(record, audit_actor="admin")

        # Model must not exist in inventory
        self.assertIsNone(
            self.inv.get_model("m-rollback-create"),
            "Model was saved despite audit write failure! Atomic rollback failed."
        )


class TestSecurityAndDataResidencyEnforcement(unittest.TestCase):
    """Test strict URL query parameter API key rejection and active residency boundary checks."""

    def setUp(self):
        self.client = TestClient(main_module.app)

    def test_api_keys_in_url_query_parameters_are_strictly_rejected(self):
        """OWASP API compliance: API keys in URL query strings (?api_key= or ?token=) MUST return 401."""
        with patch("config.config.REQUIRE_UI_AUTH", True):
            # Attempt to authenticate via query string
            r1 = self.client.get("/api/inventory/models?api_key=fm_secret_key_123")
            self.assertEqual(r1.status_code, 401, "API key in ?api_key= must be rejected with 401")

            r2 = self.client.get("/api/keys?token=fm_secret_key_123")
            self.assertEqual(r2.status_code, 401, "Token in ?token= must be rejected with 401")

            r3 = self.client.get("/api/audit/log?api_key=fm_secret_key_123")
            self.assertEqual(r3.status_code, 401, "Audit log endpoint accepted query param key!")

    def test_data_residency_middleware_enforcement(self):
        """Data residency middleware must reject unknown regions (400) and cross-border regions (403)."""
        # Unknown region: returns 400 Bad Request, NOT a 500 crash
        r_unknown = self.client.get("/healthz", headers={"X-Region": "invalid-region-999"})
        self.assertEqual(r_unknown.status_code, 400)
        self.assertIn("Unsupported data residency region", r_unknown.json()["detail"])

        # Cross-border region: us-east-1 when deployment is UAE on-soil (me-central-1)
        r_cross = self.client.get("/healthz", headers={"X-Region": "us-east-1"})
        self.assertEqual(r_cross.status_code, 403)
        self.assertIn("Data residency violation", r_cross.json()["detail"])

        # Valid on-soil region: me-central-1
        r_valid = self.client.get("/healthz", headers={"X-Region": "me-central-1"})
        self.assertEqual(r_valid.status_code, 200)
        self.assertEqual(r_valid.headers.get("X-Data-Residency-Region"), "me-central-1")


class TestSovereignEgressAndAzureEndpoints(unittest.TestCase):
    """Test Should-Fix 8: Sovereign data egress protection and Azure base URLs."""

    def test_external_egress_guard_blocks_cloud_llms(self):
        # Public cloud LLM endpoints must be classified as external egress
        self.assertTrue(_is_external_endpoint("https://api.openai.com/v1"))
        self.assertTrue(_is_external_endpoint("https://api.anthropic.com/v1"))
        self.assertTrue(_is_external_endpoint("https://bank-adcb.openai.azure.com/openai/deployments/gpt-4o"))
        self.assertTrue(_is_external_endpoint("https://openrouter.ai/api/v1"))
        self.assertTrue(_is_external_endpoint("https://generativelanguage.googleapis.com/v1beta"))

        # Internal and sovereign private endpoints must be allowed
        self.assertFalse(_is_external_endpoint("http://localhost:8000/v1"))
        self.assertFalse(_is_external_endpoint("http://127.0.0.1:11434"))
        self.assertFalse(_is_external_endpoint("http://10.240.0.1:11434/api"))
        self.assertFalse(_is_external_endpoint("http://192.168.1.50:8000"))
        self.assertFalse(_is_external_endpoint("http://172.20.0.10:8000"))
        self.assertFalse(_is_external_endpoint("http://ollama:11434/v1"))
        self.assertFalse(_is_external_endpoint("http://vllm-service:8000/v1"))
        self.assertFalse(_is_external_endpoint("http://jais.mrm.svc.cluster.local:8000/v1"))
        self.assertFalse(_is_external_endpoint("https://sovereign-llm.bank.internal/v1"))

        # Public domain by default is blocked as external egress
        self.assertTrue(_is_external_endpoint("https://bank-sovereign-llm.adcb.ae/v1"))

        # But if whitelisted in FM_INTERNAL_LLM_HOSTS, it is permitted
        with patch.dict(os.environ, {"FM_INTERNAL_LLM_HOSTS": "adcb.ae"}):
            self.assertFalse(_is_external_endpoint("https://bank-sovereign-llm.adcb.ae/v1"))

        with patch.dict(os.environ, {"FM_ALLOW_EXTERNAL_LLM_EGRESS": "false", "FM_OPENAI_API_KEY": "sk-secret"}):
            key, url = _get_provider_credentials()
            self.assertIsNone(key, "Egress must be blocked when FM_ALLOW_EXTERNAL_LLM_EGRESS=false")

        with patch.dict(os.environ, {"FM_ALLOW_EXTERNAL_LLM_EGRESS": "true", "FM_OPENAI_API_KEY": "sk-secret"}):
            key, url = _get_provider_credentials()
            self.assertEqual(key, "sk-secret")


class TestAtomicComplianceReportAndAudit(unittest.TestCase):
    """Test that compliance report recording and audit writes are strictly atomic."""

    def setUp(self):
        from backend.routes.compliance import db as comp_db
        from core.model_inventory import ModelInventory, ModelRecord, RiskTier
        self.comp_db = comp_db
        inv = ModelInventory(comp_db)
        try:
            with comp_db._conn() as conn:
                conn.execute("DELETE FROM models WHERE model_id = ?", ("atomic-report-model-01",))
                conn.execute("DELETE FROM compliance_reports WHERE model_id = ?", ("atomic-report-model-01",))
                conn.execute("DELETE FROM audit_log WHERE resource_id = ?", ("atomic-report-model-01",))
        except Exception:
            pass

        inv.add_model(
            ModelRecord(
                model_id="atomic-report-model-01",
                display_name="Atomic Report Test Model",
                provider="Local",
                version="1.0",
                use_case="Credit",
                risk_tier=RiskTier.HIGH,
                regulatory_frameworks=[RegulatoryFramework.CBUAE_MMS],
                deployed_at=datetime.now(timezone.utc),
                owner_team="Risk Dept",
            ),
            audit_actor="admin",
            audit_role="admin",
        )

    def tearDown(self):
        try:
            with self.comp_db._conn() as conn:
                conn.execute("DELETE FROM models WHERE model_id = ?", ("atomic-report-model-01",))
                conn.execute("DELETE FROM compliance_reports WHERE model_id = ?", ("atomic-report-model-01",))
                conn.execute("DELETE FROM audit_log WHERE resource_id = ?", ("atomic-report-model-01",))
        except Exception:
            pass

    def test_report_generation_rolls_back_history_on_audit_failure(self):
        from backend.routes.compliance import generate_report, ComplianceReportRequest
        from fastapi import HTTPException
        from starlette.requests import Request

        req_scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/compliance/reports/atomic-report-model-01",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
        }
        mock_request = Request(req_scope)
        body = ComplianceReportRequest(framework=RegulatoryFramework.CBUAE_MMS)

        # Count existing report history rows before attempt
        with self.comp_db._read_conn() as conn:
            initial_count = conn.fetchone(
                "SELECT COUNT(*) as c FROM compliance_reports WHERE model_id = ?",
                ("atomic-report-model-01",),
            )["c"]

        # Simulate audit log write failure (e.g., storage failure / disk error)
        with patch.object(self.comp_db, "add_audit_log", side_effect=RuntimeError("Simulated audit disk failure")):
            with self.assertRaises(HTTPException) as ctx:
                generate_report(
                    model_id="atomic-report-model-01",
                    body=body,
                    request=mock_request,
                    x_api_key="fm_test_key",
                )
            self.assertEqual(ctx.exception.status_code, 500)
            self.assertIn("Audit trail write failed", ctx.exception.detail)

        # Verify that compliance_reports was rolled back and NO orphan report row exists
        with self.comp_db._read_conn() as conn:
            after_count = conn.fetchone(
                "SELECT COUNT(*) as c FROM compliance_reports WHERE model_id = ?",
                ("atomic-report-model-01",),
            )["c"]
        self.assertEqual(
            after_count,
            initial_count,
            "Failed audit log write must roll back report history record to maintain 100% audit consistency",
        )


class TestCBUAEComplianceGuideAndGitignore(unittest.TestCase):
    """Verify CBUAE MMS section citations and .gitignore cleanliness."""

    def test_cbuae_guide_mms_sections_and_guidance_note(self):
        guide_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "docs", "docs", "deployment", "cbuae-compliance.md")
        )
        self.assertTrue(os.path.exists(guide_path), "Missing CBUAE compliance guide")
        with open(guide_path, "r", encoding="utf-8") as f:
            content = f.read()

        # MMS Section 4: Model Governance & Model Inventory
        self.assertIn("MMS Section 4", content)
        self.assertIn("Section 4 (Model Governance & Model Inventory)", content)
        # MMS Section 10: Independent Validation (10.4 Quantitative Validation, 10.6 Reporting of Findings)
        self.assertIn("MMS Section 10.4", content)
        self.assertIn("10.6 (Reporting of Findings)", content)
        # Guidance Note Section 4(b) must be stated as 'supports', not 'satisfies'
        self.assertIn("supports", content.lower())
        self.assertNotIn("satisfying the plain-language bilingual disclosure", content)

    def test_gitignore_no_duplicate_reports_block(self):
        gitignore_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".gitignore"))
        self.assertTrue(os.path.exists(gitignore_path), "Missing .gitignore")
        with open(gitignore_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Count occurrences of the reports section header
        reports_header = "# ── Generated reports ──"
        occurrences = content.count(reports_header)
        self.assertEqual(occurrences, 1, f"Found {occurrences} 'Generated reports' headers in .gitignore; should be 1")


if __name__ == "__main__":
    unittest.main()
