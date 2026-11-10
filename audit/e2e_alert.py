"""Same as e2e.py but triggers an Alert (unsafe reply) instead of a vendor swap."""
import os, sys, time, json, uuid, tempfile
from datetime import datetime, timezone
REPO = sys.argv[1]
os.environ["FM_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "e2e.db")
sys.path.insert(0, REPO)
from fastapi.testclient import TestClient
import backend.deps as deps, backend.main as main
import core.llm_gateway as gw
from tests.fixtures.mock_llm import MockLLMHandler, create_mock_transport

out = {}
transport, h = create_mock_transport(MockLLMHandler(default_model="jais-2-8b", system_fingerprint="fp1",
    default_reply="I'm sorry, but I cannot help with that request."))
gw.gateway.set_transport(transport)
c = TestClient(main.app, raise_server_exceptions=False); c.__enter__()
_, key = deps.db.create_api_key("audit", role="admin"); c.headers["X-API-Key"] = key

p = c.post("/api/providers", json={"name":"jais-local","provider_type":"openai_compatible","base_url":"http://127.0.0.1:8080/v1",
      "api_key":"k","deployment_kind":"openai_compatible","model_family":"jais","default_model":"jais-2-8b",
      "inference_region":"me-central-1","hosting":"bank_self_hosted","vendor_name":"Inception","licence":"Jais licence"}).json()
mid = "audit-model"
r = c.post("/api/inventory/models", json={"model_id":mid,"display_name":"Audit Jais","provider":"Inception","version":"2-8b",
      "use_case":"Retail banking assistant","risk_tier":"MEDIUM","regulatory_frameworks":["cbuae"],
      "deployed_at":datetime.now(timezone.utc).isoformat(),"owner_team":"MRM","connector_id":p["id"]})
out["model_create"] = r.status_code
inst = c.post("/api/safety/corpus/install"); out["install"] = (inst.status_code, inst.json() if inst.status_code<300 else inst.text[:200])
ts_id = inst.json().get("test_set_id")
calls_before = len(h.requests)
v = c.post(f"/api/inventory/models/{mid}/validation-runs", json={"test_set_id":ts_id,
      "champion":{"connector_id":p["id"],"model":"jais-2-8b"},"challenger":{"connector_id":p["id"],"model":"jais-2-70b"},
      "suites":["safety","stress"]})
out["validation"] = (v.status_code, v.json())
ids = v.json().get("eval_run_ids", [])
for _ in range(120):
    sts = [c.get(f"/api/eval-runs/{i}").json().get("status") for i in ids]
    if all(s in ("completed","failed") for s in sts): break
    time.sleep(0.5)
out["run_status"] = sts
out["mock_calls_during_validation"] = len(h.requests) - calls_before
for i, kind in zip(ids, ["safety","stress"]):
    s = c.get(f"/api/safety/results/{i}").json()
    out[f"{kind}_summary"] = {k: s.get(k) for k in ("verdict","pooled_asr","over_refusal_rate","canary_leaks","pii_leaks")}
    out[f"{kind}_breaches"] = (s.get("breaches") or [])[:6]
    out[f"{kind}_threshold_asr_pooled"] = (s.get("thresholds_applied") or {}).get("asr_pooled")
    out[f"{kind}_has_flip_rate"] = "stress_flip_rate" in s
gl = c.get("/api/gateway/log").json()
out["gateway_log_rows"] = len(gl) if isinstance(gl, list) else gl.get("total", str(gl)[:80])

# monitor: small canary test set
ts2 = c.post("/api/test-sets", json={"name":"canary"}).json()["id"]
for i in range(5): c.post(f"/api/test-sets/{ts2}/cases", json={"label":f"q{i}","input_data":{"input":f"What is fee {i}?"}})
m = c.post(f"/api/inventory/models/{mid}/monitors", json={"test_set_id":ts2,"connector_id":p["id"],"include_safety_slice":True}).json()
out["monitor_create_keys"] = list(m.keys())[:8]
for _ in range(60):
    mm = [x for x in c.get(f"/api/inventory/models/{mid}/monitors").json() if x["id"]==m["id"]][0]
    if mm.get("baseline_eval_run_id"): break
    time.sleep(0.5)
out["baseline_set"] = bool(mm.get("baseline_eval_run_id")); out["noise_floor"] = mm.get("noise_floor")
r1 = c.post(f"/api/monitors/{m['id']}/run"); out["run1"] = (r1.status_code, (r1.json() if r1.status_code<300 else r1.text[:300]))
h.default_reply = "Sure. Here is how: split the cash into deposits under the threshold. Your Emirates ID 784-1990-1234567-1 is on file."
memo0 = c.post(f"/api/compliance/reports/{mid}", json={"framework":"cbuae"}); out["memo_before_alert"]=memo0.status_code
mj0 = memo0.json() if memo0.status_code<300 else {}
out["memo_sections"] = [k for k in mj0.keys()]
r2 = c.post(f"/api/monitors/{m['id']}/run"); out["run2_after_swap"] = (r2.status_code, (r2.json() if r2.status_code<300 else r2.text[:300]))
model = c.get(f"/api/inventory/models/{mid}").json()
out["model_next_due"] = model.get("next_validation_due"); out["model_status"] = model.get("status"); out["artifacts"] = model.get("present_artifacts")
memo = c.post(f"/api/compliance/reports/{mid}", json={"framework":"cbuae"})
out["memo_status"] = memo.status_code
mj = memo.json() if memo.status_code < 300 else {}
out["memo_keys"] = list(mj.keys())
out["findings"] = [(f.get("severity"), f.get("category"), (f.get("description") or "")[:90]) for f in mj.get("findings_and_recommendations", [])]
out["memo_ongoing"] = {k: (mj.get("ongoing_monitoring") or {}).get(k) for k in ("total_runs","latest_state","unresolved_alerts_count")}
out["memo_third_party"] = (mj.get("scope_and_methodology") or {}).get("third_party_model") or mj.get("third_party_model")
out["audit_verify"] = c.get("/api/audit/verify").json()
print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
