from __future__ import annotations

import bisect
import re
from datetime import datetime

from . import indicators as ind
from .util import (Costs, add_ticks, now_taipei, round_down_tick, round_up_tick)

CODE_RE = re.compile(r"^[1-9]\d{3}$")  # 一般個股;排除 ETF(00 開頭)、權證、特別股等
STRAT_NAMES = {"bk": "帶量突破型", "pb": "拉回支撐型", "mo": "強勢續攻型"}
STRAT_ORDER = ["bk", "pb", "mo"]
GROUPS = ["技術面", "成交量與價量", "籌碼面", "動能與題材", "風險控管"]
GROUP_MAX = {"技術面": 30, "成交量與價量": 25, "籌碼面": 20, "動能與題材": 15, "風險控管": 10}


class Universe:
    """把全市場日線一次載入,讓回測可以重複切片,不必每天重新查資料庫。"""

    def __init__(self):
        self.bars = {}      # code -> [(date,o,h,l,c,vol,value), ...]
        self.dates = {}     # code -> [date, ...]
        self.names = {}
        self.mdates = []    # 全市場交易日

    @classmethod
    def load(cls, conn, since=None):
        u = cls()
        q = "SELECT code,name,date,open,high,low,close,volume,value FROM prices"
        args = ()
        if since:
            q += " WHERE date>=?"
            args = (since,)
        q += " ORDER BY code,date"
        md = set()
        for code, name, d, o, h, l, c, v, val in conn.execute(q, args):
            if not CODE_RE.match(code):
                continue
            u.bars.setdefault(code, []).append((d, o, h, l, c, v, val))
            u.names[code] = name
            md.add(d)
        for code, b in u.bars.items():
            u.dates[code] = [x[0] for x in b]
        u.mdates = sorted(md)
        return u

    def slice(self, code, asof, lookback=130):
        idx = bisect.bisect_right(self.dates[code], asof)
        return self.bars[code][max(0, idx - lookback):idx]


def load_inst(conn, since=None):
    q = "SELECT date,code,net FROM inst"
    args = ()
    if since:
        q += " WHERE date>=?"
        args = (since,)
    out = {}
    for d, code, net in conn.execute(q, args):
        out.setdefault(d, {})[code] = net
    return out


def build_features(bars, cfg):
    n = len(bars)
    if n < cfg["filters"]["min_history"]:
        return None
    o = [b[1] for b in bars]
    h = [b[2] for b in bars]
    l = [b[3] for b in bars]
    c = [b[4] for b in bars]
    v = [b[5] for b in bars]
    val = [b[6] for b in bars]
    ma5, ma10, ma20 = ind.sma(c, 5), ind.sma(c, 10), ind.sma(c, 20)
    ma20_prev3 = ind.sma(c, 20, end=n - 3)
    rsi = ind.rsi(c, 14)
    atr = ind.atr(h, l, c, 14)
    if None in (ma5, ma10, ma20, ma20_prev3, rsi, atr) or atr <= 0:
        return None
    dif, dea, hist = ind.macd(c)
    avg5 = sum(v[-6:-1]) / 5
    rng = h[-1] - l[-1]
    return {
        "close": c[-1], "open": o[-1], "high": h[-1], "low": l[-1], "prev_close": c[-2],
        "vol": v[-1], "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma20_prev3": ma20_prev3,
        "rsi": rsi, "atr": atr, "dif": dif, "dea": dea, "macd_hist": hist,
        "avg_vol5": avg5, "vr": (v[-1] / avg5) if avg5 > 0 else 0.0,
        "turnover20_wan": sum(val[-20:]) / 20 / 1e4,
        "high20_prev": max(h[-21:-1]), "high20": max(h[-20:]), "high60": max(h[-60:]),
        "low5": min(l[-5:]),
        "ret3": c[-1] / c[-4] - 1, "ret5": c[-1] / c[-6] - 1,
        "close_pos": (c[-1] - l[-1]) / rng if rng > 0 else 1.0,
        "vol_up3": v[-1] > v[-2] > v[-3],
        "gap_pct": (o[-1] / c[-2] - 1) * 100,
        "ext5": (c[-1] / ma5 - 1) * 100, "ext20": (c[-1] / ma20 - 1) * 100,
        "change_pct": (c[-1] / c[-2] - 1) * 100,
        "not_at_5d_high": c[-1] < max(c[-5:]),
    }


