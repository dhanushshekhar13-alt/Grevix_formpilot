"""Dashboard backend. Run: uvicorn server:app --port 8000
The dashboard works even if the local model isn't ready; /health reports why.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

load_dotenv()
from agent.agent import latest_resumable, load_checkpoint, run_task  # noqa: E402
from agent.config import Settings, TaskSpec, list_tasks, load_task  # noqa: E402
from agent.vision import OllamaVision  # noqa: E402

app = FastAPI()
ROOT = Path(__file__).parent
history: list = []
state = {"running": False, "pending": None, "stop": None}


class RunReq(BaseModel):
    task_name: str = "sbee_exam"
    url: Optional[str] = None        # overrides for on-the-day tweaks without editing files
    task: Optional[str] = None
    profile: Optional[dict] = None
    hints: Optional[list] = None
    fault: str = ""                  # "", "expire_after_pay", "crash_after_commit", "both"
    resume_run_id: Optional[str] = None
    approve_commits: Optional[bool] = None  # ask before every irreversible action (None = task default)
    dry_run: bool = False                   # stop before the first irreversible action without sending it
    max_amount: Optional[float] = None      # spending limit in rupees for this run


async def arm_portal_fault(url: str, mode: str):
    u = urlparse(url)
    if u.hostname not in ("localhost", "127.0.0.1"):
        return  # only the local mock portal has a fault hook
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            await c.get(f"{u.scheme}://{u.netloc}/__fault", params={"mode": mode})
    except Exception:
        pass  # a real portal has no fault hook; nothing to arm


async def emit(e: dict):
    history.append(e)


async def ask_human(kind: str, question: str) -> str:
    state["pending"] = asyncio.get_event_loop().create_future()
    try:
        return await state["pending"]
    finally:
        state["pending"] = None


@app.get("/")
def index():
    return FileResponse(ROOT / "ui" / "index.html")


@app.get("/tasks")
def tasks():
    return list_tasks()


@app.get("/tasks/{name}")
def task_detail(name: str):
    t = load_task(name)
    return {"name": t.name, "title": t.title, "url": t.url, "task": t.task, "hints": t.hints, "profile": t.profile}


@app.get("/health")
async def health():
    s = Settings.from_env()
    h = await OllamaVision(s).health()
    return {**h, "coord_mode": s.coord_mode, "running": state["running"]}


@app.get("/resumable")
def resumable(task_name: Optional[str] = None):
    if state["running"]:
        return {"run": None}
    c = latest_resumable(Settings.from_env().runs_dir, task_name)
    if not c:
        return {"run": None}
    return {"run": {k: c.get(k) for k in ("run_id", "task", "status", "reason", "step", "url")},
            "ledger": [{k: a.get(k) for k in ("name", "label", "status", "step", "reference")} for a in c.get("ledger", [])]}


@app.post("/run")
async def run(req: RunReq):
    if state["running"]:
        return {"error": "already running"}
    s = Settings.from_env()
    resume = None
    if req.resume_run_id:
        if not re.fullmatch(r"[\w-]+", req.resume_run_id) or not (s.runs_dir / req.resume_run_id / "checkpoint.json").exists():
            return {"error": "no such run"}
        resume = load_checkpoint(s.runs_dir / req.resume_run_id)
        spec = load_task(resume["task"])
        spec.url, spec.task, spec.hints = resume["spec_url"], resume["task_text"], resume["hints"]
    else:
        spec = load_task(req.task_name)
        if req.url: spec.url = req.url
        if req.task: spec.task = req.task
        if req.profile: spec.profile = req.profile
        if req.hints is not None: spec.hints = req.hints
    await arm_portal_fault(spec.url, "expire_after_pay" if req.fault in ("expire_after_pay", "both") else "")
    if req.approve_commits is not None:
        s.approve_commits = req.approve_commits
    s.dry_run = req.dry_run
    if req.max_amount is not None and req.max_amount > 0:
        s.max_amount = req.max_amount
    if req.fault in ("crash_after_commit", "both"):
        s.fault = "crash_after_commit"
    history.clear()
    state["running"] = True
    state["stop"] = asyncio.Event()

    async def go():
        try:
            await run_task(spec, emit, ask_human, s, stop=state["stop"], resume=resume)
        except Exception as e:
            await emit({"type": "finished", "status": "failed", "summary": f"{type(e).__name__}: {e}", "reference": None})
        finally:
            state["running"] = False

    asyncio.create_task(go())
    return {"ok": True}


@app.post("/stop")
async def stop():
    if state["stop"]:
        state["stop"].set()
    return {"ok": True}


class Human(BaseModel):
    answer: str


@app.post("/human")
async def human(h: Human):
    fut = state["pending"]
    if fut and not fut.done():
        fut.set_result(h.answer)
        return {"ok": True}
    return {"error": "nothing pending"}


@app.get("/events")
async def events():
    async def gen():
        i = 0
        while True:
            if i > len(history):
                i = 0
            if i < len(history):
                yield f"data: {json.dumps(history[i])}\n\n"
                i += 1
            else:
                await asyncio.sleep(0.15)
    return StreamingResponse(gen(), media_type="text/event-stream")
