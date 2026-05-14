"""
render/homepage.py — generates pages/index.html

Phase 1: Index Strip + Breadth Kachel.
Remaining 5 dimension tiles are greyed-out placeholders until data is wired up.
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

PAGES_DIR = Path(__file__).parent.parent / "pages"

_PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.26.0.min.js"

_STRIP_CONFIG = [
    ("SPY",     "SPY",    "",  False),
    ("QQQ",     "QQQ",    "",  False),
    ("IWM",     "IWM",    "",  False),
    ("^VIX",    "VIX",    "",  True),   # inverted: lower = better
    ("^TNX",    "US10Y",  "%", False),
]

_BREADTH_GREEN = 60
_BREADTH_RED   = 40

_INDUSTRY_MIN_TICKERS = 3   # minimum stocks per sub-industry for a meaningful median
_INDUSTRY_TOP_N       = 10

_SECTOR_NAMES: dict[str, str] = {
    "XLK":  "Technology",
    "XLV":  "Health Care",
    "XLF":  "Financials",
    "XLY":  "Cons. Discret.",
    "XLP":  "Cons. Staples",
    "XLE":  "Energy",
    "XLI":  "Industrials",
    "XLU":  "Utilities",
    "XLB":  "Materials",
    "XLRE": "Real Estate",
    "XLC":  "Comm. Services",
}

# Trading days per period
_SECTOR_PERIODS: dict[str, int] = {"1W": 5, "1M": 21, "3M": 63, "6M": 126}

_RGB = {
    "green":  "rgb(22,163,74)",
    "red":    "rgb(220,38,38)",
    "yellow": "rgb(217,119,6)",
    "gray":   "rgb(148,163,184)",
}

_DIMENSION_DISPLAY_LABELS: dict[str, str] = {
    "breadth":    "BREADTH",
    "risk":       "RISK ON/OFF",
    "volatility": "VOLATILITY",
    "obos":       "OB / OS",
    "sentiment":  "SENTIMENT",
    "credit":     "CREDIT",
}
_TREND_ARROWS: dict[str, str] = {"up": "↑", "down": "↓", "flat": "→"}
_TREND_COLORS: dict[str, str] = {"up": "green", "down": "red", "flat": "gray"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build(conn: sqlite3.Connection, build_date: str) -> None:
    """Generate pages/index.html. Called from main.py Step 5."""
    from compute.dimensions import compute_all_dimensions
    macro      = _get_macro_strip(conn)
    breadth_d  = _get_breadth(conn)
    sectors    = _get_sector_perf(conn)
    industries = _get_industry_perf(conn)
    hits       = _get_scanner_hits(conn)
    dimensions = compute_all_dimensions(conn)
    op_summary = _get_operation_summary(conn)
    html       = _render(macro, breadth_d, sectors, industries, hits, dimensions, op_summary, build_date)
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    (PAGES_DIR / "index.html").write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Data queries
# ---------------------------------------------------------------------------

def _get_macro_strip(conn: sqlite3.Connection) -> list[dict]:
    result = []
    for series_id, label, suffix, inverted in _STRIP_CONFIG:
        df = pd.read_sql(
            "SELECT date, value FROM macro_series "
            "WHERE series_id = ? ORDER BY date DESC LIMIT 25",
            conn, params=(series_id,),
        )
        if df.empty:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        latest = float(df["value"].iloc[-1])
        prev   = float(df["value"].iloc[-2]) if len(df) >= 2 else latest
        change = (latest / prev - 1) * 100 if prev != 0 else 0.0
        raw  = df["value"].tolist()[-20:]
        base = raw[0] if raw[0] != 0 else 1.0
        spark = [(v / base - 1) * 100 for v in raw]
        result.append({
            "label":     label,
            "price":     latest,
            "change":    change,
            "suffix":    suffix,
            "inverted":  inverted,
            "sparkline": spark,
        })
    return result


def _get_sector_perf(conn: sqlite3.Connection) -> list[dict]:
    """
    Compute 1W/1M/3M/6M performance for each sector ETF from macro_series.
    Returns list sorted by 1M performance descending (best sector first).
    """
    max_rows = max(_SECTOR_PERIODS.values()) + 10  # extra buffer for market holidays
    result = []
    for ticker, name in _SECTOR_NAMES.items():
        df = pd.read_sql(
            "SELECT date, value FROM macro_series WHERE series_id = ? ORDER BY date DESC LIMIT ?",
            conn,
            params=(ticker, max_rows),
        )
        if len(df) < 6:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        latest = float(df["value"].iloc[-1])

        perfs: dict[str, Optional[float]] = {}
        for label, lookback in _SECTOR_PERIODS.items():
            if len(df) > lookback:
                past = float(df["value"].iloc[-(lookback + 1)])
                perfs[label] = round((latest / past - 1) * 100, 2) if past else None
            else:
                perfs[label] = None

        result.append({
            "ticker": ticker,
            "name":   name,
            "latest": latest,
            **perfs,
        })

    result.sort(key=lambda x: (x.get("1M") is None, -(x.get("1M") or 0)))
    return result


def _get_industry_perf(conn: sqlite3.Connection) -> list[dict]:
    """
    Compute median 1M return per GICS sub-industry from individual stock prices.
    Equal-weighted median across all active universe stocks per sub-industry.
    Returns list sorted by 1M performance descending.
    """
    df = pd.read_sql(
        """
        SELECT industry, rn, close FROM (
            SELECT u.gics_sub_industry AS industry,
                   p.close,
                   ROW_NUMBER() OVER (PARTITION BY p.ticker ORDER BY p.date DESC) AS rn
            FROM prices p
            JOIN universe u ON u.ticker = p.ticker
            WHERE u.active = 1
              AND u.gics_sub_industry IS NOT NULL
              AND u.gics_sub_industry != ''
        )
        WHERE rn IN (1, 22)
        """,
        conn,
    )
    if df.empty:
        return []

    latest = df[df["rn"] == 1].set_index("industry")["close"].rename("latest")
    past   = df[df["rn"] == 22].set_index("industry")["close"].rename("past")

    # Multiple stocks per industry — keep as series with duplicated index
    merged = pd.DataFrame({"latest": latest, "past": past}).dropna()
    merged = merged[merged["past"] > 0]
    merged["perf_1m"] = (merged["latest"] / merged["past"] - 1) * 100

    result = (
        merged.groupby(level=0)["perf_1m"]
        .agg(perf_1m="median", n="count")
        .reset_index()
        .rename(columns={"index": "industry"})
    )
    result = result[result["n"] >= _INDUSTRY_MIN_TICKERS].copy()
    result["perf_1m"] = result["perf_1m"].round(2)
    result = result.sort_values("perf_1m", ascending=False).reset_index(drop=True)
    return result[["industry", "perf_1m", "n"]].to_dict("records")


def _get_scanner_hits(conn: sqlite3.Connection) -> list[dict]:
    df = pd.read_sql(
        """SELECT ticker, gics_sector, rs_rank, perf_1m, dist_52w_high, date
           FROM scanner_hits
           WHERE date = (SELECT MAX(date) FROM scanner_hits WHERE scanner = 'pullback_ma20')
             AND scanner = 'pullback_ma20'
           ORDER BY rs_rank DESC
           LIMIT 10""",
        conn,
    )
    if df.empty:
        return []
    return df.to_dict("records")


def _get_breadth(conn: sqlite3.Connection) -> Optional[dict]:
    df = pd.read_sql(
        "SELECT * FROM breadth_daily ORDER BY date DESC LIMIT 25", conn
    )
    if df.empty:
        return None
    df = df.sort_values("date").reset_index(drop=True)
    row = df.iloc[-1].to_dict()
    row["history_50dma"]  = df["pct_above_50dma"].dropna().tolist()
    row["history_200dma"] = df["pct_above_200dma"].dropna().tolist()
    return row


def _get_operation_summary(conn: sqlite3.Connection) -> dict:
    last_date_row = conn.execute("SELECT MAX(date) FROM prices").fetchone()
    last_date     = last_date_row[0] if last_date_row else None

    active = conn.execute("SELECT COUNT(*) FROM universe WHERE active=1").fetchone()[0]
    priced = 0
    if last_date:
        priced = conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM prices WHERE date=?", (last_date,)
        ).fetchone()[0]

    mock_count = 0
    try:
        mock_count = conn.execute(
            "SELECT COUNT(*) FROM prices WHERE source='mock-fallback'"
        ).fetchone()[0]
    except Exception:
        pass

    latest_dq = conn.execute(
        "SELECT status FROM data_quality_checks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    dq_status = latest_dq[0] if latest_dq else "ok"

    return {
        "last_date":  last_date or "—",
        "active":     active,
        "priced":     priced,
        "has_mock":   mock_count > 0,
        "mock_count": mock_count,
        "dq_status":  dq_status,
    }


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------

def _render(
    macro: list[dict],
    breadth: Optional[dict],
    sectors: list[dict],
    industries: list[dict],
    hits: list[dict],
    dimensions: list[dict],
    op_summary: dict,
    build_date: str,
) -> str:
    strip          = _index_strip_html(macro)
    pills          = _dimension_pills_html(dimensions)
    bspark         = _breadth_sparkline_section_html(breadth)
    sector_sect    = _sector_section_html(sectors)
    industry_sect  = _industry_section_html(industries)
    scanner_sect   = _scanner_section_html(hits, build_date)
    source_notice  = _source_notice_html(op_summary)
    op_line        = _operation_summary_line_html(op_summary)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Market Dashboard</title>
<script src="{_PLOTLY_CDN}"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:#f8fafc;color:#1e293b;font-size:14px}}
header{{background:#0f172a;color:#f1f5f9;padding:10px 20px;
        display:flex;align-items:center;gap:24px;flex-wrap:wrap}}
header h1{{font-size:13px;font-weight:700;letter-spacing:1px;white-space:nowrap}}
nav{{display:flex;gap:16px;flex-wrap:wrap}}
nav a{{color:#94a3b8;text-decoration:none;font-size:13px}}
nav a:hover{{color:#f1f5f9}}
nav a.active{{color:#f1f5f9;font-weight:600}}
.header-right{{margin-left:auto;display:flex;flex-direction:column;
               align-items:flex-end;gap:3px}}
.op-summary-line{{font-size:11px;color:#64748b;display:flex;gap:6px;align-items:center}}
.op-summary-line .dq-ok{{color:#16a34a}}
.op-summary-line .dq-warning{{color:#d97706}}
.op-summary-line .dq-error{{color:#dc2626}}
.op-summary-line .mock-warn{{color:#d97706;font-weight:600}}
.last-update{{color:#64748b;font-size:12px;white-space:nowrap}}
.source-notice{{background:#fef3c7;border-bottom:1px solid #fde68a;
                padding:8px 20px;font-size:12px;color:#92400e}}
.index-strip{{display:flex;gap:10px;padding:12px 20px;
              background:white;border-bottom:1px solid #e2e8f0;flex-wrap:wrap}}
.idx-card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
           padding:10px 14px;min-width:140px;flex:1;overflow:hidden}}
.idx-label{{font-size:11px;font-weight:600;color:#64748b;
            letter-spacing:.5px;text-transform:uppercase}}
.idx-price{{font-size:22px;font-weight:700;margin:2px 0;color:#0f172a}}
.idx-change{{font-size:12px;font-weight:600}}
.market-state{{display:flex;gap:10px;padding:14px 20px;flex-wrap:wrap;
               background:#f8fafc;border-bottom:1px solid #e2e8f0}}
.dim-pill{{background:white;border:1px solid #e2e8f0;border-radius:10px;
           padding:14px 16px;flex:1;min-width:140px;max-width:260px}}
.status-green{{border-top:4px solid #16a34a}}
.status-yellow{{border-top:4px solid #d97706}}
.status-red{{border-top:4px solid #dc2626}}
.status-na{{border-top:4px solid #cbd5e1}}
.dim-pill-label{{font-size:10px;font-weight:700;letter-spacing:1px;
                 text-transform:uppercase;color:#64748b;margin-bottom:6px}}
.dim-pill-value{{font-size:17px;font-weight:700;line-height:1.2;margin-bottom:4px;color:#0f172a}}
.dim-pill-value.green{{color:#16a34a}}
.dim-pill-value.red{{color:#dc2626}}
.dim-pill-value.yellow{{color:#d97706}}
.dim-pill-value.gray{{color:#94a3b8}}
.dim-pill-trend{{font-size:12px;font-weight:600;margin-bottom:4px}}
.dim-pill-trend.green{{color:#16a34a}}
.dim-pill-trend.red{{color:#dc2626}}
.dim-pill-trend.gray{{color:#94a3b8}}
.dim-pill-note{{font-size:11px;color:#94a3b8;line-height:1.3}}
.breadth-spark-row{{padding:4px 20px 0;background:#f8fafc}}
.pill{{display:inline-flex;align-items:center;padding:2px 8px;border-radius:99px;
       font-size:11px;font-weight:700;letter-spacing:.4px}}
.pill-green{{background:#dcfce7;color:#15803d}}
.pill-yellow{{background:#fef9c3;color:#a16207}}
.pill-red{{background:#fee2e2;color:#b91c1c}}
.green{{color:#16a34a}}.red{{color:#dc2626}}.yellow{{color:#d97706}}
.sector-section{{padding:16px 20px 20px}}
.sector-tile{{background:white;border:1px solid #e2e8f0;border-radius:12px;padding:16px 20px}}
.sector-header{{font-size:11px;font-weight:700;letter-spacing:1px;
                text-transform:uppercase;color:#64748b;margin-bottom:12px}}
.industry-section{{padding:0 20px 20px}}
.industry-tile{{background:white;border:1px solid #e2e8f0;border-radius:12px;padding:16px 20px}}
.industry-header{{font-size:11px;font-weight:700;letter-spacing:1px;
                  text-transform:uppercase;color:#64748b;margin-bottom:14px;
                  display:flex;align-items:center;gap:8px}}
.industry-header-count{{font-weight:400;color:#94a3b8;letter-spacing:0}}
.industry-grid{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}
.industry-sub-header{{font-size:11px;font-weight:700;letter-spacing:.5px;
                       text-transform:uppercase;margin-bottom:8px}}
.industry-sub-header.top{{color:#16a34a}}.industry-sub-header.bottom{{color:#dc2626}}
.industry-row{{display:flex;align-items:center;padding:5px 0;
               border-bottom:1px solid #f1f5f9;gap:6px}}
.industry-row:last-child{{border-bottom:none}}
.industry-name{{flex:1;font-size:13px;color:#1e293b;
                white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
                min-width:0}}
.industry-n{{font-size:11px;color:#94a3b8;white-space:nowrap;flex-shrink:0}}
.industry-perf{{font-size:13px;font-weight:600;white-space:nowrap;
                flex-shrink:0;min-width:48px;text-align:right}}
.scanner-section{{padding:0 20px 20px}}
.scanner-header{{font-size:11px;font-weight:700;letter-spacing:1px;
                 text-transform:uppercase;color:#64748b;margin-bottom:10px;
                 display:flex;align-items:center;gap:8px}}
.scanner-date{{font-weight:400;color:#94a3b8;letter-spacing:0}}
.scanner-count{{font-weight:400;color:#94a3b8;letter-spacing:0}}
.scanner-table{{width:100%;border-collapse:collapse;background:white;
                border:1px solid #e2e8f0;border-radius:12px;overflow:hidden}}
.scanner-table th{{font-size:11px;font-weight:700;letter-spacing:.5px;
                   text-transform:uppercase;color:#64748b;padding:10px 14px;
                   border-bottom:1px solid #e2e8f0;text-align:left;
                   background:#f8fafc}}
.scanner-table td{{padding:10px 14px;border-bottom:1px solid #f1f5f9;
                   font-size:13px;color:#1e293b;vertical-align:middle}}
.scanner-table tr:last-child td{{border-bottom:none}}
.scanner-table tr:hover td{{background:#f8fafc}}
.scanner-ticker{{font-weight:700;font-size:14px;color:#0f172a}}
.scanner-sector{{color:#64748b;font-size:12px}}
.scanner-empty{{color:#94a3b8;font-size:13px;padding:16px 0}}
footer{{text-align:center;padding:16px 20px;color:#94a3b8;
        font-size:12px;border-top:1px solid #e2e8f0;margin-top:4px}}
</style>
</head>
<body>
<header>
  <h1>MARKET DASHBOARD</h1>
  <nav>
    <a href="index.html" class="active">Dashboard</a>
    <a href="#">Breadth</a>
    <a href="#">Sentiment</a>
    <a href="#">Risk On/Off</a>
    <a href="#">Credit &amp; Macro</a>
    <a href="#">Volatility</a>
    <a href="#">Sectors</a>
    <a href="#">Scanners</a>
  </nav>
  <div class="header-right">
    <div class="op-summary-line">{op_line}</div>
    <span class="last-update">Last update: {build_date} ET</span>
  </div>
</header>

{source_notice}

{strip}

{pills}

{bspark}

{sector_sect}

{industry_sect}

{scanner_sect}

<footer>Data: Yahoo Finance &nbsp;·&nbsp; EOD update via GitHub Actions &nbsp;·&nbsp; Personal use only</footer>
</body>
</html>"""


