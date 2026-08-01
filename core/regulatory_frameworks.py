"""Regulatory framework definitions for ForkMark model risk management.

This module encodes the model-validation expectations of the AI-governance and
model-risk-management (MRM) regimes that ForkMark supports, so the rest of the
platform (model-inventory coverage reports, validation memos, comparison-run
setup) can reason about *what evidence each regulator expects*.

ForkMark is **UAE-first**: the three UAE regimes lead, and the international
regimes are offered as additional coverage. Declaration order below is
intentional and flows through to ``all_frameworks()`` and the coverage reports.

Frameworks covered (in ForkMark's priority order):
    CBUAE_MMS         — Central Bank of the UAE Model Management Standards &
                        Guidance, Notice 5052/2022 (2022-12-21): the binding,
                        technology-neutral lifecycle MRM regime every UAE-licensed
                        bank is examined against (inventory, materiality tiering,
                        development, independent validation, ongoing monitoring,
                        governance, data management).
    CBUAE             — CBUAE Responsible-AI guidance for licensed financial
                        institutions (2026): five principles — governance,
                        fairness, transparency, human oversight, data privacy —
                        including consumer disclosure/explainability in Arabic and
                        English.
    UAE_ENABLING_TECH — Guidelines for Financial Institutions Adopting Enabling
                        Technologies (2021), issued jointly by the CBUAE, SCA,
                        DFSA (DIFC) and FSRA (ADGM): cross-regulator expectations
                        for AI governance, model validation, material-application
                        registries, and vendor due diligence.
    EU_AI_ACT         — Regulation (EU) 2024/1689 (the "AI Act"): obligations for
                        high-risk AI systems (Title III, Chapter 2).
    SR_26_2           — US interagency (Federal Reserve / OCC / FDIC) SR 26-2
                        (2026-04-17), the current US MRM standard superseding
                        SR 11-7 (2011) — and one that *excludes* generative and
                        agentic AI, leaving the gap ForkMark is built to fill.
    PRA_SS1_23        — Bank of England / PRA Supervisory Statement SS1/23,
                        "Model risk management principles for banks" (2023-05).

A UAE bank deploying an AI/ML model is typically subject to all three UAE
instruments at once — the MMS for lifecycle discipline, the AI guidance for
AI-specific fairness/transparency, and the joint guidelines for cross-regulator
technology-adoption controls.

The metadata here is a structured, engineering-facing summary intended to drive
coverage tracking. It is not legal advice and does not replace the source
regulations or a firm's own model risk policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "RegulatoryFramework",
    "ArtifactType",
    "FrameworkRequirements",
    "get_framework_requirements",
    "get_all_requirements",
    "all_frameworks",
]

# One year, expressed in days — the common revalidation cadence across all
# regimes for higher-risk models. Kept as a named constant for clarity.
_ANNUAL_CYCLE_DAYS = 365


class RegulatoryFramework(str, Enum):
    """Supported model risk management / AI governance regimes.

    Declaration order is intentional: the UAE regimes lead because ForkMark is
    UAE-first. ``all_frameworks()`` and coverage reports follow this order.
    """

    # ── UAE — the hero market ────────────────────────────────────────────────
    CBUAE_MMS = "cbuae_mms"
    CBUAE = "cbuae"
    UAE_ENABLING_TECH = "uae_enabling_tech"
    # ── International regimes — additional coverage ──────────────────────────
    EU_AI_ACT = "eu_ai_act"
    SR_26_2 = "sr_26_2"
    PRA_SS1_23 = "pra_ss1_23"


class ArtifactType(str, Enum):
    """Canonical ForkMark evidence artifact types.

    Each value is a stable identifier used across the model-inventory coverage
    report, the validation memo's regulatory-mapping section, and the
    ``required_artifacts`` lists below, so coverage can be computed by simple set
    membership rather than fuzzy string matching.
    """

    CONCEPTUAL_SOUNDNESS = "conceptual_soundness_review"
    OUTCOME_ANALYSIS = "outcome_analysis"
    ONGOING_MONITORING = "ongoing_monitoring_report"
    BIAS_FAIRNESS = "bias_fairness_assessment"
    NUMERICAL_FIDELITY = "numerical_fidelity_assessment"
    HUMAN_OVERSIGHT = "human_oversight_evidence"
    TECHNICAL_DOCUMENTATION = "technical_documentation"
    CONFORMITY_ASSESSMENT = "conformity_assessment"
    CE_MARKING = "ce_marking_evidence"
    SUPERVISORY_EVIDENCE_PACK = "supervisory_evidence_pack"
    GOVERNANCE_RECORD = "governance_record"
    TRANSPARENCY_RECORD = "transparency_record"
    DATA_PRIVACY_RECORD = "data_privacy_record"
    # CBUAE Model Management Standards (2022) lifecycle artifacts
    MODEL_INVENTORY_RECORD = "model_inventory_record"
    MODEL_MATERIALITY_TIERING = "model_materiality_tiering"
    INDEPENDENT_VALIDATION = "independent_validation_report"
    DATA_MANAGEMENT_FRAMEWORK = "data_management_framework"
    VALIDATION_MEMO = "validation_memo"


@dataclass(frozen=True)
class FrameworkRequirements:
    """Structured validation requirements for a single regulatory framework.

    Attributes:
        framework:               The :class:`RegulatoryFramework` this describes.
        name:                    Human-readable framework name.
        jurisdiction:            Supervisory jurisdiction (e.g. "United States").
        reference:               Citation for the source regulation / guidance.
        summary:                 One-line description of the framework's focus.
        required_artifacts:      Evidence artifacts a model must have on file to
                                 be considered covered — each string is an
                                 :class:`ArtifactType` value (a ForkMark export /
                                 report type).
        validation_cycle_days:   Maximum days between full model revalidations.
        bias_test_required:      Whether bias / fairness testing is mandatory.
        human_oversight_required:Whether documented human oversight is mandatory.
        documentation_fields:    Fields a validation memo should populate to
                                 satisfy the framework's documentation expectations.
    """

    framework: RegulatoryFramework
    name: str
    jurisdiction: str
    reference: str
    summary: str
    required_artifacts: list[str]
    validation_cycle_days: int
    bias_test_required: bool
    human_oversight_required: bool
    documentation_fields: list[str]


_REQUIREMENTS: dict[RegulatoryFramework, FrameworkRequirements] = {
    # ── UAE — hero regimes ──────────────────────────────────────────────────
    RegulatoryFramework.CBUAE_MMS: FrameworkRequirements(
        framework=RegulatoryFramework.CBUAE_MMS,
        name="CBUAE Model Management Standards (2022)",
        jurisdiction="United Arab Emirates (Central Bank of the UAE)",
        reference=(
            "CBUAE Model Management Standards & Guidance, "
            "attachment to Notice 5052/2022 (2022-12-21)"
        ),
        summary=(
            "Binding, technology-neutral UAE model-risk-management standard for all "
            "licensed banks, spanning the full model lifecycle: a complete model "
            "inventory, materiality/risk tiering, sound development, independent "
            "validation with documented validation reports reviewed by a Model "
            "Oversight Committee, ongoing monitoring at a frequency set by model "
            "type, a formal data management framework, and clear model governance."
        ),
        required_artifacts=[
            ArtifactType.MODEL_INVENTORY_RECORD.value,
            ArtifactType.MODEL_MATERIALITY_TIERING.value,
            ArtifactType.CONCEPTUAL_SOUNDNESS.value,
            ArtifactType.INDEPENDENT_VALIDATION.value,
            ArtifactType.ONGOING_MONITORING.value,
            ArtifactType.OUTCOME_ANALYSIS.value,
            ArtifactType.DATA_MANAGEMENT_FRAMEWORK.value,
            ArtifactType.GOVERNANCE_RECORD.value,
            ArtifactType.VALIDATION_MEMO.value,
        ],
        validation_cycle_days=_ANNUAL_CYCLE_DAYS,
        bias_test_required=False,
        human_oversight_required=True,
        documentation_fields=[
            "model_inventory_and_ownership",
            "model_materiality_and_risk_tiering",
            "model_development_and_design",
            "independent_validation_findings",
            "ongoing_monitoring_and_performance",
            "data_management_framework",
            "model_governance_and_oversight_committee",
            "model_limitations_and_weaknesses",
        ],
    ),
    RegulatoryFramework.CBUAE: FrameworkRequirements(
        framework=RegulatoryFramework.CBUAE,
        name="CBUAE — Responsible AI Guidance for Licensed Financial Institutions",
        jurisdiction="United Arab Emirates (Central Bank of the UAE)",
        reference="CBUAE guidance on the responsible use of AI in financial services (2026)",
        summary=(
            "Five-principle UAE framework for responsible AI use: governance, "
            "fairness, transparency, human oversight, and data privacy — including "
            "consumer disclosure and explainability of AI-assisted decisions in both "
            "Arabic and English, with accountability retained by the institution even "
            "when the model is a third-party or cloud vendor."
        ),
        required_artifacts=[
            ArtifactType.GOVERNANCE_RECORD.value,
            ArtifactType.BIAS_FAIRNESS.value,
            ArtifactType.TRANSPARENCY_RECORD.value,
            ArtifactType.HUMAN_OVERSIGHT.value,
            ArtifactType.DATA_PRIVACY_RECORD.value,
            ArtifactType.VALIDATION_MEMO.value,
        ],
        validation_cycle_days=_ANNUAL_CYCLE_DAYS,
        bias_test_required=True,
        human_oversight_required=True,
        documentation_fields=[
            "governance_structure_and_accountability",
            "fairness_and_bias_testing_results",
            "model_transparency_and_explainability",
            "bilingual_consumer_disclosure_arabic_english",
            "human_oversight_arrangements",
            "data_privacy_and_protection_controls",
            "third_party_and_cloud_vendor_due_diligence",
        ],
    ),
    RegulatoryFramework.UAE_ENABLING_TECH: FrameworkRequirements(
        framework=RegulatoryFramework.UAE_ENABLING_TECH,
        name="UAE Joint Guidelines — FIs Adopting Enabling Technologies",
        jurisdiction="United Arab Emirates (CBUAE, SCA, DFSA/DIFC, FSRA/ADGM)",
        reference=(
            "Guidelines for Financial Institutions Adopting Enabling Technologies "
            "(2021), issued jointly by the CBUAE, SCA, DFSA and FSRA"
        ),
        summary=(
            "Cross-regulator UAE principles for adopting AI and other enabling "
            "technologies: governance and senior-management accountability, model "
            "development and validation, an enterprise-wide registry of material AI "
            "applications with version documentation, vendor due diligence and "
            "outsourcing controls, data protection, and supervisory engagement — "
            "spanning mainland (CBUAE/SCA), DIFC (DFSA) and ADGM (FSRA)."
        ),
        required_artifacts=[
            ArtifactType.GOVERNANCE_RECORD.value,
            ArtifactType.MODEL_INVENTORY_RECORD.value,
            ArtifactType.INDEPENDENT_VALIDATION.value,
            ArtifactType.TECHNICAL_DOCUMENTATION.value,
            ArtifactType.TRANSPARENCY_RECORD.value,
            ArtifactType.DATA_PRIVACY_RECORD.value,
            ArtifactType.HUMAN_OVERSIGHT.value,
            ArtifactType.VALIDATION_MEMO.value,
        ],
        validation_cycle_days=_ANNUAL_CYCLE_DAYS,
        bias_test_required=False,
        human_oversight_required=True,
        documentation_fields=[
            "governance_and_senior_management_accountability",
            "material_application_registry_and_versioning",
            "model_development_and_validation",
            "vendor_due_diligence_and_outsourcing_controls",
            "data_protection_and_privacy",
            "consumer_and_market_conduct_safeguards",
            "supervisory_engagement_record",
        ],
    ),
    # ── International regimes — additional coverage ──────────────────────────
    RegulatoryFramework.EU_AI_ACT: FrameworkRequirements(
        framework=RegulatoryFramework.EU_AI_ACT,
        name="EU AI Act — High-Risk AI System Conformity",
        jurisdiction="European Union",
        reference="Regulation (EU) 2024/1689, Title III Ch. 2; Annexes IV & VII",
        summary=(
            "Conformity requirements for high-risk AI systems: risk management, "
            "data governance, technical documentation, record-keeping, "
            "transparency, human oversight, accuracy/robustness, plus mandatory "
            "bias testing and CE marking before market placement."
        ),
        required_artifacts=[
            ArtifactType.CONFORMITY_ASSESSMENT.value,
            ArtifactType.CE_MARKING.value,
            ArtifactType.TECHNICAL_DOCUMENTATION.value,
            ArtifactType.BIAS_FAIRNESS.value,
            ArtifactType.HUMAN_OVERSIGHT.value,
            ArtifactType.OUTCOME_ANALYSIS.value,
            ArtifactType.VALIDATION_MEMO.value,
        ],
        validation_cycle_days=_ANNUAL_CYCLE_DAYS,
        bias_test_required=True,
        human_oversight_required=True,
        documentation_fields=[
            "intended_purpose_and_scope",
            "system_architecture_and_design",
            "training_validation_testing_data_governance",
            "accuracy_robustness_and_cybersecurity_metrics",
            "risk_management_system",
            "bias_and_fairness_mitigation",
            "human_oversight_measures",
            "post_market_monitoring_plan",
        ],
    ),
    RegulatoryFramework.SR_26_2: FrameworkRequirements(
        framework=RegulatoryFramework.SR_26_2,
        name="SR 26-2 — Model Risk Management (supersedes SR 11-7)",
        jurisdiction="United States (Federal Reserve / OCC / FDIC)",
        reference="Interagency SR 26-2 (2026-04-17), superseding Fed SR 11-7 / OCC 2011-12 (2011)",
        summary=(
            "The current US model-risk standard — a risk-based, materiality-sensitive "
            "successor to SR 11-7's three pillars (conceptual soundness, ongoing "
            "monitoring, and outcomes analysis, with effective challenge and "
            "governance). Notably it excludes generative and agentic AI from scope, "
            "leaving an AI-model governance gap ForkMark is built to fill."
        ),
        required_artifacts=[
            ArtifactType.CONCEPTUAL_SOUNDNESS.value,
            ArtifactType.OUTCOME_ANALYSIS.value,
            ArtifactType.ONGOING_MONITORING.value,
            ArtifactType.HUMAN_OVERSIGHT.value,
            ArtifactType.VALIDATION_MEMO.value,
        ],
        validation_cycle_days=_ANNUAL_CYCLE_DAYS,
        bias_test_required=False,
        human_oversight_required=True,
        documentation_fields=[
            "model_purpose_and_use",
            "conceptual_soundness_and_design",
            "development_data_and_assumptions",
            "outcome_analysis_and_benchmarking",
            "ongoing_monitoring_plan",
            "assumptions_and_limitations",
            "independent_validation_and_effective_challenge",
            "model_owner_and_governance",
        ],
    ),
    RegulatoryFramework.PRA_SS1_23: FrameworkRequirements(
        framework=RegulatoryFramework.PRA_SS1_23,
        name="PRA SS1/23 — Model Risk Management Principles for Banks",
        jurisdiction="United Kingdom (Bank of England / PRA)",
        reference="PRA Supervisory Statement SS1/23 (May 2023)",
        summary=(
            "Technology-agnostic, outcomes-focused UK MRM principles covering "
            "model identification and risk tiering, governance, development and "
            "implementation, independent validation, and risk mitigants."
        ),
        required_artifacts=[
            ArtifactType.SUPERVISORY_EVIDENCE_PACK.value,
            ArtifactType.CONCEPTUAL_SOUNDNESS.value,
            ArtifactType.OUTCOME_ANALYSIS.value,
            ArtifactType.ONGOING_MONITORING.value,
            ArtifactType.HUMAN_OVERSIGHT.value,
            ArtifactType.VALIDATION_MEMO.value,
        ],
        validation_cycle_days=_ANNUAL_CYCLE_DAYS,
        bias_test_required=False,
        human_oversight_required=True,
        documentation_fields=[
            "model_identification_and_risk_tiering",
            "governance_and_accountable_smf_owner",
            "development_and_implementation_evidence",
            "independent_validation_findings",
            "ongoing_monitoring_and_performance",
            "model_limitations_and_weaknesses",
            "risk_mitigants_and_remediation_actions",
        ],
    ),
}


def get_framework_requirements(
    framework: RegulatoryFramework,
) -> FrameworkRequirements:
    """Return the structured requirements for a single regulatory framework.

    Args:
        framework: The regulatory framework to look up.

    Returns:
        The :class:`FrameworkRequirements` for ``framework``.

    Raises:
        ValueError: If no requirements are registered for ``framework``.
    """
    try:
        return _REQUIREMENTS[framework]
    except KeyError as exc:  # pragma: no cover - defensive; enum is exhaustive
        raise ValueError(
            f"No requirements registered for framework: {framework!r}"
        ) from exc


def get_all_requirements() -> list[FrameworkRequirements]:
    """Return requirements for every supported framework, in declaration order."""
    return [_REQUIREMENTS[fw] for fw in RegulatoryFramework]


def all_frameworks() -> list[RegulatoryFramework]:
    """Return the list of every supported regulatory framework."""
    return list(RegulatoryFramework)
