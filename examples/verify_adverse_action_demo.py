#!/usr/bin/env python3
"""Clean-database verification for the adverse_action demo.

Seeds onto a brand-new SQLite file, then checks — against what the API actually
returns, not what the fixture claims:
  1. both governed models are in Model Inventory
  2. champion-vs-challenger comparisons exist with real step outputs on both branches
  3. the one-click validation memo carries computed evidence (not an empty memo)
  4. re-seeding is idempotent
Run:  python3 examples/verify_adverse_action_demo.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="forkmark_verify_"))
os.environ["FM_DB_PATH"] = str(_TMP / "verify.db")
os.environ["FM_REQUIRE_UI_AUTH"] = "false"
os.environ["FM_BOOTSTRAP_TOKEN"] = "verify-token"

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
MODEL = "adverse-action-explainer"
JAIS = "arabic-banking-assistant"
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


print(f"\nClean database: {os.environ['FM_DB_PATH']}")

# ── 1. discovery + seed ──────────────────────────────────────────────────────
demos = client.get("/api/demos").json()
names = sorted(d["name"] for d in demos)
check("demo auto-discovered", "adverse_action" in names, f"{len(demos)} demos: {names}")

r = client.post("/api/demos/seed", json={"demos": ["adverse_action", "jais_banking"]})
check("seed returned 201", r.status_code == 201, r.text[:200])
res = {x["demo"]: x for x in r.json()["results"]}
aa = res.get("adverse_action", {})
check("no seed error", "error" not in aa, aa.get("error", ""))
check("6 comparisons created", aa.get("comparisons") == 6, str(aa.get("comparisons")))
check("6 decisions recorded", aa.get("decisions") == 6, str(aa.get("decisions")))
check("3 comments recorded", aa.get("comments") == 3, str(aa.get("comments")))

# ── 2. model inventory ───────────────────────────────────────────────────────
models = {m["model_id"]: m for m in client.get("/api/inventory/models").json()}
check("new model in inventory", MODEL in models, sorted(models))
check("Jais assistant still in inventory", JAIS in models)
m = models.get(MODEL, {})
check("risk tier CRITICAL", m.get("risk_tier") == "CRITICAL", str(m.get("risk_tier")))
check("status ACTIVE", m.get("status") == "ACTIVE", str(m.get("status")))
check("provider names Jais", "Jais" in m.get("provider", ""), m.get("provider", ""))

# ── 3. champion vs challenger actually ran ───────────────────────────────────
runs = client.get(f"/api/eval-runs?limit=50").json()
er = next((e for e in (runs if isinstance(runs, list) else runs.get("items", []))
           if e.get("governed_model_id") == MODEL), None)
check("eval run linked to the governed model", er is not None)
if er:
    comps = client.get(f"/api/comparisons?eval_run_id={er['id']}&limit=50").json()
    comps = comps if isinstance(comps, list) else comps.get("items", [])
    check("6 comparisons queryable", len(comps) == 6, str(len(comps)))
    labels = sorted(c["test_case_label"] for c in comps)
    check("all six case labels present", len(set(labels)) == 6, str(labels))
    detail = client.get(f"/api/runs/{comps[0]['run_id']}").json()
    steps = detail.get("steps", [])
    check("8 step outputs per run (2 branches x 4 steps)", len(steps) == 8, str(len(steps)))
    check("no empty step output", all(s.get("output_text") for s in steps))
    branch_models = sorted({s.get("model_id") for s in steps})
    check("both Jais branches present", branch_models == ["jais-30b", "jais-30b-chat-v2"],
          str(branch_models))

# ── 4. one-click validation memo, no hand-fed evidence ───────────────────────
memo_r = client.post(f"/api/compliance/reports/{MODEL}", json={"framework": "cbuae_mms"})
check("memo generated", memo_r.status_code == 200, memo_r.text[:200])
memo = memo_r.json() if memo_r.status_code == 200 else {}
if memo:
    hr = memo["human_review_summary"]
    check("6 human-review decisions rolled up", hr["total_decisions"] == 6, str(hr["total_decisions"]))
    nf = memo["numerical_fidelity"]
    fails = [a for a in nf["assessments"] if not a["is_faithful"]]
    check("fidelity computed over all 6 comparisons", len(nf["assessments"]) == 6,
          str(len(nf["assessments"])))
    check("computed grounding FAIL present", len(fails) == 1, f"{len(fails)} failing assessment(s)")
    if fails:
        flagged = sorted({f["raw"] for f in fails[0]["flagged_numbers"]})
        check("flagged the fabricated figures", flagged == ["12", "6", "71%"], str(flagged))
    bias = memo["bias_and_fairness"]["assessments"]
    check("bias disparity computed", len(bias) == 1, str(len(bias)))
    if bias:
        check("bias FAILS the 1.2 threshold", not bias[0]["passes_threshold"],
              f"ratio {bias[0]['disparity_ratio']:.3f}")
    cats = [f["category"] for f in memo["findings_and_recommendations"]]
    check("memo is not empty/INFO-only",
          "Numerical fidelity" in cats and "Bias & fairness" in cats, str(sorted(set(cats))))
    suite = memo["scope_and_methodology"]["evaluator_suite"]
    check("evaluator suite recorded", "NumericalFidelityEvaluator" in suite, str(suite))

# docx path — this is what the "Generate Validation Report" button downloads
d = client.post(f"/api/compliance/reports/{MODEL}/docx", json={"framework": "cbuae_mms"})
check("memo .docx downloads", d.status_code == 200 and len(d.content) > 20000,
      f"{d.status_code}, {len(d.content)} bytes")

# ── 5. idempotency ───────────────────────────────────────────────────────────
before_models = len(client.get("/api/inventory/models").json())
before_wf = len(client.get("/api/workflows").json())
r2 = client.post("/api/demos/seed", json={"demos": ["adverse_action"]})
check("re-seed returned 201", r2.status_code == 201)
after_models = len(client.get("/api/inventory/models").json())
after_wf = len(client.get("/api/workflows").json())
check("no duplicate model rows", before_models == after_models, f"{before_models} -> {after_models}")
check("no duplicate workflow rows", before_wf == after_wf, f"{before_wf} -> {after_wf}")
comps2 = client.get("/api/comparisons?limit=200").json()
comps2 = comps2 if isinstance(comps2, list) else comps2.get("items", [])
aa_comps = [c for c in comps2 if c["test_case_label"] in
            {c2["label"] for c2 in json.load(open(ROOT / "examples/adverse_action_demo/fixtures.json"))["cases"]}]
check("still exactly 6 comparisons after re-seed", len(aa_comps) == 6, str(len(aa_comps)))

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
