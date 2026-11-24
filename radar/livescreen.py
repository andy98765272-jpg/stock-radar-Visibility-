"""盤中即時選股:用盤中即時報價,對全市場做一次「預估版」的選股。

做法:把今天尚未收完的這根 K 線(開高低、目前成交價、依時間推估的全日量)接到歷史日線後面,
再跑和盤後完全相同的選股與評分。因為今天的高低點、收盤位置與成交量都還會變,結果只是預估,
盤中名單可能隨時進出,收盤後仍以正式名單為準。
推估全日量 = 目前累計量 ÷ 此時段預期累計比例(U 型量能曲線),開盤 25 分鐘內太不準,不計算。
"""
from __future__ import annotations

import json

from . import intraday, screen as scr
from .util import now_taipei

MIN_MINUTES = 25


def prefilter(U, cfg):
    """先用歷史成交金額與價格濾掉不可能入選的股票,減少要查的報價數量。"""
    f = cfg["filters"]
    out = []
    for code, bars in U.bars.items():
        if len(bars) < f["min_history"]:
            continue
        last = bars[-1][4]
        if not (f["price_min"] * 0.85 <= last <= f["price_max"] * 1.15):
            continue
        tw = sum(b[6] for b in bars[-20:]) / min(20, len(bars)) / 10000
        if tw >= f["min_turnover20_wan"] * 0.5:
            out.append(code)
    return out


def with_today(U, quotes, today, minutes):
    """回傳接上今天預估 K 線的新 Universe。"""
    frac = intraday.expected_fraction(minutes)
    V = scr.Universe()
    V.names = dict(U.names)
    used = 0
    for code, bars in U.bars.items():
        q = quotes.get(code)
        if not q or q.get("price") is None or not q.get("volume") or not q.get("open") or frac <= 0:
            continue
        o, h, l, p = q["open"], q.get("high") or q["price"], q.get("low") or q["price"], q["price"]
        proj = q["volume"] / frac
        V.bars[code] = list(bars[-130:]) + [(today, o, max(h, p), min(l, p), p, proj, p * proj)]
        V.dates[code] = [b[0] for b in V.bars[code]]
        used += 1
    base = sorted({d for b in U.bars.values() for d in (x[0] for x in b[-3:])})
    V.mdates = [d for d in base if d < today] + [today]
    return V, used


def run(U, disposal, cfg, fetch=intraday.fetch_many, now=None):
    now = now or now_taipei()
    today = now.strftime("%Y-%m-%d")
    minutes = intraday.minutes_since_open(now)
    last_day = max((b[-1][0] for b in U.bars.values()), default=None)
    if last_day is None or last_day >= today:
        return {"meta": {"asof": today, "live": True, "error": "資料庫已有今天的收盤資料,請用正式名單"}, "picks": [], "watchlist": []}
    if minutes < MIN_MINUTES:
        return {"meta": {"asof": today, "live": True, "error": f"開盤未滿 {MIN_MINUTES} 分鐘,成交量推估不準,稍後再算"}, "picks": [], "watchlist": []}
    codes = prefilter(U, cfg)
    quotes = fetch(codes)
    V, used = with_today(U, quotes, today, minutes)
    # 盤中沒有法人資料,籌碼面 20 分(滿分 100)一律是 0,門檻照比例調降,否則幾乎不可能達到 75 分
    lcfg = json.loads(json.dumps(cfg))
    k = 0.8
    lcfg["score"]["min_score"] = round(cfg["score"]["min_score"] * k)
    lcfg["score"]["watch_min"] = round(cfg["score"].get("watch_min", 60) * k)
    res = scr.screen(V, {}, disposal, today, lcfg)
    res["meta"].update({"live": True, "minutes": minutes, "quoted": used, "queried": len(codes),
                        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "notes": ["盤中預估:今天的高低點、收盤位置與全日成交量都還會變,名單會隨盤中變動,收盤後以正式名單為準。",
                                  f"全日量是依目前累計量與時間推估;盤中沒有法人買賣超,籌碼面 20 分不計,正式門檻因此由 {cfg['score']['min_score']} 分按比例調降為 {lcfg['score']['min_score']} 分。"] + res["meta"].get("notes", [])})
    return res
