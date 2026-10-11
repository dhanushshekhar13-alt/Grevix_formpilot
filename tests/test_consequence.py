"""Challenge: the click you can't undo. Failures injected at the worst moment; recovery must never pay twice."""
import json
import re
from pathlib import Path

import pytest

from agent.actions import parse_action
from agent.agent import latest_resumable, load_checkpoint, run_task
from agent.consequence import (COMMIT, CONFIRMED, IN_DOUBT, INTENT, LOCAL, NAVIGATE, OBSERVE, REJECTED, CommitSpec, Ledger,
                               classify)
from tests.helpers import ScriptedPlanner, SeqPlanner, human_oracle

PAY = CommitSpec("fee_payment", r"\b(pay now|pay again)\b", r"Reference No:\s*(\w+)|Payment received", r"Select UPI")


@pytest.fixture
def portal(portal_url):
    import portal.app as p
    p.LEDGER.clear()
    p.FAULT["mode"] = ""
    yield p
    p.FAULT["mode"] = ""


def charges(p):
    return len(p.LEDGER)


def trace(res):
    return [json.loads(line) for line in (Path(res["run_dir"]) / "trace.jsonl").read_text().splitlines()]


def ledger_of(res):
    return [a for a in res["ledger"] if a["name"] == "fee_payment"]


async def test_happy_path_pays_exactly_once_and_trace_is_honest(settings, spec, sink, portal):
    res = await run_task(spec, sink, human_oracle(), settings, planner=ScriptedPlanner())
    assert res["status"] == "success" and re.fullmatch(r"SBEE26\d{8}", res["reference"])
    assert charges(portal) == 1
    [a] = ledger_of(res)
    assert a["status"] == CONFIRMED and a["reference"] == res["reference"]
    assert [h[0] for h in a["history"]] == ["intent", "dispatched", "confirmed"]
    steps = [r for r in trace(res) if r.get("type") == "step"]
    assert all("effect" in r and "outcome" in r for r in steps)
    pay = [r for r in steps if r.get("commit") == "fee_payment"]
    assert len(pay) == 1 and pay[0]["outcome"] == CONFIRMED and pay[0]["observed_target"] == "Pay Now"
    typed = [r for r in steps if r["action"]["action"] == "type"]
    assert typed and all(r["outcome"] == "verified" for r in typed)
    assert res["metrics"]["duplicate_commits_blocked"] == 0 and "unresolved_commits" not in res


async def test_guessed_otp_and_captcha_are_replaced_by_the_humans(settings, spec, sink, portal):
    human = human_oracle()
    res = await run_task(spec, sink, human, settings, planner=ScriptedPlanner(guess_codes=True))
    assert res["status"] == "success", res
    assert charges(portal) == 1 and human.log.count("otp") == 1 and human.log.count("captcha") == 1
    guarded = [r for r in trace(res) if r.get("guard")]
    assert len(guarded) == 2 and all(r["outcome"] == "verified" and r["action"]["text"] == "•••" for r in guarded)
    assert "000000" not in (Path(res["run_dir"]) / "trace.jsonl").read_text()


async def test_session_expires_right_after_payment_recovers_without_paying_twice(settings, spec, sink, portal):
    portal.FAULT["mode"] = "expire_after_pay"
    planner = ScriptedPlanner(dup_attempts=1)  # tries "Pay Again" once, like an eager model would
    res = await run_task(spec, sink, human_oracle(), settings, planner=planner)
    assert res["status"] == "success", res
    assert charges(portal) == 1
    assert {"/expired", "/resume", "/pay-status"} <= set(planner.seen)
    [a] = ledger_of(res)
    assert [h[0] for h in a["history"]] == ["intent", "dispatched", "in_doubt", "confirmed"]
    t = trace(res)
    blocked = [r for r in t if r.get("outcome") == "blocked"]
    assert len(blocked) == 1 and blocked[0]["observed_target"] == "Pay Again" and blocked[0]["dispatched"] is False
    assert any(r.get("type") == "reconcile" and r["status"] == CONFIRMED for r in t)
    assert res["metrics"]["duplicate_commits_blocked"] == 1 and res["metrics"]["in_doubt_events"] == 1


async def test_persistent_duplicate_is_escalated_to_human_not_executed(settings, spec, sink, portal):
    portal.FAULT["mode"] = "expire_after_pay"
    human = human_oracle()
    res = await run_task(spec, sink, human, settings, planner=ScriptedPlanner(dup_attempts=2))
    assert res["status"] == "success", res
    assert charges(portal) == 1
    assert human.log == ["otp", "captcha", "reconcile"]
    assert ledger_of(res)[0]["status"] == "human_confirmed"


