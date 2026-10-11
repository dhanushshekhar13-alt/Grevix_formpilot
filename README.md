# FormPilot AI (local-model edition)

Vision agent for messy Indian portals. A **local VLM via Ollama** looks at screenshots and picks one action at a time; **Playwright** executes it. No cloud inference, no API keys.

    observe (screenshot) -> reason (Ollama VLM -> JSON) -> validate (pydantic) -> act (restricted executor) -> verify (did the page change?) -> record

Local model = the *brain* (reads pixels, proposes an action). Playwright = the *hands* (clicks/types). The DOM is used only as evidence (page changed? receipt present?), never to choose targets.

## Setup (Mac, one-time)
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
| commit | Pay Now, Pay Again, Enter in the payment form, any `irreversible` submit | exactly once; human first if configured |

Commits are declared per task (`commits` in `tasks/*.json`: `label_regex`, `confirmed_regex`, `rejected_regex`, optional `human_first`; default human-first = not mock).
- **Write-ahead ledger** (`runs/<id>/ledger.json`): `intent` is fsynced *before* the click, then `dispatched`, then `confirmed` / `rejected` only on evidence on screen, otherwise `in_doubt`. A timed-out click is `in_doubt`, never "failed".
- **No repeat while in doubt**: a commit that is `in_doubt` or `confirmed` blocks any repeat (`blocked`, not executed). A repeated attempt escalates to the human: "it went through" / "not charged: allow one retry". Only a provable rejection (e.g. the portal's validation error) allows a retry on its own.
- **Reconciliation**: every step re-checks open commits against the page (receipt, "Payment received"), so the agent finds out what really happened.
- **Resume from where it stands**: each step writes `checkpoint.json` + browser `storage_state.json` (cookies, mode 600). Dashboard **Resume**, or `run_task(..., resume=load_checkpoint(dir))`, reopens the last URL with the same session and the carried-over ledger. It does not replay steps, so there is no second OTP/CAPTCHA and no second payment.
- **Honest trace**: each step records `effect`, `claimed_target` (model) vs `observed_target` (DOM), `dispatched`, and `outcome` (`verified`/`mismatch` for typed fields, `changed`/`no_effect`, `confirmed`/`rejected`/`in_doubt`/`blocked`). `summary.json` carries the ledger; runs that end with an open commit say so.

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
