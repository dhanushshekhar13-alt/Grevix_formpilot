"""Repeatable benchmark on the mock portal. Same task, same portal, per model.
  python scripts/benchmark.py --models qwen3-vl:4b-instruct,qwen3-vl:8b-instruct --trials 3
  python scripts/benchmark.py --planner scripted      # plumbing check only (no VLM, not a model result)
Human steps are answered by an oracle (OTP from the portal's SMS inbox, fixed bench CAPTCHA) and counted as interventions.
"""
import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent.agent import run_task  # noqa: E402
from agent.config import Settings, load_task  # noqa: E402
from agent.vision import OllamaVision  # noqa: E402
from tests.helpers import ScriptedPlanner, human_oracle, start_portal  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=Settings.from_env().model)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--task", default="sbee_exam")
    ap.add_argument("--planner", choices=["ollama", "scripted"], default="ollama")
    a = ap.parse_args()
    port = 5066
    start_portal(port)
    out_dir = ROOT / "runs" / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for model in ([a.models.split(",")[0]] if a.planner == "scripted" else a.models.split(",")):
        for t in range(a.trials):
            s = Settings.from_env()
            s.model, s.record_video = model, False
            if a.planner == "scripted": s.coord_mode = "pixels"
            if "qwen3-vl" not in model.lower() and not __import__("os").getenv("FORMPILOT_COORD_MODE"):
                s.coord_mode = "pixels"
            spec = load_task(a.task)
            spec.url = f"http://127.0.0.1:{port}/"
            async def sink(e): pass
            planner = ScriptedPlanner() if a.planner == "scripted" else OllamaVision(s)
            t0 = time.time()
            res = await run_task(spec, sink, human_oracle(), s, planner=planner)
            m = res.get("metrics", {})
            ok = res["status"] == "success" and bool(re.fullmatch(r"SBEE26\d{8}", res.get("reference") or ""))
            rows.append({"model": "scripted-baseline" if a.planner == "scripted" else model, "trial": t + 1, "success": ok,
                         "status": res["status"], "actions": m.get("actions_executed"), "retries": m.get("retries"),
                         "rejected": m.get("rejected_actions"), "human": m.get("human_interventions"),
                         "avg_latency_s": m.get("avg_latency_s"), "total_s": round(time.time() - t0, 1),
                         "receipt": res.get("reference"), "reason": "" if ok else res.get("summary", "")[:80]})
            print(rows[-1], flush=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out_dir / f"{stamp}.json").write_text(json.dumps(rows, indent=2))
    md = ["| model | success | trials | avg actions | avg retries | avg rejected | avg human | avg step latency s | avg total s |", "|---|---|---|---|---|---|---|---|---|"]
    for mdl in dict.fromkeys(r["model"] for r in rows):
        rs = [r for r in rows if r["model"] == mdl]
        avg = lambda k: round(sum((r[k] or 0) for r in rs) / len(rs), 1)
        md.append(f"| {mdl} | {sum(r['success'] for r in rs)}/{len(rs)} | {len(rs)} | {avg('actions')} | {avg('retries')} | {avg('rejected')} | {avg('human')} | {avg('avg_latency_s')} | {avg('total_s')} |")
    (out_dir / f"{stamp}.md").write_text("\n".join(md))
    print("\n".join(md))

asyncio.run(main())
