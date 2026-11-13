#!/usr/bin/env python3
"""Live acceptance run: a real local model through the full ForkMark pipeline.

Starts llama-server on a GGUF model, registers it as a bank-self-hosted
connection, runs the safety and stress suites through the product API, and
writes everything a reviewer needs to check the result into one folder:

    env.json            machine, llama.cpp build, model file + SHA-256, git commit, timings
    llama-server.log    raw server log
    summaries.json      per-suite summary exactly as the API returns it
    needs_review.json   every case the detectors sent to human review
    all_results.json    every case: verdict, detectors, reply excerpt (PII-masked)
    gateway_log.json    every model call the gateway made
    memo.json / memo.docx / memo.html   the validation memo
    audit_verify.json   audit-log hash-chain check
    forkmark.db         the evidence database itself

Nothing here is generated or edited by hand: if the run fails, the folder says so.

Usage (from the repo root, inside the venv):
    python scripts/live_acceptance.py --label jais-2-8b \\
        --hf-repo inception42/Jais-2-8B-Chat-GGUF --hf-file Q4_K_M.gguf \\
        --vendor Inception --family jais --llama-server ~/.forkmark/llama.cpp/llama-server
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def machine_info() -> dict:
    info = {"platform": platform.platform(), "machine": platform.machine(), "python": sys.version.split()[0]}
    if sys.platform == "darwin":
        for key, cmd in (("chip", ["sysctl", "-n", "machdep.cpu.brand_string"]),
                         ("memory_bytes", ["sysctl", "-n", "hw.memsize"]),
                         ("model", ["sysctl", "-n", "hw.model"])):
            try:
                info[key] = subprocess.check_output(cmd, text=True).strip()
            except Exception:
                pass
    return info


def git_commit() -> str:
    try:
        sha = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"], text=True).strip()
        return sha + (" (local changes)" if dirty else "")
    except Exception:
        return "unknown"


def download_model(repo: str, filename: str) -> Path:
    from huggingface_hub import hf_hub_download
    log(f"Fetching {repo}/{filename} (cached after the first run)...")
    return Path(hf_hub_download(repo_id=repo, filename=filename))


def wait_for_server(port: int, proc: subprocess.Popen, timeout_s: int = 600) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited with code {proc.returncode}; see llama-server.log")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError("llama-server did not become healthy in time; see llama-server.log")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True, help="Short name for this model, used for folder and model id")
    ap.add_argument("--hf-repo", required=True)
    ap.add_argument("--hf-file", required=True)
    ap.add_argument("--vendor", required=True)
    ap.add_argument("--family", required=True)
    ap.add_argument("--licence", default="See model card")
    ap.add_argument("--llama-server", required=True)
    ap.add_argument("--port", type=int, default=8089)
    ap.add_argument("--ctx", type=int, default=4096)
    ap.add_argument("--risk-tier", default="HIGH", choices=["LOW", "MEDIUM", "HIGH", "CRITICAL"])
    ap.add_argument("--suites", default="safety,stress")
    ap.add_argument("--limit", type=int, default=0, help="Use only the first N corpus cases (smoke test)")
    ap.add_argument("--out", default="", help="Output folder (default reports/acceptance/live/<date>/<label>)")
    ap.add_argument("--max-hours", type=float, default=8.0)
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y-%m-%d")
    out = Path(args.out) if args.out else REPO / "reports" / "acceptance" / "live" / stamp / args.label
    out.mkdir(parents=True, exist_ok=True)
    env: dict = {"label": args.label, "started_at": now(), "status": "running", "machine": machine_info(),
                 "forkmark_commit": git_commit(), "args": vars(args)}

    def save_env() -> None:
        (out / "env.json").write_text(json.dumps(env, indent=2, ensure_ascii=False))

    save_env()
    server = None
    try:
        model_path = download_model(args.hf_repo, args.hf_file)
        env["model"] = {"hf_repo": args.hf_repo, "hf_file": args.hf_file, "path": str(model_path),
                        "bytes": model_path.stat().st_size}
        log("Hashing model file...")
        env["model"]["sha256"] = sha256(model_path)
        try:
            env["llama_server_version"] = subprocess.run([args.llama_server, "--version"], capture_output=True,
                                                         text=True, timeout=30).stderr.strip().splitlines()[-2:]
        except Exception as exc:
            env["llama_server_version"] = f"unknown ({exc})"
        save_env()

        log(f"Starting llama-server on 127.0.0.1:{args.port} ...")
        server_log = open(out / "llama-server.log", "w")
        server = subprocess.Popen(
            [args.llama_server, "-m", str(model_path), "--host", "127.0.0.1", "--port", str(args.port),
             "-c", str(args.ctx), "-ngl", "99", "--parallel", "1", "--jinja", "--alias", args.label],
            stdout=server_log, stderr=subprocess.STDOUT, start_new_session=True)
        wait_for_server(args.port, server)
        log("llama-server is up.")

        # Fresh evidence database for this run; nothing shared with any other run.
        os.environ["FM_DB_PATH"] = str(out / "forkmark.db")
        os.environ.setdefault("FM_REQUIRE_UI_AUTH", "true")
        sys.path.insert(0, str(REPO))
        from fastapi.testclient import TestClient
        import backend.deps as deps
        import backend.main as app_main

        with TestClient(app_main.app, raise_server_exceptions=False) as c:
            _, key = deps.db.create_api_key("live-acceptance", role="admin")
            c.headers["X-API-Key"] = key

            # Smoke call straight to the model, so a broken template shows up immediately.
            smoke = json.loads(urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{args.port}/v1/chat/completions",
                data=json.dumps({"model": args.label, "max_tokens": 60, "temperature": 0,
                                 "messages": [{"role": "user", "content": "ما هي عاصمة الإمارات؟ أجب بجملة واحدة."}]}).encode(),
                headers={"Content-Type": "application/json"}), timeout=300).read())
            env["smoke_reply"] = smoke["choices"][0]["message"]["content"][:300]
            log(f"Smoke reply: {env['smoke_reply'][:120]!r}")
            save_env()

            p = c.post("/api/providers", json={
                "name": f"{args.label}-local", "provider_type": "openai_compatible",
                "base_url": f"http://127.0.0.1:{args.port}/v1", "api_key": "local",
                "deployment_kind": "openai_compatible", "model_family": args.family, "default_model": args.label,
                "inference_region": "me-central-1", "hosting": "bank_self_hosted",
                "vendor_name": args.vendor, "licence": args.licence})
            p.raise_for_status()
            conn_id = p.json()["id"]

            mid = f"live-{args.label}"
            r = c.post("/api/inventory/models", json={
                "model_id": mid, "display_name": f"{args.label} (live acceptance)", "provider": args.vendor,
                "version": args.hf_file, "use_case": "Retail banking customer assistant (acceptance test)",
                "risk_tier": args.risk_tier, "regulatory_frameworks": ["cbuae"],
                "deployed_at": now(), "owner_team": "Model Risk", "connector_id": conn_id})
            r.raise_for_status()

            inst = c.post("/api/safety/corpus/install")
            inst.raise_for_status()
            ts_id = inst.json()["test_set_id"]
            if args.limit:
                # Smoke test: copy the first N corpus cases (all fields) into a separate test set.
                cases = c.get(f"/api/test-sets/{ts_id}").json()["cases"][: args.limit]
                small = c.post("/api/test-sets", json={"name": f"smoke-{args.limit}"})
                small.raise_for_status()
                ts_id = small.json()["id"]
                c.post(f"/api/test-sets/{ts_id}/cases/bulk", json={"cases": [
                    {k: cs.get(k) for k in ("label", "input_data", "tags", "expected_output")} for cs in cases
                ]}).raise_for_status()
                env["limited_to_cases"] = len(cases)

            suites = [s.strip() for s in args.suites.split(",") if s.strip()]
            t0 = time.time()
            v = c.post(f"/api/inventory/models/{mid}/validation-runs", json={
                "test_set_id": ts_id, "champion": {"connector_id": conn_id, "model": args.label}, "suites": suites})
            if v.status_code >= 300:
                raise RuntimeError(f"validation-runs returned {v.status_code}: {v.text[:500]}")
            ids = v.json()["eval_run_ids"]
            env["eval_run_ids"] = dict(zip(suites, ids))
            save_env()
            log(f"Validation started: {env['eval_run_ids']}")

            last = -1
            while True:
                sts = [c.get(f"/api/eval-runs/{i}").json().get("status") for i in ids]
                done = sum(len(deps.db.list_safety_results(eval_run_id=i, limit=100000)) for i in ids)
                if done != last and (done - last >= 10 or all(s in ("completed", "failed") for s in sts)):
                    el = time.time() - t0
                    log(f"{done} cases scored, {el/60:.1f} min elapsed, status {sts}")
                    last = done
                if all(s in ("completed", "failed") for s in sts):
                    break
                if time.time() - t0 > args.max_hours * 3600:
                    raise TimeoutError(f"Run exceeded {args.max_hours} h")
                time.sleep(15)
            env["suite_seconds"] = round(time.time() - t0, 1)
            env["run_status"] = dict(zip(suites, sts))

            summaries, allres, pending = {}, {}, []
            for suite, i in zip(suites, ids):
                summaries[suite] = c.get(f"/api/safety/results/{i}").json()
                items = c.get(f"/api/safety/results/{i}/items?limit=5000").json()
                allres[suite] = items
                pending += [dict(it, suite=suite) for it in items if it.get("verdict") == "needs_review"]
            (out / "summaries.json").write_text(json.dumps(summaries, indent=2, ensure_ascii=False))
            (out / "all_results.json").write_text(json.dumps(allres, indent=2, ensure_ascii=False))
            (out / "needs_review.json").write_text(json.dumps(pending, indent=2, ensure_ascii=False))
            gl, off = [], 0
            while True:
                page = c.get(f"/api/gateway/log?limit=1000&offset={off}").json()
                gl += page
                if len(page) < 1000:
                    break
                off += 1000
            (out / "gateway_log.json").write_text(json.dumps(gl, indent=2, ensure_ascii=False))
            env["gateway_calls"] = len(gl)

            body = {"framework": "cbuae"}
            memo = c.post(f"/api/compliance/reports/{mid}", json=body)
            (out / "memo.json").write_text(json.dumps(memo.json(), indent=2, ensure_ascii=False))
            d = c.post(f"/api/compliance/reports/{mid}/docx", json=body)
            if d.status_code == 200:
                (out / "memo.docx").write_bytes(d.content)
            h = c.post(f"/api/compliance/reports/{mid}/html", json=body)
            if h.status_code == 200:
                (out / "memo.html").write_text(h.text)
            (out / "audit_verify.json").write_text(json.dumps(c.get("/api/audit/verify").json(), indent=2))

            env["result"] = {s: {k: summaries[s].get(k) for k in
                                 ("verdict", "total_cases", "pooled_asr", "over_refusal_rate", "canary_leaks",
                                  "pii_leaks", "stress_flip_rate", "cases_needing_review")} for s in summaries}
            env["memo_status"] = [memo.status_code, d.status_code, h.status_code]
        env["status"] = "completed"
        log(f"Done. Results: {json.dumps(env['result'], ensure_ascii=False)}")
        return 0
    except BaseException as exc:  # record the failure honestly, including Ctrl-C
        env["status"] = "failed"
        env["error"] = f"{type(exc).__name__}: {exc}"
        log(f"FAILED: {env['error']}")
        return 1
    finally:
        env["finished_at"] = now()
        save_env()
        if server and server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(timeout=30)
            except Exception:
                os.killpg(server.pid, signal.SIGKILL)


if __name__ == "__main__":
    sys.exit(main())
