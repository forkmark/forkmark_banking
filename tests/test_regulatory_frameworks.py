"""Unit tests for core.regulatory_frameworks."""
from __future__ import annotations

import pytest

from core.regulatory_frameworks import (
    ArtifactType,
    RegulatoryFramework,
    all_frameworks,
    get_all_requirements,
    get_framework_requirements,
)


def test_every_framework_has_complete_requirements() -> None:
    """Each supported framework resolves to fully-populated requirements."""
    frameworks = all_frameworks()
    assert set(frameworks) == {
        RegulatoryFramework.CBUAE_MMS,
        RegulatoryFramework.CBUAE,
        RegulatoryFramework.UAE_ENABLING_TECH,
        RegulatoryFramework.EU_AI_ACT,
        RegulatoryFramework.SR_26_2,
        RegulatoryFramework.PRA_SS1_23,
    }
    for fw in frameworks:
        req = get_framework_requirements(fw)
        assert type(req).__name__ == "FrameworkRequirements"
        assert req.framework == fw
        assert req.name and req.jurisdiction and req.reference
        assert req.required_artifacts, "required_artifacts must be non-empty"
        assert req.documentation_fields, "documentation_fields must be non-empty"
        # All four regimes use an annual (365-day) revalidation cadence.
        assert req.validation_cycle_days == 365


def test_bias_and_human_oversight_flags_match_regimes() -> None:
    """Bias testing is mandatory only for the EU AI Act and CBUAE; human
    oversight is mandatory across all four regimes."""
    bias_required = {
        fw for fw in all_frameworks()
        if get_framework_requirements(fw).bias_test_required
    }
    assert bias_required == {RegulatoryFramework.EU_AI_ACT, RegulatoryFramework.CBUAE}

    for fw in all_frameworks():
        assert get_framework_requirements(fw).human_oversight_required is True


def test_required_artifacts_are_valid_and_include_validation_memo() -> None:
    """Every required artifact maps to a known ArtifactType, and every framework
    requires a validation memo as the umbrella evidence document."""
    valid_values = {a.value for a in ArtifactType}
    for req in get_all_requirements():
        for artifact in req.required_artifacts:
            assert artifact in valid_values, f"unknown artifact {artifact!r}"
        assert ArtifactType.VALIDATION_MEMO.value in req.required_artifacts


def test_get_all_requirements_matches_framework_order() -> None:
    """get_all_requirements() returns one entry per framework in enum order."""
    reqs = get_all_requirements()
    assert [r.framework for r in reqs] == all_frameworks()
    assert len(reqs) == 6


def test_eu_ai_act_requires_conformity_and_bias_artifacts() -> None:
    """Spot-check that the EU AI Act carries its distinctive artifacts."""
    req = get_framework_requirements(RegulatoryFramework.EU_AI_ACT)
    assert ArtifactType.CONFORMITY_ASSESSMENT.value in req.required_artifacts
    assert ArtifactType.CE_MARKING.value in req.required_artifacts
    assert ArtifactType.BIAS_FAIRNESS.value in req.required_artifacts


def test_cbuae_mms_carries_lifecycle_artifacts() -> None:
    """The CBUAE Model Management Standards (2022) carry the distinctive
    lifecycle-MRM artifacts UAE banks are examined against, and do not require
    AI-style bias testing (that lives in the 2026 CBUAE AI guidance)."""
    req = get_framework_requirements(RegulatoryFramework.CBUAE_MMS)
    # Value equality (RegulatoryFramework is a str-Enum), not identity: another
    # suite may reload core.regulatory_frameworks in place, which rebuilds the
    # requirements table with a fresh enum class. Identity (`is`) would then fail
    # spuriously in a full run even though the framework is correct.
    assert req.framework == RegulatoryFramework.CBUAE_MMS
    assert "2022" in req.reference
    for a in (
        ArtifactType.MODEL_INVENTORY_RECORD,
        ArtifactType.MODEL_MATERIALITY_TIERING,
        ArtifactType.INDEPENDENT_VALIDATION,
        ArtifactType.DATA_MANAGEMENT_FRAMEWORK,
    ):
        assert a.value in req.required_artifacts
    assert req.bias_test_required is False
    assert req.human_oversight_required is True


def test_framework_enum_is_string_valued() -> None:
    """RegulatoryFramework values are stable lowercase string identifiers."""
    assert RegulatoryFramework.SR_26_2.value == "sr_26_2"
    assert RegulatoryFramework("eu_ai_act") is RegulatoryFramework.EU_AI_ACT
    with pytest.raises(ValueError):
        RegulatoryFramework("not_a_framework")
