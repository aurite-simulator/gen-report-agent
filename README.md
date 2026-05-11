# Gen Report Agent

Generates a self-contained HTML dashboard from all simulation outputs — monthly CSV reports, risk alerts, and utilization data — including LLM-authored executive summaries.

## What It Does

Fires on the 1st of each month (at 2am, after reports and utilization have run at midnight). For each run it:

1. Scans `model_data/reports/` for monthly CSV report directories
2. Reads `model_data/risk_alerts.jsonl` and `model_data/utilization/` CSVs for summary charts
3. Reads the most recent markdown files from `model_data/risk_analysis/` and `model_data/utilization_analysis/` for the executive summary
4. Renders everything into a single self-contained HTML file with no external dependencies

The output is a tabbed dashboard with:
- **Summary** — executive summary from the latest LLM reports, revenue/profit charts, risk overview, and workforce utilization trends
- **Monthly tabs** — detailed management tables for each reporting period (BU revenue, fixed-price profitability, project status, consultant hours)
- **Risk Analysis tab** — all weekly risk reports selectable by date
- **Utilization Analysis tab** — all monthly utilization reports selectable by month

## Installation

Clone into the framework's `agents/` directory and run the installer:

```bash
git clone https://github.com/aurite-simulator/gen-report-agent agents/gen_report
bash agents/gen_report/install.sh
```

`install.sh` installs dependencies into the shared virtualenv and appends the monthly cron entry to the model's `crontab` file (idempotent — safe to run multiple times).

## Running

The agent is launched automatically by the simulation's cron worker on the 1st of each month. To run manually at any time:

```bash
source venv/bin/activate
python agents/gen_report/gen_report.py
```

No Redis or database connection required — reads only from `model_data/` files.

## Output

| File | Description |
|------|-------------|
| `model_data/report.html` | Self-contained HTML dashboard (open in any browser) |
