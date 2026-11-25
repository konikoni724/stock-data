"""株価以外の「判断材料」を集めて data/extras.json に保存する。

- fundamentals : PER・PBR・配当利回り・時価総額・次の決算日・直近四半期の売上と営業利益（Yahoo）
- disclosures  : 会社の適時開示（TDnet。決算短信・業績修正・配当変更など）
- news         : 銘柄ごとのニュース見出し（Googleニュースを切り口別に複数回検索。topic=general/analyst/analyst_earnings/business_risk）
- market_news  : 市場全体・米国市場・経済指標のニュース見出し（Googleニュース）
- calendar     : 経済指標カレンダー（取れたサイトの表をそのまま保存）

fetch_prices.py の後に実行する。どこかで失敗しても、取れた分は保存する。
"""
from __future__ import annotations

import io
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (stock-data personal watchlist; github actions)"}
errors: list[str] = []


def names_and_codes() -> dict[str, str]:
    """tickers.txt のコメントと latest.json のランキングから「コード→銘柄名」を作る。"""
    names: dict[str, str] = {}
    for line in (ROOT / "tickers.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([0-9A-Z]{4})\s*#\s*(.+)", line)
        if m:
            names[m.group(1)] = re.sub(r"[（(].*", "", m.group(2)).strip()
    latest = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    rank_names = {r["code"]: r["name"].replace("　", " ") for r in latest.get("ranking", [])}
    for code in latest.get("stocks", {}):
        names.setdefault(code, rank_names.get(code, code))
    return names


