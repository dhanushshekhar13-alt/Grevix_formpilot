"""FormPilot loop: observe -> reconcile -> reason (local VLM) -> validate -> classify -> act -> verify -> record.

Vision-first: the model only sees screenshots. DOM is used only as *evidence* (did the page change? is a receipt
present? what is really under the pointer?), never to pick targets. One action per step; bounded retries; human
checkpoints. Every action has a consequence class (agent/consequence.py); irreversible ones go through a write-ahead
ledger and are never repeated while their outcome is unknown. Each step writes a checkpoint, so a run that dies can be
resumed from where the world actually stands instead of from the start.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import async_playwright

from .actions import ActionExecutor, AskHuman, Done, Fail, Type, mask, parse_action, redacted
from .config import Settings, TaskSpec
from .consequence import (COMMIT, CONFIRMED, DISPATCHED, DONE, HUMAN_CONFIRMED, HUMAN_VOIDED, IN_DOUBT, LOCAL,
                          NAVIGATE, OBSERVE, OPEN, REJECTED, RETRY_POLICY, Ledger, classify, commit_specs, probe)
from .vision import (ModelMissing, OllamaVision, PlanContext, PlanFailed, VisionError, VisionUnavailable)

Emit = Callable[[dict], Awaitable[None]]
AskHumanCb = Callable[[str, str], Awaitable[str]]

FINGERPRINT_JS = """() => JSON.stringify([location.href, (document.body.innerText||'').slice(0,6000),
  [...document.querySelectorAll('input,select,textarea')].map(e=>(e.type==='checkbox'||e.type==='radio')?e.checked:e.value),
  window.scrollY|0, !!document.querySelector('#ad,[role=dialog]'),
  (e => e && ['INPUT','TEXTAREA'].includes(e.tagName) ? (e.name || e.id || e.tagName) : '')(document.activeElement)])"""
# Error banners the page is showing right now (evidence for the model, which often overlooks them in the screenshot).
PAGE_ERRORS_JS = """() => [...new Set([...document.querySelectorAll('[class*=err i],[class*=invalid i],[role=alert]')]
  .filter(e => e.offsetParent !== null).map(e => (e.innerText || '').trim().replace(/\\s+/g, ' ')).filter(Boolean))].join(' | ').slice(0, 300)"""
# Which visible form controls hold nothing (names only, never values). Sent with a page error, when the model has
# evidently misjudged what it filled in.
EMPTY_FIELDS_JS = """() => {
  const out = [], groups = {};
  const lab = e => ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || e.placeholder || e.name || e.id || '').trim().slice(0, 40);
  for (const e of document.querySelectorAll('input,select,textarea')) {
    const t = (e.type || '').toLowerCase();
    if (e.offsetParent === null || e.disabled || ['checkbox', 'submit', 'button', 'hidden', 'image', 'file'].includes(t)) continue;
    if (t === 'radio') (groups[e.name] = groups[e.name] || []).push(e);
    else if (e.tagName === 'SELECT') { if (e.selectedIndex <= 0 && e.options.length > 1) out.push(`dropdown showing '${e.options[0].text.trim()}' has nothing selected`); }
    else if (!e.value) out.push(`text box '${lab(e)}' is empty`);
  }
  for (const g of Object.values(groups))
    if (!g.some(r => r.checked)) out.push('none of the radio buttons [' + g.map(lab).join(' | ') + '] is selected');
  return out.slice(0, 8).join('; ');
}"""
FOCUSED_VALUE_JS = "() => { const e = document.activeElement; return e && 'value' in e ? String(e.value) : null }"
# What kind of secret a text field asks for, judged from the field itself and the images beside it (never from the model).
CODE_FIELD_JS = """([x, y]) => {
  const el = x === null ? document.activeElement : document.elementFromPoint(x, y);
  const c = (el && el.closest('input,textarea')) || (document.activeElement && document.activeElement.closest('input,textarea'));
  if (!c) return '';
  const box = (c.form || c).parentElement;
  const imgs = box ? [...box.querySelectorAll('img')].map(i => i.getAttribute('src') + ' ' + i.alt).join(' ') : '';
  return [c.name, c.id, c.placeholder, c.getAttribute('aria-label') || '', c.autocomplete || '', imgs].join(' ').slice(0, 400);
}"""
HUMAN_ONLY_CODES = (("otp", r"\botp\b|one[- ]time", "the OTP sent to your phone"),
                    ("captcha", r"captcha|security code", "the characters shown in the CAPTCHA image"))
RESUMABLE = ("running", "interrupted", "failed", "stopped", "incomplete")


class InjectedCrash(RuntimeError):
    """Simulated agent death at the worst moment: an irreversible click was dispatched, its outcome not yet seen."""


def _flatten_secrets(profile: dict, keys: list) -> list:
    out = []

    def walk(d):
        for k, v in d.items():
            if isinstance(v, dict):
                walk(v)
            elif k in keys and v:
                out.append(str(v))
    walk(profile)
    return out


def _akey(act) -> str:
    d = act.model_dump(exclude_none=True)
    keep = {k: (round(v / 25) if k in ("x", "y") else v) for k, v in d.items() if k in ("action", "x", "y", "key", "dy", "option", "text")}
    return json.dumps(keep, sort_keys=True)


def load_checkpoint(run_dir) -> dict:
    return json.loads((Path(run_dir) / "checkpoint.json").read_text())


def latest_resumable(runs_dir: Path, task_name: Optional[str] = None) -> Optional[dict]:
    """Most recent run that did not end in a verified success (or that left an irreversible action in doubt)."""
    best = None
    for p in Path(runs_dir).glob("*/checkpoint.json"):
        try:
            c = json.loads(p.read_text())
        except Exception:
            continue
        if task_name and c.get("task") != task_name:
            continue
        unresolved = any(a["status"] in OPEN for a in c.get("ledger", []))
        if (c.get("status") in RESUMABLE or unresolved) and not c.get("resumed_by"):
            if best is None or c.get("updated", 0) > best.get("updated", 0):
                best = c
    return best


async def run_task(spec: TaskSpec, emit: Emit, ask_human: AskHumanCb, settings: Optional[Settings] = None,
                   stop: Optional[asyncio.Event] = None, planner=None, run_dir: Optional[Path] = None,
                   resume: Optional[dict] = None) -> dict:
    s = settings or Settings.from_env()
    stop = stop or asyncio.Event()
    planner = planner or OllamaVision(s)
    max_steps = spec.max_steps or s.max_steps
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    run_dir = run_dir or (s.runs_dir / run_id)
    (run_dir / "frames").mkdir(parents=True, exist_ok=True)
    secrets = _flatten_secrets(spec.profile, spec.sensitive_keys)
    specs = commit_specs(spec.commits, spec.success_regex)
    t_start = time.time()
    m = {"steps": 0, "actions_executed": 0, "retries": 0, "rejected_actions": 0, "malformed_steps": 0,
         "human_interventions": 0, "no_effect_actions": 0, "model_latency_total": 0.0, "model_calls": 0,
         "commits_dispatched": 0, "duplicate_commits_blocked": 0, "in_doubt_events": 0, "reconciled": 0}
    result = {"status": "failed", "summary": "", "reference": None, "verified": False}
    trace_f = (run_dir / "trace.jsonl").open("w")
    ledger = Ledger(run_dir / "ledger.json")
    history, warnings, human_inputs = [], [], []
    base_step = 0
    storage_in = None
    start_url = spec.url

    if resume:
        prev_dir = Path(resume["run_dir"])
        ledger = Ledger.from_public(run_dir / "ledger.json", resume.get("ledger", []))
        for a in ledger.open():
            if a.status != IN_DOUBT:
                ledger.set(a, IN_DOUBT, f"carried over from run {resume['run_id']} ({a.status}); outcome never observed")
        ledger.save()
        history = list(resume.get("history", []))
        base_step = int(resume.get("step", 0))
        start_url = resume.get("url") or spec.url
        if (prev_dir / "storage_state.json").exists():
            storage_in = str(prev_dir / "storage_state.json")
        warnings.append(f"RESUMED: an earlier run stopped at step {base_step} ({resume.get('reason') or resume.get('status')}). "
                        "Do not redo steps that are already finished; continue from what the screen shows now.")
        try:
            prev = load_checkpoint(prev_dir)
            prev["resumed_by"] = run_id
            (prev_dir / "checkpoint.json").write_text(json.dumps(prev, indent=2))
        except Exception:
            pass

    def record(rec: dict):
        trace_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        trace_f.flush()

    async def ev(e: dict):
        e["t"] = round(time.time(), 2)
        await emit(e)

    async def ev_ledger():
        await ev({"type": "ledger", "entries": [{k: a[k] for k in ("name", "label", "status", "step", "run_id", "reference", "evidence")}
                                                for a in ledger.public()]})

    def finish_meta(res: dict):
        res.update(run_id=run_id, run_dir=str(run_dir))
        unresolved = [a.name for a in ledger.open()]
        if unresolved:
            res["unresolved_commits"] = unresolved
        res["ledger"] = ledger.public()
        n = max(m["model_calls"], 1)
        summary = {"run_id": run_id, "model": s.model, "task": spec.name, "resumed_from": resume and resume["run_id"],
                   "result": res, "ledger": ledger.public(), "metrics": {
                       **m, "avg_latency_s": round(m["model_latency_total"] / n, 2),
                       "total_time_s": round(time.time() - t_start, 1),
                       "receipt_observed": bool(res.get("verified") and res.get("reference"))},
                   "config": {k: v for k, v in s.public().items() if k not in ("runs_dir",)}}
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        return summary

    page_ref = {"page": None, "ctx": None, "step": base_step}

    async def checkpoint(status: str = "running", reason: str = "", save_storage: bool = True):
        page, ctx = page_ref["page"], page_ref["ctx"]
        url = None
        if page is not None:
            try:
                url = page.url
            except Exception:
                url = None
        if save_storage and ctx is not None:
            try:
                sp = run_dir / "storage_state.json"
                await ctx.storage_state(path=str(sp))
                os.chmod(sp, 0o600)
            except Exception:
                pass
        cp = {"run_id": run_id, "run_dir": str(run_dir), "task": spec.name, "status": status, "reason": reason,
              "step": page_ref["step"], "url": url or start_url, "spec_url": spec.url, "task_text": spec.task, "hints": spec.hints,
              "history": history[-20:], "ledger": ledger.public(), "resumed_from": resume and resume["run_id"], "updated": time.time()}
        tmp = run_dir / "checkpoint.tmp"
        tmp.write_text(json.dumps(cp, indent=2))
        os.replace(tmp, run_dir / "checkpoint.json")

    async def await_human(kind: str, question: str) -> Optional[str]:
        m["human_interventions"] += 1
        t = asyncio.ensure_future(ask_human(kind, question))
        sg = asyncio.ensure_future(stop.wait())
        done, _ = await asyncio.wait({t, sg}, timeout=s.human_timeout, return_when=asyncio.FIRST_COMPLETED)
        for f in (t, sg):
            if f not in done:
                f.cancel()
        return t.result() if t in done else None

    # Fail fast (before opening a browser) if the local model isn't usable.
    if hasattr(planner, "health"):
        h = await planner.health()
        if not h.get("model_ready"):
            result["summary"] = h["message"]
            await ev({"type": "finished", **result})
            await checkpoint("failed", h["message"], save_storage=False)
            finish_meta(result)
            trace_f.close()
            return result

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not s.headed)
        kw = {"record_video_dir": str(run_dir / "video"), "record_video_size": {"width": s.width, "height": s.height}} if s.record_video else {}
        if storage_in:
            kw["storage_state"] = storage_in
        ctx = await browser.new_context(viewport={"width": s.width, "height": s.height}, **kw)
        page = await ctx.new_page()
        page_ref.update(page=page, ctx=ctx)
        if hasattr(planner, "bind"):
            planner.bind(page)
        executor = ActionExecutor(page, s.action_timeout)
        record({"type": "run", "run_id": run_id, "model": s.model, "task": spec.name, "url": start_url, "started": t_start,
                "resumed_from": resume and resume["run_id"], "policy": RETRY_POLICY})
        if resume:
            record({"type": "resume", "from_run": resume["run_id"], "from_step": base_step, "url": start_url, "ledger": ledger.public()})
            await ev({"type": "resumed", "from_run": resume["run_id"], "from_step": base_step, "url": start_url})
            await ev_ledger()

        async def snap() -> bytes:
            return await page.screenshot(type="jpeg", quality=70)

        async def fp() -> str:
            try:
                return hashlib.sha1((await page.evaluate(FINGERPRINT_JS)).encode()).hexdigest()
            except Exception:
                return ""

        async def page_text() -> str:
            try:
                return await page.inner_text("body")
            except Exception:
                return ""

        async def fail(reason: str, status: str = "failed"):
            result.update(status=status, summary=reason)
            if ledger.open():
                result["summary"] += (" | WARNING: outcome still unknown for " + ", ".join(a.name for a in ledger.open()) +
                                      " - do not retry it by hand; resume the run or check the portal/bank first.")
            await ev({"type": "finished", **result})

        try:
            await page.goto(start_url, timeout=15000)
            await page.wait_for_timeout(700)
        except Exception as e:
            await fail(f"could not open {start_url}: {type(e).__name__}: {str(e)[:120]}")
            await checkpoint(result["status"], result["summary"], save_storage=False)
            await ctx.close(); await browser.close(); trace_f.close()
            finish_meta(result)
            return result

        recent = deque(maxlen=6)
        no_change, escalations, bad_done = 0, 0, 0
        consecutive_bad = 0
        commit_blocks: dict = {}
        err_told = ""            # page error the model has already been told about (reset when a step is submitted)
        human_codes: dict = {}   # kind -> the human's latest OTP / CAPTCHA reply that has not been submitted yet
        typed_codes: set = set()
        finished = False
        crash_fired = False
        validate = lambda raw: parse_action(raw, s.width, s.height, s.coord_mode)

        async def reconcile(step: int, text: str):
            for a in ledger.reconcile(text):
                m["reconciled"] += 1
                record({"step": step, "type": "reconcile", "commit": a.name, "attempt": a.id, "status": a.status,
                        "evidence": a.evidence, "reference": a.reference})
                history.append(f"{step}. evidence on screen: {a.name} {('CONFIRMED' if a.status == CONFIRMED else 'did NOT go through')}")
                await ev({"type": "reconciled", "step": step, "name": a.name, "status": a.status, "evidence": a.evidence})
                await ev_ledger()

        try:
            for step in range(base_step + 1, base_step + max_steps + 1):
                if stop.is_set():
                    await fail("stopped by user", "stopped"); finished = True
                    break
                m["steps"] = step - base_step
                page_ref["step"] = step
                before = await snap()
                (run_dir / "frames" / f"{step:03d}_before.jpg").write_bytes(before)
                fp_before = await fp()
                if step == base_step + 1:
                    await ev({"type": "screenshot", "image": base64.b64encode(before).decode(), "step": 0})
                if ledger.open():
                    await reconcile(step, await page_text())

                pctx = PlanContext(task=spec.task, profile=spec.profile, hints=spec.hints, history=list(history),
                                   warnings=list(warnings), human_inputs=list(human_inputs), width=s.width, height=s.height,
                                   step=step, commitments=ledger.notes())
                warnings.clear()
                try:
                    shown_err = mask(await page.evaluate(PAGE_ERRORS_JS), secrets)
                except Exception:
                    shown_err = ""
                if shown_err and shown_err != err_told:
                    pctx.warnings.append(f"The page shows this error: '{shown_err}'. Fix exactly what it names (and re-check every dropdown) "
                                         "before you submit again.")
                elif shown_err:  # the banner only goes away on the next submit: do not let it trigger the same fix forever
                    pctx.warnings.append(f"The error '{shown_err}' is still displayed from your LAST submit and stays until you submit again. "
                                         "RECENT ACTIONS shows what you have fixed since. Do not redo a fix that worked: fill what is still "
                                         "empty, then click the submit button.")
                if shown_err:
                    try:
                        empties = await page.evaluate(EMPTY_FIELDS_JS)
                    except Exception:
                        empties = ""
                    if empties:
                        pctx.warnings.append(f"Not filled in on this page right now: {empties}. Set each one that the form requires, "
                                             "one action each, before you submit.")
                err_told = shown_err
                try:
                    act, meta = await planner.plan(before, pctx, validate)
                except (VisionUnavailable, ModelMissing) as e:
                    await fail(str(e)); finished = True
                    break
                except PlanFailed as e:
                    m["malformed_steps"] += 1
                    m["retries"] += max(e.attempts - 1, 0)
                    m["rejected_actions"] += e.attempts
                    consecutive_bad += 1
                    record({"step": step, "type": "model_failure", "error": str(e)})
                    if consecutive_bad >= s.max_bad_steps:
                        await fail(f"model returned invalid output {consecutive_bad} steps in a row: {e}"); finished = True
                        break
                    warnings.append("Your previous reply was not a valid action. Reply with one valid JSON action.")
                    continue
                except VisionError as e:
                    await fail(f"model error: {e}"); finished = True
                    break
                consecutive_bad = 0
                m["model_calls"] += meta["attempts"]
                m["model_latency_total"] += meta["latency"]
                m["retries"] += meta["attempts"] - 1
                m["rejected_actions"] += meta["rejected"]

                # Human first: an OTP / CAPTCHA field only ever receives what the human supplied, never the model's guess.
                guard = None
                if isinstance(act, Type):
                    try:
                        desc = await page.evaluate(CODE_FIELD_JS, [act.x, act.y])
                    except Exception:
                        desc = ""
                    code = next((c for c in HUMAN_ONLY_CODES if re.search(c[1], desc, re.I)), None)
                    if code:
                        kind = code[0]
                        if kind not in human_codes:
                            q = f"The page asks for {code[2]}. The agent will not guess it: please type it here."
                            await ev({"type": "need_human", "kind": kind, "question": q, "image": base64.b64encode(before).decode()})
                            ans = await await_human(kind, q)
                            if ans is None:
                                await fail("stopped by user" if stop.is_set() else "timed out waiting for human input",
                                           "stopped" if stop.is_set() else "failed"); finished = True
                                break
                            await ev({"type": "human_answer", "kind": kind, "text": "(provided)"})
                            human_codes[kind] = ans.strip()
                            human_inputs.append(f"{kind}: {ans}")
                            secrets.append(ans)
                            guard = f"model tried to fill the {kind} field itself; not typed. Human was asked and their reply typed instead"
                        elif act.text.strip() != human_codes[kind]:
                            guard = f"model's text for the {kind} field did not match the human's reply; the human's reply was typed instead"
                        act = act.model_copy(update={"text": human_codes[kind], "sensitive": True})
                        typed_codes.add(kind)

                pr = await probe(page, act)
                effect, cspec, label = classify(act, pr, specs, spec.success_regex)
                shown_label = mask(label or "", secrets)
                ad = redacted(act, secrets)
                rec = {"step": step, "type": "step", "action": ad, "latency_s": round(meta["latency"], 2), "attempts": meta["attempts"],
                       "before": f"frames/{step:03d}_before.jpg", "effect": effect, "retry_policy": RETRY_POLICY[effect]}
                if pr:
                    rec["claimed_target"] = mask(getattr(act, "target", "") or "", secrets)
                    rec["observed_target"] = shown_label
                if cspec:
                    rec["commit"] = cspec.name
                if guard:
                    rec["guard"] = guard
                    history.append(f"{step}. {guard}")
                if act.rationale:
                    await ev({"type": "thought", "step": step, "text": ad.get("rationale", "")})
                tgt = getattr(act, "target", "") or ""
                txt = act.action if not tgt else f"{act.action} → {tgt}"
                if pr and shown_label and tgt and shown_label.lower() not in tgt.lower() and tgt.lower() not in shown_label.lower():
                    txt += f"  (pointer is on '{shown_label}')"
                await ev({"type": "action", "step": step, "name": act.action, "input": ad, "text": txt, "effect": effect,
                          "commit": cspec.name if cspec else None})

                async def outcome(o: str, detail: str):
                    rec["outcome"] = o
                    await ev({"type": "outcome", "step": step, "effect": effect, "outcome": o, "detail": detail})

                if isinstance(act, Done):
                    ok, ref, why = await _check_done(page, spec, act)
                    rec["verification"] = {"done_accepted": ok, "detail": why}
                    if ok:
                        await reconcile(step, await page_text())
                        result.update(status="success", summary=act.summary or "Task completed", reference=ref, verified=bool(spec.success_regex))
                        rec["outcome"] = "verified"
                        record(rec)
                        await ev({"type": "screenshot", "image": base64.b64encode(await snap()).decode(), "step": step})
                        await ev({"type": "finished", **result})
                        finished = True
                        break
                    bad_done += 1
                    rec["outcome"] = "rejected_claim"
                    history.append(f"{step}. done → REJECTED: {why}")
                    warnings.append(f"You said done but there is no confirmation on screen ({why}). 'done' is only accepted on the page that "
                                    "shows the receipt with its reference number. Click the link or button that opens it "
                                    "(for example 'Check payment status', 'Refresh Status' or 'View Receipt'). Do not say done again before you see it.")
                    record(rec)
                    if bad_done >= 3:
                        await fail("agent claimed completion 3 times without a visible receipt"); finished = True
                        break
                    continue
                if isinstance(act, Fail):
                    record(rec); await fail(f"agent gave up: {act.reason}"); finished = True
                    break

                if isinstance(act, AskHuman):
                    kind, q = act.kind, act.question
                    await ev({"type": "need_human", "kind": kind, "question": q, "image": base64.b64encode(before).decode()})
                    ans = await await_human(kind, q)
                    if ans is None:
                        record(rec); await fail("stopped by user" if stop.is_set() else "timed out waiting for human input", "stopped" if stop.is_set() else "failed"); finished = True
                        break
                    shown = "(provided)" if kind in ("otp", "captcha") else mask_answer(ans, secrets)
                    await ev({"type": "human_answer", "kind": kind, "text": shown})
                    rec["human"] = {"kind": kind, "answer": shown}
                    if kind == "confirm" and ans.strip().lower() not in ("yes", "y", "approve", "ok"):
                        rec["outcome"] = "declined"
                        record(rec); await fail("user declined the action", "stopped"); finished = True
                        break
                    human_inputs.append(f"{kind}: {ans}")
                    if kind in ("otp", "captcha"):
                        secrets.append(ans)
                        human_codes[kind] = ans.strip()
                    history.append(f"{step}. ask_human({kind}) → user replied; use the reply from HUMAN REPLIES")
                    rec["result"] = f"human provided {kind}"
                    rec["outcome"] = "human_input"
                    record(rec)
                    await ev({"type": "screenshot", "image": base64.b64encode(await snap()).decode(), "step": step, "result": rec["result"]})
                    await checkpoint()
                    continue

                attempt = None
                if effect == COMMIT:
                    prior = ledger.blocking(cspec.name)
                    approved = False
                    if prior:
                        m["duplicate_commits_blocked"] += 1
                        n_blk = commit_blocks[cspec.name] = commit_blocks.get(cspec.name, 0) + 1
                        happened = prior.status in DONE
                        why = (f"'{shown_label}' would repeat {cspec.name}, which already happened at step {prior.step}"
                               f"{' (ref ' + prior.reference + ')' if prior.reference else ''}" if happened else
                               f"'{shown_label}' would repeat {cspec.name}; the attempt at step {prior.step} has an UNKNOWN outcome "
                               "and may already have gone through")
                        rec.update(dispatched=False, result="BLOCKED, not executed: " + why, blocked_by=prior.id)
                        await outcome("blocked", why)
                        record(rec)
                        history.append(f"{step}. {act.action} '{shown_label}' → BLOCKED (not executed): {why}")
                        if happened:
                            warnings.append(f"{cspec.name} is already done. Never do it again; finish the rest of the task (e.g. open the receipt).")
                            continue
                        warnings.append(f"Do NOT repeat {cspec.name}. First find out whether the earlier attempt went through: log in again "
                                        "if the session ended, look for a payment status or receipt page. If you cannot tell, ask_human.")
                        if n_blk < 2:
                            continue
                        commit_blocks[cspec.name] = 0
                        q = (f"The agent cannot tell whether {cspec.name} ('{prior.label}', step {prior.step}, run {prior.run_id}) went through, "
                             "and wants to do it again. Check your bank / the portal. Reply 'paid' if it went through (it will never be repeated), "
                             "'not paid' to allow exactly one new attempt, or type guidance.")
                        await ev({"type": "need_human", "kind": "reconcile", "question": q, "image": base64.b64encode(before).decode()})
                        ans = await await_human("reconcile", q)
                        if ans is None:
                            await fail("stopped by user" if stop.is_set() else "timed out waiting for human input",
                                       "stopped" if stop.is_set() else "failed"); finished = True
                            break
                        a_l = ans.strip().lower()
                        await ev({"type": "human_answer", "kind": "reconcile", "text": mask_answer(ans, secrets)})
                        if a_l in ("paid", "yes", "went through", "it went through"):
                            ledger.set(prior, HUMAN_CONFIRMED, "human: it went through")
                            record({"step": step, "type": "reconcile", "commit": prior.name, "attempt": prior.id, "status": HUMAN_CONFIRMED, "evidence": "human"})
                            history.append(f"{step}. human confirmed {prior.name} went through; never repeat it")
                            warnings.append(f"{prior.name} is done (the human confirmed it). Do not pay again. Now open the receipt: click "
                                            "'Check payment status', 'Refresh Status' or 'View Receipt', whichever is on screen.")
                            await ev_ledger()
                            continue
                        if a_l in ("not paid", "no", "retry", "not charged"):
                            ledger.set(prior, HUMAN_VOIDED, "human: it did not go through")
                            record({"step": step, "type": "reconcile", "commit": prior.name, "attempt": prior.id, "status": HUMAN_VOIDED, "evidence": "human"})
                            await ev_ledger()
                            approved = True
                            rec.update(result="", human={"kind": "reconcile", "answer": "not paid"}, unblocked_by_human=prior.id)
                            rec.pop("blocked_by", None)
                        else:
                            human_inputs.append(f"guidance: {ans}")
                            continue
                    human_first = cspec.human_first if cspec.human_first is not None else not spec.mock
                    if human_first and not approved:
                        q = f"The agent is about to '{shown_label or cspec.name}' ({cspec.name}). This cannot be undone. Approve?"
                        await ev({"type": "need_human", "kind": "confirm", "question": q, "image": base64.b64encode(before).decode()})
                        ans = await await_human("confirm", q)
                        if ans is None:
                            record(rec); await fail("stopped by user" if stop.is_set() else "timed out waiting for human input", "stopped" if stop.is_set() else "failed"); finished = True
                            break
                        await ev({"type": "human_answer", "kind": "confirm", "text": mask_answer(ans, secrets)})
                        rec["human"] = {"kind": "confirm", "answer": mask_answer(ans, secrets)}
                        if ans.strip().lower() not in ("yes", "y", "approve", "ok"):
                            rec["outcome"] = "declined"
                            record(rec); await fail("user declined the irreversible action", "stopped"); finished = True
                            break
                    attempt = ledger.begin(cspec, shown_label, run_id, step)
                    rec["attempt"] = attempt.id
                    await checkpoint()  # durable BEFORE the click: a crash after this point leaves an 'intent' on record
                    await ev_ledger()

                res = await executor.execute(act)
                m["actions_executed"] += 1
                if attempt is not None:
                    m["commits_dispatched"] += 1
                    if s.fault == "crash_after_commit" and not crash_fired:
                        crash_fired = True
                        raise InjectedCrash(f"injected crash right after dispatching {attempt.name} ('{attempt.label}') at step {step}")
                    # A failed or timed-out click may still have reached the server: never read it as "did not happen".
                    ledger.set(attempt, DISPATCHED if res.ok else IN_DOUBT, res.detail)
                elif not res.ok and effect in (OBSERVE, LOCAL):
                    m["retries"] += 1
                    rec["auto_retry"] = res.detail
                    res = await executor.execute(act)
                if not res.ok:
                    m["rejected_actions"] += 1
                elif effect in (NAVIGATE, COMMIT):
                    err_told = ""  # a new submit: whatever error shows next is news
                    for k in typed_codes:  # a submitted code is single-use, the next one is asked afresh
                        human_codes.pop(k, None)
                    typed_codes.clear()
                rec["ok"] = res.ok
                rec["dispatched"] = True if res.ok else ("unknown" if attempt is not None else False)

                after = await snap()
                (run_dir / "frames" / f"{step:03d}_after.jpg").write_bytes(after)
                rec["after"] = f"frames/{step:03d}_after.jpg"
                changed = (await fp()) != fp_before
                rec["result"] = (rec.get("result") + "; " if rec.get("result") else "") + res.detail
                rec["verification"] = {"page_changed": changed}

                if attempt is not None:
                    verdict = ledger.judge(attempt, await page_text())
                    if verdict:
                        ledger.set(attempt, verdict[0], verdict[2], verdict[1])
                        o = verdict[0]
                    else:
                        m["in_doubt_events"] += 1
                        ledger.set(attempt, IN_DOUBT, "no confirmation or rejection visible after dispatch" + ("" if changed else " (page unchanged)"))
                        o = IN_DOUBT
                    rec["verification"]["commit"] = {"status": attempt.status, "evidence": attempt.evidence, "reference": attempt.reference}
                    await outcome(o, attempt.evidence)
                    await ev_ledger()
                    note = {CONFIRMED: "CONFIRMED by evidence on screen", REJECTED: "did NOT go through (" + attempt.evidence + ")",
                            IN_DOUBT: "OUTCOME UNKNOWN - it may have gone through; do not repeat"}[o]
                elif not res.ok:
                    await outcome("error", res.detail)
                    note = "FAILED: " + res.detail
                elif isinstance(act, Type):
                    val = None
                    try:
                        val = await page.evaluate(FOCUSED_VALUE_JS)
                    except Exception:
                        pass
                    o = "unverified" if val is None else ("verified" if val == act.text else "mismatch")
                    rec["verification"]["field_value_matches"] = None if val is None else val == act.text
                    await outcome(o, "field holds the typed text" if o == "verified" else
                                  "field shows something else (length limit or wrong field?)" if o == "mismatch" else "no focused field to check")
                    note = res.detail + {"verified": "; the field now holds it, go on to the next thing", "mismatch": " BUT the field shows something different", "unverified": " (could not verify field)"}[o]
                else:
                    o = "changed" if changed else ("no_effect" if effect != OBSERVE else "observed")
                    await outcome(o, res.detail)
                    note = res.detail
                record(rec)
                await ev({"type": "screenshot", "image": base64.b64encode(after).decode(), "step": step,
                          "result": rec["result"] if rec.get("ok", True) else "error: " + rec["result"]})

                if act.action == "click" and pr.get("tag") == "SELECT":
                    warnings.append("You clicked a dropdown, which does not open in screenshots. Use the select action on it: same x,y plus the option text.")
                active = act.action in ("click", "type", "select", "press")
                if changed:
                    bad_done = 0  # the limit is for claiming completion again and again without doing anything
                if active and not changed:
                    no_change += 1
                    m["no_effect_actions"] += 1
                elif active:
                    no_change = 0
                tl = f" '{shown_label}'" if shown_label and act.action in ("click", "press") else (" " + tgt if tgt else "")
                history.append(f"{step}. {act.action}{tl} → {note}{' (no visible change)' if active and not changed and attempt is None else ''}")
                recent.append(_akey(act))
                await checkpoint()
                repeats = recent.count(recent[-1])
                if no_change >= 2 or repeats >= 3:
                    warnings.append("Your recent actions had no visible effect or repeat. Try a different target, scroll, or ask_human.")
                if no_change >= 4 or repeats >= 5:
                    escalations += 1
                    if escalations > 2:
                        await fail("stuck: repeated actions with no effect after 2 human escalations"); finished = True
                        break
                    q = "The agent seems stuck (repeating actions with no effect). Fix the page or give guidance, then press Send."
                    await ev({"type": "need_human", "kind": "info", "question": q, "image": base64.b64encode(after).decode()})
                    ans = await await_human("info", q)
                    if ans is None:
                        await fail("stopped by user" if stop.is_set() else "timed out waiting for human input"); finished = True
                        break
                    await ev({"type": "human_answer", "kind": "info", "text": mask_answer(ans, secrets)})
                    human_inputs.append(f"guidance: {ans}")
                    no_change, recent = 0, deque(maxlen=6)

            if not finished:
                await fail(f"step limit ({max_steps}) reached without a verified completion", "incomplete")
            await checkpoint(result["status"], result["summary"])
        except Exception as e:
            # Simulates/handles a dead agent: nothing more is written to the ledger or storage, exactly as after a real crash.
            result.update(status="interrupted", summary=f"agent died: {type(e).__name__}: {str(e)[:200]}. "
                          "Resume the run: it continues from the last checkpoint without repeating irreversible actions.")
            record({"type": "crash", "error": f"{type(e).__name__}: {str(e)[:200]}", "ledger": ledger.public()})
            cp_path = run_dir / "checkpoint.json"
            if cp_path.exists():  # keep the last durable state; only mark it
                cp = json.loads(cp_path.read_text())
                cp.update(status="interrupted", reason=result["summary"], updated=time.time())
                cp_path.write_text(json.dumps(cp, indent=2))
            else:
                await checkpoint("interrupted", result["summary"], save_storage=False)
            await ev({"type": "finished", **result})
        finally:
            try:
                await ctx.close()
                await browser.close()
            except Exception:
                pass

    trace_f.close()
    summary = finish_meta(result)
    return {**result, "metrics": summary["metrics"]}


def mask_answer(ans: str, secrets: list) -> str:
    for sec in secrets:
        if sec and len(sec) >= 4:
            ans = ans.replace(sec, "•••")
    return ans


async def _check_done(page, spec: TaskSpec, act: Done):
    try:
        text = await page.inner_text("body")
    except Exception:
        text = ""
    if spec.success_regex:
        mt = re.search(spec.success_regex, text, re.I | re.S)
        if not mt:
            return False, None, "success pattern not found on page"
        return True, (mt.group(1) if mt.groups() else mt.group(0)), "success pattern matched"
    return True, act.reference, "unverified (task defines no success_regex)"
