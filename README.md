# FormPilot AI (local-model edition)

Vision agent for messy Indian portals. A **local VLM via Ollama** looks at screenshots and picks one action at a time; **Playwright** executes it. No cloud inference, no API keys.

    observe (screenshot) -> reason (Ollama VLM -> JSON) -> validate (pydantic) -> act (restricted executor) -> verify (did the page change?) -> record

Local model = the *brain* (reads pixels, proposes an action). Playwright = the *hands* (clicks/types). The DOM is used only as evidence (page changed? receipt present?), never to choose targets.

## Quick start (one command)
    git clone https://github.com/dhanushshekhar13-alt/formpilot && cd formpilot
    ./setup.sh
It installs whatever is missing (Ollama, a Python environment, the packages, Chromium), downloads the vision model once (several GB, so the first run takes a while), then starts the portal and dashboard and opens http://localhost:8000. Safe to run again. Options: `--no-start` (install only), `--skip-model` (everything except the model download; the scripted demo and tests still work), `FORMPILOT_NO_BROWSER=1` (don't open a browser tab). Needs Python 3.9+, about 6 GB of free disk, and Homebrew (macOS) or `curl` and sudo rights (Linux) only if Ollama is not yet installed.

## Demo video and guide
- [FormPilot_Demo.mp4](FormPilot_Demo.mp4): 12 test cases, each with its problem, how FormPilot handles it, and the result (the normal path, three failures injected at the worst moment, six safety guards, a session with the real local model, and the test suite). Cases 1 to 10 use the scripted driver and are labelled so; case 11 is a recorded real-model session, sped up.
- [FormPilot_Project_Guide.pdf](FormPilot_Project_Guide.pdf): the workflow, every file, the terms in plain words, how to run and test it.

## Feedback that changed the project
The project was posted on r/LocalLLM ([post](screenshots/reddit_1_post.png)). A commenter asked where it would break on a real government site, whether the approval screen is enough, and suggested a spending limit so the agent cannot pay more than a set amount ([comment](screenshots/reddit_2_feedback.png)). Built in response, shown here on the dashboard with the real local model:

| change | what it does | screenshot |
|---|---|---|
| Hard spending limit | the amount is read from the page; over the limit nothing is clicked and the run stops | [limit blocks the payment](screenshots/2_spending_limit_blocks_payment.png) |
| Dry run | everything for real up to the irreversible click, then stop with nothing sent; Resume continues for real | [dry run stops before Pay](screenshots/1_dry_run_stops_before_pay.png) |
| Approval for any irreversible step | final submit, delete, cancel and the like are treated like a payment; the approval screen also shows the limit | [approval with limit](screenshots/3_approval_with_limit.png), [resumed and paid once](screenshots/4_resumed_and_paid_once.png) |

The limit is stricter than what was asked for: over it there is no second approval to click through, the run just stops. The real-site question is not solved: this is still tested only on the mock portal.

## Setup by hand (Mac, one-time)
    brew install ollama            # then open the Ollama app, or run: ollama serve
    ollama pull qwen3-vl:4b-instruct   # several GB. Optional: qwen3-vl:8b-instruct
                                       # (use -instruct: plain qwen3-vl:4b is a thinking model that ignores think:false and never answers)
    cd ~/formpilot && cp .env.example .env
    ./run.sh                       # portal :5050, dashboard :8000 (checks Ollama + model, warns, never silently downloads)

Open http://localhost:8000 -> pick a task -> **Run agent**. **Stop** is the emergency stop.
The dashboard works without the model; the status line shows what's missing and the agent refuses to start until it's ready.

