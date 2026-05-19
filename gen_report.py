#!/usr/bin/env python3
"""Generate a self-contained HTML dashboard from simulation CSV reports."""
import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPORTS_DIR  = Path(__file__).parents[2] / "model_data" / "reports"
RISK_DIR     = Path(__file__).parents[2] / "model_data" / "risk_analysis"
UTIL_DIR     = Path(__file__).parents[2] / "model_data" / "utilization_analysis"
UTIL_CSV_DIR = Path(__file__).parents[2] / "model_data" / "utilization"
RISK_LOG     = Path(__file__).parents[2] / "model_data" / "risk_alerts.jsonl"
OUTPUT_FILE  = Path(__file__).parents[2] / "model_data" / "report.html"

REPORT_GROUPS = [
    ("Business Unit Summary", [
        ("bu_revenue_current_month",             "BU Revenue — Current Month"),
        ("bu_revenue_ytd",                       "BU Revenue — Year to Date"),
        ("bu_revenue_forecast_remainder",        "BU Revenue — Forecast Remainder"),
        ("bu_profit_forecast_eoy",               "BU Profit Forecast — End of Year"),
        ("bu_pl_monthly",                        "BU P&L — Current Month"),
        ("realization_by_bu",                    "Realization Rate by BU"),
        ("dso_by_bu",                            "DSO by BU"),
    ]),
    ("Fixed Price Projects", [
        ("fixed_price_contract_values",          "Contract Values"),
        ("fixed_price_revenue_to_date",          "Revenue to Date"),
        ("fixed_price_costs_to_date",            "Costs to Date"),
        ("fixed_price_expected_costs_at_completion", "Expected Costs at Completion"),
        ("fixed_price_expected_profit",          "Expected Profit"),
    ]),
    ("Project Operations", [
        ("project_status",                       "Project Status"),
        ("project_hours_expenses_to_date",       "Hours & Expenses to Date"),
        ("project_hours_forecast",               "Hours Forecast"),
        ("fully_loaded_margin",                  "Fully Loaded Margin by Project"),
    ]),
    ("Accounts Receivable", [
        ("ar_aging",                             "AR Aging by Client"),
        ("writeoffs_summary",                    "Write-offs — Year to Date"),
    ]),
    ("Clients", [
        ("client_concentration",                 "Client Concentration"),
    ]),
    ("Staff Activity", [
        ("consultant_projects_ytd",              "Consultant Projects — Year to Date"),
    ]),
]

_CURRENCY_KEYWORDS = ("revenue", "cost", "profit", "value", "amount", "expense",
                      "labor", "salary", "margin", "overhead", "ebitda", "writeoff")
_HOURS_KEYWORDS    = ("hours",)
# AR aging bucket columns and AR balance columns — currency, but names don't match keywords.
_CURRENCY_COLUMNS  = frozenset({"current", "30_60", "60_90", "90_120", "120_plus",
                                "total_ar", "open_ar", "open_ar_snapshot"})
_MONTH_ABBR = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
CHART_COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6"]


# ── table helpers ─────────────────────────────────────────────────────────────

def _fmt(col: str, val: str) -> str:
    if not val or not val.strip():
        return ""
    col_l = col.lower()
    try:
        num = float(val)
    except ValueError:
        return val
    if col_l == "pct_complete":
        return f"{num:.1f}%"
    if col_l == "slip_factor":
        return f"{num:.3f}×"
    if col_l == "dso_days":
        return f"{num:,.1f} days"
    if col_l.endswith("_pct") or col_l.endswith("_rate") or col_l.startswith("share_"):
        return f"{num * 100:.2f}%"
    if any(kw in col_l for kw in _HOURS_KEYWORDS):
        return f"{num:,.1f}"
    if col_l in _CURRENCY_COLUMNS or any(kw in col_l for kw in _CURRENCY_KEYWORDS):
        return f"${num:,.2f}"
    if num == int(num):
        return str(int(num))
    return f"{num:,.4f}"