def base_exclusions(f, code, cfg, disposed):
    """回傳不符合基本條件的原因清單(空清單代表通過)。"""
    flt = cfg["filters"]
    why = []
    if not (flt["price_min"] <= f["close"] <= flt["price_max"]):
        why.append("股價不在範圍內")
    if f["turnover20_wan"] < flt["min_turnover20_wan"]:
        why.append("流動性不足")
    if disposed:
        why.append("處置股")
    if f["gap_pct"] > flt["gap_max_pct"]:
        why.append("跳空過大")
    if f["ext5"] > flt["ext_ma5_max_pct"] or f["ext20"] > flt["ext_ma20_max_pct"]:
        why.append("乖離過大")
    if flt["ma_alignment"] == "full":
        ok = f["ma5"] > f["ma10"] > f["ma20"]
    else:
        ok = f["ma5"] > f["ma20"]
    if not (ok and f["close"] > f["ma20"]):
        why.append("均線未多頭排列")
    return why


def match_strategies(f, ctx, cfg):
    """回傳 {策略代號: [符合條件說明]};沒有符合的策略就不出現。"""
    s = cfg["strategies"]
    res = {}
    # A 帶量突破型
    b = s["bk"]
    if (b["rsi"][0] <= f["rsi"] <= b["rsi"][1] and f["vr"] >= b["min_vol_ratio"]
            and f["close"] >= f["high20_prev"] * (1 - b["near_high_pct"] / 100)):
        res["bk"] = [f"RSI {f['rsi']:.0f}", f"量比 {f['vr']:.1f} 倍", f"接近或突破前 20 日高點 {f['high20_prev']:.2f}"]
    # B 拉回支撐型
    p = s["pb"]
    d5 = abs(f["close"] - f["ma5"]) / f["ma5"] * 100
    d10 = abs(f["close"] - f["ma10"]) / f["ma10"] * 100
    if (min(d5, d10) <= p["near_ma_pct"] and f["vol"] < f["avg_vol5"]
            and (f["close"] >= f["open"] or f["close"] > f["prev_close"])
            and p["rsi"][0] <= f["rsi"] <= p["rsi"][1] and f["not_at_5d_high"]):
        res["pb"] = ["均線多頭排列", f"回到 {'MA5' if d5 <= d10 else 'MA10'} 附近", "量縮", "出現止跌訊號"]
    # C 強勢續攻型
    m = s["mo"]
    if (f["ret3"] > 0 and f["ret3"] >= ctx["median_ret3"] and f["vol_up3"]
            and f["close_pos"] >= m["min_close_pos"] and f["ext5"] <= m["max_ext_ma5_pct"]):
        res["mo"] = [f"近 3 日報酬 {f['ret3'] * 100:.1f}%,高於市場中位數", "成交量連 3 日增加",
                     f"收盤接近當日高點({f['close_pos'] * 100:.0f}%)", "未過度偏離 MA5"]
    return res


def make_plan(f, strat, cfg):
    p = cfg["plan"]
    atr = f["atr"]
    c = f["close"]
    if strat == "bk":
        level = round_up_tick(f["high20_prev"])
        if c > level:
            entry, order = level, "limit"     # 已突破:等回測壓力區不破
        else:
            entry, order = add_ticks(level, 1), "stop"  # 尚未突破:突破前高才進
    else:
        entry, order = add_ticks(round_up_tick(f["high"]), 1), "stop"  # 突破今日高點代表重新轉強
    levels = [f["low5"], f["ma10"], f["ma5"], f["ma20"]]
    if strat in ("pb", "mo"):
        levels.append(f["low"])  # 轉強失敗的位置:跌破今日低點
    cands = [x for x in levels if x < entry]
    support = max(cands) if cands else entry * 0.97
    stop = round_down_tick(support - p["atr_stop_cushion"] * atr)
    if stop >= entry:
        stop = round_down_tick(entry * 0.97)
    t1 = round_up_tick(entry + p["atr_t1"] * atr)
    t2 = round_up_tick(entry + p["atr_t2"] * atr)
    if f["high20"] > entry:
        resistance = f["high20"]
    elif f["high60"] > entry:
        resistance = f["high60"]
    else:
        resistance = t1
    rr = (t1 - entry) / (entry - stop)
    return {"entry": entry, "order": order, "stop": stop, "t1": t1, "t2": t2,
            "support": round(support, 2), "resistance": round(resistance, 2), "rr": rr}