def _index_strip_html(macro: list[dict]) -> str:
    cards = ""
    for i, t in enumerate(macro):
        up      = (t["change"] >= 0) != t["inverted"]   # inverted = VIX down is good
        color   = "green" if up else "red"
        sign    = "+" if t["change"] >= 0 else ""
        price_s = f"{t['price']:.2f}{t['suffix']}"
        chg_s   = f"{sign}{t['change']:.2f}%"
        spark   = _sparkline(t["sparkline"], f"si{i}", _RGB[color], height=36)
        cards += f"""
  <div class="idx-card">
    <div class="idx-label">{t['label']}</div>
    <div class="idx-price">{price_s}</div>
    <div class="idx-change {color}">{chg_s}</div>
    {spark}
  </div>"""
    return f'<div class="index-strip">{cards}\n</div>'


def _breadth_tile_html(data: Optional[dict]) -> str:
    if data is None or data.get("pct_above_50dma") is None:
        return _placeholder_tile("BREADTH", "No data yet")

    val = float(data["pct_above_50dma"])

    if val >= _BREADTH_GREEN:
        pill_cls, color = "pill-green",  "green"
    elif val >= _BREADTH_RED:
        pill_cls, color = "pill-yellow", "yellow"
    else:
        pill_cls, color = "pill-red",    "red"

    pct200 = data.get("pct_above_200dma")
    nh     = data.get("new_highs_52w")
    nl     = data.get("new_lows_52w")
    date_s = data.get("date", "")
    hist   = data.get("history_50dma", [])

    pct200_s = f"{pct200:.1f}%" if pct200 is not None else "—"
    nh_s     = str(int(nh))     if nh     is not None else "—"
    nl_s     = str(int(nl))     if nl     is not None else "—"

    spark = _sparkline(hist, "sb", _RGB[color], height=55)

    return f"""<div class="tile">
  <div class="tile-label">BREADTH <span class="pill {pill_cls}">% &gt; 50DMA</span></div>
  <div class="tile-big {color}">{val:.1f}<span style="font-size:30px">%</span></div>
  <div class="tile-sub">S&amp;P 1500 above 50-day MA &nbsp;·&nbsp; {date_s}</div>
  {spark}
  <div class="tile-stats">
    <div class="tile-stat">
      <span class="tile-stat-l">% &gt; 200DMA</span>
      <span class="tile-stat-v">{pct200_s}</span>
    </div>
    <div class="tile-stat">
      <span class="tile-stat-l">52W Highs</span>
      <span class="tile-stat-v {'' if nh is None else 'green'}">{nh_s}</span>
    </div>
    <div class="tile-stat">
      <span class="tile-stat-l">52W Lows</span>
      <span class="tile-stat-v {'' if nl is None else 'red'}">{nl_s}</span>
    </div>
  </div>
</div>"""


