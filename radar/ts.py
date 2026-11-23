"""投信隔日沖:依 XQ 回測設定重做的選股與回測。

條件(全部成立):
  成交量 ≥ min_lots 張、成交量 > 前一日 min_vol_ratio 倍、漲幅 < max_change_pct %、
  最高價創 high_days 日新高、投信當日買超(買賣超股數 > 0)。
  依「投信買超股數 ÷ 當日成交量」由大到小排序,取前 top_n 名。
出場:停利 tp_pct %、停損 sl_pct %、最多持有 max_hold_days 日(第 N 日收盤出場)。

與 XQ 設定的差異(必須知道):
  1. XQ 條件是「投信持股比例增加」,證交所盤後資料只有「投信買賣超股數」,這裡以買超代替;
     排序也因為沒有發行股數,改用買超佔成交量比重。
  2. 投信買賣超要收盤後才公布,收盤前不可能知道。XQ 回測用「當日收盤價進場」,實務上做不到。
     所以回測同時算兩種:same_close(照 XQ,當日收盤價進場,做不到,僅供對照)與
     next_open(收盤後選股、隔日開盤價進場,真正做得到的版本)。
日線模擬的保守假設:同一天同時碰到停損與停利,以停損計;跳空越過停損或停利,以開盤價成交。
"""
from __future__ import annotations

from .util import Costs
from . import screen as scr

MODES = {"same_close": "當日收盤進場(照 XQ,實務做不到)", "next_open": "隔日開盤進場(收盤後選股,做得到)"}


def screen_ts(U, trust, disposal, asof, cfg):
    t = cfg["ts"]
    dates = [d for d in U.mdates if d <= asof]
    if not dates:
        return {"asof": asof, "ready": False, "picks": [], "counts": {}}
    latest = dates[-1]
    today_trust = trust.get(latest)
    if not today_trust:
        return {"asof": latest, "ready": False, "picks": [], "counts": {}}
    disposed = set()
    for code, s, e in disposal or []:
        if s is None or (s <= latest and (e is None or latest <= e)):
            disposed.add(code)
    need = t["high_days"]
    counts = {"scanned": 0, "liquid": 0, "vol_up": 0, "not_limit_up": 0, "new_high": 0, "trust_buy": 0}
    out = []
    for code in U.bars:
        bars = U.slice(code, latest, need + 2)
        if len(bars) < need + 1 or bars[-1][0] != latest:
            continue
        counts["scanned"] += 1
        d, o, h, l, c, v, val = bars[-1]
        pv, pc = bars[-2][5], bars[-2][4]
        if v < t["min_lots"] * 1000:
            continue
        counts["liquid"] += 1
        if not pv or v <= pv * t["min_vol_ratio"]:
            continue
        counts["vol_up"] += 1
        chg = (c / pc - 1) * 100
        if chg >= t["max_change_pct"]:
            continue
        counts["not_limit_up"] += 1
        hi = max(b[2] for b in bars[-need:])
        if h < hi - 1e-9:
            continue
        counts["new_high"] += 1
        net = today_trust.get(code)
        if net is None or net <= 0:
            continue
        counts["trust_buy"] += 1
        if t.get("exclude_disposed", True) and code in disposed:
            continue
        out.append({
            "code": code, "name": U.names.get(code, ""), "close": c, "change_pct": round(chg, 2),
            "vol_lots": round(v / 1000), "vol_ratio": round(v / pv, 2),
            "trust_lots": round(net / 1000), "trust_share_pct": round(net / v * 100, 2),
            "tp_ref": round(c * (1 + t["tp_pct"] / 100), 2), "sl_ref": round(c * (1 - t["sl_pct"] / 100), 2),
            "history": scr.history(U.slice(code, latest, 60), 40),
        })
    out.sort(key=lambda p: -p["trust_share_pct"])
    for i, p in enumerate(out, 1):
        p["rank"] = i
    return {"asof": latest, "ready": True, "picks": out[: t["top_n"]], "counts": counts}


