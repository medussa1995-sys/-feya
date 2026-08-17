#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
THRESHOLDS = ROOT / "thresholds.json"

SYMBOLS = ["HYPEUSDT", "PENDLEUSDT", "ONDOUSDT", "WIFUSDT"]
WINDOW_DAYS = 180
Q = 0.80
RETENTION_DAYS = 185
BASE = "https://data.binance.vision/data/futures/um/daily"
TIMEOUT = 40

session = requests.Session()
session.headers.update({"User-Agent": "Feya-1.0-GitHub-Action/2.0"})


def get_zip_csv(url: str) -> pd.DataFrame:
    r = session.get(url, timeout=TIMEOUT)
    if r.status_code == 404:
        raise FileNotFoundError(url)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError(f"No CSV inside {url}")
        return pd.read_csv(z.open(names[0]))


def metrics_day(symbol: str, day: date) -> pd.DataFrame:
    ds = day.isoformat()
    df = get_zip_csv(f"{BASE}/metrics/{symbol}/{symbol}-metrics-{ds}.zip")
    if "create_time" not in df.columns or "sum_open_interest_value" not in df.columns:
        raise RuntimeError(f"{symbol} {ds}: unexpected metrics columns")
    out = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(df["create_time"], utc=True, errors="coerce"),
            "oi_value": pd.to_numeric(df["sum_open_interest_value"], errors="coerce"),
        }
    )
    return out.dropna().drop_duplicates("timestamp").sort_values("timestamp")


def klines_day(symbol: str, day: date) -> pd.DataFrame:
    ds = day.isoformat()
    df = get_zip_csv(f"{BASE}/klines/{symbol}/1m/{symbol}-1m-{ds}.zip")

    # Binance Vision files may arrive with or without column names.
    if "open_time" not in df.columns:
        cols = [
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "count", "taker_buy_volume",
            "taker_buy_quote_volume", "ignore",
        ]
        df.columns = cols[: len(df.columns)]

    if "open_time" not in df.columns:
        raise RuntimeError(f"{symbol} {ds}: no open_time in kline archive")

    qcol = "quote_volume" if "quote_volume" in df.columns else "quote_asset_volume"
    if qcol not in df.columns:
        raise RuntimeError(f"{symbol} {ds}: no quote-volume column in kline archive")

    ot = pd.to_numeric(df["open_time"], errors="coerce")
    valid = ot.dropna()
    if valid.empty:
        raise RuntimeError(f"{symbol} {ds}: empty open_time")

    # Newer Binance Vision archives can use microseconds; older ones milliseconds.
    unit = "us" if valid.median() > 1e14 else "ms"
    out = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(ot, unit=unit, utc=True, errors="coerce"),
            "quote_volume": pd.to_numeric(df[qcol], errors="coerce"),
        }
    )
    return out.dropna().drop_duplicates("timestamp").sort_values("timestamp")


def ratios_for_day(symbol: str, day: date) -> pd.DataFrame:
    """Reconstruct the same ratio used in the research:
    sum_open_interest_value / trailing 24h USDT quote turnover.

    We need the prior calendar day of 1m quote-volume so every metric point on
    `day` has a full trailing-24h window available.
    """
    prev = day - timedelta(days=1)
    k = pd.concat([klines_day(symbol, prev), klines_day(symbol, day)], ignore_index=True)
    k = k.drop_duplicates("timestamp").sort_values("timestamp").set_index("timestamp")

    # Time-based rolling window, matching a trailing 24h turnover rather than a UTC-day total.
    k["turnover_24h"] = k["quote_volume"].rolling("24h", min_periods=1440).sum()

    m = metrics_day(symbol, day).set_index("timestamp")

    # Metrics timestamps are normally aligned to minute boundaries. Reindex onto
    # the 1m turnover series and allow at most 59 seconds tolerance as a safeguard.
    turnover = k["turnover_24h"].reindex(m.index, method="ffill", tolerance=pd.Timedelta(seconds=59))
    x = m.copy()
    x["turnover_24h"] = turnover
    x["ratio"] = x["oi_value"] / x["turnover_24h"]
    x = x.replace([np.inf, -np.inf], np.nan).dropna(subset=["ratio"])
    return x.reset_index()[["timestamp", "ratio"]]


