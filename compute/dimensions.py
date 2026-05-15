"""
Compute the 6 market-state dimension metrics from the live database.

Each function returns a standardised dict:
    {
        "metric_id":   str,
        "value":       float | None,
        "label":       str,          # human-readable main value
        "status":      str,          # "green" | "yellow" | "red" | "na"
        "trend":       str,          # "up" | "down" | "flat"
        "note":        str,          # context line rendered below value
        "prior_value": float | None,
        "change_1w":   float | None,
    }
"""

import sqlite3

import pandas as pd


def _na(metric_id: str, label: str, note: str) -> dict:
    return {
        "metric_id": metric_id, "value": None, "label": label,
        "status": "na", "trend": "flat", "note": note,
        "prior_value": None, "change_1w": None,
    }


def _macro_close(conn: sqlite3.Connection, series_id: str, limit: int = 30) -> pd.Series:
    """Return a date-indexed Series of close values for a macro series."""
    df = pd.read_sql(
        "SELECT date, value FROM macro_series WHERE series_id=? ORDER BY date DESC LIMIT ?",
        conn, params=(series_id, limit),
    )
    if df.empty:
        return pd.Series(dtype=float, name=series_id)
    return df.sort_values("date").set_index("date")["value"].astype(float)


# ---------------------------------------------------------------------------
# Dimension 1 — Breadth
# ---------------------------------------------------------------------------

def compute_breadth(conn: sqlite3.Connection) -> dict:
    df = pd.read_sql(
        "SELECT date, pct_above_50dma, pct_above_200dma, new_highs_52w, new_lows_52w"
        " FROM breadth_daily ORDER BY date DESC LIMIT 10",
        conn,
    )
    if df.empty or df["pct_above_50dma"].isna().all():
        return _na("breadth", "No data", "breadth_daily table is empty")

    df = df.sort_values("date").reset_index(drop=True)
    latest = df.iloc[-1]
    val    = float(latest["pct_above_50dma"])

    if val >= 60:   status = "green"
    elif val >= 40: status = "yellow"
    else:           status = "red"

    prior = None
    if len(df) >= 6:
        pv = df.iloc[-6]["pct_above_50dma"]
        if pd.notna(pv):
            prior = float(pv)

    change_1w = round(val - prior, 1) if prior is not None else None
    trend = ("up"   if change_1w is not None and change_1w > 0.5 else
             "down" if change_1w is not None and change_1w < -0.5 else "flat")

    pct200 = latest.get("pct_above_200dma")
    nh     = latest.get("new_highs_52w")
    nl     = latest.get("new_lows_52w")
    parts: list[str] = []
    if pd.notna(pct200): parts.append(f"{float(pct200):.1f}% > SMA200")
    if pd.notna(nh) and pd.notna(nl): parts.append(f"NH/NL {int(nh)}/{int(nl)}")
    note = " | ".join(parts)

    return {
        "metric_id":   "breadth",
        "value":       round(val, 1),
        "label":       f"{val:.1f}% > SMA50",
        "status":      status,
        "trend":       trend,
        "note":        note,
        "prior_value": round(prior, 1) if prior is not None else None,
        "change_1w":   change_1w,
    }


# ---------------------------------------------------------------------------
# Dimension 2 — Risk On/Off (XLY / XLP ratio)
# ---------------------------------------------------------------------------