## Config (.env; all optional)
| var | default | |
|---|---|---|
| OLLAMA_BASE_URL | http://localhost:11434 | |
| FORMPILOT_MODEL | qwen3-vl:4b-instruct | any Ollama vision model (non-thinking) |
| FORMPILOT_FAULT | (empty) | `crash_after_commit` = demo: agent dies right after an irreversible click |
| FORMPILOT_COORD_MODE | norm1000 for qwen3-vl, else pixels | how the model expresses x,y |
| FORMPILOT_MAX_STEPS | 60 | |
| FORMPILOT_APPROVE_COMMITS | task default | 1 = ask the human before every irreversible action |
| FORMPILOT_PRESUBMIT_CHECK | 1 | hold a submit while its form has required fields empty |
| FORMPILOT_DRY_RUN | 0 | 1 = stop right before the first irreversible action, sending nothing |
| FORMPILOT_MAX_AMOUNT | (none) | spending limit in rupees for every irreversible action |
| FORMPILOT_ACTION_TIMEOUT | 10 | seconds per browser action |
| FORMPILOT_REQUEST_TIMEOUT / _MODEL_RETRIES | 120 / 2 | per model call / extra tries on bad output |
| FORMPILOT_HEADED | 0 | 1 = show the real browser window |
| FORMPILOT_RECORD_VIDEO | 1 | 0 for sensitive demos (frames still saved) |

