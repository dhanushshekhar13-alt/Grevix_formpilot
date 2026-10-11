"""Challenge demo: failures injected at the worst moment, recovery with no duplicated payment.
  python scripts/demo_challenge.py                    # deterministic scripted driver (proves the safety machinery)
  python scripts/demo_challenge.py --planner ollama   # same scenarios driven by the local VLM
Ground truth for "no duplicate" is the portal's own charge ledger, not the agent's opinion of itself.
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent.agent import load_checkpoint, run_task  # noqa: E402
from agent.config import Settings, load_task  # noqa: E402
from agent.vision import OllamaVision  # noqa: E402
from tests.helpers import ScriptedPlanner, human_oracle, start_portal  # noqa: E402

SCENARIOS = [
    ("happy path", "", ""),
    ("session expires right after payment", "expire_after_pay", ""),
    ("agent crashes right after Pay click, then resumes", "", "crash_after_commit"),
    ("crash + expired session, then resumes", "expire_after_pay", "crash_after_commit"),
]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--planner", choices=["scripted", "ollama"], default="scripted")
    ap.add_argument("--only", default="", help="run only scenarios whose name contains this text")
    a = ap.parse_args()
    port = 5067
    __import__("logging").getLogger("werkzeug").setLevel("ERROR")
    start_portal(port)
    import portal.app as portal
    out = ROOT / "runs" / "challenge"
    rows = []
    for name, portal_fault, agent_fault in [x for x in SCENARIOS if a.only in x[0]]:
        portal.LEDGER.clear()
        portal.FAULT["mode"] = portal_fault
        s = Settings.from_env()
        s.record_video, s.runs_dir, s.fault = False, out, agent_fault
        if a.planner == "scripted":
            s.coord_mode = "pixels"
        spec = load_task("sbee_exam")
        spec.url = f"http://127.0.0.1:{port}/"

        async def sink(e):
            if e["type"] in ("outcome", "reconciled", "resumed", "finished") and e.get("outcome") not in ("changed", "verified", "observed"):
                print("   ", {k: v for k, v in e.items() if k not in ("t", "image")}, flush=True)

        def human():
            base = human_oracle()

            async def ask(kind, q):
                if kind == "reconcile":  # a real person would check their bank: answer from the portal's own charge ledger
                    return "paid" if portal.LEDGER else "not paid"
                return await base(kind, q)
            return ask

        def planner():
            return ScriptedPlanner(dup_attempts=1) if a.planner == "scripted" else OllamaVision(s)

        print(f"\n== {name}", flush=True)
        t0 = time.time()
        res = await run_task(spec, sink, human(), s, planner=planner())
        runs = [res["run_id"]]
        if res["status"] == "interrupted":
            s.fault = ""
            print("   -- resuming from checkpoint", flush=True)
            res = await run_task(spec, sink, human(), s, planner=planner(), resume=load_checkpoint(res["run_dir"]))
            runs.append(res["run_id"])
        m = res["metrics"]
        pay = [x for x in res["ledger"] if x["name"] == "fee_payment"]
        rows.append({"scenario": name, "status": res["status"], "portal_charges": len(portal.LEDGER),
                     "duplicates_blocked": m["duplicate_commits_blocked"], "payment_history": " > ".join(h[0] for h in pay[-1]["history"]) if pay else "-",
                     "reference": res.get("reference"), "runs": ", ".join(runs), "s": round(time.time() - t0, 1)})
    print("\n| scenario | status | portal charges | duplicates blocked | payment ledger | runs |\n|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['scenario']} | {r['status']} | {r['portal_charges']} | {r['duplicates_blocked']} | {r['payment_history']} | {r['runs']} |")
    ok = all(r["status"] == "success" and r["portal_charges"] == 1 for r in rows)
    print("\nALL SCENARIOS: success with exactly one charge" if ok else "\nSOME SCENARIOS FAILED")
    sys.exit(0 if ok else 1)

asyncio.run(main())
