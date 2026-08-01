"""
ForkMark Demo Suite — Master Runner
=====================================

Seeds every demo discovered under ``examples/*/fixtures.json`` by calling the
ForkMark REST API. This stays in sync automatically: whatever banking demos are
present on disk get seeded — no hard-coded demo list to maintain.

Usage:
    python run_all_demos.py                 # seed all demos
    python run_all_demos.py --only credit_scoring
    python run_all_demos.py --reset         # clear all seeded demo data
    python run_all_demos.py --list          # list available demos

Prerequisites:
    1. ForkMark backend running:
         cd forkmark && python run.py
    2. pip install httpx
"""

import argparse
import os
import sys

try:
    import httpx
except ImportError:
    print("[error] httpx is required: pip install httpx")
    sys.exit(1)

BASE_URL = os.getenv("FM_URL", "http://localhost:7700")
API_KEY = os.getenv("FORKMARK_API_KEY", os.getenv("FM_BOOTSTRAP_TOKEN", ""))


def _headers():
    return {"X-API-Key": API_KEY} if API_KEY else {}


def list_demos(client):
    r = client.get(f"{BASE_URL}/api/demos", headers=_headers())
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser(description="Seed ForkMark banking demos via the REST API.")
    ap.add_argument("--only", help="Seed a single demo by name (e.g. credit_scoring).")
    ap.add_argument("--reset", action="store_true", help="Clear all seeded demo data and exit.")
    ap.add_argument("--list", action="store_true", help="List available demos and exit.")
    args = ap.parse_args()

    with httpx.Client(timeout=120) as client:
        demos = list_demos(client)
        names = [d["name"] for d in demos]

        if args.list:
            print(f"Available demos ({len(names)}):")
            for d in demos:
                print(f"  - {d['name']:16} {d.get('display_name','')}  ({d.get('cases',0)} cases)")
            return

        if args.reset:
            r = client.request("DELETE", f"{BASE_URL}/api/demos/reset",
                               json={"demos": []}, headers=_headers())
            r.raise_for_status()
            print(f"Reset: {', '.join(r.json().get('reset', [])) or 'nothing to reset'}")
            return

        body = {"demos": [args.only] if args.only else []}
        print(f"Seeding {'1 demo' if args.only else f'{len(names)} demos'} at {BASE_URL} ...")
        r = client.post(f"{BASE_URL}/api/demos/seed", json=body, headers=_headers())
        r.raise_for_status()
        result = r.json()

        total = 0
        for res in result.get("results", []):
            if "error" in res:
                print(f"  x {res['demo']}: {res['error']}")
            else:
                total += res.get("comparisons", 0)
                print(f"  + {res['demo']:16} {res.get('cases',0)} cases, {res.get('comparisons',0)} comparisons")
        print(f"\nSeeded {result.get('seeded',0)} demo(s), {result.get('errors',0)} error(s), {total} comparisons total.")


if __name__ == "__main__":
    main()
