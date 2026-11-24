"""チャート分析つきの画像を作る（レポート用）

使い方（例）:
  python scripts/make_chart.py 5801 --name 古河電工 --entry 1781 --review 1500 --targets 2500,3700,5000 --out /tmp/5801.png
  python scripts/make_chart.py 5016 --name JX金属 --stop 1450 --take 1700,1850 --out /tmp/5016.png

描くもの:
  ローソク足・5/25/75日線・ボリンジャーバンド±2σ・一目均衡表の雲・フィボナッチ・リトレースメント・
  支持線/抵抗線（直近20日・60日の高値安値）・未埋めの窓・売買サイン（ゴールデン/デッドクロス、MACDクロス）・
  出来高（20日平均つき）・RSI・MACD・保有株なら取得単価/見直しライン/利確目安、候補なら撤退/利確の線
最後に分析の数字をJSONで表示する（レポート本文でそのまま使える）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIB = [0.236, 0.382, 0.5, 0.618, 0.786]

for f in ("Noto Sans CJK JP", "Noto Sans JP", "IPAexGothic", "IPAGothic", "TakaoGothic"):
    if any(f in x.name for x in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = f
        break
plt.rcParams["axes.unicode_minus"] = False

UP, DOWN = "#d64550", "#2f6fb2"  # 日本式：陽線=赤、陰線=青
INK, MUTED, GRID = "#1f2933", "#6b7785", "#e6e9ee"


def load(code: str, csv: str | None) -> pd.DataFrame:
    p = Path(csv) if csv else ROOT / "data" / "daily" / f"{code}.csv"
    df = pd.read_csv(p, parse_dates=["Date"]).set_index("Date").sort_index()
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"]
    for n in (5, 25, 75):
        df[f"ma{n}"] = c.rolling(n).mean()
    sd = c.rolling(20).std()
    df["bb_mid"] = c.rolling(20).mean()
    df["bb_up"], df["bb_dn"] = df["bb_mid"] + 2 * sd, df["bb_mid"] - 2 * sd
    d = c.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss)
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["hist"] = df["macd"] - df["signal"]
    hi, lo = df["High"], df["Low"]
    df["tenkan"] = (hi.rolling(9).max() + lo.rolling(9).min()) / 2
    df["kijun"] = (hi.rolling(26).max() + lo.rolling(26).min()) / 2
    df["span_a"] = ((df["tenkan"] + df["kijun"]) / 2).shift(26)
    df["span_b"] = ((hi.rolling(52).max() + lo.rolling(52).min()) / 2).shift(26)
    df["vol20"] = df["Volume"].rolling(20).mean()
    return df


def trend(df: pd.DataFrame) -> dict:
    r = df.iloc[-1]
    c, m5, m25, m75 = r["Close"], r["ma5"], r["ma25"], r["ma75"]
    short = "⬆️上昇" if c > m5 > m25 else "⬇️下落" if c < m5 < m25 else "➡️もみ合い"
    m75_up = df["ma75"].iloc[-1] > df["ma75"].iloc[-6]
    mid = "⬆️上昇" if (m25 > m75 and m75_up) else "⬇️下落" if (m25 < m75 and not m75_up) else "➡️もみ合い"
    return {"short": short, "mid": mid}


def fibonacci(df: pd.DataFrame, lookback: int = 120) -> dict:
    w = df.tail(lookback)
    hi_d, lo_d = w["High"].idxmax(), w["Low"].idxmin()
    hi, lo = float(w["High"].max()), float(w["Low"].min())
    if hi_d > lo_d:  # 安値→高値の上げ：押し目の目安
        direction = "上げに対する押し目"
        levels = {f"{int(f*1000)/10}%": hi - (hi - lo) * f for f in FIB}
    else:  # 高値→安値の下げ：戻りの目安
        direction = "下げに対する戻り"
        levels = {f"{int(f*1000)/10}%": lo + (hi - lo) * f for f in FIB}
    return {"direction": direction, "high": hi, "high_date": str(hi_d.date()), "low": lo,
            "low_date": str(lo_d.date()), "levels": {k: round(v, 1) for k, v in levels.items()}}


def gaps(df: pd.DataFrame, lookback: int = 60) -> list[dict]:
    out = []
    w = df.tail(lookback)
    for i in range(1, len(w)):
        prev, cur = w.iloc[i - 1], w.iloc[i]
        rest = w.iloc[i:]
        if cur["Low"] > prev["High"] and rest["Low"].min() > prev["High"]:
            out.append({"date": str(w.index[i].date()), "type": "上の窓（下に空いた支え）",
                        "from": float(prev["High"]), "to": float(cur["Low"])})
        if cur["High"] < prev["Low"] and rest["High"].max() < prev["Low"]:
            out.append({"date": str(w.index[i].date()), "type": "下の窓（上に空いた壁）",
                        "from": float(cur["High"]), "to": float(prev["Low"])})
    return out[-3:]


def signals(df: pd.DataFrame, days: int) -> list[dict]:
    w = df.tail(days)
    out = []
    a, b = w["ma5"] - w["ma25"], w["macd"] - w["signal"]
    for i in range(1, len(w)):
        d = w.index[i]
        if a.iloc[i - 1] <= 0 < a.iloc[i]:
            out.append({"date": str(d.date()), "kind": "GC", "label": "ゴールデンクロス（5日線が25日線を上抜け）"})
        if a.iloc[i - 1] >= 0 > a.iloc[i]:
            out.append({"date": str(d.date()), "kind": "DC", "label": "デッドクロス（5日線が25日線を下抜け）"})
        if b.iloc[i - 1] <= 0 < b.iloc[i]:
            out.append({"date": str(d.date()), "kind": "MU", "label": "MACDがシグナルを上抜け"})
        if b.iloc[i - 1] >= 0 > b.iloc[i]:
            out.append({"date": str(d.date()), "kind": "MD", "label": "MACDがシグナルを下抜け"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--name", default="")
    ap.add_argument("--csv")
    ap.add_argument("--days", type=int, default=80, help="表示する営業日数")
    ap.add_argument("--entry", type=float, help="取得単価（保有株）")
    ap.add_argument("--review", type=float, help="見直しライン（保有株）")
    ap.add_argument("--targets", help="利確目安（カンマ区切り）")
    ap.add_argument("--stop", type=float, help="もし買うならの撤退の目安")
    ap.add_argument("--take", help="もし買うならの利確の目安（カンマ区切り）")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    df = indicators(load(a.code, a.csv))
    w = df.tail(a.days).copy()
    x = np.arange(len(w))
    last = df.iloc[-1]
    tr, fib, gp, sig = trend(df), fibonacci(df), gaps(df), signals(df, a.days)
    sr = {"20日高値": float(df["High"].tail(20).max()), "20日安値": float(df["Low"].tail(20).min()),
          "60日高値": float(df["High"].tail(60).max()), "60日安値": float(df["Low"].tail(60).min())}

    fig = plt.figure(figsize=(11, 10), dpi=130)
    gs = fig.add_gridspec(4, 1, height_ratios=[5.2, 1.2, 1.1, 1.3], hspace=0.06)
    ax, axv, axr, axm = (fig.add_subplot(gs[i]) for i in range(4))
    for k in (axv, axr, axm):
        k.sharex(ax)
    for k in (ax, axv, axr, axm):
        k.grid(color=GRID, lw=0.6)
        k.set_axisbelow(True)
        for s in ("top", "right"):
            k.spines[s].set_visible(False)
        k.tick_params(colors=MUTED, labelsize=8)

    # 一目の雲・ボリンジャー
    ax.fill_between(x, w["span_a"], w["span_b"], where=w["span_a"] >= w["span_b"], color="#f3b6bb", alpha=0.35, lw=0)
    ax.fill_between(x, w["span_a"], w["span_b"], where=w["span_a"] < w["span_b"], color="#b6cdea", alpha=0.35, lw=0)
    ax.plot(x, w["bb_up"], color="#9aa5b1", lw=0.8, ls=":")
    ax.plot(x, w["bb_dn"], color="#9aa5b1", lw=0.8, ls=":")

    # ローソク足
    for i, (_, r) in enumerate(w.iterrows()):
        col = UP if r["Close"] >= r["Open"] else DOWN
        ax.vlines(i, r["Low"], r["High"], color=col, lw=0.8)
        ax.add_patch(plt.Rectangle((i - 0.32, min(r["Open"], r["Close"])), 0.64,
                                   max(abs(r["Close"] - r["Open"]), 1e-6), color=col, lw=0))
    for n, col in ((5, "#e0a100"), (25, "#2e8b57"), (75, "#7b4fb3")):
        ax.plot(x, w[f"ma{n}"], color=col, lw=1.4, label=f"{n}日線")

    ymin, ymax = w["Low"].min(), w["High"].max()
    pad = (ymax - ymin) * 0.06
    lo_lim, hi_lim = ymin - pad, ymax + pad

    labels: list[tuple[float, str, str]] = []

    def hline(y, color, text, ls="-", lw=1.1, side="right"):
        if lo_lim <= y <= hi_lim:
            ax.hlines(y, -1, len(w) - 0.5, color=color, ls=ls, lw=lw, alpha=0.9)
            labels.append((y, color, f"{text} {y:,.0f}"))
            return True
        return False

    offchart = []
    # フィボナッチ
    for k, v in fib["levels"].items():
        hline(v, "#c47f00", f"Fib {k}", ls="--", lw=0.8)
    # 支持線・抵抗線
    hline(sr["60日高値"], MUTED, "60日高値", ls="-.", lw=0.8)
    hline(sr["60日安値"], MUTED, "60日安値", ls="-.", lw=0.8)
    # 窓
    gidx = {str(d.date()): i for i, d in enumerate(w.index)}
    for g in gp:
        i0 = gidx.get(g["date"], 0)
        ax.fill_between([i0 - 0.5, len(w) - 0.5], g["from"], g["to"], color="#ffd166", alpha=0.45, lw=0)
    # 保有株の線 / もし買うならの線
    if a.entry:
        hline(a.entry, "#1f2933", "取得単価", lw=1.4)
    if a.review:
        if not hline(a.review, "#c0392b", "見直し", ls="--", lw=1.4):
            offchart.append(f"見直しライン {a.review:,.0f}円")
    for t in (a.targets or "").split(","):
        if t.strip() and not hline(float(t), "#1e8449", "利確目安", ls="--", lw=1.2):
            offchart.append(f"利確目安 {float(t):,.0f}円")
    if a.stop and not hline(a.stop, "#c0392b", "撤退", ls="--", lw=1.4):
        offchart.append(f"撤退 {a.stop:,.0f}円")
    for t in (a.take or "").split(","):
        if t.strip() and not hline(float(t), "#1e8449", "利確", ls="--", lw=1.2):
            offchart.append(f"利確 {float(t):,.0f}円")
    ax.set_ylim(lo_lim, hi_lim)
    # 右側のラベルを重ならないように並べる
    gap_min = (hi_lim - lo_lim) * 0.034
    placed: list[float] = []
    for y, col, txt in sorted(labels, key=lambda t: t[0]):
        yy = max(y, placed[-1] + gap_min) if placed else y
        placed.append(yy)
        ax.annotate(txt, xy=(len(w) - 0.5, y), xytext=(len(w) + 1.2, yy), color=col, fontsize=7.5, va="center",
                    annotation_clip=False, arrowprops=dict(arrowstyle="-", color=col, lw=0.5, alpha=0.6))

    # サインの印
    idx = {str(d.date()): i for i, d in enumerate(w.index)}
    for s in sig:
        i = idx.get(s["date"])
        if i is None:
            continue
        if s["kind"] in ("GC", "MU"):
            ax.annotate("▲" if s["kind"] == "GC" else "△", (i, w["Low"].iloc[i]), xytext=(0, -12),
                        textcoords="offset points", ha="center", color="#1e8449", fontsize=10 if s["kind"] == "GC" else 8)
        else:
            ax.annotate("▼" if s["kind"] == "DC" else "▽", (i, w["High"].iloc[i]), xytext=(0, 6),
                        textcoords="offset points", ha="center", color="#c0392b", fontsize=10 if s["kind"] == "DC" else 8)

    # 要約ボックス
    near_sup = max([v for v in list(fib["levels"].values()) + list(sr.values()) if v < last["Close"]], default=None)
    near_res = min([v for v in list(fib["levels"].values()) + list(sr.values()) if v > last["Close"]], default=None)
    dev = (last["Close"] / last["ma25"] - 1) * 100
    cloud = "雲の上" if last["Close"] > max(last["span_a"], last["span_b"]) else \
            "雲の下" if last["Close"] < min(last["span_a"], last["span_b"]) else "雲の中"
    box = [f"終値 {last['Close']:,.0f}円（{df.index[-1]:%m/%d}）",
           f"短期 {tr['short']}／中期 {tr['mid']}",
           f"RSI {last['rsi']:.0f}" + ("（売られすぎ）" if last["rsi"] <= 30 else "（買われすぎ）" if last["rsi"] >= 70 else ""),
           f"25日線から {dev:+.1f}%　一目：{cloud}",
           f"下の支え {near_sup:,.0f}円" if near_sup else "下の支え なし",
           f"上の壁 {near_res:,.0f}円" if near_res else "上の壁 なし"]
    if offchart:
        box.append("画面外：" + "、".join(offchart))
    box = [b.replace("⬆️", "↑").replace("⬇️", "↓").replace("➡️", "→") for b in box]
    ax.text(0.0, 1.01, "　｜　".join(box[:4]) + "\n" + "　｜　".join(box[4:]), transform=ax.transAxes, va="bottom",
            ha="left", fontsize=8.5, color=INK)
    ax.set_title(f"{a.code} {a.name}　日足（フィボナッチ＝{fib['direction']}、"
                 f"{fib['low_date'][5:]}安値〜{fib['high_date'][5:]}高値）", fontsize=11, color=INK, loc="left", pad=34)

    # 出来高
    vcol = [UP if c >= o else DOWN for o, c in zip(w["Open"], w["Close"])]
    axv.bar(x, w["Volume"], color=vcol, width=0.7, alpha=0.7)
    axv.plot(x, w["vol20"], color=INK, lw=0.9)
    axv.set_ylabel("出来高", fontsize=8, color=MUTED)
    axv.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v/1e6:.0f}M" if v >= 1e6 else f"{v/1e3:.0f}K"))
    # RSI
    axr.plot(x, w["rsi"], color="#7b4fb3", lw=1.1)
    for lv in (30, 70):
        axr.axhline(lv, color=MUTED, ls="--", lw=0.7)
    axr.set_ylim(0, 100)
    axr.set_ylabel("RSI", fontsize=8, color=MUTED)
    # MACD
    axm.bar(x, w["hist"], color=[UP if h >= 0 else DOWN for h in w["hist"]], width=0.7, alpha=0.6)
    axm.plot(x, w["macd"], color=INK, lw=1.0, label="MACD")
    axm.plot(x, w["signal"], color="#e0a100", lw=1.0, label="シグナル")
    axm.axhline(0, color=MUTED, lw=0.6)
    axm.set_ylabel("MACD", fontsize=8, color=MUTED)
    axm.legend(loc="upper left", fontsize=7, frameon=False, ncol=2)

    ticks = list(range(0, len(w), max(len(w) // 8, 1)))
    axm.set_xticks(ticks)
    axm.set_xticklabels([w.index[i].strftime("%m/%d") for i in ticks])
    for k in (ax, axv, axr):
        plt.setp(k.get_xticklabels(), visible=False)
    ax.set_xlim(-1, len(w) + 14)
    axm.text(0.0, -0.42, transform=axm.transAxes, fontsize=7.5, color=MUTED, s= "線：黄＝5日線 緑＝25日線 紫＝75日線　▲ゴールデンクロス ▼デッドクロス △▽MACDのクロス　点線＝ボリンジャー±2σ　帯＝一目の雲　黄色＝未埋めの窓　茶の破線＝フィボナッチ")
    fig.savefig(a.out, bbox_inches="tight", facecolor="white")

    print(json.dumps({"code": a.code, "close": float(last["Close"]), "trend": tr, "rsi": round(float(last["rsi"]), 1),
                      "dev25_pct": round(float(dev), 1), "ichimoku": cloud, "fibonacci": fib, "support_resistance": sr,
                      "nearest_support": near_sup, "nearest_resistance": near_res, "gaps": gp,
                      "signals_recent": sig[-5:], "offchart_lines": offchart}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