async def test_agent_crash_after_pay_click_then_resume_from_where_it_stands(settings, spec, sink, portal):
    settings.fault = "crash_after_commit"
    res1 = await run_task(spec, sink, human_oracle(), settings, planner=ScriptedPlanner())
    assert res1["status"] == "interrupted" and "injected crash" in res1["summary"]
    assert charges(portal) == 1  # the money moved before the agent died
    cp = load_checkpoint(res1["run_dir"])
    assert cp["status"] == "interrupted" and cp["ledger"][0]["status"] == INTENT  # nothing written after the click
    assert latest_resumable(settings.runs_dir)["run_id"] == res1["run_id"]

    settings.fault = ""
    human = human_oracle()
    planner = ScriptedPlanner(dup_attempts=1)
    res2 = await run_task(spec, sink, human, settings, planner=planner, resume=cp)
    assert res2["status"] == "success", res2
    assert charges(portal) == 1
    assert human.log == []  # no OTP / CAPTCHA again: it did not restart from scratch
    assert planner.seen[0] == "/pay"  # picked up on the page it was on, with its session
    [a] = ledger_of(res2)
    assert [h[0] for h in a["history"]] == ["intent", "in_doubt", "confirmed"]
    t = trace(res2)
    assert t[1]["type"] == "resume" and t[1]["from_run"] == res1["run_id"]
    assert min(r["step"] for r in t if r.get("type") == "step") == cp["step"] + 1
    assert any(r.get("outcome") == "blocked" for r in t)
    assert latest_resumable(settings.runs_dir) is None


async def test_crash_and_session_expiry_together(settings, spec, sink, portal):
    portal.FAULT["mode"] = "expire_after_pay"
    settings.fault = "crash_after_commit"
    res1 = await run_task(spec, sink, human_oracle(), settings, planner=ScriptedPlanner())
    assert res1["status"] == "interrupted" and charges(portal) == 1
    settings.fault = ""
    planner = ScriptedPlanner(dup_attempts=1)
    res2 = await run_task(spec, sink, human_oracle(), settings, planner=planner, resume=load_checkpoint(res1["run_dir"]))
    assert res2["status"] == "success", res2
    assert charges(portal) == 1
    assert planner.seen[:2] == ["/expired", "/resume"]  # replaying the old cookie does not revive an expired session


async def test_incomplete_payment_form_is_held_before_any_intent_is_logged(settings, spec, sink, portal):
    res = await run_task(spec, sink, human_oracle(), settings, planner=ScriptedPlanner(pay_without_upi=True))
    assert res["status"] == "success", res
    assert charges(portal) == 1 and res["metrics"]["submits_held"] == 1
    [a] = ledger_of(res)  # the held click never became an irreversible attempt
    assert [h[0] for h in a["history"]] == ["intent", "dispatched", "confirmed"]
    [held] = [r for r in trace(res) if r.get("outcome") == "held"]
    assert held["dispatched"] is False and "radio buttons" in held["result"] and "attempt" not in held


async def test_irreversible_click_is_held_for_any_empty_box_and_never_offered_for_approval(settings, spec, sink, portal):
    settings.approve_commits = True
    human = human_oracle()
    res = await run_task(spec, sink, human, settings, planner=ScriptedPlanner(pay_without_upi_id=True))
    assert res["status"] == "success" and charges(portal) == 1 and human.log.count("confirm") == 1
    [held] = [r for r in trace(res) if r.get("outcome") == "held"]
    assert "text box 'name@bank' is empty" in held["result"] and "approval_shown" not in held
    [a] = ledger_of(res)
    assert [h[0] for h in a["history"]] == ["intent", "dispatched", "confirmed"]


async def test_human_approves_payment_with_facts_read_from_the_page(settings, spec, sink, portal):
    settings.approve_commits = True
    human = human_oracle()
    res = await run_task(spec, sink, human, settings, planner=ScriptedPlanner())
    assert res["status"] == "success" and charges(portal) == 1 and human.log.count("confirm") == 1
    [ask] = [e for e in sink.of("need_human") if e["kind"] == "confirm"]
    d = ask["details"]
    assert d["button"] == "Pay Now" and any("800" in a for a in d["amounts"]) and ["selected option", "UPI"] in d["fields"]
    assert "ananya@okaxis" not in json.dumps(d)  # the UPI ID is a secret: masked on the approval screen and in the trace
    [pay] = [r for r in trace(res) if r.get("commit") == "fee_payment"]
    assert pay["approval_shown"] == d and pay["human"]["answer"] == "yes"