def _placeholder_tile(label: str, sub: str) -> str:
    return f"""<div class="tile placeholder">
  <div class="tile-label">{label}</div>
  <div class="tile-big">{sub}</div>
  <div class="tile-sub">Not yet implemented</div>
</div>"""


def _dimension_pills_html(dimensions: list[dict]) -> str:
    if not dimensions:
        return ""
    pills = ""
    for dim in dimensions:
        mid       = dim["metric_id"]
        d_label   = _DIMENSION_DISPLAY_LABELS.get(mid, mid.upper())
        status    = dim.get("status", "na")
        val_label = dim.get("label", "—")
        trend     = dim.get("trend", "flat")
        change_1w = dim.get("change_1w")
        note      = dim.get("note", "")

        arrow   = _TREND_ARROWS.get(trend, "→")
        a_color = _TREND_COLORS.get(trend, "gray")
        v_color = status if status != "na" else "gray"

        chg_s = ""
        if change_1w is not None:
            sign  = "+" if float(change_1w) > 0 else ""
            chg_s = f"&nbsp;{sign}{change_1w}"

        pills += (
            f'<div class="dim-pill status-{status}">'
            f'<div class="dim-pill-label">{d_label}</div>'
            f'<div class="dim-pill-value {v_color}">{val_label}</div>'
            f'<div class="dim-pill-trend {a_color}">{arrow}{chg_s}</div>'
            f'<div class="dim-pill-note">{note}</div>'
            f'</div>\n'
        )
    return f'<section class="market-state">\n{pills}</section>'


