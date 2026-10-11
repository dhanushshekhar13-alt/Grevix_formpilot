import json
import re
from pathlib import Path

import pytest

from agent.agent import run_task
from agent.vision import ModelMissing, OllamaVision, VisionUnavailable
from tests.helpers import ScriptedPlanner, SeqPlanner, human_oracle


async def test_full_registration_reaches_verified_receipt_with_human_checkpoints(settings, spec, sink):
    human = human_oracle()
    res = await run_task(spec, sink, human, settings, planner=ScriptedPlanner())
    assert res["status"] == "success", res
    assert res["verified"] is True and re.fullmatch(r"SBEE26\d{8}", res["reference"])
    assert human.log == ["otp", "captcha"]
    assert res["metrics"]["human_interventions"] == 2 and res["metrics"]["receipt_observed"]
    assert len(sink.of("need_human")) == 2 and len(sink.of("human_answer")) == 2
    # evidence on disk, and secrets kept out of it
    run_dir = Path(res["run_dir"])
    assert (run_dir / "summary.json").exists() and (run_dir / "frames" / "001_before.jpg").exists()
    blob = (run_dir / "trace.jsonl").read_text() + (run_dir / "summary.json").read_text() + json.dumps(sink.events, default=str).replace(
        sink.of("need_human")[0]["image"], "").replace(sink.of("need_human")[1]["image"], "")
    for secret in ("9876543210", "123412341234", "ananya@okaxis", "K7M2Q"):
        assert secret not in re.sub(r'"image": "[^"]*"', "", blob), secret


async def test_max_steps_terminates_as_incomplete(settings, spec, sink):
    settings.max_steps = 3
    res = await run_task(spec, sink, human_oracle(), settings, planner=SeqPlanner([{"action": "screenshot"}]))
    assert res["status"] == "incomplete" and res["metrics"]["steps"] == 3
    assert "step limit" in sink.of("finished")[0]["summary"]
    assert (Path(res["run_dir"]) / "trace.jsonl").exists()


async def test_repeated_noeffect_actions_escalate_then_stop(settings, spec, sink):
    human = human_oracle()
    # (3,3) is inside the page margin: clicking it does nothing, forever
    res = await run_task(spec, sink, human, settings, planner=SeqPlanner([{"action": "click", "x": 3, "y": 3}]))
    assert res["status"] == "failed" and "stuck" in res["summary"]
    assert human.log == ["info", "info"]  # bounded: 2 escalations, then abort
    assert res["metrics"]["no_effect_actions"] >= 8


async def test_done_without_receipt_is_rejected(settings, spec, sink):
    res = await run_task(spec, sink, human_oracle(), settings,
                         planner=SeqPlanner([{"action": "done", "summary": "all good", "reference": "FAKE123456"}]))
    assert res["status"] == "failed" and "without a visible receipt" in res["summary"]
    assert res["reference"] is None


async def test_irreversible_click_needs_confirmation_and_denial_stops(settings, spec, sink):
    spec.mock = False

    async def deny(kind, q):
        assert kind == "confirm"
        return "no"
    res = await run_task(spec, sink, deny, settings, planner=SeqPlanner([{"action": "click", "x": 100, "y": 100, "irreversible": True, "target": "Pay Now"}]))
    assert res["status"] == "stopped" and "declined" in res["summary"]
    assert res["metrics"]["actions_executed"] == 0


async def test_emergency_stop_during_human_wait(settings, spec, sink):
    import asyncio
    stop = asyncio.Event()

    async def hang(kind, q):
        stop.set()
        await asyncio.sleep(30)
    res = await asyncio.wait_for(run_task(spec, sink, hang, settings, stop=stop,
                                          planner=SeqPlanner([{"action": "ask_human", "kind": "otp", "question": "otp?"}])), 20)
    assert res["status"] == "stopped"


async def test_invalid_model_output_repeatedly_ends_with_diagnostic(settings, spec, sink):
    from agent.vision import PlanFailed

    class Bad:
        async def plan(self, *a):
            raise PlanFailed("no valid action after 3 attempts: unknown action 'dance'", 3)
    res = await run_task(spec, sink, human_oracle(), settings, planner=Bad())
    assert res["status"] == "failed" and "invalid output 3 steps in a row" in res["summary"]


@pytest.mark.parametrize("exc", [VisionUnavailable("cannot reach Ollama"), ModelMissing("run: ollama pull x")])
async def test_model_unavailable_fails_cleanly_before_browser(settings, spec, sink, exc):
    class Down:
        async def health(self):
            return {"model_ready": False, "message": str(exc)}
    res = await run_task(spec, sink, human_oracle(), settings, planner=Down())
    assert res["status"] == "failed" and str(exc) in res["summary"]


def test_default_pipeline_does_not_use_anthropic():
    import subprocess, sys
    code = "import sys, agent.agent, agent.vision, agent.actions, server; print('anthropic' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=Path(__file__).parent.parent)
    assert out.stdout.strip() == "False", out.stderr
    root = Path(__file__).parent.parent
    for f in list((root / "agent").glob("*.py")) + [root / "server.py", root / "requirements.txt", root / ".env.example"]:
        assert "anthropic" not in f.read_text().lower(), f
