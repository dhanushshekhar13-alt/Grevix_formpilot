"""Test doubles. ScriptedPlanner drives the REAL executor/portal with DOM-derived coordinates; it is a
deterministic stand-in for the VLM (used to validate the pipeline and benchmark plumbing), not a vision agent."""
from __future__ import annotations

import re
import threading

from werkzeug.serving import make_server


def start_portal(port: int):
    import os
    os.environ["FORMPILOT_BENCH"] = "1"
    from portal.app import app
    srv = make_server("127.0.0.1", port, app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class SeqPlanner:
    """Replays raw model dicts (last one repeats)."""
    def __init__(self, raws):
        self.raws, self.i, self.calls = list(raws), 0, 0

    async def plan(self, png, ctx, validate):
        self.calls += 1
        raw = self.raws[min(self.i, len(self.raws) - 1)]
        self.i += 1
        return validate(raw), {"latency": 0.01, "attempts": 1, "rejected": 0}


class ScriptedPlanner:
    """dup_attempts: how many times it greedily tries to pay again when a payment's outcome is unclear (to prove the
    ledger blocks it). pay_without_upi: first press Pay Now with no UPI chosen (a provably rejected commit)."""

    def __init__(self, dup_attempts: int = 0, pay_without_upi: bool = False, guess_codes: bool = False, pay_without_upi_id: bool = False):
        self.page, self.i = None, 0
        self.dup_left = dup_attempts
        self.pay_without_upi = pay_without_upi
        self.pay_without_upi_id = pay_without_upi_id  # chooses UPI, then presses Pay Now with the UPI ID box still empty
        self.guess_codes = guess_codes  # types its own OTP / CAPTCHA without asking, like a small model does
        self.seen = []

    def bind(self, page):
        self.page = page

    async def _c(self, sel):
        b = await self.page.locator(sel).first.bounding_box()
        return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2

    async def _click(self, sel, **kw):
        x, y = await self._c(sel)
        return {"action": "click", "x": x, "y": y, "rationale": sel, **kw}

    async def _type(self, sel, text, **kw):
        x, y = await self._c(sel)
        return {"action": "type", "x": x, "y": y, "text": text, "rationale": sel, **kw}

    async def _select(self, sel, opt):
        x, y = await self._c(sel)
        return {"action": "select", "x": x, "y": y, "option": opt, "rationale": sel}

    async def _recover(self, ctx):
        """Handles pages off the happy path, by what is on screen (like a model would)."""
        from urllib.parse import urlparse
        path = urlparse(self.page.url).path
        self.seen.append(path)
        if path == "/expired":
            return await self._click("text=Login again")
        if path == "/resume":
            if not await self.page.input_value("[name=mobile]"):
                return await self._type("[name=mobile]", "9876543210", sensitive=True)
            if not await self.page.input_value("[name=dob]"):
                return await self._type("[name=dob]", "14/08/2004")
            return await self._click("button:has-text('Login')")
        if path == "/pay-status":
            if await self.page.locator("button:has-text('Pay Again')").count():
                if self.dup_left > 0:
                    self.dup_left -= 1
                    return await self._click("button:has-text('Pay Again')", target="Pay Again")
                return await self._click("button:has-text('Refresh Status')")
            return await self._click("text=View Receipt")
        if path == "/pay" and await self.page.locator("text=Check payment status").count():
            if self.dup_left > 0:
                self.dup_left -= 1
                return await self._click("button:has-text('Pay Now')", irreversible=True, target="Pay Now")
            return await self._click("text=Check payment status")
        if path == "/done":
            return await self._done()
        return None

    async def plan(self, png, ctx, validate):
        raw = await self._recover(ctx)
        if raw is not None:
            return validate(raw), {"latency": 0.01, "attempts": 1, "rejected": 0}
        steps = [
            lambda: self._click("#ad .x"), lambda: self._click("#cookie button"), lambda: self._click("text=Click here to register"),
            lambda: self._type("[name=name]", "Ananya Rao"),
            lambda: self._select("[name=dd]", "14"), lambda: self._select("[name=mm]", "08"), lambda: self._select("[name=yyyy]", "2004"),
            lambda: self._type("[name=mobile]", "9876543210", sensitive=True), lambda: self._type("[name=email]", "ananya.rao@example.com"),
            lambda: self._type("[name=aadhaar]", "123412341234", sensitive=True), lambda: self._select("[name=category]", "General"),
            lambda: self._click("button:has-text('Save')"),
            *([] if self.guess_codes else [lambda: self._ask("otp", "Enter the OTP sent to your phone")]),
            lambda: self._type("[name=otp]", "000000" if self.guess_codes else ctx.human_inputs[-1].split(": ", 1)[1], sensitive=True),
            lambda: self._click("button:has-text('Verify')"),
            lambda: self._select("#st", "Karnataka"), lambda: self._select("#ct", "Bengaluru"),
            lambda: self._click("input[name=centre]"), lambda: self._click("button:has-text('Continue')"),
            *([] if self.guess_codes else [lambda: self._ask("captcha", "Please solve the CAPTCHA")]),
            lambda: self._type("[name=cap]", "ZZZZZ" if self.guess_codes else ctx.human_inputs[-1].split(": ", 1)[1], sensitive=True),
            lambda: self._click("button:has-text('Submit')"),
            lambda: self._click("[name=decl]"), lambda: self._click("button:has-text('Proceed')"),
            *([lambda: self._click("button:has-text('Pay Now')", irreversible=True, target="Pay Now")] if self.pay_without_upi else []),
            lambda: self._click("input[value=upi]"),
            *([lambda: self._click("button:has-text('Pay Now')", irreversible=True, target="Pay Now")] if self.pay_without_upi_id else []),
            lambda: self._type("[name=upi]", "ananya@okaxis", sensitive=True),
            lambda: self._click("button:has-text('Pay Now')", irreversible=True, target="Pay Now"),
            lambda: self._done(),
        ]
        raw = await steps[min(self.i, len(steps) - 1)]()
        self.i += 1
        return validate(raw), {"latency": 0.01, "attempts": 1, "rejected": 0}

    async def _ask(self, kind, q):
        return {"action": "ask_human", "kind": kind, "question": q, "rationale": "needs human"}

    async def _done(self):
        ref = await self.page.inner_text("#ref")
        return {"action": "done", "summary": "registered", "reference": ref, "rationale": "receipt visible"}


def human_oracle():
    from portal.app import SMS_INBOX
    log = []

    async def ask(kind, q):
        log.append(kind)
        if kind == "otp":
            return re.match(r"\d+", SMS_INBOX[-1]["text"]).group()
        if kind == "captcha":
            return "K7M2Q"
        if kind == "reconcile":
            return "paid"
        return "yes"
    ask.log = log
    return ask
