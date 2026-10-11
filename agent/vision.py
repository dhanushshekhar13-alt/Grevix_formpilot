"""Local vision-language model client (Ollama). Screenshots never leave this machine."""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import httpx

from .actions import ACTION_TYPES, Action, ActionError
from .config import ROOT, Settings

log = logging.getLogger("formpilot.vision")


class VisionError(Exception):
    pass


class VisionUnavailable(VisionError):
    """Ollama is not reachable."""


class ModelMissing(VisionError):
    """Ollama is up but the configured model isn't pulled."""


class PlanFailed(VisionError):
    """Model replied, but never with a valid action (after bounded retries)."""

    def __init__(self, msg: str, attempts: int = 0):
        super().__init__(msg)
        self.attempts = attempts


@dataclass
class PlanContext:
    task: str
    profile: dict
    hints: List[str] = field(default_factory=list)
    history: List[str] = field(default_factory=list)   # short outcome lines of recent actions
    warnings: List[str] = field(default_factory=list)  # loop / no-effect warnings
    human_inputs: List[str] = field(default_factory=list)  # replies from the human (prompt only, never logged)
    width: int = 1280
    height: int = 800
    step: int = 0
    commitments: List[str] = field(default_factory=list)  # irreversible actions already attempted (from the ledger)


# Flat schema: small VLMs follow this far better than nested unions. Strict validation happens in actions.py.
# Order matters: the grammar emits keys in this order and the model skips optional ones, so x/y are required and come
# right after action (otherwise clicks and types arrive without coordinates). Non-spatial actions ignore them.
WIRE_SCHEMA = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "action": {"type": "string", "enum": ACTION_TYPES},
        "x": {"type": "number"}, "y": {"type": "number"},
        "target": {"type": "string"},
        "text": {"type": "string"}, "option": {"type": "string"}, "key": {"type": "string"},
        "dy": {"type": "integer"}, "seconds": {"type": "number"},
        "kind": {"type": "string", "enum": ["otp", "captcha", "info", "confirm"]},
        "question": {"type": "string"},
        "irreversible": {"type": "boolean"}, "sensitive": {"type": "boolean"}, "clear_first": {"type": "boolean"},
        "summary": {"type": "string"}, "reference": {"type": "string"}, "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["rationale", "action", "x", "y"],
}


def system_prompt(s: Settings) -> str:
    text = (ROOT / "prompts" / "system.md").read_text()
    if s.coord_mode == "norm1000":
        rules = ("Give x and y on a NORMALISED 0-1000 grid: (0,0) is the top-left of the screenshot, (1000,1000) the bottom-right, "
                 "regardless of its pixel size. Aim for the centre of the target.")
    else:
        rules = "Give x and y in screenshot pixels, (0,0) = top-left. Aim for the centre of the target."
    return text.replace("{W}", str(s.width)).replace("{H}", str(s.height)).replace("{COORD_RULES}", rules)


def user_prompt(ctx: PlanContext) -> str:
    parts = [f"TASK: {ctx.task}"]
    if ctx.hints:
        parts.append("HINTS:\n" + "\n".join(f"- {h}" for h in ctx.hints))
    parts.append("USER PROFILE (the only personal data you may use):\n" + json.dumps(ctx.profile, ensure_ascii=False))
    if ctx.human_inputs:
        parts.append("HUMAN REPLIES SO FAR:\n" + "\n".join(f"- {h}" for h in ctx.human_inputs))
    if ctx.commitments:
        parts.append("IRREVERSIBLE ACTIONS ALREADY ATTEMPTED (these change the real world):\n" + "\n".join(f"- {c}" for c in ctx.commitments))
    if ctx.history:
        parts.append("RECENT ACTIONS (oldest first):\n" + "\n".join(ctx.history[-6:]))
    if ctx.warnings:
        parts.append("WARNINGS:\n" + "\n".join(f"- {w}" for w in ctx.warnings))
    parts.append(f"Step {ctx.step}. Look at the screenshot and reply with the single next action as JSON.")
    return "\n\n".join(parts)


def extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        v = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise ActionError("reply was not JSON")
        try:
            v = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise ActionError(f"malformed JSON: {e.msg}")
    if not isinstance(v, dict):
        raise ActionError("reply JSON was not an object")
    return v


class OllamaVision:
    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None):
        self.s = settings
        self._client = client
        self._think = False  # sent as "think": false; dropped automatically if the model rejects it

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.s.request_timeout)
        return self._client

    async def health(self) -> dict:
        try:
            r = await self._http().get(f"{self.s.ollama_url}/api/tags", timeout=4)
            r.raise_for_status()
        except Exception:
            return {"server": False, "model_ready": False, "model": self.s.model,
                    "message": "Ollama is not running. Install it (brew install ollama) and run `ollama serve`."}
        names = [m.get("name", "") for m in r.json().get("models", [])]
        want = self.s.model if ":" in self.s.model else self.s.model + ":latest"
        ready = want in names
        return {"server": True, "model_ready": ready, "model": self.s.model, "installed": names,
                "message": "ready" if ready else f"Model not installed. Run: ollama pull {self.s.model}"}

    async def _chat(self, messages: list) -> str:
        payload = {
            "model": self.s.model, "messages": messages, "stream": False, "format": WIRE_SCHEMA,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_ctx": self.s.num_ctx, "num_predict": 500},
        }
        if self._think is not None:
            payload["think"] = self._think
        try:
            r = await self._http().post(f"{self.s.ollama_url}/api/chat", json=payload, timeout=self.s.request_timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise VisionUnavailable(f"cannot reach Ollama at {self.s.ollama_url}") from e
        except httpx.TimeoutException as e:
            raise VisionError(f"model timed out after {self.s.request_timeout}s (first call loads the model)") from e
        if r.status_code == 404:
            raise ModelMissing(f"model {self.s.model!r} not found. Run: ollama pull {self.s.model}")
        if r.status_code == 400 and "think" in r.text.lower() and "think" in payload:  # model lacks thinking support
            self._think = None  # model has no thinking mode: retry without the field
            payload.pop("think")
            r = await self._http().post(f"{self.s.ollama_url}/api/chat", json=payload, timeout=self.s.request_timeout)
        if r.status_code >= 400:
            raise VisionError(f"Ollama error {r.status_code}: {r.text[:200]}")
        return (r.json().get("message") or {}).get("content", "")

    async def plan(self, png: bytes, ctx: PlanContext, validate: Callable[[dict], Action]) -> Tuple[Action, dict]:
        """Returns (validated action, meta). Bounded: at most model_retries+1 model calls."""
        messages = [
            {"role": "system", "content": system_prompt(self.s)},
            {"role": "user", "content": user_prompt(ctx), "images": [base64.b64encode(png).decode()]},
        ]
        meta = {"latency": 0.0, "attempts": 0, "rejected": 0}
        last_err = ""
        for attempt in range(self.s.model_retries + 1):
            meta["attempts"] = attempt + 1
            t0 = time.time()
            try:
                content = await self._chat(messages)
            except (VisionUnavailable, ModelMissing):
                raise
            except VisionError as e:
                meta["latency"] += time.time() - t0
                last_err = str(e)
                log.warning("model call failed: %s", last_err)
                if attempt >= self.s.model_retries:
                    break
                continue
            meta["latency"] += time.time() - t0
            try:
                return validate(extract_json(content)), meta
            except ActionError as e:
                meta["rejected"] += 1
                last_err = str(e)
                log.warning("rejected model output: %s", last_err)
                messages.append({"role": "assistant", "content": content[:600]})
                messages.append({"role": "user", "content": f"That reply was invalid ({last_err}). Reply with ONE valid JSON action only."})
        raise PlanFailed(f"no valid action after {meta['attempts']} attempts: {last_err}", meta["attempts"])
