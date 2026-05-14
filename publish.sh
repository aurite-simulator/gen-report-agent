#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${REPORT_REPO_URL:=https://github.com/paulwilcox99/consulting-firm-dashboard.git}"
export REPORT_REPO_URL
exec venv/bin/python3 agents/gen_report/gen_report.py --publish
