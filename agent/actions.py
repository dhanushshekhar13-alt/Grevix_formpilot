"""Strict action schema, validation, and the restricted executor.

The model can only produce these action types. Nothing is eval'd, no shell, no arbitrary JS.
"""
from __future__ import annotations

import asyncio
import difflib
import math
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator
from typing_extensions import Annotated

ALLOWED_KEYS = ["Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "Backspace", "PageDown", "PageUp"]
ACTION_TYPES = ["click", "type", "select", "press", "scroll", "wait", "screenshot", "ask_human", "done", "error"]


class ActionError(ValueError):
    """Model output could not be turned into a valid, safe action."""


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rationale: str = Field(default="", max_length=400)
    confidence: Optional[float] = Field(default=None, ge=0, le=1)  # uncalibrated model self-report


class Click(_Base):
    action: Literal["click"]
    x: float
    y: float
    target: str = Field(default="", max_length=120)
    irreversible: bool = False
    is_critical_payment: bool = False  # True for "pay now", "confirm payment" buttons

    def get_consequence(self) -> ActionConsequence:
        """Classify the consequence of this click."""
        if self.is_critical_payment:
            return ActionConsequence.IRREVERSIBLE
        if self.irreversible:
            return ActionConsequence.CRITICAL
        return ActionConsequence.REVERSIBLE


class Type(_Base):
    action: Literal["type"]
    text: str = Field(max_length=200)
    target: str = Field(default="", max_length=120)
    x: Optional[float] = None
    y: Optional[float] = None
    clear_first: bool = True
    sensitive: bool = False

    @field_validator("text")
    @classmethod
    def _no_control(cls, v: str) -> str:
        if any(ord(c) < 32 for c in v):
            raise ValueError("text contains control characters")
        return v

    @model_validator(mode="after")
    def _xy_pair(self):
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be given together")
        return self


class Select(_Base):
    action: Literal["select"]
    x: float
    y: float
    option: str = Field(max_length=120)
    target: str = Field(default="", max_length=120)


class Press(_Base):
    action: Literal["press"]
    key: Literal["Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "Backspace", "PageDown", "PageUp"]


class Scroll(_Base):
    action: Literal["scroll"]
    dy: int = Field(ge=-3000, le=3000)

    @field_validator("dy")
    @classmethod
    def _nonzero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("dy must be non-zero")
        return v


class Wait(_Base):
    action: Literal["wait"]
    seconds: float = Field(gt=0, le=10)


class Observe(_Base):
    action: Literal["screenshot"]


class AskHuman(_Base):
    action: Literal["ask_human"]
    kind: Literal["otp", "captcha", "info", "confirm"]
    question: str = Field(min_length=1, max_length=400)


class Done(_Base):
    action: Literal["done"]
    summary: str = Field(default="", max_length=400)
    reference: Optional[str] = Field(default=None, max_length=100)


class Fail(_Base):
    action: Literal["error"]
    reason: str = Field(min_length=1, max_length=400)


Action = Union[Click, Type, Select, Press, Scroll, Wait, Observe, AskHuman, Done, Fail]
_ADAPTER = TypeAdapter(Annotated[Action, Field(discriminator="action")])

_PIXEL_ACTIONS = (Click, Type, Select)


def _num(v: Any, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ActionError(f"{name} must be a number")
    if not math.isfinite(v):
        raise ActionError(f"{name} must be finite")
    return float(v)


def parse_action(raw: Any, width: int, height: int, coord_mode: str = "pixels") -> Action:
    """Validate raw model JSON -> typed Action with coordinates converted to screenshot pixels."""
    if not isinstance(raw, dict):
        raise ActionError("response must be a JSON object")
    kind = raw.get("action")
    if kind not in ACTION_TYPES:
        raise ActionError(f"unknown action type {kind!r}; allowed: {ACTION_TYPES}")
    cleaned = {k: v for k, v in raw.items() if v is not None}
    if isinstance(cleaned.get("rationale"), str):  # a long explanation is not worth a rejected step
        cleaned["rationale"] = cleaned["rationale"][:400]
    try:
        act = _ADAPTER.validate_python(cleaned)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:4])
        raise ActionError(f"invalid {kind} action: {errs}")

    if isinstance(act, _PIXEL_ACTIONS):
        updates: Dict[str, float] = {}
        for ax, limit in (("x", width), ("y", height)):
            v = getattr(act, ax)
            if v is None:
                continue
            v = _num(v, ax)
            if coord_mode == "norm1000":
                if not 0 <= v <= 1000:
                    raise ActionError(f"{ax}={v} outside 0..1000 grid")
                updates[ax] = round(v / 1000 * limit)
            else:
                if not 0 <= v <= limit:
                    raise ActionError(f"{ax}={v} outside screenshot (0..{limit})")
                updates[ax] = round(v)
        act = act.model_copy(update=updates)
    return act


def redacted(act: Action, secrets: Optional[list] = None) -> dict:
    """Action as a dict safe for logs: typed text of sensitive fields and known secrets masked."""
    d = act.model_dump(exclude_none=True)
    if isinstance(act, Type) and act.sensitive:
        d["text"] = "•••"
    elif "text" in d:
        d["text"] = mask(d["text"], secrets)
    for k in ("rationale", "question", "summary", "option"):
        if k in d:
            d[k] = mask(d[k], secrets)
    return d


def mask(s: str, secrets: Optional[list]) -> str:
    for sec in secrets or []:
        if sec and len(str(sec)) >= 4:
            s = s.replace(str(sec), "•••")
    return s


# Is there a text field that keystrokes would go into: at a point ([x, y]) or, with [null, null], the focused element.
TEXT_FIELD_JS = """([x, y]) => {
  const el = x === null ? document.activeElement : document.elementFromPoint(x, y);
  const c = el && el.closest('input,textarea,[contenteditable],label');
  const t = c && c.tagName === 'LABEL' ? c.control : c;
  if (!t) return null;
  const ok = t.isContentEditable || (['INPUT', 'TEXTAREA'].includes(t.tagName) && !t.readOnly && !t.disabled
    && !['checkbox', 'radio', 'submit', 'button', 'image', 'file', 'hidden'].includes((t.type || '').toLowerCase()));
  return ok ? (t.getAttribute('aria-label') || t.name || t.placeholder || t.id || t.tagName.toLowerCase()) : null;
}"""


@dataclass
class ExecResult:
    ok: bool
    detail: str = ""


class ActionExecutor:
    """Executes one validated action on a Playwright page. Pixel coordinates only."""

    def __init__(self, page, timeout: float = 10.0):
        self.page = page
        self.timeout = timeout

    async def execute(self, act: Action) -> ExecResult:
        if not isinstance(act, (Click, Type, Select, Press, Scroll, Wait, Observe)):
            raise ActionError(f"executor does not run {getattr(act, 'action', type(act).__name__)!r} actions")
        try:
            res = await asyncio.wait_for(self._run(act), timeout=self.timeout + (act.seconds if isinstance(act, Wait) else 0))
        except asyncio.TimeoutError:
            return ExecResult(False, f"action timed out after {self.timeout}s")
        except Exception as e:  # playwright errors become feedback, never crashes
            return ExecResult(False, f"{type(e).__name__}: {str(e)[:160]}")
        if res.ok:
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass
            await self.page.wait_for_timeout(350)
        return res

    async def _run(self, a: Action) -> ExecResult:
        p = self.page
        if isinstance(a, Click):
            await p.mouse.click(a.x, a.y)
            return ExecResult(True, f"clicked ({a.x:.0f},{a.y:.0f})")
        if isinstance(a, Type):
            kept = ""
            if a.x is not None:
                # x,y that miss every text field must not steal focus from a field the model has just clicked into
                if await p.evaluate(TEXT_FIELD_JS, [a.x, a.y]) or not await p.evaluate(TEXT_FIELD_JS, [None, None]):
                    await p.mouse.click(a.x, a.y)
                else:
                    kept = f" into the already focused field (x,y=({a.x:.0f},{a.y:.0f}) is not on a text field)"
            name = await p.evaluate(TEXT_FIELD_JS, [None, None])
            if not name:
                where = f" at ({a.x:.0f},{a.y:.0f})" if a.x is not None else " (no x,y given)"
                return ExecResult(False, f"no text field is focused{where}; nothing was typed. Give x,y of the input box")
            if a.clear_first:
                await p.keyboard.press("ControlOrMeta+a")
                await p.keyboard.press("Backspace")
            await p.keyboard.type(a.text, delay=20)
            return ExecResult(True, f"typed {len(a.text)} chars{kept and kept.replace('field', repr(name) + ' field', 1)}")
        if isinstance(a, Select):
            return await self._select(a)
        if isinstance(a, Press):
            await p.keyboard.press(a.key)
            return ExecResult(True, f"pressed {a.key}")
        if isinstance(a, Scroll):
            await p.mouse.wheel(0, a.dy)
            return ExecResult(True, f"scrolled {a.dy}")
        if isinstance(a, Wait):
            await asyncio.sleep(a.seconds)
            return ExecResult(True, f"waited {a.seconds}s")
        return ExecResult(True, "observed")

    async def _select(self, a: Select) -> ExecResult:
        h = await self.page.evaluate_handle("([x,y])=>document.elementFromPoint(x,y)", [a.x, a.y])
        el = h.as_element()
        tag = await el.evaluate("e=>e.tagName") if el else None
        kept = ""
        if tag != "SELECT":  # x,y missed, but the model has just clicked a dropdown: that one is meant
            h = await self.page.evaluate_handle("()=>document.activeElement && document.activeElement.tagName==='SELECT' ? document.activeElement : null")
            if h.as_element():
                el = h.as_element()
                kept = f" in the focused dropdown {await el.evaluate('e=>e.name||e.id')!r} (x,y=({a.x:.0f},{a.y:.0f}) is not on a dropdown)"
        if tag != "SELECT" and not kept:
            return ExecResult(False, f"no dropdown at ({a.x:.0f},{a.y:.0f}) (found {tag}); click exactly on the dropdown box")
        opts = await el.evaluate("e=>[...e.options].map(o=>o.text)")
        want = a.option.strip().lower()
        pick = next((o for o in opts if o.strip().lower() == want), None)
        pick = pick or next((o for o in opts if want and want in o.lower()), None)
        if not pick:
            close = difflib.get_close_matches(a.option, opts, n=1, cutoff=0.6)
            pick = close[0] if close else None
        if not pick:
            return ExecResult(False, f"no option matching {a.option!r}; available: {opts[:25]}")
        await el.select_option(label=pick)
        return ExecResult(True, f"selected {pick!r}{kept}")