def _operation_summary_line_html(summary: dict) -> str:
    date_s   = summary.get("last_date", "—")
    priced   = summary.get("priced", 0)
    active   = summary.get("active", 0)
    has_mock = summary.get("has_mock", False)
    dq_st    = summary.get("dq_status") or "ok"
    dq_cls   = f"dq-{dq_st}" if dq_st in ("ok", "warning", "error") else "dq-ok"
    src      = '<span class="mock-warn">⚠ mock</span>' if has_mock else "yfinance"
    return (
        f"{src}"
        f'<span style="color:#334155">·</span>'
        f"<span>{date_s}</span>"
        f'<span style="color:#334155">·</span>'
        f"<span>{priced}/{active} eq.</span>"
        f'<span style="color:#334155">·</span>'
        f'<span class="{dq_cls}">DQ: {dq_st}</span>'
    )


def _source_notice_html(summary: dict) -> str:
    if not summary.get("has_mock"):
        return ""
    ct = summary.get("mock_count", 0)
    return (
        f'<div class="source-notice">'
        f'⚠ yfinance fetch failed for {ct:,} rows — mock-fallback data active. '
        f'Values do not reflect live market data.'
        f'</div>'
    )


def _breadth_sparkline_section_html(breadth_data: Optional[dict]) -> str:
    if breadth_data is None or not breadth_data.get("history_50dma"):
        return ""
    hist = breadth_data.get("history_50dma", [])
    if not hist:
        return ""
    val   = float(breadth_data.get("pct_above_50dma") or 0)
    color = (_RGB["green"]  if val >= _BREADTH_GREEN else
             _RGB["yellow"] if val >= _BREADTH_RED   else _RGB["red"])
    spark = _sparkline(hist, "sbreadth", color, height=48)
    return f'<div class="breadth-spark-row">{spark}</div>'