def _cell_class(col: str, val: str) -> str:
    col_l = col.lower()
    try:
        num = float(val)
    except (ValueError, TypeError):
        return ""
    if "profit" in col_l:
        return "positive" if num >= 0 else "negative"
    if col_l == "pct_complete":
        if num >= 100: return "complete"
        if num >= 50:  return "mid"
        return "low"
    if col_l == "slip_factor":
        if num <= 1.0: return "complete"
        if num <= 1.2: return "mid"
        return "negative"
    return ""


def _load_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    return (rows[0], rows[1:]) if rows else ([], [])


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    if not headers:
        return "<p class='empty'>No data</p>"
    ths = "".join(f"<th>{h.replace('_',' ').title()}</th>" for h in headers)
    trs = ""
    for row in rows:
        cells = ""
        for i, val in enumerate(row):
            col = headers[i] if i < len(headers) else ""
            cls = _cell_class(col, val)
            attr = f' class="{cls}"' if cls else ""
            cells += f"<td{attr}>{_fmt(col, val)}</td>"
        trs += f"<tr>{cells}</tr>"
    return (
        f'<div class="table-wrap">'
        f"<table><thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table>"
        f"</div>"
    )


def _render_month(month: str, month_dir: Path) -> str:
    parts = []
    for group_name, reports in REPORT_GROUPS:
        cards = []
        for report_id, title in reports:
            p = month_dir / f"{report_id}.csv"
            if not p.exists():
                continue
            headers, rows = _load_csv(p)
            cards.append(
                f'<div class="card"><h3>{title}</h3>{_render_table(headers, rows)}</div>'
            )
        if cards:
            parts.append(
                f'<div class="section"><h2>{group_name}</h2>{"".join(cards)}</div>'
            )
    return "\n".join(parts)


# ── chart helpers ──────────────────────────────────────────────────────────────