def compute_risk(conn: sqlite3.Connection) -> dict:
    xly = _macro_close(conn, "XLY", 30)
    xlp = _macro_close(conn, "XLP", 30)

    combined = pd.DataFrame({"xly": xly, "xlp": xlp}).dropna()
    if len(combined) < 2:
        return _na("risk", "XLY/XLP", "No sector ETF data in macro_series")

    ratio_s = combined["xly"] / combined["xlp"]
    ratio   = float(ratio_s.iloc[-1])

    prior_ratio = float(ratio_s.iloc[-21]) if len(ratio_s) >= 21 else None
    change_1w   = round(ratio - float(ratio_s.iloc[-6]), 3) if len(ratio_s) >= 6 else None

    if prior_ratio is not None:
        if ratio > prior_ratio * 1.005:   status, trend = "green", "up"
        elif ratio < prior_ratio * 0.995: status, trend = "red",   "down"
        else:                             status, trend = "yellow", "flat"
    else:
        status, trend = "yellow", "flat"

    if status == "green":
        note = "Risk-On: Discretionary outpacing Staples"
    elif status == "red":
        note = "Risk-Off: Defensives outpacing Discretionary"
    else:
        note = "Neutral: Consumer sectors tracking closely"

    return {
        "metric_id":   "risk",
        "value":       round(ratio, 3),
        "label":       f"XLY/XLP {ratio:.2f}",
        "status":      status,
        "trend":       trend,
        "note":        note,
        "prior_value": round(prior_ratio, 3) if prior_ratio is not None else None,
        "change_1w":   change_1w,
    }


# ---------------------------------------------------------------------------
# Dimension 3 — Volatility (VIX + term structure)
# ---------------------------------------------------------------------------

def compute_volatility(conn: sqlite3.Connection) -> dict:
    vix   = _macro_close(conn, "^VIX",   10)
    vix3m = _macro_close(conn, "^VIX3M",  5)

    if vix.empty:
        return _na("volatility", "VIX", "No VIX data in macro_series")

    level = float(vix.iloc[-1])
    prior = float(vix.iloc[-6]) if len(vix) >= 6 else None

    if level < 20:   status = "green"
    elif level < 30: status = "yellow"
    else:            status = "red"

    change_1w = round(level - prior, 1) if prior is not None else None
    trend = ("up"   if change_1w is not None and change_1w > 0.5 else
             "down" if change_1w is not None and change_1w < -0.5 else "flat")

    note = ""
    if not vix3m.empty:
        v3m = float(vix3m.iloc[-1])
        if v3m > 0:
            ts = level / v3m
            if ts > 1.0:
                note = f"Backwardation VIX/VIX3M {ts:.2f} — elevated near-term stress"
            else:
                note = f"Contango VIX/VIX3M {ts:.2f} — normal term structure"

    return {
        "metric_id":   "volatility",
        "value":       round(level, 1),
        "label":       f"VIX {level:.1f}",
        "status":      status,
        "trend":       trend,
        "note":        note,
        "prior_value": round(prior, 1) if prior is not None else None,
        "change_1w":   change_1w,
    }


# ---------------------------------------------------------------------------
# Dimension 4 — OB/OS (SPY vs SMA50 in ATR units)
# ---------------------------------------------------------------------------

def compute_obos(conn: sqlite3.Connection) -> dict:
    df = pd.read_sql(
        "SELECT date, value AS close, high, low"
        " FROM macro_series WHERE series_id='SPY' ORDER BY date DESC LIMIT 70",
        conn,
    ).sort_values("date").reset_index(drop=True)

    if len(df) < 52:
        return _na("obos", "SPY vs SMA50", "Insufficient SPY history (need 52+ rows)")

    close     = df["close"].astype(float)
    sma50_val = close.rolling(50).mean().iloc[-1]

    if pd.isna(sma50_val):
        return _na("obos", "SPY vs SMA50", "Cannot compute SMA50")

    # ATR14: use OHLC if available, fall back to std-based estimate
    has_ohl = (df["high"].notna().sum() >= 15) and (df["low"].notna().sum() >= 15)
    if has_ohl:
        high, low = df["high"].astype(float), df["low"].astype(float)
        prev_c = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_c).abs(), (low - prev_c).abs()],
            axis=1,
        ).max(axis=1)
        atr14 = float(tr.rolling(14).mean().iloc[-1])
    else:
        std14 = close.pct_change().rolling(14).std().iloc[-1]
        atr14 = float(std14) * float(close.iloc[-1]) if pd.notna(std14) else float("nan")

    spy_close = float(close.iloc[-1])
    if pd.isna(atr14) or atr14 == 0:
        return _na("obos", "SPY vs SMA50", "Cannot compute ATR14")

    z = (spy_close - float(sma50_val)) / atr14

    if abs(z) < 2:   status = "green"
    elif abs(z) < 3: status = "yellow"
    else:            status = "red"

    src  = "ATR14" if has_ohl else "est. ATR14"
    note = f"SPY {spy_close:.1f} vs SMA50 {float(sma50_val):.1f} ({src} {atr14:.1f})"

    return {
        "metric_id":   "obos",
        "value":       round(z, 2),
        "label":       f"{z:+.1f}σ vs SMA50",
        "status":      status,
        "trend":       "up" if z > 0 else "down",
        "note":        note,
        "prior_value": None,
        "change_1w":   None,
    }