def _sector_heatmap_html(sectors: list[dict]) -> str:
    periods  = list(_SECTOR_PERIODS.keys())   # ["1W", "1M", "3M", "6M"]
    y_labels = [s["name"] for s in sectors]

    z: list[list] = []
    text: list[list] = []
    for s in sectors:
        row_z, row_t = [], []
        for p in periods:
            v = s.get(p)
            row_z.append(v)
            row_t.append(f"{v:+.1f}%" if v is not None else "—")
        z.append(row_z)
        text.append(row_t)

    data = [{
        "type": "heatmap",
        "z": z,
        "x": periods,
        "y": y_labels,
        "text": text,
        "texttemplate": "%{text}",
        "textfont": {"size": 12, "color": "#1e293b"},
        "colorscale": [
            [0.0,  "rgb(220,38,38)"],
            [0.45, "rgb(254,202,202)"],
            [0.5,  "rgb(248,250,252)"],
            [0.55, "rgb(187,247,208)"],
            [1.0,  "rgb(22,163,74)"],
        ],
        "zmid": 0,
        "showscale": False,
        "xgap": 3,
        "ygap": 3,
        "hovertemplate": "<b>%{y}</b> %{x}: %{text}<extra></extra>",
    }]
    layout = {
        "margin": {"l": 120, "r": 10, "t": 8, "b": 8},
        "height": 300,
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor":  "rgba(0,0,0,0)",
        "font": {
            "family": "-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
            "size": 12,
            "color": "#1e293b",
        },
        "xaxis": {
            "side": "top",
            "tickfont": {"size": 11, "color": "#64748b"},
            "fixedrange": True,
        },
        "yaxis": {
            "autorange": "reversed",
            "tickfont": {"size": 12, "color": "#1e293b"},
            "fixedrange": True,
        },
    }
    cfg = {"displayModeBar": False}
    return (
        f'<div style="height:300px;overflow:hidden">'
        f'<div id="sector_hm" style="height:300px"></div>'
        f'</div>'
        f'<script>Plotly.newPlot("sector_hm",{json.dumps(data)},'
        f'{json.dumps(layout)},{json.dumps(cfg)});</script>'
    )