def _y_fmt_currency(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"


def _nice_y_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    span = hi - lo or 1.0
    raw  = span / n
    mag  = 10 ** math.floor(math.log10(raw))
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, t = [], start
    while t <= hi + step * 0.001:
        ticks.append(round(t, 10))
        t += step
    return ticks


def _svg_line_chart(
    series: list[tuple[str, list]],
    x_labels: list[str],
    y_fmt=None,
    w: int = 560,
    h: int = 210,
) -> str:
    if y_fmt is None:
        y_fmt = lambda v: f"{v:,.0f}"
    PL, PR, PT, PB = 68, 16, 18, 36
    pw, ph = w - PL - PR, h - PT - PB
    n = len(x_labels)

    all_vals = [v for _, vals in series for v in vals if v is not None]
    if not all_vals:
        return "<p class='empty'>No data</p>"

    ticks = _nice_y_ticks(min(0, min(all_vals)), max(all_vals))
    lo, hi = ticks[0], ticks[-1]
    if lo == hi:
        hi = lo + 1

    def cx(i): return PL + (i / max(n - 1, 1)) * pw
    def cy(v): return PT + ph * (1 - (v - lo) / (hi - lo))

    elems = []
    for t in ticks:
        y = cy(t)
        elems.append(f'<line x1="{PL}" y1="{y:.1f}" x2="{w-PR}" y2="{y:.1f}" stroke="#e5e7eb" stroke-width="1"/>')
        elems.append(f'<text x="{PL-5}" y="{y+3.5:.1f}" text-anchor="end" font-size="10" fill="#9ca3af">{y_fmt(t)}</text>')

    elems.append(f'<line x1="{PL}" y1="{PT+ph:.1f}" x2="{w-PR}" y2="{PT+ph:.1f}" stroke="#e5e7eb" stroke-width="1"/>')
    for i, lbl in enumerate(x_labels):
        elems.append(f'<text x="{cx(i):.1f}" y="{h-4}" text-anchor="middle" font-size="10" fill="#9ca3af">{lbl}</text>')

    for j, (_, vals) in enumerate(series):
        color = CHART_COLORS[j % len(CHART_COLORS)]
        pts = [(cx(i), cy(v)) for i, v in enumerate(vals) if v is not None]
        if len(pts) >= 2:
            d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            elems.append(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>')
        for x, y in pts:
            elems.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}"/>')

    body = "\n  ".join(elems)
    return f'<svg viewBox="0 0 {w} {h}" style="width:100%" xmlns="http://www.w3.org/2000/svg">\n  {body}\n</svg>'


def _chart_legend(series: list[tuple[str, list]]) -> str:
    if len(series) <= 1:
        return ""
    items = "".join(
        f'<span class="legend-item">'
        f'<span class="legend-dot" style="background:{CHART_COLORS[j % len(CHART_COLORS)]}"></span>'
        f'{label}</span>'
        for j, (label, _) in enumerate(series)
    )
    return f'<div class="chart-legend">{items}</div>'


def _chart_card(title: str, series: list[tuple[str, list]], x_labels: list[str], y_fmt=None) -> str:
    svg = _svg_line_chart(series, x_labels, y_fmt)
    legend = _chart_legend(series)
    return f'<div class="card"><h3>{title}</h3>{svg}{legend}</div>'


# ── markdown renderer ─────────────────────────────────────────────────────────

def _inline_md(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*',     r'<em>\1</em>',         text)
    text = re.sub(r'`(.+?)`',       r'<code>\1</code>',      text)
    return text


def _md_to_html(text: str) -> str:
    lines = text.split('\n')
    out, table_lines = [], []
    in_ul = in_ol = False

    def flush_table():
        if not table_lines:
            return
        rows = [[c.strip() for c in l.strip('|').split('|')] for l in table_lines]
        sep = next((i for i, r in enumerate(rows)
                    if all(re.match(r'^[-:\s]+$', c) for c in r if c)), None)
        headers, data = (rows[:sep], rows[sep+1:]) if sep is not None else ([], rows)
        out.append('<div class="table-wrap"><table>')
        if headers:
            out.append('<thead>' + ''.join(
                '<tr>' + ''.join(f'<th>{_inline_md(c)}</th>' for c in r) + '</tr>'
                for r in headers) + '</thead>')
        out.append('<tbody>' + ''.join(
            '<tr>' + ''.join(f'<td>{_inline_md(c)}</td>' for c in r) + '</tr>'
            for r in data) + '</tbody></table></div>')
        table_lines.clear()

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul: out.append('</ul>'); in_ul = False
        if in_ol: out.append('</ol>'); in_ol = False

    for line in lines:
        s = line.strip()
        if s.startswith('|'):
            close_lists()
            table_lines.append(s)
            continue
        flush_table()
        if s.startswith('### '):
            close_lists(); out.append(f'<h3>{_inline_md(s[4:])}</h3>')
        elif s.startswith('## '):
            close_lists(); out.append(f'<h2>{_inline_md(s[3:])}</h2>')
        elif s.startswith('# '):
            close_lists(); out.append(f'<h1>{_inline_md(s[2:])}</h1>')
        elif re.match(r'^-{3,}$', s) or s in ('***', '___'):
            close_lists()
        elif s.startswith('- ') or s.startswith('* '):
            if in_ol: out.append('</ol>'); in_ol = False
            if not in_ul: out.append('<ul>'); in_ul = True
            out.append(f'<li>{_inline_md(s[2:])}</li>')
        elif re.match(r'^\d+\.\s', s):
            if in_ul: out.append('</ul>'); in_ul = False
            if not in_ol: out.append('<ol>'); in_ol = True
            item = re.sub(r'^\d+\.\s+', '', s)
            out.append(f'<li>{_inline_md(item)}</li>')
        elif not s:
            close_lists()
        else:
            close_lists(); out.append(f'<p>{_inline_md(s)}</p>')

    flush_table()
    close_lists()
    return '\n'.join(out)


def _render_agent_tab(md_dir: Path, prefix: str) -> str:
    files = sorted(md_dir.glob("*.md")) if md_dir.exists() else []
    if not files:
        return "<p class='empty'>No reports found.</p>"

    options = "".join(
        f'<option value="{f.stem}">{f.stem}</option>'
        for f in files
    )
    selector = (
        f'<div class="report-selector">'
        f'<label>Report: </label>'
        f'<select onchange="showReport(this,\'{prefix}\')">{options}</select>'
        f'</div>'
    )

    reports = ""
    for i, f in enumerate(files):
        display = "block" if i == len(files) - 1 else "none"
        content = _md_to_html(f.read_text(encoding="utf-8"))
        reports += (
            f'<div class="md-report" id="{prefix}-{f.stem}" style="display:{display}">'
            f'<div class="md-content">{content}</div>'
            f'</div>'
        )

    # Set the selector to show the last (most recent) report by default
    selector = selector.replace(
        f'value="{files[-1].stem}"',
        f'value="{files[-1].stem}" selected'
    )

    return f'<div class="agent-panel">{selector}{reports}</div>'


# ── summary page ───────────────────────────────────────────────────────────────

def _load_risk_summary(months: list[str]) -> dict:
    """Aggregate risk alerts by month from risk_alerts.jsonl."""
    by_date: dict[str, list] = {}
    if RISK_LOG.exists():
        with RISK_LOG.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    by_date[rec["sim_date"]] = rec["alerts"]
                except Exception:
                    pass

    by_month: dict[str, dict] = {}
    for date_str, alerts in by_date.items():
        ym = date_str[:7]
        s = by_month.setdefault(ym, {"high": 0, "warn": 0, "projects": {}})
        s["high"] += sum(1 for a in alerts if a["severity"] == "HIGH")
        s["warn"] += sum(1 for a in alerts if a["severity"] == "WARN")
        for a in alerts:
            proj = a.get("project", "")
            if proj:
                s["projects"][proj] = s["projects"].get(proj, 0) + 1

    high_by_month = [by_month.get(m, {}).get("high") for m in months]
    warn_by_month = [by_month.get(m, {}).get("warn") for m in months]

    project_totals: dict[str, int] = {}
    for s in by_month.values():
        for proj, cnt in s["projects"].items():
            project_totals[proj] = project_totals.get(proj, 0) + cnt
    top_projects = sorted(project_totals.items(), key=lambda x: -x[1])[:8]

    return {
        "high_by_month": high_by_month,
        "warn_by_month": warn_by_month,
        "top_projects":  top_projects,
    }


def _load_util_summary(months: list[str]) -> dict:
    """Aggregate utilization stats by month from per-month CSV files."""
    avg_util, n_bench, n_over = [], [], []
    for ym in months:
        p = UTIL_CSV_DIR / f"{ym}.csv"
        if not p.exists():
            avg_util.append(None); n_bench.append(None); n_over.append(None)
            continue
        hdrs, rows = _load_csv(p)
        if not rows:
            avg_util.append(None); n_bench.append(None); n_over.append(None)
            continue
        hi = hdrs.index("hours")
        si = hdrs.index("status")
        hours = [float(r[hi]) for r in rows]
        statuses = [r[si] for r in rows]
        cap = 160.0 * len(rows)
        avg_util.append(sum(hours) / cap if cap else None)
        n_bench.append(sum(1 for s in statuses if s == "bench"))
        n_over.append(sum(1 for s in statuses if s == "overloaded"))
    return {"avg_util": avg_util, "bench": n_bench, "overloaded": n_over}


def _load_summary_data(months: list[str], reports_dir: Path) -> dict:
    total_revenue_ytd        = []
    total_forecast_profit    = []
    bu_revenues_ytd: dict[str, list] = {}
    bu_forecast_profits: dict[str, list] = {}
    project_counts           = []
    avg_completions          = []

    for month in months:
        d = reports_dir / month

        p = d / "bu_revenue_ytd.csv"
        if p.exists():
            hdrs, rows = _load_csv(p)
            bc, rc = hdrs.index("business_unit"), hdrs.index("revenue_ytd")
            total = 0.0
            for row in rows:
                bu, val = row[bc], float(row[rc])
                total += val
                bu_revenues_ytd.setdefault(bu, []).append(val)
            total_revenue_ytd.append(total)
        else:
            total_revenue_ytd.append(None)

        p = d / "bu_profit_forecast_eoy.csv"
        if p.exists():
            hdrs, rows = _load_csv(p)
            bc, pc = hdrs.index("business_unit"), hdrs.index("forecast_profit_eoy")
            total = 0.0
            for row in rows:
                bu, val = row[bc], float(row[pc])
                total += val
                bu_forecast_profits.setdefault(bu, []).append(val)
            total_forecast_profit.append(total)
        else:
            total_forecast_profit.append(None)

        p = d / "project_status.csv"
        if p.exists():
            hdrs, rows = _load_csv(p)
            pct_col = hdrs.index("pct_complete")
            pcts = [float(r[pct_col]) for r in rows]
            project_counts.append(len(pcts))
            avg_completions.append(sum(pcts) / len(pcts) if pcts else 0.0)
        else:
            project_counts.append(None)
            avg_completions.append(None)

    return {
        "total_revenue_ytd":     total_revenue_ytd,
        "total_forecast_profit": total_forecast_profit,
        "bu_revenues_ytd":       bu_revenues_ytd,
        "bu_forecast_profits":   bu_forecast_profits,
        "project_counts":        project_counts,
        "avg_completions":       avg_completions,
    }


def _render_summary(months: list[str], reports_dir: Path) -> str:
    data = _load_summary_data(months, reports_dir)
    x_labels = [_MONTH_ABBR[int(m[5:7]) - 1] for m in months]

    revenue_charts = (
        _chart_card("Total Revenue YTD",
                    [("Revenue", data["total_revenue_ytd"])],
                    x_labels, _y_fmt_currency)
        + _chart_card("Revenue YTD by Business Unit",
                      list(data["bu_revenues_ytd"].items()),
                      x_labels, _y_fmt_currency)
    )

    profit_charts = (
        _chart_card("Forecast Profit EOY (Total)",
                    [("Forecast Profit", data["total_forecast_profit"])],
                    x_labels, _y_fmt_currency)
        + _chart_card("Forecast Profit EOY by Business Unit",
                      list(data["bu_forecast_profits"].items()),
                      x_labels, _y_fmt_currency)
    )

    # Month-over-month table
    table_rows = ""
    prev_rev = None
    for i, month in enumerate(months):
        rev    = data["total_revenue_ytd"][i]
        profit = data["total_forecast_profit"][i]
        proj   = data["project_counts"][i]
        avg    = data["avg_completions"][i]

        if prev_rev is not None and rev is not None:
            delta = rev - prev_rev
            sign  = "+" if delta >= 0 else ""
            pct   = (delta / prev_rev * 100) if prev_rev else 0.0
            cls   = "positive" if delta >= 0 else "negative"
            mom   = f'<td class="{cls}">{sign}${delta:,.0f} ({sign}{pct:.1f}%)</td>'
        else:
            mom = "<td>—</td>"

        profit_cls = "positive" if (profit or 0) >= 0 else "negative"
        table_rows += (
            f"<tr>"
            f"<td><strong>{month}</strong></td>"
            f"<td>{'$'+f'{rev:,.0f}' if rev is not None else '—'}</td>"
            f"{mom}"
            f"<td class='{profit_cls}'>{'$'+f'{profit:,.0f}' if profit is not None else '—'}</td>"
            f"<td>{proj if proj is not None else '—'}</td>"
            f"<td>{f'{avg:.1f}%' if avg is not None else '—'}</td>"
            f"</tr>"
        )
        if rev is not None:
            prev_rev = rev

    mom_table = (
        '<div class="card"><h3>Month-over-Month Key Metrics</h3>'
        '<div class="table-wrap"><table>'
        "<thead><tr>"
        "<th>Month</th><th>Total Revenue YTD</th><th>MoM Revenue Change</th>"
        "<th>Forecast Profit EOY</th><th>Projects Tracked</th><th>Avg Completion</th>"
        f"</tr></thead><tbody>{table_rows}</tbody></table></div></div>"
    )

    # ── Executive summary (most recent LLM reports) ───────────────────────────
    exec_cards = ""

    latest_risk = sorted(RISK_DIR.glob("*.md"))[-1] if RISK_DIR.exists() and list(RISK_DIR.glob("*.md")) else None
    latest_util = sorted(UTIL_DIR.glob("*.md"))[-1] if UTIL_DIR.exists() and list(UTIL_DIR.glob("*.md")) else None

    if latest_risk:
        exec_cards += (
            f'<div class="card exec-card">'
            f'<div class="exec-label">Risk Report — {latest_risk.stem}</div>'
            f'<div class="md-content">{_md_to_html(latest_risk.read_text(encoding="utf-8"))}</div>'
            f'</div>'
        )
    if latest_util:
        exec_cards += (
            f'<div class="card exec-card">'
            f'<div class="exec-label">Utilization Report — {latest_util.stem}</div>'
            f'<div class="md-content">{_md_to_html(latest_util.read_text(encoding="utf-8"))}</div>'
            f'</div>'
        )

    exec_section = (
        f'<div class="section"><h2>Executive Summary &amp; Recommendations</h2>'
        f'<div class="exec-grid">{exec_cards}</div></div>'
    ) if exec_cards else ""

    # ── Risk summary ──────────────────────────────────────────────────────────
    risk = _load_risk_summary(months)
    risk_chart = _chart_card(
        "Monthly Alert Volume",
        [("HIGH", risk["high_by_month"]), ("WARN", risk["warn_by_month"])],
        x_labels,
        y_fmt=lambda v: f"{v:.0f}",
    )
    if risk["top_projects"]:
        proj_rows = "".join(
            f"<tr><td>{proj}</td><td style='text-align:right'>{cnt}</td></tr>"
            for proj, cnt in risk["top_projects"]
        )
        proj_table = (
            '<div class="card"><h3>Most-Flagged Projects (All Time)</h3>'
            '<div class="table-wrap"><table>'
            '<thead><tr><th>Project</th><th style="text-align:right">Alert Count</th></tr></thead>'
            f'<tbody>{proj_rows}</tbody></table></div></div>'
        )
    else:
        proj_table = "<p class='empty'>No risk alert data found.</p>"

    # Risk text summary
    total_high = sum(v for v in risk["high_by_month"] if v is not None)
    total_warn = sum(v for v in risk["warn_by_month"] if v is not None)
    peak_month = None
    peak_val   = 0
    for i, m in enumerate(months):
        h = risk["high_by_month"][i] or 0
        w = risk["warn_by_month"][i] or 0
        if h + w > peak_val:
            peak_val   = h + w
            peak_month = m
    top_proj   = risk["top_projects"][0][0] if risk["top_projects"] else "N/A"
    risk_summary_card = (
        f'<div class="card"><h3>Summary</h3>'
        f'<p>The simulation generated <strong>{total_high} HIGH</strong> and '
        f'<strong>{total_warn} WARN</strong> risk alerts across the year. '
        f'Alert volume peaked in <strong>{peak_month}</strong> with {peak_val} total flags. '
        f'<strong>{top_proj}</strong> was the most frequently flagged project.</p></div>'
    )

    risk_section = (
        f'<div class="section"><h2>Risk Overview</h2>'
        f'{risk_summary_card}'
        f'<div class="chart-grid">{risk_chart}</div>'
        f'{proj_table}</div>'
    )

    # ── Utilization summary ───────────────────────────────────────────────────
    util = _load_util_summary(months)
    util_chart = _chart_card(
        "Average Utilization by Month",
        [("Avg Utilization", [v * 100 if v is not None else None for v in util["avg_util"]])],
        x_labels,
        y_fmt=lambda v: f"{v:.0f}%",
    )
    bench_chart = _chart_card(
        "Bench & Overloaded Headcount",
        [("Bench", util["bench"]), ("Overloaded", util["overloaded"])],
        x_labels,
        y_fmt=lambda v: f"{v:.0f}",
    )
    # Utilization text summary
    util_vals   = [v for v in util["avg_util"] if v is not None]
    bench_vals  = [v for v in util["bench"]    if v is not None]
    over_vals   = [v for v in util["overloaded"] if v is not None]
    avg_u       = sum(util_vals) / len(util_vals) if util_vals else 0
    max_bench   = max(bench_vals)  if bench_vals  else 0
    max_over    = max(over_vals)   if over_vals   else 0
    peak_bench_month = months[util["bench"].index(max_bench)]    if bench_vals else "N/A"
    peak_over_month  = months[util["overloaded"].index(max_over)] if over_vals  else "N/A"
    util_summary_card = (
        f'<div class="card"><h3>Summary</h3>'
        f'<p>Average firm-wide utilization across the year was <strong>{avg_u:.0%}</strong>. '
        f'Bench headcount peaked at <strong>{int(max_bench)} consultants</strong> in {peak_bench_month}, '
        f'while overloaded headcount peaked at <strong>{int(max_over)} consultants</strong> in {peak_over_month}.</p></div>'
    )

    util_section = (
        f'<div class="section"><h2>Workforce Utilization</h2>'
        f'{util_summary_card}'
        f'<div class="chart-grid">{util_chart}{bench_chart}</div></div>'
    )

    return (
        f'{exec_section}'
        f'<div class="section"><h2>Revenue</h2><div class="chart-grid">{revenue_charts}</div></div>'
        f'<div class="section"><h2>Profitability</h2><div class="chart-grid">{profit_charts}</div></div>'
        f'<div class="section"><h2>Month-over-Month Metrics</h2>{mom_table}</div>'
        f'{risk_section}'
        f'{util_section}'
    )


# ── page template ──────────────────────────────────────────────────────────────

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f0f2f5;color:#111827}
header{background:#1a1a2e;color:#fff;padding:1.25rem 2rem;display:flex;align-items:center;gap:.75rem}
header h1{font-size:1.2rem;font-weight:600}
header .sub{opacity:.45;font-size:.85rem}
#tabs{background:#16213e;display:flex;overflow-x:auto;scrollbar-width:none}
#tabs::-webkit-scrollbar{display:none}
#tabs button{background:none;border:none;color:rgba(255,255,255,.55);padding:.7rem 1.2rem;
  cursor:pointer;white-space:nowrap;font-size:.8rem;border-bottom:3px solid transparent;
  transition:color .15s,border-color .15s}
#tabs button:hover{color:#fff}
#tabs button.active{color:#4fc3f7;border-bottom-color:#4fc3f7}
main{padding:1.5rem 2rem;max-width:1440px;margin:0 auto}
.panel{display:none}.panel.active{display:block}
.section{margin-bottom:2rem}
.section>h2{font-size:.7rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  color:#6b7280;margin-bottom:.75rem;padding-bottom:.4rem;border-bottom:1px solid #e5e7eb}
.card{background:#fff;border-radius:8px;padding:1.25rem;margin-bottom:.75rem;
  box-shadow:0 1px 3px rgba(0,0,0,.07)}
.card h3{font-size:.85rem;font-weight:600;color:#374151;margin-bottom:.75rem}
.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:.75rem;margin-bottom:.75rem}
@media(max-width:900px){.chart-grid{grid-template-columns:1fr}}
.chart-legend{display:flex;flex-wrap:wrap;gap:.4rem .9rem;margin-top:.6rem}
.legend-item{display:flex;align-items:center;gap:.35rem;font-size:.75rem;color:#6b7280}
.legend-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.78rem}
th{background:#f9fafb;text-align:left;padding:.45rem .7rem;font-weight:600;color:#6b7280;
  font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;
  border-bottom:1px solid #e5e7eb;white-space:nowrap}
td{padding:.4rem .7rem;border-bottom:1px solid #f3f4f6;white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:#f9fafb}
td.positive{color:#059669;font-weight:500}
td.negative{color:#dc2626;font-weight:500}
td.complete{color:#059669}
td.mid{color:#d97706}
td.low{color:#dc2626}
.empty{color:#9ca3af;font-style:italic;font-size:.8rem;padding:.5rem}
.exec-grid{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}
@media(max-width:900px){.exec-grid{grid-template-columns:1fr}}
.exec-card{border-top:3px solid #3b82f6}
.exec-label{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#3b82f6;margin-bottom:.75rem}
.report-selector{margin-bottom:1rem}
.report-selector label{font-size:.8rem;font-weight:600;color:#6b7280;margin-right:.5rem}
.report-selector select{font-size:.8rem;padding:.3rem .6rem;border:1px solid #d1d5db;border-radius:4px;background:#fff;color:#111827;cursor:pointer}
.md-content{line-height:1.65;color:#1f2937}
.md-content h1{font-size:1.15rem;font-weight:700;color:#111827;margin:0 0 1rem}
.md-content h2{font-size:.9rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;margin:1.5rem 0 .6rem;padding-bottom:.35rem;border-bottom:1px solid #e5e7eb}
.md-content h3{font-size:.85rem;font-weight:600;color:#374151;margin:.9rem 0 .4rem}
.md-content p{font-size:.83rem;margin-bottom:.6rem}
.md-content ul,.md-content ol{font-size:.83rem;padding-left:1.4rem;margin-bottom:.6rem}
.md-content li{margin-bottom:.3rem}
.md-content strong{font-weight:600;color:#111827}
.md-content code{font-family:monospace;background:#f3f4f6;padding:.1rem .3rem;border-radius:3px;font-size:.8rem}
"""

_JS = """
function show(id){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('#tabs button').forEach(b=>b.classList.remove('active'));
  document.getElementById('p-'+id).classList.add('active');
  document.getElementById('t-'+id).classList.add('active');
}
function showReport(sel,prefix){
  document.querySelectorAll('.md-report[id^="'+prefix+'-"]').forEach(d=>d.style.display='none');
  document.getElementById(prefix+'-'+sel.value).style.display='block';
}
"""


def _publish(html_path: Path) -> None:
    repo_url = os.environ.get("REPORT_REPO_URL")
    if not repo_url:
        print("error: --publish requires REPORT_REPO_URL env var", file=sys.stderr)
        sys.exit(1)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "clone", "--depth=1", repo_url, tmp], check=True)
        shutil.copy(html_path, Path(tmp) / "index.html")
        git = ["git", "-C", tmp]
        subprocess.run([*git, "add", "index.html"], check=True)
        if subprocess.run([*git, "diff", "--cached", "--quiet"]).returncode == 0:
            print("no changes to publish")
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        subprocess.run([*git, "commit", "-m", f"Update report {ts}"], check=True)
        subprocess.run([*git, "push"], check=True)
        print(f"published → {repo_url}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--publish", action="store_true",
                    help="After writing report.html, push it as index.html to REPORT_REPO_URL")
    args = ap.parse_args()

    if not REPORTS_DIR.exists():
        print(f"error: reports directory not found: {REPORTS_DIR}", file=sys.stderr)
        sys.exit(1)

    months = sorted(
        p.name for p in REPORTS_DIR.iterdir()
        if p.is_dir() and len(p.name) == 7 and p.name[4] == "-"
    )
    if not months:
        print("error: no report months found", file=sys.stderr)
        sys.exit(1)

    summary_html = _render_summary(months, REPORTS_DIR)
    tabs   = '<button id="t-summary" onclick="show(\'summary\')" class="active">Summary</button>'
    panels = f'<div class="panel active" id="p-summary">{summary_html}</div>'

    for month in months:
        sid = month.replace("-", "_")
        tabs   += f'<button id="t-{sid}" onclick="show(\'{sid}\')">{month}</button>'
        panels += f'<div class="panel" id="p-{sid}">{_render_month(month, REPORTS_DIR / month)}</div>'

    tabs   += '<button id="t-risk" onclick="show(\'risk\')">Risk Analysis</button>'
    panels += f'<div class="panel" id="p-risk">{_render_agent_tab(RISK_DIR, "risk")}</div>'

    tabs   += '<button id="t-util" onclick="show(\'util\')">Utilization Analysis</button>'
    panels += f'<div class="panel" id="p-util">{_render_agent_tab(UTIL_DIR, "util")}</div>'

    n = len(months)
    html = (
        "<!DOCTYPE html>\n<html lang='en'>\n<head>\n"
        "<meta charset='UTF-8'>\n"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>\n"
        "<title>Consulting Firm — Simulation Reports</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n"
        "<header>\n"
        "  <h1>Consulting Firm Simulation</h1>\n"
        "  <span class='sub'>·</span>\n"
        f"  <span class='sub'>{n} reporting period{'s' if n != 1 else ''}</span>\n"
        "</header>\n"
        f"<nav id='tabs'>{tabs}</nav>\n"
        f"<main>{panels}</main>\n"
        f"<script>{_JS}</script>\n"
        "</body>\n</html>"
    )

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"wrote → {OUTPUT_FILE}")

    if args.publish:
        _publish(OUTPUT_FILE)


if __name__ == "__main__":
    main()
