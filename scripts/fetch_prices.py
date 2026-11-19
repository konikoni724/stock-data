"""ウォッチ銘柄・市場指標・約定回数ランキングを取得して data/ に保存する。

GitHub Actions から1日数回実行される。どこかで失敗しても、取れた分は保存し、
失敗は data/latest.json の errors に記録する。
"""
from __future__ import annotations

import io
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DAILY = DATA / "daily"
MARKET = DATA / "market"
JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (stock-data personal watchlist; github actions)"}

MARKET_TICKERS = {
    "nikkei225": "^N225",
    "usdjpy": "JPY=X",
    "sox": "^SOX",
    "us10y": "^TNX",
}

errors: list[str] = []


def read_tickers() -> list[str]:
    codes = []
    for line in (ROOT / "tickers.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if re.fullmatch(r"[0-9A-Z]{4}", line):
            codes.append(line)
    return codes


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[cols].dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "Date"
    return df[~df.index.duplicated(keep="last")].sort_index()


def from_yahoo(symbol: str) -> pd.DataFrame:
    df = yf.download(symbol, period="1y", interval="1d", auto_adjust=False,
                     progress=False, threads=False)
    return normalize(df)


def from_stooq(code: str) -> pd.DataFrame:
    url = f"https://stooq.com/q/d/l/?s={code.lower()}.jp&i=d"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), parse_dates=["Date"], index_col="Date")
    return normalize(df.tail(260))


def fetch_stock(code: str) -> pd.DataFrame:
    for name, fn in (("yahoo", lambda: from_yahoo(f"{code}.T")),
                     ("stooq", lambda: from_stooq(code))):
        try:
            df = fn()
            if not df.empty:
                return df
        except Exception as e:  # noqa: BLE001
            errors.append(f"{code} {name}: {e}")
        time.sleep(1)
    errors.append(f"{code}: データを取得できませんでした")
    return pd.DataFrame()


def save(df: pd.DataFrame, path: Path) -> dict | None:
    if df.empty:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    df.round(2).to_csv(path)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else None
    out = {"date": df.index[-1].strftime("%Y-%m-%d"), "close": float(last["Close"])}
    for k in ("Open", "High", "Low", "Volume"):
        if k in df.columns:
            out[k.lower()] = float(last[k])
    if prev is not None:
        out["prev_close"] = float(prev["Close"])
    return out


def fetch_ranking() -> list[dict]:
    """株探の「本日活況銘柄（約定回数）」から、株価100円以上3,000円未満の個別株を拾う。"""
    url = "https://kabutan.jp/warning/?mode=2_9"
    try:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        tables = pd.read_html(io.StringIO(r.text))
    except Exception as e:  # noqa: BLE001
        errors.append(f"ranking: {e}")
        return []
    table = next((t for t in tables if any("約定" in str(c) for c in t.columns)), None)
    if table is None:
        errors.append("ranking: 約定回数の表が見つかりませんでした")
        return []
    table.columns = [str(c[-1] if isinstance(c, tuple) else c) for c in table.columns]
    code_col = next(c for c in table.columns if "コード" in c)
    name_col = next(c for c in table.columns if "銘柄" in c)
    price_col = next(c for c in table.columns if "株価" in c)
    tick_col = next(c for c in table.columns if "約定" in c)
    rows = []
    for _, row in table.iterrows():
        code = str(row[code_col]).strip()
        try:
            price = float(str(row[price_col]).replace(",", ""))
            ticks = int(float(str(row[tick_col]).replace(",", "")))
        except ValueError:
            continue
        # ETF・ETNを除いて個別株だけ残す（コード帯と名前の両方で判定）
        name = str(row[name_col])
        is_etf = (code.isdigit() and (1300 <= int(code) <= 1699 or 2500 <= int(code) <= 2599)) \
            or any(w in name for w in ("ETF", "ＥＴＦ", "ETN", "ＥＴＮ", "レバ", "ベア", "ブル", "インバ"))
        rows.append({"code": code, "name": str(row[name_col]).strip(), "price": price,
                     "ticks": ticks, "eligible": (100 <= price < 3000) and not is_etf})
    return rows


def main() -> int:
    now = datetime.now(JST)
    latest: dict = {"updated_at_jst": now.strftime("%Y-%m-%d %H:%M"), "stocks": {},
                    "market": {}, "ranking": [], "errors": []}

    codes = read_tickers()
    ranking = fetch_ranking()
    latest["ranking"] = ranking[:30]
    picks = [r["code"] for r in ranking if r["eligible"] and r["code"] not in codes][:5]

    for code in codes + picks:
        info = save(fetch_stock(code), DAILY / f"{code}.csv")
        if info:
            info["source_list"] = "watch" if code in codes else "ranking"
            latest["stocks"][code] = info

    for name, symbol in MARKET_TICKERS.items():
        try:
            info = save(from_yahoo(symbol), MARKET / f"{name}.csv")
            if info:
                latest["market"][name] = info
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")

    latest["errors"] = errors
    DATA.mkdir(exist_ok=True)
    (DATA / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stocks": len(latest["stocks"]), "errors": errors}, ensure_ascii=False))
    return 0 if latest["stocks"] else 1


if __name__ == "__main__":
    sys.exit(main())