def _sector_section_html(sectors: list[dict]) -> str:
    if not sectors:
        return ""
    return f"""<div class="sector-section">
  <div class="sector-tile">
    <div class="sector-header">SECTOR PERFORMANCE</div>
    {_sector_heatmap_html(sectors)}
  </div>
</div>"""


def _industry_section_html(industries: list[dict]) -> str:
    if not industries:
        return ""

    top    = industries[:_INDUSTRY_TOP_N]
    bottom = list(reversed(industries[-_INDUSTRY_TOP_N:]))
    total  = len(industries)

    def rows_html(items: list[dict], color_cls: str) -> str:
        html = ""
        for item in items:
            perf = item["perf_1m"]
            sign = "+" if perf >= 0 else ""
            cls  = "green" if perf >= 0 else "red"
            html += (
                f'<div class="industry-row">'
                f'<span class="industry-name" title="{item["industry"]}">{item["industry"]}</span>'
                f'<span class="industry-n">{int(item["n"])} stocks</span>'
                f'<span class="industry-perf {cls}">{sign}{perf:.1f}%</span>'
                f'</div>'
            )
        return html

    return f"""<div class="industry-section">
  <div class="industry-tile">
    <div class="industry-header">
      INDUSTRY PERFORMANCE — 1M MEDIAN
      <span class="industry-header-count">({total} industries)</span>
    </div>
    <div class="industry-grid">
      <div>
        <div class="industry-sub-header top">Top {len(top)}</div>
        {rows_html(top, "green")}
      </div>
      <div>
        <div class="industry-sub-header bottom">Bottom {len(bottom)}</div>
        {rows_html(bottom, "red")}
      </div>
    </div>
  </div>
</div>"""


