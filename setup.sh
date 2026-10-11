#!/usr/bin/env bash
# One-command setup and start for FormPilot (macOS or Linux). Safe to run again: it only does what is missing.
#   ./setup.sh              install everything that is missing, then start the dashboard
#   ./setup.sh --no-start   install only
#   ./setup.sh --skip-model do everything except downloading the AI model (the scripted demo and tests still work)
# It will: install Ollama (if missing) -> start it -> create .venv and install the Python packages and Chromium
#          -> create .env -> download the vision model (several GB, once) -> start the portal and dashboard.
set -euo pipefail
cd "$(dirname "$0")"

START=1; PULL=1
for a in "$@"; do
  case "$a" in
    --no-start) START=0 ;;
    --skip-model) PULL=0 ;;
    -h|--help) sed -n 2,7p "$0"; exit 0 ;;
    *) echo "Unknown option: $a (try --help)"; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m  %s\n' "$*"; }
die()  { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -f .env ] || cp .env.example .env
_model="${FORMPILOT_MODEL:-}"; _base="${OLLAMA_BASE_URL:-}"   # a value given on the command line beats .env
set -a; . ./.env; set +a
[ -z "$_model" ] || FORMPILOT_MODEL="$_model"; [ -z "$_base" ] || OLLAMA_BASE_URL="$_base"
MODEL="${FORMPILOT_MODEL:-qwen3-vl:4b-instruct}"
BASE="${OLLAMA_BASE_URL:-http://localhost:11434}"
VENV="${FORMPILOT_VENV:-.venv}"
OS="$(uname -s)"

# ---- 1. Python 3 --------------------------------------------------------------------------------------------
say "1/5 Python"
PY=""
for c in python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then PY="$c"; break; fi
done
[ -n "$PY" ] || die "Python 3.9 or newer is required. macOS: brew install python   Linux: sudo apt install python3 python3-venv"
ok "$($PY --version) ($PY)"

# ---- 2. Ollama ----------------------------------------------------------------------------------------------
say "2/5 Ollama (runs the AI model on this computer)"
if ! command -v ollama >/dev/null 2>&1; then
  if [ "$OS" = "Darwin" ]; then
    command -v brew >/dev/null 2>&1 || die "Ollama is not installed and Homebrew is not available. Install Ollama from https://ollama.com/download (drag it to Applications, open it once), then run ./setup.sh again."
    echo "    installing Ollama with Homebrew..."
    brew install ollama
  elif [ "$OS" = "Linux" ]; then
    echo "    installing Ollama with its official installer (https://ollama.com/install.sh; may ask for your password)..."
    curl -fsSL https://ollama.com/install.sh | sh
  else
    die "Unsupported system '$OS'. Install Ollama from https://ollama.com/download and run ./setup.sh again."
  fi
fi
ok "$(ollama --version 2>&1 | tail -1)"

if ! curl -sf -m 3 "$BASE/api/tags" >/dev/null 2>&1; then
  echo "    starting Ollama in the background..."
  nohup ollama serve >"${TMPDIR:-/tmp}/formpilot_ollama.log" 2>&1 &
  for _ in $(seq 1 40); do curl -sf -m 2 "$BASE/api/tags" >/dev/null 2>&1 && break; sleep 1; done
  curl -sf -m 3 "$BASE/api/tags" >/dev/null 2>&1 || die "Ollama did not start. Open the Ollama app, or run 'ollama serve' in another terminal, then run ./setup.sh again. (log: ${TMPDIR:-/tmp}/formpilot_ollama.log)"
fi
ok "Ollama is running at $BASE"

# ---- 3. Python packages and browser -------------------------------------------------------------------------
say "3/5 Python packages and Chromium (the browser the agent controls)"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV" || die "could not create a virtual environment (Linux: sudo apt install python3-venv)"
"$VENV/bin/python" -m pip install -q --disable-pip-version-check -r requirements.txt
ok "packages installed in $VENV"
"$VENV/bin/python" -m playwright install chromium >/dev/null
ok "Chromium ready"

# ---- 4. Model -----------------------------------------------------------------------------------------------
say "4/5 AI model: $MODEL"
have_model() { curl -sf -m 5 "$BASE/api/tags" | grep -qF "\"$MODEL\"" || curl -sf -m 5 "$BASE/api/tags" | grep -qF "\"$MODEL:latest\""; }
if have_model; then
  ok "already downloaded"
elif [ "$PULL" = 0 ]; then
  echo "    skipped (--skip-model). The agent cannot run until you do: ollama pull $MODEL"
else
  FREE_GB=$(df -Pk . | awk 'NR==2 {printf "%d", $4/1048576}')
  [ "$FREE_GB" -ge 6 ] || die "only ${FREE_GB} GB of free disk space; the model needs about 6 GB. Free some space and run ./setup.sh again."
  echo "    downloading (several GB, one time only; you can leave it running)..."
  ollama pull "$MODEL" || die "model download failed. Check the model name (FORMPILOT_MODEL in .env) and your internet connection, then run ./setup.sh again; a partly finished download resumes."
  have_model || die "the model '$MODEL' is still not listed by Ollama after the download."
  ok "downloaded"
fi
if have_model; then  # load it into memory now so the first agent step is not slow
  echo "    warming the model up..."
  curl -sf -m 180 "$BASE/api/generate" -d "{\"model\":\"$MODEL\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":\"30m\",\"options\":{\"num_predict\":1}}" >/dev/null 2>&1 \
    && ok "model loaded" || echo "    (warm-up skipped; the first step may be slow)"
fi

# ---- 5. Start -----------------------------------------------------------------------------------------------
say "5/5 Ready"
echo "    Dashboard:   http://localhost:8000"
echo "    Mock portal: http://localhost:5050"
echo "    Proof demo:  $VENV/bin/python scripts/demo_challenge.py      (about 1 minute, no model needed)"
echo "    Tests:       $VENV/bin/python -m pytest -q                   (about 3 minutes, no model needed)"
if [ "$START" = 0 ]; then echo "    Setup finished. Start any time with: ./run.sh"; exit 0; fi

echo "    Starting now. Press Ctrl+C to stop."
[ "${FORMPILOT_NO_BROWSER:-0}" = 1 ] || ( sleep 4; { command -v open >/dev/null 2>&1 && open "http://localhost:8000"; } || { command -v xdg-open >/dev/null 2>&1 && xdg-open "http://localhost:8000"; } || true ) >/dev/null 2>&1 &
exec ./run.sh
