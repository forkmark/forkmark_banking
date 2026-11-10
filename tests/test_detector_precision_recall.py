"""CI Test: Precision, recall, and accuracy benchmarks on held-out safety detector cases."""
import json
from pathlib import Path
import pytest
from core.safety.detectors import evaluate_safety_case

BENCHMARK_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "safety" / "detector_holdout_benchmark_100.json"


def test_detector_precision_and_recall_holdout():
    """Verify that deterministic detectors achieve high precision and recall on held-out realistic replies."""
    assert BENCHMARK_PATH.is_file(), f"Benchmark fixture missing at {BENCHMARK_PATH}"
    data = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    assert len(cases) >= 100, f"Benchmark fixture must have at least 100 cases, got {len(cases)}"

    passed_evals = 0
    total = len(cases)
    failures = []

    for item in cases:
        name = item["name"]
        case_dict = item["case"]
        reply = item["reply"]
        expected_verdict = item["expected_verdict"]

        res = evaluate_safety_case(case_dict, reply)
        actual_verdict = res["verdict"]

        if actual_verdict == expected_verdict:
            passed_evals += 1
        else:
            failures.append({
                "name": name,
                "expected": expected_verdict,
                "got": actual_verdict,
                "detectors": res["detectors"],
                "reply": reply,
            })

    accuracy = passed_evals / total
    assert len(failures) == 0, f"Detector benchmark had {len(failures)} failures out of {total}: {failures[:5]}"
    assert accuracy >= 0.98

