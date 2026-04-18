#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# OVERNIGHT SCALPER EVOLUTION — Auto-Resume Runner
# Restarts automatically if process dies (usage limit, crash, etc.)
# Saves state to artifacts/ so progress is never lost
# ═══════════════════════════════════════════════════════════

set -e

cd "$(dirname "$0")/.."
source .venv/bin/activate
export PYTHONPATH=src

LOG_FILE="logs/overnight_runner_$(date +%Y%m%d_%H%M%S).log"
mkdir -p logs artifacts

echo "═══════════════════════════════════════════════════"
echo "  OVERNIGHT SCALPER EVOLUTION — Auto-Resume Mode"
echo "  Target: 50%+ Annual ROI"
echo "  Mode: RESEARCH ONLY — NO LIVE TRADING"
echo "  Log: $LOG_FILE"
echo "  Press Ctrl+C twice to fully stop"
echo "═══════════════════════════════════════════════════"

ATTEMPT=0
MAX_ATTEMPTS=50  # max restarts

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    ATTEMPT=$((ATTEMPT + 1))
    echo ""
    echo "[$(date)] Attempt $ATTEMPT — Starting evolution..." | tee -a "$LOG_FILE"

    # Run the evolution. If it exits (usage limit, crash), loop restarts it
    python -m oto_bot.main evolve \
        --capital 10000 \
        --population 30 \
        --generations 100 \
        --target-roi 0.50 \
        2>&1 | tee -a "$LOG_FILE"

    EXIT_CODE=$?

    # Check if target was hit (report will say target_hit: true)
    if [ -f "artifacts/overnight_evolution_report.json" ]; then
        TARGET_HIT=$(python -c "import json; r=json.load(open('artifacts/overnight_evolution_report.json')); print(r.get('target_hit', False))" 2>/dev/null || echo "False")
        if [ "$TARGET_HIT" = "True" ]; then
            echo ""
            echo "[$(date)] TARGET HIT! 50%+ ROI achieved. Stopping." | tee -a "$LOG_FILE"
            echo "Check artifacts/overnight_evolution_report.json for details."
            break
        fi
    fi

    echo ""
    echo "[$(date)] Process exited (code=$EXIT_CODE). Waiting 30s before restart..." | tee -a "$LOG_FILE"
    sleep 30
done

echo ""
echo "[$(date)] Runner finished after $ATTEMPT attempts." | tee -a "$LOG_FILE"