## Changing things on hackathon day (no code edits needed)
1. **New portal / task**: copy `tasks/sbee_exam.json` -> `tasks/<name>.json`. Set `url`, `task`, `profile`, `hints` (portal quirks in plain English), `success_regex` (text that proves completion; the agent can't claim `done` without it), `sensitive_keys`, `mock`. It appears in the dashboard dropdown. URL/task can also be overridden live in the UI.
2. **Model / behaviour**: change `FORMPILOT_MODEL` (+ `FORMPILOT_COORD_MODE` if clicks land off-target), and/or edit `prompts/system.md` (re-read on every step).

## Consequences, recovery, and an honest trace ("the click you can't undo")
Every action is classified by what is **actually under the pointer** (DOM probe), not by the model's self-report:

| class | examples | policy |
|---|---|---|
| observe | scroll, wait, screenshot | retried freely |
| local | type, select, tick a box, Tab | safe to redo (auto-retried once on a browser error) |
| navigate | Continue, Verify, links | redo only after re-reading the screen |
| commit | Pay Now, Pay Again, Enter in the payment form, any `irreversible` submit | exactly once; human approves first (dashboard tick box, on by default) |

Commits are declared per task (`commits` in `tasks/*.json`: `label_regex`, `confirmed_regex`, `rejected_regex`, optional `human_first`, `max_amount`, `amount_regex`; default human-first = not mock). A button is also a commit when its own label says so, whatever the task file or the model says: final submit, submit application, lock, delete, withdraw, cancel application/registration/booking, place order, pay (`IRREVERSIBLE_LABELS` in `agent/consequence.py`). So approval, the ledger and the no-repeat rule cover every irreversible step, not only payments.
- **Write-ahead ledger** (`runs/<id>/ledger.json`): `intent` is fsynced *before* the click, then `dispatched`, then `confirmed` / `rejected` only on evidence on screen, otherwise `in_doubt`. A timed-out click is `in_doubt`, never "failed".
- **No repeat while in doubt**: a commit that is `in_doubt` or `confirmed` blocks any repeat (`blocked`, not executed). A repeated attempt escalates to the human: "it went through" / "not charged: allow one retry". Only a provable rejection (e.g. the portal's validation error) allows a retry on its own.
- **Reconciliation**: every step re-checks open commits against the page (receipt, "Payment received"), so the agent finds out what really happened.
- **Resume from where it stands**: each step writes `checkpoint.json` + browser `storage_state.json` (cookies, mode 600). Dashboard **Resume**, or `run_task(..., resume=load_checkpoint(dir))`, reopens the last URL with the same session and the carried-over ledger. It does not replay steps, so there is no second OTP/CAPTCHA and no second payment.
- **Human approves first**: before an irreversible click the agent stops and shows what it is about to do, read from the page rather than from the model: the button, the amounts on screen, and what the form will send (secrets masked), with the screenshot. Deny stops the run with nothing sent. On by default in the dashboard; `FORMPILOT_APPROVE_COMMITS=1` elsewhere (default: on for non-mock tasks). The trace keeps what was shown (`approval_shown`).
- **Spending limit (hard)**: `max_amount` on a commit in the task file caps what may be paid. The amount is read from the page (every rupee figure, summed; or `amount_regex` to pinpoint it). Over the cap, or no readable amount at all, means the click is not made, nothing is logged as an intent, and the run stops (`over_limit`). It is not offered for approval: the limit is not something to click through. The dashboard's "Spending limit" box (or `FORMPILOT_MAX_AMOUNT`) adds a limit for one run; the lower of the two wins.
- **Dry run**: tick "Dry run" (or `FORMPILOT_DRY_RUN=1`). The agent does everything for real up to the first irreversible action, including OTP, CAPTCHA and the completeness check, then stops without clicking it and reports what it would have sent (`dry_run`). Nothing is charged. **Resume** continues the same run for real from that page, with no second OTP or CAPTCHA.
- **Complete before submit**: a submit whose form still has a required field empty, or a radio group with nothing selected, is not clicked (`held`), and the model is told exactly what is missing. This runs before any intent is logged, so a doomed Pay click never becomes an irreversible action of unknown outcome. After two holds for the same gap the click goes through and the portal decides. `FORMPILOT_PRESUBMIT_CHECK=0` turns it off.
- **Honest trace**: each step records `effect`, `claimed_target` (model) vs `observed_target` (DOM), `dispatched`, and `outcome` (`verified`/`mismatch` for typed fields, `changed`/`no_effect`, `held`, `over_limit`, `dry_run`, `confirmed`/`rejected`/`in_doubt`/`blocked`). `summary.json` carries the ledger; runs that end with an open commit say so.

**Failure injection** (dashboard dropdown, or `FORMPILOT_FAULT=crash_after_commit` / `GET :5050/__fault?mode=expire_after_pay`):
- *session expires right after payment*: the portal takes the money, kills the session, and after re-login shows a Payment Status page the agent has never seen, with a **Pay Again** trap.
- *agent crashes right after clicking Pay*: the process dies before seeing the result (ledger left at `intent`). Then press **Resume**.

Proof: `python scripts/demo_challenge.py` (add `--planner ollama` for the VLM). It runs the happy path and every fault and checks the portal's own charge ledger (`:5050/__ledger`) shows exactly one charge. Tests: `tests/test_consequence.py`.

## Layout
- `agent/vision.py` Ollama client (structured JSON output, bounded retries, health checks)
- `agent/actions.py` strict action schema, coordinate validation, restricted executor (no eval/shell/JS)
- `agent/consequence.py` action classes, DOM probe, commit specs, write-ahead ledger
- `agent/agent.py` the loop: verification, reconciliation, loop detection, human checkpoints, checkpoints/resume, traces
- `agent/config.py` env settings + task specs; `tasks/` task files; `prompts/system.md` system prompt
- `portal/app.py` mock exam portal; `server.py` + `ui/index.html` dashboard
- `scripts/benchmark.py`, `scripts/export_video.py`; `tests/`

## Safety
Pauses for a human on OTP, CAPTCHA, missing info, stuck loops, and (non-mock tasks) any click the model marks irreversible. Never guesses OTP/CAPTCHA: this is enforced in code, not left to the model. Text typed into an OTP or CAPTCHA field must be the human's reply; a model guess is not typed, the human is asked, and the trace records it (`guard`). `done` is rejected unless the page shows the success pattern. Text logs/traces mask sensitive values; **screenshots and video are not redacted** (they show what the page shows) - use `FORMPILOT_RECORD_VIDEO=0` and don't share `runs/` for real data.

## Runs
`runs/<id>/`: `trace.jsonl` (per step: action JSON, result, verification, latency), `summary.json` (metrics + config), `frames/NNN_before|after.jpg`, `video/*.webm`. MP4 from frames: `python scripts/export_video.py runs/<id>` (needs `brew install ffmpeg`).

## Tests & benchmark
    .venv/bin/python -m pytest -q                       # no model needed
    .venv/bin/python scripts/benchmark.py --models qwen3-vl:4b-instruct,qwen3-vl:8b-instruct --trials 3
`--planner scripted` validates benchmark plumbing only (deterministic DOM-driven stand-in, NOT a model result).

## Troubleshooting
- "Ollama is not running": open the Ollama app / `ollama serve`. - "model not installed": `ollama pull <model>`.
- First step very slow: the model is loading (timeout 120s). Warm it: `ollama run qwen3-vl:4b-instruct "hi"`.
- Clicks consistently off-target: try `FORMPILOT_COORD_MODE=pixels` (or norm1000), or the 8B model.
- Port in use: `lsof -iTCP:8000 -sTCP:LISTEN`. - Chromium rejects some ports (e.g. 5060/5061) as unsafe.