def _scanner_section_html(hits: list[dict], build_date: str) -> str:
    hit_date = hits[0]["date"] if hits else build_date
    count_s  = f"({len(hits)} hit{'s' if len(hits) != 1 else ''})" if hits else "(0 hits)"

    if not hits:
        body = f'<p class="scanner-empty">No pullback setups today.</p>'
    else:
        rows = ""
        for h in hits:
            rs    = h.get("rs_rank")
            perf  = h.get("perf_1m")
            dist  = h.get("dist_52w_high")
            rs_s  = f"{rs:.0f}" if rs is not None else "—"
            perf_s = (
                f'<span class="{"green" if perf >= 0 else "red"}">'
                f'{"+" if perf >= 0 else ""}{perf:.1f}%</span>'
                if perf is not None else "—"
            )
            dist_s = f"{dist:.1f}%" if dist is not None else "—"
            sector = h.get("gics_sector") or "—"
            rows += f"""
      <tr>
        <td><span class="scanner-ticker">{h['ticker']}</span></td>
        <td><span class="scanner-sector">{sector}</span></td>
        <td>{rs_s}</td>
        <td>{perf_s}</td>
        <td>{dist_s}</td>
      </tr>"""
        body = f"""
    <table class="scanner-table">
      <thead>
        <tr>
          <th>Ticker</th>
          <th>Sector</th>
          <th>RS Rank</th>
          <th>1M Perf</th>
          <th>Dist 52W High</th>
        </tr>
      </thead>
      <tbody>{rows}
      </tbody>
    </table>"""

    return f"""<div class="scanner-section">
  <div class="scanner-header">
    PULLBACK MA20
    <span class="scanner-date">{hit_date}</span>
    <span class="scanner-count">{count_s}</span>
  </div>
  {body}
</div>"""


