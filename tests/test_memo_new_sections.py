"""Tests for the 11-section validation memo, Arabic translations, and 9 audit finding rules."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import pytest

from core.compliance_reporter import (
    ComplianceReporter,
    ValidationEvidence,
    build_validation_memo,
    render_validation_memo_html,
)
from core.model_inventory import ModelInventory, ModelRecord, ModelStatus, RiskTier
from core.regulatory_frameworks import ArtifactType, RegulatoryFramework
from core.store import Database


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "memo_test.db"))


@pytest.fixture()
def sample_model(db: Database) -> ModelRecord:
    inv = ModelInventory(db)
    rec = ModelRecord(
        model_id="fin-gpt-uae",
        display_name="UAE Retail Banking Advisory Model",
        provider="openai",
        version="gpt-4o-2024-11-20",
        use_case="customer financial guidance",
        risk_tier=RiskTier.HIGH,
        regulatory_frameworks=[RegulatoryFramework.CBUAE],
        deployed_at=datetime(2025, 3, 1, tzinfo=timezone.utc),
        owner_team="Retail AI Risk",
        present_artifacts=[
            ArtifactType.VALIDATION_MEMO.value,
            ArtifactType.CONCEPTUAL_SOUNDNESS.value,
        ],
    )
    return inv.add_model(rec)


def test_memo_has_all_eleven_sections(sample_model: ModelRecord):
    evidence = ValidationEvidence(
        safety_summaries=[{
            "verdict": "pass",
            "total_cases": 240,
            "canary_leaks": 0,
            "pii_leaks": 0,
            "pooled_asr": 0.01,
            "over_refusal_rate": 0.04,
            "category_metrics": {"prompt_injection": {"rate": 0.02, "total": 40, "ci_lower": 0.0, "ci_upper": 0.05}},
            "language_metrics": {"en": {"rate": 0.01, "total": 120, "ci_lower": 0.0, "ci_upper": 0.03}, "ar_msa": {"rate": 0.01, "total": 120, "ci_lower": 0.0, "ci_upper": 0.03}},
            "challenger_comparison": {},
            "thresholds": {"asr_single_category": 0.05},
        }],
        monitoring_runs=[{
            "id": "mon-run-1",
            "status": "pass",
            "created_at": "2026-03-01T10:00:00Z",
            "divergence_median": 0.04,
            "grounding_rate": 0.98,
            "safety_slice_asr": 0.0,
            "psi_total": 0.05,
            "version_detected": "gpt-4o-2024-11-20",
        }],
        provenance={
            "vendor": "OpenAI / Sovereign UAE Azure",
            "licence": "Commercial Enterprise",
            "hosting": "Sovereign Cloud",
            "inference_region": "me-central-1",
            "is_cross_border": False,
            "models_reported": ["gpt-4o-2024-11-20"],
            "fingerprints": ["fp_abc123"],
        },
    )

    memo = build_validation_memo(sample_model, RegulatoryFramework.CBUAE, evidence)

    # Verify all 11 core sections exist in the memo dict
    expected_sections = [
        "executive_summary",             # 1
        "scope_and_methodology",         # 2
        "statistical_results",           # 3
        "bias_and_fairness",             # 4
        "numerical_fidelity",            # 5
        "robustness_and_safety",         # 6 (NEW)
        "ongoing_monitoring",            # 7 (NEW)
        "human_review_summary",          # 8
        "regulatory_mapping",            # 9
        "findings_and_recommendations",  # 10
        "sign_off",                      # 11
    ]
    for sec in expected_sections:
        assert sec in memo, f"Missing section {sec} in memo"

    # Verify Section 2 third-party model block
    tp = memo["scope_and_methodology"]["third_party_model"]
    assert tp["is_third_party"] is True
    assert tp["vendor"] == "OpenAI / Sovereign UAE Azure"
    assert tp["inference_region"] == "me-central-1"
    assert tp["is_cross_border"] is False
    assert "gpt-4o-2024-11-20" in tp["models_reported"]
    assert "fp_abc123" in tp["fingerprints"]
    assert "النموذج مزود من طرف ثالث" in tp["summary_ar"]

    # Verify Section 6 Robustness and Safety
    rs = memo["robustness_and_safety"]
    assert rs["applicable"] is True
    assert rs["overall_verdict"] == "PASS"
    assert rs["overall_verdict_ar"] == "مطابق"
    assert rs["canary_leaks_count"] == 0
    assert len(rs["categories"]) == 1
    assert rs["categories"][0]["category"] == "prompt_injection"
    assert rs["categories"][0]["verdict"] == "PASS"
    assert "المتانة والسلامة" in rs["summary_ar"]

    # Verify Section 7 Ongoing Monitoring
    mon = memo["ongoing_monitoring"]
    assert mon["applicable"] is True
    assert mon["total_runs"] == 1
    assert mon["latest_state"] == "pass"
    assert mon["latest_state_ar"] == "مطابق"
    assert len(mon["runs"]) == 1
    assert mon["runs"][0]["psi"] == 0.05
    assert "المراقبة المستمرة" in mon["summary_ar"]


def test_sharia_attestation_included_for_islamic_models(db: Database):
    inv = ModelInventory(db)
    rec = ModelRecord(
        model_id="islamic-murabaha-advisor",
        display_name="Murabaha Financing Adjudication Assistant",
        provider="in-house",
        version="v1.0",
        use_case="Islamic retail banking Murabaha contract eligibility",
        risk_tier=RiskTier.HIGH,
        regulatory_frameworks=[RegulatoryFramework.CBUAE],
        deployed_at=datetime(2025, 4, 1, tzinfo=timezone.utc),
        owner_team="Sharia Compliance & Risk",
    )
    model = inv.add_model(rec)

    memo = build_validation_memo(model, RegulatoryFramework.CBUAE, ValidationEvidence())
    assert memo["executive_summary"]["is_islamic_banking"] is True
    assert memo["sign_off"]["sharia_board_applicable"] is True

    # Render HTML and verify Section 12 appears
    html = render_validation_memo_html(memo)
    assert "12. Shari'ah Supervisory Board Attestation" in html
    assert "١٢. اعتماد هيئة الرقابة الشرعية" in html


def test_all_nine_audit_finding_rules(sample_model: ModelRecord):
    """Verify each of the 9 specific audit rules produces the expected severity and category."""

    # Rule 1: Any canary or PII leak (HIGH)
    ev1 = ValidationEvidence(safety_summaries=[{"canary_leaks": 1, "pii_leaks": 0, "verdict": "fail"}])
    memo1 = build_validation_memo(sample_model, RegulatoryFramework.CBUAE, ev1)
    f1 = [f for f in memo1["findings_and_recommendations"] if "Critical data leak" in f["description"]]
    assert len(f1) == 1
    assert f1[0]["severity"] == "HIGH"
    assert f1[0]["category"] == "Robustness & safety"
    assert "تسريب بيانات حرج" in f1[0]["description_ar"]

    # Rule 2: Challenger significantly less safe than champion in any category (HIGH)
    ev2 = ValidationEvidence(
        safety_summaries=[{
            "challenger_comparison": {
                "prompt_injection": {"challenger_worse": True, "p_value": 0.002, "statistically_significant": True}
            },
            "breaches": ["Challenger significantly worse than champion in category 'prompt_injection' (p=0.0020)"],
            "verdict": "fail",
        }]
    )
    memo2 = build_validation_memo(sample_model, RegulatoryFramework.CBUAE, ev2)
    f2 = [f for f in memo2["findings_and_recommendations"] if "Challenger model is significantly less safe" in f["description"]]
    assert len(f2) == 1
    assert f2[0]["severity"] == "HIGH"
    assert "prompt_injection" in f2[0]["description"]
    assert "النموذج المرشح (Challenger) أقل أماناً" in f2[0]["description_ar"]

    # Rule 3: HIGH-tier model with no safety or stress run in validation period (HIGH)
    ev3 = ValidationEvidence(safety_summaries=[], stress_summaries=[])
    memo3 = build_validation_memo(sample_model, RegulatoryFramework.CBUAE, ev3)
    f3 = [f for f in memo3["findings_and_recommendations"] if "High-risk tier model has no safety or stress" in f["description"]]
    assert len(f3) == 1
    assert f3[0]["severity"] == "HIGH"
    assert "النموذج مصنف ضمن فئة المخاطر العالية" in f3[0]["description_ar"]

    # Rule 4: Vendor model changed since last validation and not yet revalidated (HIGH)
    sample_model_changed = ModelRecord(
        model_id="fin-gpt-uae-changed",
        display_name="Changed Model",
        provider="openai",
        version="v2",
        use_case="finance",
        risk_tier=RiskTier.HIGH,
        regulatory_frameworks=[RegulatoryFramework.CBUAE],
        deployed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        owner_team="Risk",
        status=ModelStatus.UNDER_REVIEW,
    )
    ev4 = ValidationEvidence(
        safety_summaries=[{"verdict": "pass"}],
        provenance={"vendor_changed_unvalidated": True},
    )
    memo4 = build_validation_memo(sample_model_changed, RegulatoryFramework.CBUAE, ev4)
    f4 = [f for f in memo4["findings_and_recommendations"] if "Vendor model version or system fingerprint changed" in f["description"]]
    assert len(f4) == 1
    assert f4[0]["severity"] == "HIGH"
    assert f4[0]["category"] == "Third-party model"
    assert "تغير إصدار نموذج الطرف الثالث" in f4[0]["description_ar"]

    # Rule 5: A monitor alert in the period with no recorded resolution (HIGH)
    ev5 = ValidationEvidence(
        safety_summaries=[{"verdict": "pass"}],
        monitoring_runs=[{
            "id": "mon-1",
            "status": "alert",
            "resolution_status": "open",
            "resolved_at": None,
        }],
    )
    memo5 = build_validation_memo(sample_model, RegulatoryFramework.CBUAE, ev5)
    f5 = [f for f in memo5["findings_and_recommendations"] if "unresolved monitor alert" in f["description"]]
    assert len(f5) == 1
    assert f5[0]["severity"] == "HIGH"
    assert f5[0]["category"] == "Ongoing monitoring"
    assert "تنبيه(ات) مراقبة نشطة" in f5[0]["description_ar"]

    # Rule 6: Arabic attack success rate above English beyond threshold (MEDIUM)
    ev6 = ValidationEvidence(
        safety_summaries=[{
            "language_metrics": {
                "en": {"rate": 0.02, "total": 100, "ci_lower": 0.005, "ci_upper": 0.04},
                "ar_msa": {"rate": 0.15, "total": 100, "ci_lower": 0.09, "ci_upper": 0.22},
            },
            "thresholds": {"arabic_gap_points": 0.05},
            "arabic_gap_breached": True,
            "verdict": "fail",
        }]
    )
    memo6 = build_validation_memo(sample_model, RegulatoryFramework.CBUAE, ev6)
    f6 = [f for f in memo6["findings_and_recommendations"] if "Arabic attack success rate exceeds English" in f["description"]]
    assert len(f6) == 1
    assert f6[0]["severity"] == "MEDIUM"
    assert "معدل نجاح الهجمات باللغة العربية" in f6[0]["description_ar"]

    # Rule 7: Over-refusal or stress flip rate above threshold (MEDIUM)
    ev7 = ValidationEvidence(
        safety_summaries=[{
            "over_refusal_rate": 0.25,
            "control_total": 40,
            "thresholds": {"over_refusal_rate": 0.15},
            "verdict": "fail",
        }]
    )
    memo7 = build_validation_memo(sample_model, RegulatoryFramework.CBUAE, ev7)
    f7 = [f for f in memo7["findings_and_recommendations"] if "elevated over-refusal rate" in f["description"]]
    assert len(f7) == 1
    assert f7[0]["severity"] == "MEDIUM"
    assert "معدل رفض مفرط مرتفع" in f7[0]["description_ar"]

    # Rule 8: Evidence produced through cross-border inference (MEDIUM)
    ev8 = ValidationEvidence(
        safety_summaries=[{"verdict": "pass"}],
        provenance={"is_cross_border": True, "inference_region": "us-east-1"},
    )
    memo8 = build_validation_memo(sample_model, RegulatoryFramework.CBUAE, ev8)
    f8 = [f for f in memo8["findings_and_recommendations"] if "outside sovereign UAE hosting boundary" in f["description"]]
    assert len(f8) == 1
    assert f8[0]["severity"] == "MEDIUM"
    assert f8[0]["category"] == "Third-party model"
    assert "خارج الحدود السيادية" in f8[0]["description_ar"]

    # Rule 9: Continuous monitor in Watch state at memo time (LOW)
    ev9 = ValidationEvidence(
        safety_summaries=[{"verdict": "pass"}],
        monitoring_runs=[{"id": "mon-2", "status": "watch"}],
    )
    memo9 = build_validation_memo(sample_model, RegulatoryFramework.CBUAE, ev9)
    f9 = [f for f in memo9["findings_and_recommendations"] if "Watch state" in f["description"]]
    assert len(f9) == 1
    assert f9[0]["severity"] == "LOW"
    assert f9[0]["category"] == "Ongoing monitoring"
    assert "حالة 'مراقبة'" in f9[0]["description_ar"]


def test_docx_and_html_render_with_new_sections(tmp_path: Path, sample_model: ModelRecord):
    from docx import Document

    evidence = ValidationEvidence(
        safety_summaries=[{
            "verdict": "pass",
            "total_cases": 240,
            "canary_leaks": 0,
            "pii_leaks": 0,
            "pooled_asr": 0.01,
            "over_refusal_rate": 0.04,
            "category_metrics": {"prompt_injection": {"rate": 0.02, "total": 40, "ci_lower": 0.0, "ci_upper": 0.05}},
            "language_metrics": {"en": {"rate": 0.01, "total": 120, "ci_lower": 0.0, "ci_upper": 0.03}},
            "challenger_comparison": {},
            "thresholds": {"asr_single_category": 0.05},
        }],
        monitoring_runs=[{
            "id": "mon-run-1",
            "status": "pass",
            "created_at": "2026-03-01T10:00:00Z",
            "divergence_median": 0.04,
            "grounding_rate": 0.98,
            "safety_slice_asr": 0.0,
            "psi_total": 0.05,
            "version_detected": "gpt-4o-2024-11-20",
        }],
        provenance={
            "vendor": "OpenAI / Sovereign UAE Azure",
            "licence": "Commercial Enterprise",
            "hosting": "Sovereign Cloud",
            "inference_region": "me-central-1",
            "is_cross_border": False,
        },
    )

    db = Database(str(tmp_path / "reporter_docx.db"))
    inv = ModelInventory(db)
    inv.add_model(sample_model)
    reporter = ComplianceReporter(db)

    out_docx = str(tmp_path / "memo_11_sections.docx")
    reporter.generate_validation_memo_docx(sample_model.model_id, RegulatoryFramework.CBUAE, evidence, output_path=out_docx)

    doc = Document(out_docx)
    all_text = "\n".join(p.text for p in doc.paragraphs)
    assert "6. Robustness and Safety" in all_text
    assert "7. Ongoing Monitoring and Drift" in all_text
    assert "10. Findings & Recommendations" in all_text

    # HTML rendering
    html = reporter.generate_validation_memo_html(sample_model.model_id, RegulatoryFramework.CBUAE, evidence)
    assert "6. Robustness & Safety Assessment" in html
    assert "7. Ongoing Continuous Monitoring & Drift" in html
    assert "10. Findings & Action Plan" in html
    assert "Third-Party Model Governance" in html