async def test_declined_payment_is_never_dispatched(settings, spec, sink, portal):
    settings.approve_commits = True
    base = human_oracle()

    async def human(kind, q):
        return "no" if kind == "confirm" else await base(kind, q)
    res = await run_task(spec, sink, human, settings, planner=ScriptedPlanner())
    assert res["status"] == "stopped" and charges(portal) == 0 and res["ledger"] == []


async def test_payment_over_the_spending_limit_is_never_sent(settings, spec, sink, portal):
    spec.commits[0]["max_amount"] = 500  # the portal asks for Rs. 800 + Rs. 18
    settings.approve_commits = True
    human = human_oracle()
    res = await run_task(spec, sink, human, settings, planner=ScriptedPlanner())
    assert res["status"] == "stopped" and "over the limit of Rs. 500" in res["summary"] and "800 + 18 = 818" in res["summary"]
    assert charges(portal) == 0 and res["ledger"] == [] and "confirm" not in human.log  # not even offered for approval
    [stop] = [r for r in trace(res) if r.get("outcome") == "over_limit"]
    assert stop["dispatched"] is False and stop["amount_seen"] == [800.0, 18.0] and "attempt" not in stop


async def test_run_wide_limit_applies_on_top_of_the_task_cap(settings, spec, sink, portal):
    settings.max_amount = 817  # set from the dashboard; lower than the task's own cap of 1000
    res = await run_task(spec, sink, human_oracle(), settings, planner=ScriptedPlanner())
    assert res["status"] == "stopped" and "over the limit of Rs. 817" in res["summary"] and charges(portal) == 0


async def test_capped_payment_is_refused_when_no_amount_can_be_read(settings, spec, sink, portal):
    # the landing page shows no amount at all: with a cap in the task, an unreadable amount is not "probably fine"
    res = await run_task(spec, sink, human_oracle(), settings,
                         planner=SeqPlanner([{"action": "click", "x": 100, "y": 100, "irreversible": True, "target": "Pay Now"}]))
    assert res["status"] == "stopped" and "no amount can be read" in res["summary"] and res["ledger"] == []


async def test_dry_run_fills_everything_stops_before_pay_then_resumes_for_real(settings, spec, sink, portal):
    settings.dry_run = True
    settings.approve_commits = True
    human = human_oracle()
    res = await run_task(spec, sink, human, settings, planner=ScriptedPlanner())
    assert res["status"] == "dry_run" and "NOT clicked" in res["summary"] and "Rs. 800" in res["summary"]
    assert charges(portal) == 0 and res["ledger"] == [] and "confirm" not in human.log
    [last] = [r for r in trace(res) if r.get("outcome") == "dry_run"]
    assert last["dispatched"] is False and last["observed_target"] == "Pay Now" and "attempt" not in last
    assert ["selected option", "UPI"] in last["would_send"]["fields"] and last["would_send"]["total"] == 818
    assert not [r for r in trace(res) if r.get("form_gaps")]  # it stopped with the form complete, not on a half-filled one
    # the same run, continued for real: no second OTP or CAPTCHA, the human approves, exactly one charge
    cp = latest_resumable(settings.runs_dir, spec.name)
    assert cp["run_id"] == res["run_id"]
    settings.dry_run = False
    planner = ScriptedPlanner()
    planner.i = 24  # the fee page: choose UPI, type the UPI ID, Pay Now
    res2 = await run_task(spec, sink, human, settings, planner=planner, resume=cp)
    assert res2["status"] == "success" and charges(portal) == 1
    assert human.log == ["otp", "captcha", "confirm"]


FINAL = ("data:text/html,<h3>Review your application</h3><button type='button' style='position:absolute;left:40px;top:120px;width:160px;height:40px' "
         "onclick=\"document.body.innerHTML='<h3>Application Submitted. Ref: AB12345678</h3>'\">{label}</button>")


async def test_any_irreversible_button_needs_approval_not_only_payments(settings, spec, sink):
    spec.url, spec.commits, spec.success_regex = FINAL.format(label="Final Submit"), [], r"Application Submitted\. Ref: ([A-Z0-9]{8,})"
    settings.approve_commits = True
    human = human_oracle()
    # the model does not flag it and the task does not declare it: the button's own label is enough
    res = await run_task(spec, sink, human, settings, planner=SeqPlanner([
        {"action": "click", "x": 120, "y": 140, "target": "next"}, {"action": "done", "summary": "submitted", "reference": "AB12345678"}]))
    assert res["status"] == "success" and human.log == ["confirm"]
    [a] = res["ledger"]
    assert a["name"] == "irreversible: Final Submit" and [h[0] for h in a["history"]] == ["intent", "dispatched", "confirmed"]
    [step] = [r for r in trace(res) if r.get("commit")]
    assert step["effect"] == COMMIT and step["approval_shown"]["button"] == "Final Submit" and step["claimed_target"] == "next"


