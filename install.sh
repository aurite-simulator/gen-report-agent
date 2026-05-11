#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRAMEWORK_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Install dependencies into the shared venv
if [ -f "$FRAMEWORK_DIR/venv/bin/pip" ]; then
    "$FRAMEWORK_DIR/venv/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"
    echo "[install] dependencies installed"
else
    echo "[install] WARNING: no venv found at $FRAMEWORK_DIR/venv — skipping pip install"
fi

# Find the model's crontab
CRONTAB_FILE=$(find "$FRAMEWORK_DIR/models" -name "crontab" | head -1)
if [ -z "$CRONTAB_FILE" ]; then
    echo "[install] ERROR: no crontab file found under $FRAMEWORK_DIR/models"
    exit 1
fi
echo "[install] found crontab: $CRONTAB_FILE"

CRON_ENTRY='0 2 1 * *     venv/bin/python3 agents/gen_report/gen_report.py'
CRON_COMMENT='# Monthly: regenerate HTML dashboard (after reports and utilization at midnight)'

if grep -qF "gen_report/gen_report.py" "$CRONTAB_FILE"; then
    echo "[install] crontab entry already present — skipping"
else
    printf '\n%s\n%s\n' "$CRON_COMMENT" "$CRON_ENTRY" >> "$CRONTAB_FILE"
    echo "[install] crontab entry added"
fi

echo "[install] gen_report agent installed"
