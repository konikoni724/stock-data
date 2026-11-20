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
    # 東証グロース市場250指数そのものはYahooに無いため、連動ETF（2516）で値動きを代用
    "growth250_etf": "2516.T",
}
JGB_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"

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
    out = {"date": df.index[-1].strftime("%Y-%m-%d"), "close": round(float(last["Close"]), 2)}
    for k in ("Open", "High", "Low", "Volume"):
        if k in df.columns:
            out[k.lower()] = round(float(last[k]), 2)
    if prev is not None:
        out["prev_close"] = round(float(prev["Close"]), 2)
    return out


RANKING_URLS = [
    # 松井証券 Tick回数ランキング（市場全体・上位100）
    "https://finance.matsui.co.jp/ranking-tick/index",
    "https://finance.matsui.co.jp/ranking-tick/index?page=2",
    # 株探 本日活況銘柄（予備）
    "https://kabutan.jp/warning/?mode=2_9",
]


def _num(text) -> float:
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(text))
    if not m:
        raise ValueError(text)
    return float(m.group(0).replace(",", ""))


def _parse_ranking_table(t: pd.DataFrame) -> list[dict]:
    t.columns = [str(c[-1] if isinstance(c, tuple) else c) for c in t.columns]
    name_col = next(c for c in t.columns if "銘柄" in c)
    price_col = next(c for c in t.columns if "現在値" in c or "株価" in c)
    tick_col = next(c for c in t.columns if "約定" in c)
    code_col = next((c for c in t.columns if "コード" in c and c != name_col), None)
    code_re = re.compile(r"(?<![0-9A-Z])([0-9]{3}[0-9A-Z])(?![0-9A-Z])")
    rows = []
    for _, row in t.iterrows():
        name_cell = str(row[name_col])
        src = str(row[code_col]) if code_col else name_cell
        m = code_re.search(src)
        if not m:
            continue
        code = m.group(1)
        if not code_col:  # 「フジクラ 5803 東P」のように銘柄名の中にコードがある形式
            name_cell = name_cell[:m.start()].strip()
        try:
            price, ticks = _num(row[price_col]), int(_num(row[tick_col]))
        except ValueError:
            continue
        rows.append({"code": code, "name": name_cell, "price": price, "ticks": ticks})
    return rows


def fetch_ranking() -> list[dict]:
    """約定回数（ティック回数）ランキングから、株価100円以上3,000円未満の個別株を拾う。"""
    collected: list[dict] = []
    for url in RANKING_URLS:
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            tables = pd.read_html(io.StringIO(r.text))
            table = next((t for t in tables if any("約定" in str(c) for c in t.columns)), None)
            if table is None:
                raise ValueError("約定回数の表が見つかりませんでした")
            collected += _parse_ranking_table(table)
        except Exception as e:  # noqa: BLE001
            errors.append(f"ranking {url}: {e}")
        if collected and "kabutan" not in url and url.endswith("page=2"):
            break  # 松井証券で取れたら株探は見に行かない
        time.sleep(1)
    seen, rows = set(), []
    for row in sorted(collected, key=lambda r: -r["ticks"]):
        if row["code"] in seen:
            continue
        seen.add(row["code"])
        code, price = row["code"], row["price"]
        name = row["name"]
        # ETF・ETNを除いて個別株だけ残す（コード帯と名前の両方で判定）
        is_etf = (code.isdigit() and (1300 <= int(code) <= 1699 or 2500 <= int(code) <= 2599)) \
            or any(w in name for w in ("ETF", "ＥＴＦ", "ETN", "ＥＴＮ", "レバ", "ベア", "ブル", "インバ"))
        rows.append({**row, "eligible": (100 <= price < 3000) and not is_etf})
    return rows


def fetch_jgb10y() -> dict | None:
    """財務省の国債金利（当月分CSV）から日本10年国債利回りを取る。"""
    r = requests.get(JGB_URL, headers=UA, timeout=30)
    r.raise_for_status()
    text = r.content.decode("shift_jis", errors="replace")
    rows = [line.split(",") for line in text.splitlines() if line.strip()]
    header = next(row for row in rows if "10年" in row)
    i10 = header.index("10年")
    def ok(row):
        try:
            return len(row) > i10 and re.match(r"[RH]\d", row[0].strip()) and _num(row[i10]) is not None
        except ValueError:
            return False
    data = [row for row in rows[rows.index(header) + 1:] if ok(row)]

    def to_date(w: str) -> str:  # 和暦「R8.11.19」→「2026-11-19」
        era, y, m, d = re.match(r"([RH])(\d+)\.(\d+)\.(\d+)", w.strip()).groups()
        year = int(y) + (2018 if era == "R" else 1988)
        return f"{year:04d}-{int(m):02d}-{int(d):02d}"

    last = data[-1]
    out = {"date": to_date(last[0]), "close": _num(last[i10])}
    if len(data) > 1:
        out["prev_close"] = _num(data[-2][i10])
    return out


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

    try:
        info = fetch_jgb10y()
        if info:
            latest["market"]["jgb10y"] = info
    except Exception as e:  # noqa: BLE001
        errors.append(f"jgb10y: {e}")

    latest["errors"] = errors
    DATA.mkdir(exist_ok=True)
    (DATA / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stocks": len(latest["stocks"]), "errors": errors}, ensure_ascii=False))
    return 0 if latest["stocks"] else 1


if __name__ == "__main__":
    sys.exit(main())
