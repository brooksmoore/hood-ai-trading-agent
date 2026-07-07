#!/bin/bash
# OOS paper runner — called by launchd on market days.
cd /Users/brooksmoore/Desktop/hood_agent_1

# Load secrets: .env (project) takes priority, then ~/.zshenv.
# Use || true so a zsh-specific line in .zshenv never aborts this bash script.
if [ -f .env ]; then
    set -a; source .env 2>/dev/null || true; set +a
elif [ -f "$HOME/.zshenv" ]; then
    source "$HOME/.zshenv" 2>/dev/null || true
fi

echo "[$(date)] oos_runner start key=$([ -n "$ANTHROPIC_API_KEY" ] && echo SET || echo MISSING)" \
    >> logs/oos_run.log 2>&1

PYTHONPATH=. .venv/bin/python3 run_paper.py \
    --real \
    --feed dynamic \
    --market-days-only \
    --daily-usd-cap 0.10 \
    --max-cycles 300 \
    --hold-hours 24 \
    --data-dir data_real \
    --source yahoo \
    >> logs/oos_run.log 2>&1
RUN_EXIT=$?

PYTHONPATH=. .venv/bin/python3 notify_if_trade.py >> logs/oos_run.log 2>&1
NOTIFY_EXIT=$?

echo "[$(date)] run_paper_exit=$RUN_EXIT notify_exit=$NOTIFY_EXIT" >> logs/oos_run.log 2>&1
exit 0