def _sparkline(values: list, div_id: str, color: str, height: int = 40) -> str:
    if not values:
        return f'<div id="{div_id}" style="height:{height}px"></div>'

    mn, mx = min(values), max(values)
    pad = (mx - mn) * 0.15 if mx != mn else abs(mn) * 0.01 or 0.01

    fill_color = color.replace("rgb(", "rgba(").replace(")", ",0.10)")
    data = [
        # invisible baseline at min — needed for toself fill
        {
            "type": "scatter",
            "x": list(range(len(values))) + list(range(len(values) - 1, -1, -1)),
            "y": values + [mn - pad] * len(values),
            "mode": "lines",
            "line": {"width": 0},
            "fill": "toself",
            "fillcolor": fill_color,
            "hoverinfo": "skip",
            "showlegend": False,
        },
        # the actual line on top
        {
            "type": "scatter",
            "x": list(range(len(values))),
            "y": values,
            "mode": "lines",
            "line": {"color": color, "width": 2},
            "hoverinfo": "skip",
            "showlegend": False,
        },
    ]
    layout = {
        "margin": {"l": 0, "r": 0, "t": 0, "b": 0},
        "showlegend": False,
        "xaxis": {"visible": False, "fixedrange": True},
        "yaxis": {
            "visible": False,
            "fixedrange": True,
            "range": [mn - pad, mx + pad],
        },
        "height": height,
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor":  "rgba(0,0,0,0)",
    }
    cfg = {"displayModeBar": False, "staticPlot": True}
    return (
        f'<div style="height:{height}px;overflow:hidden;margin-top:10px">'
        f'<div id="{div_id}" style="height:{height}px"></div>'
        f'</div>'
        f'<script>Plotly.newPlot("{div_id}",{json.dumps(data)},'
        f'{json.dumps(layout)},{json.dumps(cfg)});</script>'
    )
