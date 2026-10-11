"""Consequence policy: what an action can do to the world, and what may be done about it afterwards.

  observe  - no side effects (scroll, wait, look)           -> retried freely
  local    - edits the current page only (type, select, tick) -> safe to redo
  navigate - submits a step / follows a link                  -> redo only after re-reading the screen (never auto-retried)
  commit   - irreversible server-side effect (payment, final submit, delete, cancel, withdraw...)
             -> exactly once: logged BEFORE it is dispatched, verified against evidence after,
                never repeated while its outcome is unknown; a human resolves doubt (and approves first, if configured)
                A commit is: one the task declares, one the model flags, or any button whose own label says so
                (IRREVERSIBLE_LABELS). A task may cap the amount (max_amount): above it, nothing is sent.

The class is decided from what is actually under the pointer (DOM probe), not from the model's self-report alone.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .actions import Click, Press, Select, Type

OBSERVE, LOCAL, NAVIGATE, COMMIT = "observe", "local", "navigate", "commit"
RETRY_POLICY = {
    OBSERVE: "retry freely",
    LOCAL: "safe to redo",
    NAVIGATE: "redo only after re-reading the screen",
    COMMIT: "never repeat; verify; human resolves doubt",
}

# Buttons that cannot be taken back on any portal, whatever the task file says. Matched against the DOM label only.
IRREVERSIBLE_LABELS = (r"\b(pay|pay now|pay again|make payment|confirm payment|retry payment|final submit|final submission|"
                       r"submit application|submit (and|&) lock|confirm (and|&) submit|lock (choices|application|form)|freeze|"
                       r"delete|withdraw|surrender|cancel (application|registration|booking|admission)|place order|book now|"
                       r"send money|transfer)\b")
MONEY = re.compile(r"(?:Rs\.?|\u20b9|INR)\s*(\d[\d,]*(?:\.\d+)?)", re.I)


def money(text: str) -> List[float]:
    """Every rupee figure in a piece of page text."""
    out = []
    for g in MONEY.findall(text or ""):
        try:
            out.append(float(g.replace(",", "")))
        except ValueError:
            pass
    return out


INTENT, DISPATCHED, IN_DOUBT = "intent", "dispatched", "in_doubt"
CONFIRMED, REJECTED = "confirmed", "rejected"
HUMAN_CONFIRMED, HUMAN_VOIDED = "human_confirmed", "human_voided"
OPEN = (INTENT, DISPATCHED, IN_DOUBT)      # outcome unknown -> a repeat is blocked
DONE = (CONFIRMED, HUMAN_CONFIRMED)        # it happened     -> a repeat is blocked forever
# REJECTED / HUMAN_VOIDED: provably did not happen -> one new attempt is allowed

PROBE_JS = """([x, y]) => {
  const byKey = x === null;
  const el = byKey ? document.activeElement : document.elementFromPoint(x, y);
  const c = el && el.closest('button,a[href],input,select,textarea,label,[onclick],[role=button]');
  if (!c) return {actionable: false, tag: el ? el.tagName : null, label: '', submits: false};
  const tag = c.tagName, type = (c.getAttribute('type') || '').toLowerCase();
  const text = e => ((e.innerText || e.value || e.getAttribute('aria-label') || e.title || '') + '').trim().replace(/\\s+/g, ' ').slice(0, 80);
  const field = (tag === 'INPUT' && !['submit', 'button', 'image'].includes(type)) || tag === 'SELECT' || tag === 'TEXTAREA';
  const label = field ? (c.getAttribute('aria-label') || c.name || c.placeholder || type || tag.toLowerCase()) : text(c);
  const isSubmit = (tag === 'BUTTON' && !['button', 'reset'].includes(type) && !!c.form) || (tag === 'INPUT' && ['submit', 'image'].includes(type));
  if (byKey) {
    if (tag === 'INPUT' && c.form && !['checkbox', 'radio', 'button', 'submit', 'image'].includes(type)) {
      const b = c.form.querySelector('button:not([type=button]):not([type=reset]),input[type=submit]');
      return {actionable: true, tag, submits: true, label: b ? text(b) : 'form submit', via: 'enter'};
    }
    if (tag === 'BUTTON' || tag === 'A') return {actionable: true, tag, submits: tag === 'A' || isSubmit, label, via: 'enter'};
    return {actionable: false, tag, label: '', submits: false};
  }
  return {actionable: true, tag, submits: isSubmit || tag === 'A', label, field};
}"""


@dataclass
class CommitSpec:
    name: str
    label_regex: str
    confirmed_regex: Optional[str] = None   # page text that proves it happened (group 1 = reference)
    rejected_regex: Optional[str] = None    # page text that proves it did NOT happen (safe to try again)
    human_first: Optional[bool] = None      # None -> task default (not mock)
    max_amount: Optional[float] = None      # hard cap: if the page shows more (or no readable amount), nothing is sent
    amount_regex: Optional[str] = None      # where the amount is on the page (its groups are summed); default: every rupee figure

    def amount(self, text: str) -> List[float]:
        if not self.amount_regex:
            return money(text)
        m = re.search(self.amount_regex, text or "", re.I | re.S)
        return [float(g.replace(",", "")) for g in (m.groups() if m else ()) if g and re.fullmatch(r"\d[\d,]*(?:\.\d+)?", g)]

    def matches(self, label: str) -> bool:
        return bool(label) and bool(re.search(self.label_regex, label, re.I))


def commit_specs(raw: list, success_regex: Optional[str]) -> List[CommitSpec]:
    out = []
    for d in raw or []:
        c = CommitSpec(**{k: d[k] for k in CommitSpec.__dataclass_fields__ if k in d})
        c.confirmed_regex = c.confirmed_regex or success_regex
        out.append(c)
    return out


async def probe(page, act) -> dict:
    """What the action would really hit: element under the pointer, or what Enter would trigger."""
    if isinstance(act, Click):
        xy = [act.x, act.y]
    elif isinstance(act, Press) and act.key == "Enter":
        xy = [None, None]
    else:
        return {}
    try:
        return await page.evaluate(PROBE_JS, xy) or {}
    except Exception as e:
        return {"actionable": False, "label": "", "submits": False, "error": type(e).__name__}


def classify(act, pr: dict, specs: List[CommitSpec], success_regex: Optional[str]) -> Tuple[str, Optional[CommitSpec], str]:
    """-> (effect class, commit spec if it is a commit, label of what it really hits)."""
    if isinstance(act, (Type, Select)):
        return LOCAL, None, getattr(act, "target", "")
    if isinstance(act, Press) and act.key != "Enter":
        return LOCAL, None, act.key
    if not isinstance(act, (Click, Press)):
        return OBSERVE, None, ""
    # The DOM label is authoritative when there is a real element; the model's words are only a fallback.
    label = pr.get("label", "") if pr.get("actionable") else (getattr(act, "target", "") or "")
    for c in specs:
        if c.matches(label):
            return COMMIT, c, label
    flagged = getattr(act, "irreversible", False)
    named = pr.get("actionable") and not pr.get("field") and re.search(IRREVERSIBLE_LABELS, label, re.I)  # the button says so itself
    if named or (flagged and (pr.get("submits") or not pr.get("actionable"))):
        return COMMIT, CommitSpec(name=f"irreversible: {label[:40] or 'unlabelled'}", label_regex=re.escape(label or "-"),
                                  confirmed_regex=success_regex), label
    return (NAVIGATE if pr.get("submits") else LOCAL), None, label


@dataclass
class Attempt:
    id: str
    name: str
    label: str
    run_id: str
    step: int
    status: str
    ts: float
    confirmed_regex: Optional[str] = None
    rejected_regex: Optional[str] = None
    reference: Optional[str] = None
    evidence: str = ""
    history: list = field(default_factory=list)


class Ledger:
    """Write-ahead log of irreversible actions. Survives the run (copied into the checkpoint) so a resumed run
    knows what may already have happened in the world."""

    def __init__(self, path: Path, attempts: Optional[List[Attempt]] = None):
        self.path = path
        self.attempts: List[Attempt] = list(attempts or [])

    @classmethod
    def from_public(cls, path: Path, rows: list) -> "Ledger":
        return cls(path, [Attempt(**r) for r in rows or []])

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(self.public(), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def public(self) -> list:
        return [asdict(a) for a in self.attempts]

    def begin(self, spec: CommitSpec, label: str, run_id: str, step: int) -> Attempt:
        a = Attempt(id=uuid.uuid4().hex[:8], name=spec.name, label=label, run_id=run_id, step=step, status=INTENT,
                    ts=time.time(), confirmed_regex=spec.confirmed_regex, rejected_regex=spec.rejected_regex)
        a.history.append([INTENT, round(a.ts, 2), "logged before dispatch"])
        self.attempts.append(a)
        self.save()
        return a

    def set(self, a: Attempt, status: str, evidence: str = "", reference: Optional[str] = None):
        a.status, a.evidence = status, evidence[:200]
        if reference:
            a.reference = reference
        a.history.append([status, round(time.time(), 2), evidence[:200]])
        self.save()

    def blocking(self, name: str) -> Optional[Attempt]:
        for a in reversed(self.attempts):
            if a.name == name and a.status in OPEN + DONE:
                return a
        return None

    def open(self) -> List[Attempt]:
        return [a for a in self.attempts if a.status in OPEN]

    def judge(self, a: Attempt, text: str) -> Optional[Tuple[str, Optional[str], str]]:
        """Evidence on the page -> (status, reference, evidence) or None if the page proves nothing."""
        if a.confirmed_regex:
            m = re.search(a.confirmed_regex, text, re.I | re.S)
            if m:
                ref = next((g for g in m.groups() if g), None)
                return CONFIRMED, ref, f"page shows: {m.group(0)[:80]!r}"
        if a.rejected_regex:
            m = re.search(a.rejected_regex, text, re.I | re.S)
            if m:
                return REJECTED, None, f"page shows: {m.group(0)[:80]!r}"
        return None

    def reconcile(self, text: str) -> List[Attempt]:
        changed = []
        for a in self.open():
            v = self.judge(a, text)
            if v:
                self.set(a, v[0], v[2], v[1])
                changed.append(a)
        return changed

    def notes(self) -> List[str]:
        out = []
        for a in self.attempts:
            where = f"'{a.label}' at step {a.step}"
            if a.status in DONE:
                out.append(f"{a.name}: DONE ({where}{', ref ' + a.reference if a.reference else ''}). Never do it again.")
            elif a.status in OPEN:
                out.append(f"{a.name}: OUTCOME UNKNOWN ({where}; it may already have gone through). Do NOT do it again. "
                           "Find out whether it happened: log in again if the session ended, look for a status or receipt page, or ask_human.")
            elif a.status == REJECTED:
                out.append(f"{a.name}: did not go through ({where}; {a.evidence}). Fix the problem, then you may try once more.")
            elif a.status == HUMAN_VOIDED:
                out.append(f"{a.name}: the human confirmed {where} did NOT go through. You may try once more.")
        return out
