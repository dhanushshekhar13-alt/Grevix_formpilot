"""Runtime settings (env) and task specs (tasks/*.json). Nothing here is a secret."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent


def _b(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    ollama_url: str = "http://localhost:11434"
    model: str = "qwen3-vl:4b-instruct"
    max_steps: int = 60
    action_timeout: float = 10.0
    request_timeout: float = 120.0
    model_retries: int = 2
    max_bad_steps: int = 3
    human_timeout: float = 600.0
    num_ctx: int = 8192
    coord_mode: str = "norm1000"  # "norm1000" (Qwen3-VL grid 0..1000) or "pixels"
    headed: bool = False
    record_video: bool = True
    fault: str = ""  # "crash_after_commit": simulate the agent dying right after an irreversible click is dispatched
    width: int = 1280
    height: int = 800
    runs_dir: Path = field(default_factory=lambda: ROOT / "runs")

    @classmethod
    def from_env(cls) -> "Settings":
        model = os.getenv("FORMPILOT_MODEL", "qwen3-vl:4b-instruct")
        default_mode = "norm1000" if "qwen3-vl" in model.lower() else "pixels"
        return cls(
            ollama_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/"),
            model=model,
            max_steps=int(os.getenv("FORMPILOT_MAX_STEPS", "60")),
            action_timeout=float(os.getenv("FORMPILOT_ACTION_TIMEOUT", "10")),
            request_timeout=float(os.getenv("FORMPILOT_REQUEST_TIMEOUT", "120")),
            model_retries=int(os.getenv("FORMPILOT_MODEL_RETRIES", "2")),
            human_timeout=float(os.getenv("FORMPILOT_HUMAN_TIMEOUT", "600")),
            num_ctx=int(os.getenv("FORMPILOT_NUM_CTX", "8192")),
            coord_mode=os.getenv("FORMPILOT_COORD_MODE", default_mode),
            headed=_b("FORMPILOT_HEADED", "0"),
            record_video=_b("FORMPILOT_RECORD_VIDEO", "1"),
            fault=os.getenv("FORMPILOT_FAULT", ""),
        )

    def public(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["runs_dir"] = str(self.runs_dir)
        return d


@dataclass
class TaskSpec:
    """Everything portal/task-specific lives here, so a new challenge = a new JSON file."""
    name: str
    title: str
    url: str
    task: str
    profile: dict = field(default_factory=dict)
    hints: List[str] = field(default_factory=list)          # extra guidance appended to the prompt
    success_regex: Optional[str] = None                      # 'done' is only accepted if page text matches
    sensitive_keys: List[str] = field(default_factory=list)  # profile keys redacted from logs
    mock: bool = False                                       # mock payment: skip confirm-before-irreversible
    max_steps: Optional[int] = None
    commits: List[dict] = field(default_factory=list)        # irreversible actions: see agent/consequence.py

    @classmethod
    def from_dict(cls, d: dict) -> "TaskSpec":
        allowed = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        allowed.setdefault("name", "custom")
        allowed.setdefault("title", allowed["name"])
        return cls(**allowed)


def list_tasks() -> List[dict]:
    out = []
    for p in sorted((ROOT / "tasks").glob("*.json")):
        try:
            d = json.loads(p.read_text())
            out.append({"name": p.stem, "title": d.get("title", p.stem), "url": d.get("url", "")})
        except Exception:
            continue
    return out


def load_task(name_or_path: str) -> TaskSpec:
    p = Path(name_or_path)
    if not p.suffix:
        p = ROOT / "tasks" / f"{name_or_path}.json"
    d = json.loads(p.read_text())
    d.setdefault("name", p.stem)
    return TaskSpec.from_dict(d)