def load_history(symbol: str) -> pd.DataFrame:
    path = DATA / f"{symbol}_ratio_history.csv"
    if not path.exists():
        raise RuntimeError(f"Missing seed history: {path.name}")
    hist = pd.read_csv(path)
    if not {"timestamp", "ratio"}.issubset(hist.columns):
        raise RuntimeError(f"{path.name}: expected timestamp,ratio columns")
    hist["timestamp"] = pd.to_datetime(hist["timestamp"], utc=True, errors="coerce")
    hist["ratio"] = pd.to_numeric(hist["ratio"], errors="coerce")
    return hist.dropna().drop_duplicates("timestamp", keep="last").sort_values("timestamp")


def missing_days(hist: pd.DataFrame, through: date) -> list[date]:
    if hist.empty:
        raise RuntimeError("History is empty")
    last_date = hist["timestamp"].max().date()
    start = last_date + timedelta(days=1)
    if start > through:
        return []
    return [start + timedelta(days=i) for i in range((through - start).days + 1)]


def update_symbol(symbol: str, through: date) -> dict:
    path = DATA / f"{symbol}_ratio_history.csv"
    hist = load_history(symbol)
    added = 0

    for day in missing_days(hist, through):
        try:
            new = ratios_for_day(symbol, day)
        except FileNotFoundError:
            # Daily archive may not be published yet. Stop here so we do not create gaps.
            print(f"{symbol}: archive not yet available for {day}; keeping previous threshold", file=sys.stderr)
            break
        hist = pd.concat([hist, new], ignore_index=True)
        hist = hist.dropna().drop_duplicates("timestamp", keep="last").sort_values("timestamp")
        added += len(new)

    if hist.empty:
        raise RuntimeError(f"{symbol}: empty history")

    latest = hist["timestamp"].max()
    cutoff = latest - pd.Timedelta(days=WINDOW_DAYS)
    win = hist[hist["timestamp"] >= cutoff].copy()

    # 180 days of 5m Binance metrics is ~51,840 points. Require enough points to
    # make an accidental short-history recalculation impossible.
    if len(win) < 20_000:
        raise RuntimeError(f"{symbol}: insufficient 180d history ({len(win)} points)")

    threshold = float(win["ratio"].quantile(Q))

    keep_from = latest - pd.Timedelta(days=RETENTION_DAYS)
    hist = hist[hist["timestamp"] >= keep_from]
    hist.to_csv(path, index=False)

    return {
        "threshold": threshold,
        "latest": latest,
        "points": int(len(win)),
        "added": int(added),
    }


def main() -> None:
    now = datetime.now(timezone.utc)
    # Yesterday is the newest complete UTC day we attempt to ingest.
    through = now.date() - timedelta(days=1)

    old = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    new = json.loads(json.dumps(old))

    # Fail-safe: thresholds.json is written only if all four symbols succeed.
    results: dict[str, dict] = {}
    for symbol in SYMBOLS:
        results[symbol] = update_symbol(symbol, through)

    for symbol, r in results.items():
        new["symbols"][symbol]["threshold"] = r["threshold"]
        new["symbols"][symbol]["history_points"] = r["points"]
        new["symbols"][symbol]["history_through_utc"] = r["latest"].isoformat().replace("+00:00", "Z")

    common_latest = min(r["latest"] for r in results.values())
    new["version"] = "Feya 1.0"
    new["role"] = "crowding_gate_only"
    new["method"] = "adaptive_q80_rolling_180d"
    new["window_days"] = WINDOW_DAYS
    new["quantile"] = Q
    new["updated_at_utc"] = now.isoformat().replace("+00:00", "Z")
    new["history_through_utc"] = common_latest.isoformat().replace("+00:00", "Z")
    new["status"] = "ok"

    THRESHOLDS.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")

    for symbol, r in results.items():
        print(
            f"{symbol}: Q80={r['threshold']:.10f}, through={r['latest']}, "
            f"points={r['points']}, added={r['added']}"
        )


if __name__ == "__main__":
    main()
