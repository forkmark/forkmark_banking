"""Egress check: external connection with egress off through every live path. Usage: python egress.py /path/to/forkmark_banking"""
import os, sys, tempfile, json
REPO=sys.argv[1]; os.environ["FM_DB_PATH"]=os.path.join(tempfile.mkdtemp(),"eg.db"); os.environ.pop("FM_ALLOW_EXTERNAL_LLM_EGRESS",None)
sys.path.insert(0,REPO)
from fastapi.testclient import TestClient
import backend.deps as deps, backend.main as main, core.llm_gateway as gw
from tests.fixtures.mock_llm import MockLLMHandler, create_mock_transport
t,h=create_mock_transport(MockLLMHandler()); gw.gateway.set_transport(t)
c=TestClient(main.app, raise_server_exceptions=False); _,k=deps.db.create_api_key("a",role="admin"); c.headers["X-API-Key"]=k
p=c.post("/api/providers",json={"name":"ext","provider_type":"openai","base_url":"https://api.openai.com/v1","api_key":"sk-x","deployment_kind":"openai_compatible"}).json()
out={}
out["provider_test"]=c.post(f"/api/providers/{p['id']}/test").json()
r=c.post("/api/runner",json={"workflow_name":"w","eval_run_name":"e","branch_a":{"name":"a","model_id":"gpt-4o","provider_id":p["id"],"steps":[{"name":"s","system_prompt":"","user_prompt_template":"{{input}}"}]},"branch_b":{"name":"b","model_id":"gpt-4o","provider_id":p["id"],"steps":[{"name":"s","system_prompt":"","user_prompt_template":"{{input}}"}]},"test_cases":[{"label":"x","input":"hi"}]})
out["runner"]=(r.status_code, str(r.json())[:200])
pg=c.post("/api/playground",json={"prompt":"hi","provider_id_a":p["id"],"provider_id_b":p["id"]})
out["playground"]=(pg.status_code, str(pg.json())[:160])
import asyncio
from core.evaluators import run_evaluators
os.environ["FM_OPENAI_API_KEY"]="sk-x"
out["judge"]=[e.detail[:100] for e in asyncio.run(run_evaluators("text",[{"name":"toxicity"}],{}))]
out["mock_requests"]=len(h.requests)
log=c.get("/api/gateway/log").json(); rows=log if isinstance(log,list) else log.get("items",[])
out["log_decisions"]=sorted({(x.get("purpose"),x.get("decision")) for x in rows})
print(json.dumps(out,indent=1,default=str))