def _num(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def fundamentals(code: str) -> dict:
    t = yf.Ticker(f"{code}.T")
    out: dict = {}
    try:
        info = t.info or {}
        out.update({
            "per": _num(info.get("trailingPE")),
            "per_forward": _num(info.get("forwardPE")),
            "pbr": _num(info.get("priceToBook")),
            "dividend_yield_pct": _num(info.get("dividendYield")),
            "market_cap": _num(info.get("marketCap")),
        })
    except Exception as e:  # noqa: BLE001
        errors.append(f"{code} info: {e}")
    try:
        cal = t.calendar or {}
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            out["next_earnings"] = [str(d) for d in dates]
            if dates and all(str(d) < datetime.now(JST).strftime("%Y-%m-%d") for d in dates):
                out["next_earnings_note"] = "過去の日付（Yahoo側が未更新）。earnings_schedule を優先し、無ければ「次回は未発表（前回＋約3か月が目安）」と書く"
    except Exception as e:  # noqa: BLE001
        errors.append(f"{code} calendar: {e}")
    try:
        q = t.quarterly_income_stmt
        rows = []
        if q is not None and not q.empty:
            for col in list(q.columns)[:5]:
                rows.append({
                    "period_end": str(pd.Timestamp(col).date()),
                    "revenue": _num(q.at["Total Revenue", col]) if "Total Revenue" in q.index else None,
                    "operating_income": _num(q.at["Operating Income", col]) if "Operating Income" in q.index else None,
                    "net_income": _num(q.at["Net Income", col]) if "Net Income" in q.index else None,
                })
        out["quarterly"] = rows
    except Exception as e:  # noqa: BLE001
        errors.append(f"{code} quarterly: {e}")
    return out


def disclosures(code: str) -> list[dict]:
    """TDnet の適時開示（やのしん TDnet WEB-API 経由）。"""
    url = f"https://webapi.yanoshin.jp/webapi/tdnet/list/{code}.json?limit=8"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    items = []
    for it in r.json().get("items", []):
        d = it.get("Tdnet", it)
        items.append({"time": d.get("pubdate"), "title": d.get("title"),
                      "url": d.get("document_url")})
    return items


def google_news(query: str, limit: int = 6) -> list[dict]:
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ja&gl=JP&ceid=JP:ja"
    for wait in (0, 10, 30):  # 混み合い（429）のときは少し待ってやり直す
        if wait:
            time.sleep(wait)
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code != 429:
            break
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for item in root.iter("item"):
        pub = item.findtext("pubDate")
        try:
            pub = parsedate_to_datetime(pub).astimezone(JST).strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            pass
        src = item.find("source")
        items.append({"title": item.findtext("title"), "source": src.text if src is not None else None,
                      "published_jst": pub, "url": item.findtext("link")})
    items.sort(key=lambda x: x.get("published_jst") or "", reverse=True)
    return items[:limit]


def rss_feed(url: str, limit: int = 10) -> list[dict]:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for item in root.iter("item"):
        pub = item.findtext("pubDate")
        try:
            pub = parsedate_to_datetime(pub).astimezone(JST).strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            pass
        items.append({"title": item.findtext("title"), "source": "NHK", "published_jst": pub,
                      "url": item.findtext("link")})
    items.sort(key=lambda x: x.get("published_jst") or "", reverse=True)
    return items[:limit]


# 検索で使う短い名前（長い社名だと記事に当たりにくい銘柄だけ）
SHORT_NAMES = {
    "7532": "パンパシフィック OR PPIH OR ドン・キホーテ",
    "8306": "三菱UFJ OR 三菱ＵＦＪ OR MUFG",
    "6723": "ルネサス",
    "6752": "パナソニック",
    "8766": "東京海上",
    "4755": "楽天グループ OR 楽天G",
    "3436": "SUMCO",
}

# 1銘柄あたりの検索の切り口（topic名, 検索語の後ろにつける言葉, 期間）
NEWS_TOPICS = [
    ("general", "株", "3d"),
    ("analyst", "アナリスト評価 OR アイフィスジャパン OR 目標株価 引き上げ OR 目標株価 引き下げ", "14d"),
    ("analyst_earnings", "目標株価 OR レーティング OR 業績 OR 決算 OR 上方修正 OR 下方修正 OR 配当", "7d"),
    ("business_risk", "受注 OR 提携 OR 新製品 OR 増産 OR 買収 OR 不正 OR 事故 OR 漏えい OR 訴訟 OR 行政処分", "7d"),
]


def short_name(code: str, name: str) -> str:
    if code in SHORT_NAMES:
        return SHORT_NAMES[code]
    for w in ("株式会社", "ホールディングス", "グループ", "フィナンシャル"):
        name = name.replace(w, "")
    return name.strip() or name


def stock_news(code: str, name: str, per_topic: int = 6, limit: int = 15) -> list[dict]:
    """切り口を変えて何回か検索し、重複を除いてまとめる（topic で何の検索で見つかったか分かる）"""
    base = short_name(code, name)
    seen: set[str] = set()
    merged: list[dict] = []
    for topic, words, period in NEWS_TOPICS:
        q = f"({base}) {words} when:{period}"
        try:
            items = google_news(q, limit=per_topic)
        except Exception:  # noqa: BLE001
            continue
        for it in items:
            key = re.sub(r"\s+", "", (it.get("title") or "").split(" - ")[0])
            if not key or key in seen:
                continue
            seen.add(key)
            it["topic"] = topic
            merged.append(it)
        time.sleep(1.5)
    merged.sort(key=lambda x: x.get("published_jst") or "", reverse=True)
    return merged[:limit]


CALENDAR_URLS = [
    "https://www.gaitame.com/markets/calendar/",
    "https://info.finance.yahoo.co.jp/fx/marketcalendar/",
    "https://fx.minkabu.jp/indicators",
]


def calendar() -> dict:
    for url in CALENDAR_URLS:
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            tables = pd.read_html(io.StringIO(r.text))
            big = [t for t in tables if len(t) >= 5]
            if not big:
                raise ValueError("表が見つかりませんでした")
            rows = []
            for t in big[:3]:
                t = t.astype(str)
                rows += [" | ".join(str(v) for v in row if str(v) not in ("", "nan")) for row in t.values.tolist()]
            return {"source": url, "rows": rows[:80]}
        except Exception as e:  # noqa: BLE001
            errors.append(f"calendar {url}: {e}")
        time.sleep(1)
    return {}


# ---- 決算短信などのPDFを中身まで読む ----
FILING_WORDS = ("決算短信", "業績予想", "修正", "配当", "自己株式", "決算説明", "月次")


def pdf_text(url: str, max_pages: int = 4, max_chars: int = 7000) -> str:
    import pdfplumber
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        texts = []
        for page in pdf.pages[:max_pages]:
            texts.append(page.extract_text() or "")
    text = re.sub(r"[ \t]+", " ", "\n".join(texts))
    return text[:max_chars]


def filings(code: str, items: list[dict], now: datetime) -> list[dict]:
    """直近14日の重要開示（最大3件）＋、それより古くても「最新の決算短信」と「最新の業績予想の修正」（120日以内）は必ず入れる"""
    picked: list[dict] = []
    seen_kinds: set[str] = set()

    def kind(title: str) -> str | None:
        if "決算短信" in title:
            return "tanshin"
        if "業績予想" in title and "修正" in title:
            return "revision"
        return None

    def age(t: str):
        try:
            return now - datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        except ValueError:
            return None

    recent = []
    for it in items:
        title, t = it.get("title") or "", it.get("time") or ""
        a = age(t)
        if a is None or not any(w in title for w in FILING_WORDS):
            continue
        if a <= timedelta(days=14) and len(recent) < 3:
            recent.append(it)
    picked.extend(recent)
    for it in recent:
        k = kind(it.get("title") or "")
        if k:
            seen_kinds.add(k)
    for it in items:  # 新しい順に並んでいる
        title, t = it.get("title") or "", it.get("time") or ""
        k, a = kind(title), age(t)
        if k and k not in seen_kinds and a is not None and a <= timedelta(days=120):
            picked.append(it)
            seen_kinds.add(k)

    out = []
    for it in picked:
        url = (it.get("url") or "").split("rd.php?", 1)[-1]
        a = age(it.get("time") or "")
        try:
            out.append({"time": it.get("time"), "title": it.get("title"), "url": url,
                        "older_than_14d": bool(a and a > timedelta(days=14)),
                        "text": pdf_text(url)})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{code} filing pdf: {e}")
        time.sleep(1)
    return out


# ---- 決算発表予定日（日本取引所）・中央銀行の会合日程 ----
def jpx_earnings_schedule(codes: list[str]) -> dict:
    """JPXの「決算発表予定日」一覧（Excel）から、ウォッチ銘柄の予定日を探す"""
    out: dict = {}
    page_url = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
    r = requests.get(page_url, headers=UA, timeout=30)
    r.raise_for_status()
    links = sorted(set(re.findall(r'href="([^"]+\.xlsx?)"', r.text)))
    for link in links[:6]:
        url = link if link.startswith("http") else "https://www.jpx.co.jp" + link
        try:
            x = requests.get(url, headers=UA, timeout=60)
            x.raise_for_status()
            sheets = pd.read_excel(io.BytesIO(x.content), sheet_name=None, header=None)
        except Exception as e:  # noqa: BLE001
            errors.append(f"jpx earnings xlsx {url}: {e}")
            continue
        for df in sheets.values():
            for row in df.itertuples(index=False):
                cells = [c for c in row if pd.notna(c)]
                strs = [str(c).strip() for c in cells]
                hit = next((c for c in codes if any(s in (c, c + "0", f"{c}.0") for s in strs)), None)
                if not hit:
                    continue
                for c in cells:
                    d = None
                    if isinstance(c, (datetime, pd.Timestamp)):
                        d = pd.Timestamp(c)
                    elif isinstance(c, str) and re.match(r"^20\d\d[/-]\d{1,2}[/-]\d{1,2}", c):
                        d = pd.Timestamp(c[:10].replace("/", "-"))
                    if d is not None:
                        out.setdefault(hit, set()).add(d.strftime("%Y-%m-%d"))
                        break
    return {k: sorted(v) for k, v in out.items()}


def key_events(now: datetime, days: int = 70) -> list[dict]:
    """手で管理している中央銀行の会合日程（key_events.json）から、近い予定を出す"""
    path = DATA.parent / "key_events.json"
    if not path.exists():
        return []
    ev = json.loads(path.read_text(encoding="utf-8"))
    today = now.date()
    out = []
    for e in ev.get("events", []):
        end = datetime.strptime(e["end"], "%Y-%m-%d").date()
        if today <= end <= today + timedelta(days=days):
            out.append({**e, "days_until_end": (end - today).days})
    return sorted(out, key=lambda x: x["end"])


# ---- 需給データ（日本取引所） ----
JPX = "https://www.jpx.co.jp"


def _latest_link(page: str, pattern: str) -> str | None:
    r = requests.get(JPX + page, headers=UA, timeout=30)
    r.raise_for_status()
    links = re.findall(r'href="([^"]*' + pattern + ')"', r.text)
    return (JPX + links[0]) if links and links[0].startswith("/") else (links[0] if links else None)


def jpx_supply(codes: list[str]) -> dict:
    import pdfplumber
    out: dict = {}
    # 日々公表銘柄の信用残（その日に公表対象になっている銘柄だけ載る）
    try:
        url = _latest_link("/markets/statistics-equities/margin/01.html", r"_mtall\.pdf")
        r = requests.get(url, headers=UA, timeout=60)
        r.raise_for_status()
        lines = []
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for page in pdf.pages:
                lines += (page.extract_text() or "").splitlines()
        hits = {c: [ln for ln in lines if re.search(rf"(?<!\d){c}(?!\d)", ln)][:3] for c in codes}
        out["daily_margin"] = {"source": url, "header": lines[:6],
                               "rows": {c: v for c, v in hits.items() if v}}
    except Exception as e:  # noqa: BLE001
        errors.append(f"jpx daily_margin: {e}")
    # 逆日歩（品貸料）
    try:
        url = JPX + "/markets/statistics-equities/margin/tvdivq0000001rb8-att/Premium_Charges.xlsx"
        df = pd.read_excel(url, header=None, dtype=str, storage_options=UA)
        rows = {}
        for c in codes:
            m = df[df.apply(lambda r: any(str(v).strip().startswith(c) for v in r.values), axis=1)]
            if not m.empty:
                rows[c] = [" | ".join(v for v in r if v and v != "nan") for r in m.astype(str).values.tolist()][:2]
        out["premium_charges"] = {"source": url, "rows": rows}
    except Exception as e:  # noqa: BLE001
        errors.append(f"jpx premium: {e}")
    # 空売り比率（市場全体）
    try:
        url = _latest_link("/markets/statistics-equities/short-selling/index.html", r"-m\.pdf")
        out["short_selling"] = {"source": url, "text": pdf_text(url, max_pages=1, max_chars=2500)}
    except Exception as e:  # noqa: BLE001
        errors.append(f"jpx short_selling: {e}")
    # 信用取引現在高（市場全体・週次）
    try:
        url = _latest_link("/markets/statistics-equities/margin/04.html", r"_mtcurrent\.pdf")
        out["margin_total_weekly"] = {"source": url, "text": pdf_text(url, max_pages=1, max_chars=2500)}
    except Exception as e:  # noqa: BLE001
        errors.append(f"jpx margin_total: {e}")
    return out


def main() -> int:
    now = datetime.now(JST)
    names = names_and_codes()
    out: dict = {"updated_at_jst": now.strftime("%Y-%m-%d %H:%M"), "names": names,
                 "fundamentals": {}, "disclosures": {}, "news": {}, "market_news": {},
                 "calendar": {}, "filings": {}, "supply": {}, "earnings_schedule": {},
                 "key_events": [], "errors": []}

    for code, name in names.items():
        out["fundamentals"][code] = fundamentals(code)
        try:
            out["disclosures"][code] = disclosures(code)
            f = filings(code, out["disclosures"][code], now)
            if f:
                out["filings"][code] = f
        except Exception as e:  # noqa: BLE001
            errors.append(f"{code} disclosures: {e}")
        try:
            out["news"][code] = stock_news(code, name)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{code} news: {e}")
        time.sleep(1)

    for key, q in {
        "japan_market": "日経平均 株式市場 when:1d",
        "us_market": "米国株 ダウ ナスダック SOX when:1d",
        "economic_indicators": "米経済指標 今夜 発表予定 when:2d",
        "boj_fed": "日銀 金融政策決定会合 OR FOMC when:3d",
        "analyst_moves": "目標株価 引き上げ OR 引き下げ OR レーティング when:1d",
        "semiconductor_ai": "半導体株 OR AI関連株 OR データセンター 株価 when:2d",
        "nonferrous_wire": "電線株 OR 非鉄株 OR 光ファイバー 需要 when:3d",
        "us_earnings": "米国 決算発表 株価 when:2d",
        "forex_rates": "ドル円 OR 長期金利 OR 円相場 when:1d",
    }.items():
        try:
            out["market_news"][key] = google_news(q, limit=8)
        except Exception as e:  # noqa: BLE001
            errors.append(f"market_news {key}: {e}")

    # Googleニュース以外の無料の見出し配信
    for key, url in {"nhk_economy": "https://www3.nhk.or.jp/rss/news/cat5.xml"}.items():
        try:
            out["market_news"][key] = rss_feed(url, limit=10)
        except Exception as e:  # noqa: BLE001
            errors.append(f"market_news {key}: {e}")

    try:
        out["earnings_schedule"] = jpx_earnings_schedule(list(names))
    except Exception as e:  # noqa: BLE001
        errors.append(f"jpx earnings schedule: {e}")
    out["key_events"] = key_events(now)

    out["calendar"] = calendar()
    out["supply"] = jpx_supply(list(names))
    out["errors"] = errors
    (DATA / "extras.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"codes": len(names), "errors": errors}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