# ---------------------------------------------------------------------------
# Dimension 5 — Sentiment (CNN Fear & Greed, stored daily in macro_series)
# ---------------------------------------------------------------------------

def compute_sentiment(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT value, date FROM macro_series WHERE series_id='CNN_FNG'"
        " ORDER BY date DESC LIMIT 1"
    ).fetchone()

    if row is None:
        return _na("sentiment", "Fear & Greed", "CNN F&G not yet fetched — runs with daily build")

    score  = float(row[0])
    date_s = row[1]

    if score < 25:
        status, rating = "red",    "Extreme Fear"
        note = f"Contrarian: extreme fear often marks bottoms · {date_s}"
    elif score > 75:
        status, rating = "red",    "Extreme Greed"
        note = f"Contrarian: extreme greed often marks tops · {date_s}"
    elif score < 45:
        status, rating = "yellow", "Fear"
        note = f"Below neutral · {date_s}"
    elif score > 55:
        status, rating = "yellow", "Greed"
        note = f"Above neutral · {date_s}"
    else:
        status, rating = "yellow", "Neutral"
        note = f"Neutral zone · {date_s}"

    rows_1w = conn.execute(
        "SELECT value FROM macro_series WHERE series_id='CNN_FNG'"
        " ORDER BY date DESC LIMIT 6"
    ).fetchall()
    change_1w = None
    trend = "flat"
    if len(rows_1w) >= 6:
        prior     = float(rows_1w[-1][0])
        change_1w = round(score - prior, 1)
        trend = "up" if change_1w > 2 else "down" if change_1w < -2 else "flat"

    return {
        "metric_id":   "sentiment",
        "value":       round(score, 1),
        "label":       f"F&G {score:.0f} · {rating}",
        "status":      status,
        "trend":       trend,
        "note":        note,
        "prior_value": None,
        "change_1w":   change_1w,
    }


# ---------------------------------------------------------------------------
# Dimension 6 — Credit (FRED HY OAS or N/A)
# ---------------------------------------------------------------------------

def compute_credit(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT value, date FROM macro_series WHERE series_id='BAMLH0A0HYM2'"
        " ORDER BY date DESC LIMIT 1"
    ).fetchone()

    if row is None:
        return _na("credit", "HY OAS", "FRED not configured — set FRED_API_KEY")

    oas    = float(row[0])
    date_s = row[1]

    if oas < 350:   status = "green"
    elif oas < 500: status = "yellow"
    else:           status = "red"

    rows_1w = conn.execute(
        "SELECT value FROM macro_series WHERE series_id='BAMLH0A0HYM2'"
        " ORDER BY date DESC LIMIT 6"
    ).fetchall()
    change_1w = None
    if len(rows_1w) >= 6:
        prior_oas = float(rows_1w[-1][0])
        change_1w = round(oas - prior_oas, 0)

    return {
        "metric_id":   "credit",
        "value":       round(oas, 0),
        "label":       f"HY OAS {oas:.0f}bp",
        "status":      status,
        "trend":       ("up"   if change_1w and change_1w > 5 else
                        "down" if change_1w and change_1w < -5 else "flat"),
        "note":        f"US HY Option-Adjusted Spread · {date_s}",
        "prior_value": None,
        "change_1w":   change_1w,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_all_dimensions(conn: sqlite3.Connection) -> list[dict]:
    return [
        compute_breadth(conn),
        compute_risk(conn),
        compute_volatility(conn),
        compute_obos(conn),
        compute_sentiment(conn),
        compute_credit(conn),
    ]