def score_stock(f, plan, chips, ctx, cfg):
    """依 100 分制計分。回傳 (明細列, 各構面得分, 總分, 旗標)。"""
    rows = []
    flags = []

    def add(g, label, got, mx):
        rows.append({"group": g, "label": label, "got": got, "max": mx})

    add("技術面", "MA5 大於 MA10", 6 if f["ma5"] > f["ma10"] else 0, 6)
    add("技術面", "MA10 大於 MA20", 6 if f["ma10"] > f["ma20"] else 0, 6)
    add("技術面", "股價站在 MA20 之上", 5 if f["close"] > f["ma20"] else 0, 5)
    add("技術面", "MA20 上揚", 4 if f["ma20"] > f["ma20_prev3"] else 0, 4)
    add("技術面", "MACD 偏多", 4 if (f["dif"] > f["dea"] and f["dif"] > 0) else 0, 4)
    r = f["rsi"]
    add("技術面", "RSI 落在 55~70", 5 if 55 <= r <= 70 else (2 if (50 <= r < 55 or 70 < r <= 75) else 0), 5)

    vr, tw = f["vr"], f["turnover20_wan"]
    add("成交量與價量", "量比 1.5 倍以上", 10 if vr >= 1.5 else (5 if vr >= 1.2 else 0), 10)
    add("成交量與價量", "20 日均成交金額 5,000 萬以上", 8 if tw >= 5000 else (3 if tw >= 3000 else 0), 8)
    add("成交量與價量", "量增價漲", 7 if (f["close"] > f["prev_close"] and f["vol"] > f["avg_vol5"]) else 0, 7)

    # 籌碼面 20:三大法人當日買超 8、近 3 日累計買超 6、買超占成交量比重 6
    chips_got = 0
    if chips is None:
        flags.append("缺少法人資料,籌碼面以 0 分計")
        add("籌碼面", "三大法人當日買超", 0, 8)
        add("籌碼面", "近 3 日法人累計買超", 0, 6)
        add("籌碼面", "買超占成交量比重", 0, 6)
    else:
        net1, net3 = chips
        a = 8 if net1 > 0 else 0
        b = 6 if net3 > 0 else 0
        ratio = net1 / f["vol"] if f["vol"] else 0
        c_ = 0
        if net1 > 0:
            c_ = 6 if ratio >= 0.05 else (3 if ratio >= 0.02 else 0)
        add("籌碼面", "三大法人當日買超", a, 8)
        add("籌碼面", "近 3 日法人累計買超", b, 6)
        add("籌碼面", "買超占成交量比重", c_, 6)

    # 動能與題材 15:題材無法自動判斷,以價格動能代替
    rs = ctx["rank5"](f["ret5"])
    add("動能與題材", "近 5 日報酬居市場前段", 6 if rs >= 0.8 else (3 if rs >= 0.6 else 0), 6)
    add("動能與題材", "收盤接近當日高點", 4 if f["close_pos"] >= 0.85 else (2 if f["close_pos"] >= 0.7 else 0), 4)
    add("動能與題材", "近 3 日成交量遞增", 3 if f["vol_up3"] else 0, 3)
    add("動能與題材", "近 3 日強於市場中位數", 2 if f["ret3"] > ctx["median_ret3"] else 0, 2)

    flt = cfg["filters"]
    rr = plan["rr"]
    add("風險控管", "風險報酬比 1:2 以上", 6 if rr >= 2 else (3 if rr >= 1.5 else 0), 6)
    add("風險控管", "有明確停損位置", 2 if plan["stop"] < plan["entry"] else 0, 2)
    calm = f["gap_pct"] <= flt["gap_max_pct"] / 2 and f["ext5"] <= flt["ext_ma5_max_pct"] / 2
    add("風險控管", "沒有過大跳空或乖離", 2 if calm else 1, 2)

    by_group = {g: sum(x["got"] for x in rows if x["group"] == g) for g in GROUPS}
    return rows, by_group, sum(by_group.values()), flags