def simulate(close_t, future, cfg, costs, mode, tp_pct=None, sl_pct=None):
    """future:訊號日之後的日線 [(date,o,h,l,c), ...]。回傳成交結果或 pending/open。"""
    t = cfg["ts"]
    if not future:
        return {"status": "pending"}
    fill = close_t if mode == "same_close" else future[0][1]
    tp_pct = t["tp_pct"] if tp_pct is None else tp_pct
    sl_pct = t["sl_pct"] if sl_pct is None else sl_pct
    tp, sl, hold = fill * (1 + tp_pct / 100), fill * (1 - sl_pct / 100), t["max_hold_days"]
    status = px = d_exit = None
    for j, b in enumerate(future):
        d, o, h, l, c = b[0], b[1], b[2], b[3], b[4]
        if j > 0 and o <= sl:
            px, status = o, "stop"
        elif j > 0 and o >= tp:
            px, status = o, "tp"
        elif l <= sl:
            px, status = sl, "stop"
        elif h >= tp:
            px, status = tp, "tp"
        elif j >= hold - 1:
            px, status = c, "time"
        if status:
            d_exit = d
            break
    if status is None:
        return {"status": "open"}
    net = costs.net_pnl(fill, px)
    return {"status": status, "fill": fill, "exit": px, "exit_date": d_exit,
            "gross_pct": (px / fill - 1) * 100, "net_pct": net / fill * 100}


def stats(done):
    n = len(done)
    if not n:
        return {"n": 0}
    net = [t["net_pct"] for t in done]
    gross = [t["gross_pct"] for t in done]
    wn = [x for x in net if x > 0]
    ln = [x for x in net if x <= 0]
    gl = -sum(ln)
    cum = peak = dd = 0.0
    for t in sorted(done, key=lambda x: (x["exit_date"], x["date"], x["code"])):
        cum += t["net_pct"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {
        "n": n,
        "win_rate_gross": round(sum(1 for x in gross if x > 0) / n * 100, 1),
        "win_rate_net": round(len(wn) / n * 100, 1),
        "avg_gross_pct": round(sum(gross) / n, 3),
        "avg_net_pct": round(sum(net) / n, 3),
        "avg_win_pct": round(sum(wn) / len(wn), 2) if wn else 0,
        "avg_loss_pct": round(sum(ln) / len(ln), 2) if ln else 0,
        "profit_factor": round(sum(wn) / gl, 2) if gl > 0 else None,
        "total_net_pct": round(sum(net), 1),
        "max_drawdown_pct": round(dd, 1),
        "exits": {k: sum(1 for t in done if t["status"] == k) for k in ("tp", "stop", "time")},
    }


def backtest(conn, U, trust, disposal, cfg, dates):
    """對每個有投信資料的交易日重播選股並模擬兩種進場方式。回傳 stats 結構。"""
    costs = Costs(cfg)
    horizon = cfg["ts"]["max_hold_days"] + 1
    trades = {m: [] for m in MODES}
    sweep_pairs = [tuple(x) for x in cfg["ts"].get("sweep", [])]
    sweep_trades = {pair: [] for pair in sweep_pairs}
    days = 0
    for d in dates:
        if d not in trust:
            continue
        res = screen_ts(U, trust, disposal, d, cfg)
        if not res["ready"]:
            continue
        days += 1
        for p in res["picks"]:
            fut = conn.execute("SELECT date,open,high,low,close FROM prices WHERE code=? AND date>? ORDER BY date LIMIT ?",
                               (p["code"], d, horizon)).fetchall()
            for m in MODES:
                r = simulate(p["close"], fut, cfg, costs, m)
                if r["status"] in ("tp", "stop", "time"):
                    r.update({"date": d, "code": p["code"]})
                    trades[m].append(r)
            for pair in sweep_pairs:  # 同一批訊號、隔日開盤進場,比較不同停利停損組合
                r = simulate(p["close"], fut, cfg, costs, "next_open", pair[0], pair[1])
                if r["status"] in ("tp", "stop", "time"):
                    r.update({"date": d, "code": p["code"]})
                    sweep_trades[pair].append(r)
    out = {"days": days, "modes": {}}
    for m, label in MODES.items():
        done = trades[m]
        s = stats(done)
        eq, cum = [], 0.0
        for t in sorted(done, key=lambda x: (x["exit_date"], x["date"], x["code"])):
            cum += t["net_pct"]
            eq.append({"d": t["exit_date"], "v": round(cum, 1)})
        s["equity"] = eq
        s["label"] = label
        out["modes"][m] = s
    out["sweep"] = []
    for (tp, sl), done in sweep_trades.items():
        st = stats(done)
        st.pop("exits", None)
        st.update({"tp": tp, "sl": sl, "breakeven_win_rate": round(sl / (tp + sl) * 100, 1)})
        out["sweep"].append(st)
    return out
