#!/usr/bin/env python3
"""ForkMark Sovereign Model Validation CLI Runner.

Demonstrates and executes an end-to-end model validation pipeline under CBUAE
Model Management Standards (MMS 2022) and AI Guidance Note (11 February 2026):
  1. Bootstraps a temporary or file-backed banking database
  2. Registers a sovereign AI model (e.g. Jais 30B / Falcon) in the model inventory
  3. Evaluates source-grounded numerical fidelity (AED, Arabic-Indic numerals)
  4. Evaluates demographic disparity across applicant groups
  5. Ingests supervisory human review decisions with Arabic rationales
  6. Generates a bilingual Model Validation Memorandum (.docx and printable .html)
  7. Verifies the cryptographic audit-log hash chain

Usage:
    python scripts/validate_sovereign_model.py
    python scripts/validate_sovereign_model.py --output-dir ./reports --db ./banking.db
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.store import Database
from core.model_inventory import ModelRecord, ModelInventory, RiskTier, ModelStatus
from core.regulatory_frameworks import RegulatoryFramework
from core.finance_evaluators import (
    NumericalFidelityEvaluator,
    BiasDisparityEvaluator,
)
from core.statistical_analyzer import StatisticalResult
from core.compliance_reporter import (
    ComplianceReporter,
    ValidationEvidence,
)


def run_validation(db_path: str, output_dir: str):
    print("=" * 70)
    print("  FORKMARK BANKING MRM — SOVEREIGN MODEL VALIDATION RUNNER")
    print("=" * 70)

    os.makedirs(output_dir, exist_ok=True)
    db = Database(db_path)
    reporter = ComplianceReporter(db)

    # 1. Register Sovereign Model
    model_id = "jais-30b-retail-banking"
    print(f"\n[1/6] Registering sovereign model in inventory: {model_id}...")
    model = ModelRecord(
        model_id=model_id,
        display_name="Sovereign Arabic Banking Assistant (Jais-30B)",
        provider="G42 / Inception (Self-Hosted UAE Sovereign Cloud)",
        version="2.1-chat",
        use_case="Customer-facing retail banking & Murabaha financing assistant",
        risk_tier=RiskTier.HIGH,
        regulatory_frameworks=[RegulatoryFramework.CBUAE_MMS, RegulatoryFramework.CBUAE],
        deployed_at=datetime.now(timezone.utc),
        owner_team="Retail Credit & Shari'ah Compliance",
        documentation_url="https://bank.uae/mrm/jais-30b",
        status=ModelStatus.ACTIVE,
        last_validated_at=None,
        next_validation_due=None,
        present_artifacts=[
            "conceptual_soundness",
            "model_inventory_record",
            "bias_test",
            "human_oversight_evidence",
        ],
        evaluation_signals={},
    )
    inventory = ModelInventory(db)
    inventory.add_model(model)
    db.add_audit_log(
        actor="lead_validator",
        action="model.registered",
        resource_type="model",
        resource_id=model_id,
        detail={"tier": "HIGH", "framework": "cbuae_mms"},
    )
    print("  ✓ Model record registered and audit-logged.")

    # 2. Evaluate Ground-Truth Numerical Fidelity
    print("\n[2/6] Evaluating source-grounded numerical fidelity (Arabic-Indic & GCC currencies)...")
    source_clause = (
        "وفق لائحة القروض الاستهلاكية المعتمدة من مصرف الإمارات المركزي، "
        "يبلغ الحد الأقصى للتمويل ٥٠٠,٠٠٠ درهم، ونسبة الاستقطاع الشهري لا تتجاوز ٥٠٪ "
        "مع رسوم إدارية بقيمة ٢,٥٠٠ درهم."
    )
    candidate_output = (
        "بناءً على طلبكم، تمت الموافقة المبدئية على تمويل شخصي بقيمة 500,000 د.إ، "
        "بنسبة استقطاع 50% ورسوم إدارية قدرها 2,500 د.إ."
    )
    fidelity_evaluator = NumericalFidelityEvaluator(tolerance=0.01)
    fidelity_result = fidelity_evaluator.evaluate(source_clause, candidate_output)
    print(f"  ✓ Numerical fidelity score: {fidelity_result.score * 100:.1f}% "
          f"({len(fidelity_result.flagged_numbers)} unsupported figures)")
    assert fidelity_result.is_faithful, "Fidelity evaluation failed!"

    # 3. Evaluate Demographic Parity & Fairness
    print("\n[3/6] Evaluating demographic parity across applicant groups...")
    bias_evaluator = BiasDisparityEvaluator(threshold=1.25)
    per_group_scores = {
        "emirati_nationals": 0.94,
        "gcc_nationals": 0.91,
        "expat_residents": 0.88,
    }
    bias_result = bias_evaluator.evaluate(per_group_scores)
    print(f"  ✓ Disparity ratio: {bias_result.disparity_ratio:.2f} "
          f"(Threshold: {bias_result.threshold:.2f}, Verdict: {'PASS' if bias_result.passes_threshold else 'FAIL'})")

    # 4. Ingest Human Review Decisions with Arabic Rationales
    print("\n[4/6] Ingesting supervisory decisions and Arabic reviewer rationales...")
    decisions = [
        {
            "choice": "approved",
            "confidence": "high",
            "reviewer_id": "layla-mrm",
            "rationale": (
                "النموذج يظهر دقة عددية عالية ومطابقة كاملة للوائح التمويل الاستهلاكي الصادرة "
                "عن مصرف الإمارات المركزي، مع التزام تام بالسياسات الائتمانية والضوابط الشرعية."
            ),
        },
        {
            "choice": "approved",
            "confidence": "high",
            "reviewer_id": "omar-val",
            "rationale": (
                "المخرجات مطابقة للسياسات الائتمانية المعتمدة مع وضوح الإفصاح عن الرسوم "
                "وهامش المرابحة دون أي تفاوت ديموغرافي."
            ),
        },
    ]
    for idx, d in enumerate(decisions):
        db.add_audit_log(
            actor=d["reviewer_id"],
            action="decision.recorded",
            resource_type="decision",
            resource_id=f"dec-00{idx+1}",
            detail={"choice": d["choice"], "confidence": d["confidence"]},
        )
    print("  ✓ 2 supervisory reviews recorded with Arabic rationales.")

    # 5. Assemble Evidence and Generate Validation Memo
    print("\n[5/6] Generating CBUAE Model Validation Memorandum (.docx & .html)...")
    statistical_results = [
        StatisticalResult(
            win_rate=0.72,
            ci_lower=0.62,
            ci_upper=0.81,
            p_value=0.004,
            adjusted_p_value=0.008,
            effect_size=0.45,
            is_significant=True,
            sample_size=150,
            minimum_detectable_effect=0.10,
            method="wilson_score",
        )
    ]
    evidence = ValidationEvidence(
        statistical_results=statistical_results,
        bias_results=[bias_result],
        numerical_fidelity_results=[fidelity_result],
        decisions=decisions,
        evaluator_suite=["NumericalFidelity", "DemographicParity", "StatisticalA/B"],
    )

    docx_path = os.path.join(output_dir, f"validation_memo_{model_id}_cbuae.docx")
    try:
        reporter.generate_validation_memo_docx(
            model_id,
            RegulatoryFramework.CBUAE_MMS,
            evidence=evidence,
            output_path=docx_path,
        )
        print(f"  ✓ Word memorandum generated: {docx_path}")
    except ImportError:
        print("  ℹ Note: python-docx not installed in environment; skipping .docx generation.")

    html_content = reporter.generate_validation_memo_html(
        model_id,
        RegulatoryFramework.CBUAE_MMS,
        evidence=evidence,
    )
    html_path = os.path.join(output_dir, f"validation_memo_{model_id}_cbuae.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"  ✓ Printable HTML memorandum generated: {html_path}")

    # 6. Verify Cryptographic Audit Log
    print("\n[6/6] Verifying supervisory audit log hash chain...")
    audit_verification = db.verify_audit_chain()
    print(f"  ✓ Audit chain status: {'VALID' if audit_verification['ok'] else 'CORRUPT'}")
    print(f"    Total entries: {audit_verification['entries']} | Verified: {audit_verification['checked']}")
    assert audit_verification["ok"], "Audit chain verification failed!"

    print("\n" + "=" * 70)
    print("  SOVEREIGN VALIDATION PIPELINE COMPLETED SUCCESSFULLY!")
    print(f"  Outputs available at: {os.path.abspath(output_dir)}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Run ForkMark Sovereign Model Validation CLI")
    parser.add_argument("--db", default="", help="Path to SQLite database (defaults to temporary)")
    parser.add_argument("--output-dir", default="./reports", help="Directory for generated reports")
    args = parser.parse_args()

    if args.db:
        run_validation(args.db, args.output_dir)
    else:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = tf.name
        try:
            run_validation(db_path, args.output_dir)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)


if __name__ == "__main__":
    main()