def grade(score):
    if score >= 85:
        return "強勢觀察"
    if score >= 75:
        return "潛力觀察"
    if score >= 65:
        return "一般觀察"
    return "暫不列入"


ENTRY_TEXT = {
    ("bk", "stop"): "股價帶量突破前 20 日高點後才進場,不追第一根紅 K。",
    ("bk", "limit"): "已突破前高,等回測壓力區(現為支撐)不破並重新轉強再進場。",
    ("pb", "stop"): "回測支撐後止跌,隔日突破今日高點、確認賣壓減弱並重新轉強再進場。",
    ("mo", "stop"): "隔日突破今日高點、延續強勢才進場,避免追已連續大漲的股票。",
}


def history(bars, n=60):
    """最近 n 根日 K,每根附上當天的 MA5 / MA10 / MA20(給網站畫圖用)。"""
    c = [b[4] for b in bars]

    def ma(i, k):
        return round(sum(c[i - k + 1:i + 1]) / k, 2) if i - k + 1 >= 0 else None

    out = []
    for i in range(max(0, len(bars) - n), len(bars)):
        b = bars[i]
        out.append([b[0], b[1], b[2], b[3], b[4], round(b[5]), ma(i, 5), ma(i, 10), ma(i, 20)])
    return out


def screen(U, inst, disposal, asof, cfg, include_all_scores=False):
    """以 asof 當天(含)以前的資料執行選股。回傳 {meta, picks}。"""
    mdates = [d for d in U.mdates if d <= asof]
    if not mdates:
        return {"meta": {"asof": asof, "error": "沒有行情資料"}, "picks": []}
    latest = mdates[-1]
    last3 = mdates[-3:]
    costs = Costs(cfg)
    inst_today = inst.get(latest)

    feats = {}
    for code in U.bars:
        bars = U.slice(code, latest)
        if not bars or bars[-1][0] != latest:  # 當天沒資料 = 資料不完整,略過
            continue
        f = build_features(bars, cfg)
        if f:
            feats[code] = f
    ret3s = sorted(f["ret3"] for f in feats.values())
    ret5s = sorted(f["ret5"] for f in feats.values())
    ctx = {
        "median_ret3": ret3s[len(ret3s) // 2] if ret3s else 0.0,
        "rank5": lambda x: bisect.bisect_left(ret5s, x) / len(ret5s) if ret5s else 0.0,
    }

    disposed = set()
    for code, s, e in disposal or []:
        if s is None or (s <= latest and (e is None or latest <= e)):
            disposed.add(code)

    counts = {"scanned": len(feats), "eligible": 0, "matched": {"bk": 0, "pb": 0, "mo": 0},
              "dropped_rr": 0, "below_min_score": 0, "qualified": 0, "watch": 0}
    cands = []
    watch = []
    watch_min = cfg["score"].get("watch_min", 60)
    for code, f in feats.items():
        why = base_exclusions(f, code, cfg, code in disposed)
        watch_only = False
        if why:
            # 只差在均線沒有完整多頭排列(MA5 > MA20 且站上 MA20)的,可以進觀察名單
            if why == ["均線未多頭排列"] and f["ma5"] > f["ma20"] and f["close"] > f["ma20"]:
                watch_only = True
            else:
                continue
        else:
            counts["eligible"] += 1
        matches = match_strategies(f, ctx, cfg)
        if not matches:
            continue
        for k in matches:
            counts["matched"][k] += 1
        strat = next(k for k in STRAT_ORDER if k in matches)
        plan = make_plan(f, strat, cfg)
        if plan["rr"] < cfg["filters"]["min_rr"]:
            counts["dropped_rr"] += 1
            continue
        chips = None
        if inst_today is not None and code in inst_today:
            nets = [inst.get(d, {}).get(code) for d in last3]
            chips = (inst_today[code], sum(x for x in nets if x is not None))
        rows, by_group, total, flags = score_stock(f, plan, chips, ctx, cfg)
        reason = None
        if watch_only:
            if total < watch_min:
                continue
            reason = "均線尚未完整多頭排列(MA5 > MA20,但 MA5、MA10、MA20 未依序排列),不算正式訊號。"
        elif total < cfg["score"]["min_score"] and not include_all_scores:
            counts["below_min_score"] += 1
            if total < watch_min:
                continue
            reason = f"評分 {total} 分,未達正式名單門檻 {cfg['score']['min_score']} 分,不算正式訊號。"
        if reason is None:
            counts["qualified"] += 1
        else:
            counts["watch"] += 1
        shares = costs.shares(plan["entry"], plan["stop"], cfg["risk"]["max_risk_per_trade"])
        reasons = [x["label"] for x in rows if x["got"] == x["max"] and x["group"] != "風險控管"][:6]
        pick = {
            "code": code, "name": U.names.get(code, ""), "strategy": strat, "strategy_name": STRAT_NAMES[strat],
            "also": [STRAT_NAMES[k] for k in STRAT_ORDER if k in matches and k != strat],
            "score": total, "grade": grade(total), "breakdown": by_group, "rows": rows, "flags": flags,
            "close": f["close"], "change_pct": round(f["change_pct"], 2), "vol_ratio": round(f["vr"], 2),
            "rsi": round(f["rsi"], 1), "ma5": round(f["ma5"], 2), "ma10": round(f["ma10"], 2),
            "ma20": round(f["ma20"], 2), "turnover20_wan": round(f["turnover20_wan"]),
            "atr": round(f["atr"], 2), "avg_vol5": round(f["avg_vol5"]),
            "history": history(U.slice(code, latest, 90)),
            "info": {
                "open": f["open"], "high": f["high"], "low": f["low"], "prev_close": f["prev_close"], "vol": round(f["vol"]),
                "ret3": round(f["ret3"] * 100, 2), "ret5": round(f["ret5"] * 100, 2),
                "close_pos": round(f["close_pos"] * 100), "gap_pct": round(f["gap_pct"], 2),
                "ext5": round(f["ext5"], 2), "ext20": round(f["ext20"], 2),
                "high20_prev": round(f["high20_prev"], 2), "high60": round(f["high60"], 2),
                "dif": round(f["dif"], 3), "dea": round(f["dea"], 3), "macd_hist": round(f["macd_hist"], 3),
            },
            "support": plan["support"], "resistance": plan["resistance"],
            "entry": plan["entry"], "order_type": plan["order"], "stop": plan["stop"],
            "t1": plan["t1"], "t2": plan["t2"], "rr": round(plan["rr"], 2),
            "risk_pct": round((plan["entry"] - plan["stop"]) / plan["entry"] * 100, 2),
            "reward1_pct": round((plan["t1"] - plan["entry"]) / plan["entry"] * 100, 2),
            "reward2_pct": round((plan["t2"] - plan["entry"]) / plan["entry"] * 100, 2),
            "shares": shares,
            "entry_rule": ENTRY_TEXT[(strat, plan["order"])],
            "invalid_rule": f"收盤跌破 {plan['stop']:.2f} 即視為交易邏輯失效,出場。",
            "why": matches[strat] + reasons,
            "chips": None if chips is None else {"net_today": chips[0], "net3": chips[1]},
            "_tw": f["turnover20_wan"],
        }
        if reason is None:
            cands.append(pick)
        else:
            pick["watch_reason"] = reason
            watch.append(pick)

    cands.sort(key=lambda p: (-p["score"], -p["_tw"]))
    for p in cands:
        p.pop("_tw", None)
    picks = cands if include_all_scores else cands[: cfg["score"]["max_picks"]]
    watch.sort(key=lambda p: (-p["score"], -p["_tw"]))
    for p in watch:
        p.pop("_tw", None)
    watchlist = watch[: cfg["score"].get("max_watch", 10)]
    meta = {
        "asof": latest,
        "generated_at": now_taipei().isoformat(timespec="seconds"),
        "synthetic": False,
        "counts": counts,
        "params": cfg,
        "notes": [
            "量比為盤後數字:當日成交量 ÷ 前 5 日平均成交量。",
            "題材無法自動判斷,「動能與題材」以價格動能計分。",
            "評分僅代表條件符合程度,不等於獲利機率。",
        ],
    }
    return {"meta": meta, "picks": picks, "watchlist": watchlist}
