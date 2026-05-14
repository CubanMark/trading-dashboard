import pandas as pd


def add_sma(df: pd.DataFrame, windows: list[int] = [20, 50, 200]) -> pd.DataFrame:
    for w in windows:
        df[f"sma{w}"] = df["close"].rolling(w).mean()
    return df


def add_sma_slope(
    df: pd.DataFrame, windows: list[int] = [50, 200], lookback: int = 20
) -> pd.DataFrame:
    for w in windows:
        col = f"sma{w}"
        if col not in df.columns:
            df[col] = df["close"].rolling(w).mean()
        df[f"{col}_slope"] = df[col] / df[col].shift(lookback) - 1
    return df


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.rolling(period).mean()
    return df


def add_adr(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    df["adr_pct"] = (
        (df["high"] - df["low"]) / df["close"]
    ).rolling(period).mean() * 100
    return df


def add_momentum(df: pd.DataFrame, windows: list[int] = [21, 63, 126]) -> pd.DataFrame:
    for w in windows:
        df[f"mom{w}d"] = df["close"] / df["close"].shift(w) - 1
    return df


def add_rs(df: pd.DataFrame, spy_close: pd.Series) -> pd.DataFrame:
    """Outperformance vs. SPY over 1M (21d) and 3M (63d)."""
    df["rs_1m"] = (
        df["close"] / df["close"].shift(21) - 1
    ) - (spy_close / spy_close.shift(21) - 1)
    df["rs_3m"] = (
        df["close"] / df["close"].shift(63) - 1
    ) - (spy_close / spy_close.shift(63) - 1)
    return df


def rs_rank(universe_rs: pd.Series) -> pd.Series:
    """Convert raw RS values to percentile rank 0-100 across the universe."""
    return universe_rs.rank(pct=True) * 100


def is_uptrend(df: pd.DataFrame) -> pd.Series:
    """Close > SMA50 > SMA200."""
    if "sma50" not in df.columns or "sma200" not in df.columns:
        df = add_sma(df, [50, 200])
    return (df["close"] > df["sma50"]) & (df["sma50"] > df["sma200"])