async def test_denied_irreversible_button_is_never_clicked(settings, spec, sink):
    spec.url, spec.commits = FINAL.format(label="Cancel Registration"), []
    settings.approve_commits = True

    async def deny(kind, q):
        return "no"
    res = await run_task(spec, sink, deny, settings, planner=SeqPlanner([{"action": "click", "x": 120, "y": 140}]))
    assert res["status"] == "stopped" and "declined" in res["summary"] and res["ledger"] == []
    assert res["metrics"]["commits_dispatched"] == 0 and res["metrics"]["actions_executed"] == 0


async def test_provably_rejected_payment_may_be_retried(settings, spec, sink, portal):
    settings.presubmit_check = False  # let the incomplete form through, so the portal itself rejects it
    res = await run_task(spec, sink, human_oracle(), settings, planner=ScriptedPlanner(pay_without_upi=True))
    assert res["status"] == "success", res
    assert charges(portal) == 1
    first, second = ledger_of(res)
    assert first["status"] == REJECTED and second["status"] == CONFIRMED
    assert res["metrics"]["duplicate_commits_blocked"] == 0


def _act(raw):
    return parse_action(raw, 1280, 800, "pixels")


@pytest.mark.parametrize("raw,probe,want", [
    ({"action": "scroll", "dy": 300}, {}, OBSERVE),
    ({"action": "click", "x": 5, "y": 5}, {"actionable": True, "submits": False, "tag": "BUTTON", "label": "Delete application"}, COMMIT),
    ({"action": "click", "x": 5, "y": 5}, {"actionable": True, "submits": True, "label": "Proceed to Payment"}, NAVIGATE),
    ({"action": "click", "x": 5, "y": 5}, {"actionable": True, "field": True, "tag": "INPUT", "label": "delete"}, LOCAL),
    ({"action": "type", "text": "x", "x": 5, "y": 5}, {}, LOCAL),
    ({"action": "press", "key": "Tab"}, {}, LOCAL),
    # the model's words say "pay", but the pointer is on a radio button: not a payment
    ({"action": "click", "x": 5, "y": 5, "target": "pay via UPI option"}, {"actionable": True, "label": "UPI", "submits": False}, LOCAL),
    ({"action": "click", "x": 5, "y": 5, "target": "continue"}, {"actionable": True, "label": "Continue", "submits": True}, NAVIGATE),
    # the model forgot irreversible=true, but the button under the pointer is Pay Now
    ({"action": "click", "x": 5, "y": 5, "target": "submit"}, {"actionable": True, "label": "Pay Now", "submits": True}, COMMIT),
    # Enter inside the UPI field submits the payment form
    ({"action": "press", "key": "Enter"}, {"actionable": True, "label": "Pay Now", "submits": True, "via": "enter"}, COMMIT),
    # nothing actionable found: fall back to the model's words
    ({"action": "click", "x": 5, "y": 5, "target": "Pay Again button"}, {"actionable": False, "label": ""}, COMMIT),
    # flagged irreversible on a submit button with an unknown label: treated as a commit
    ({"action": "click", "x": 5, "y": 5, "irreversible": True}, {"actionable": True, "label": "Submit Application", "submits": True}, COMMIT),
    # flagged irreversible on a non-submitting element: the flag alone does not make it one
    ({"action": "click", "x": 5, "y": 5, "irreversible": True}, {"actionable": True, "label": "I agree", "submits": False}, LOCAL),
])
def test_classification_uses_what_is_really_under_the_pointer(raw, probe, want):
    assert classify(_act(raw), probe, [PAY], None)[0] == want


def test_ledger_blocks_repeat_until_resolved(tmp_path):
    led = Ledger(tmp_path / "l.json")
    a = led.begin(PAY, "Pay Now", "r1", 7)
    assert led.blocking("fee_payment") is a and json.loads((tmp_path / "l.json").read_text())[0]["status"] == INTENT
    led.set(a, IN_DOUBT, "session expired")
    assert led.reconcile("Session Expired. Login again") == [] and led.blocking("fee_payment") is a
    assert led.reconcile("Payment Status ... Payment received.") == [a] and a.status == CONFIRMED
    assert led.blocking("fee_payment") is a  # done: blocked forever
    b = Ledger(tmp_path / "m.json")
    x = b.begin(PAY, "Pay Now", "r1", 3)
    b.reconcile("Select UPI and enter a valid UPI ID")
    assert x.status == REJECTED and b.blocking("fee_payment") is None
    assert "DONE" in led.notes()[0] and "did not go through" in b.notes()[0]
    assert Ledger.from_public(tmp_path / "p.json", led.public()).attempts[0].status == CONFIRMED
