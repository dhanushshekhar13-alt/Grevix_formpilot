#!/bin/bash
# Starts the mock portal (:5050) and dashboard (:8000). The dashboard runs even if the local model isn't ready.
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
MODEL="${FORMPILOT_MODEL:-qwen3-vl:4b-instruct}"
BASE="${OLLAMA_BASE_URL:-http://localhost:11434}"

for port in 5050 8000; do
  if lsof -iTCP:$port -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: port $port is already in use. Free it with: lsof -iTCP:$port -sTCP:LISTEN  (then kill the PID)"; exit 1
  fi
done

if ! command -v ollama >/dev/null 2>&1; then
  echo "WARN: Ollama is not installed -> agent cannot run. Install: brew install ollama"
elif ! curl -sf -m 3 "$BASE/api/tags" >/tmp/formpilot_tags.json; then
  echo "WARN: Ollama is not running -> agent cannot run. Start it: ollama serve   (or open the Ollama app)"
elif ! grep -q "\"$MODEL\"" /tmp/formpilot_tags.json && ! grep -q "\"$MODEL:latest\"" /tmp/formpilot_tags.json; then
  echo "WARN: model '$MODEL' is not downloaded -> agent cannot run. Pull it (several GB): ollama pull $MODEL"
else
  echo "OK: Ollama is running and '$MODEL' is available."
fi

source .venv/bin/activate
python portal/app.py &
PORTAL=$!
trap "kill $PORTAL 2>/dev/null" EXIT
sleep 1
kill -0 $PORTAL 2>/dev/null || { echo "ERROR: mock portal failed to start"; exit 1; }
echo "Dashboard: http://localhost:8000   Mock portal: http://localhost:5050"
uvicorn server:app --port 8000
