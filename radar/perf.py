"""歷史績效:把每天的選股訊號,用後續日線模擬成交,統計勝率、獲利因子、回撤等。

日線模擬的保守假設(請見 README):
- 買進價:stop 單(突破)在最高價觸及進場價時成交,開盤跳空則以開盤價成交;limit 單(回測)在最低價觸及時成交。
- 成交當天只檢查停損,不檢查停利(無法得知當天先後順序);之後同一天若同時碰到停損與停利,以停損計。
- 開盤跳空越過停損價,以開盤價出場。
- 成交後滿 max_hold_days 個交易日,以收盤價出場。
"""
from __future__ import annotations

from .util import Costs


def simulate(sig, future, cfg, costs):
    valid = cfg["perf"]["entry_valid_days"]
    hold = cfg["perf"]["max_hold_days"]
    entry, stop, t1, order = sig["entry"], sig["stop"], sig["t1"], sig["order"]
    fill = None
    fill_idx = None
    for i, b in enumerate(future[:valid]):
        o, h, l = b[1], b[2], b[3]
        if order == "stop" and h >= entry:
            fill, fill_idx = max(o, entry), i
            break
        if order == "limit" and l <= entry:
            fill, fill_idx = min(o, entry), i
            break
    if fill_idx is None:
        return {"status": "nofill" if len(future) >= valid else "pending"}

    exit_px = exit_date = status = None
    for j in range(fill_idx, len(future)):
        d, o, h, l, c = future[j][0], future[j][1], future[j][2], future[j][3], future[j][4]
        if l <= stop:
            exit_px = min(stop, fill) if j == fill_idx else min(stop, o)
            status = "stop"
        elif j > fill_idx and h >= t1:
            exit_px = max(t1, o)
            status = "target"
        elif j - fill_idx >= hold:
            exit_px = c
            status = "time"
        if status:
            exit_date = d
            break
    if status is None:
        return {"status": "open"}
    pnl = costs.net_pnl(fill, exit_px)
    risk = max(entry - stop, 1e-9)
    return {"status": status, "fill": fill, "exit": exit_px, "exit_date": exit_date,
            "pnl": pnl, "pnl_pct": pnl / fill * 100, "r": pnl / risk}


def evaluate(conn, signals, cfg):
    costs = Costs(cfg)
    trades = []
    cache = {}
    horizon = cfg["perf"]["entry_valid_days"] + cfg["perf"]["max_hold_days"] + 2
    for s in signals:
        key = (s["code"], s["date"])
        if key not in cache:
            cache[key] = conn.execute(
                "SELECT date,open,high,low,close FROM prices WHERE code=? AND date>? ORDER BY date LIMIT ?",
                (s["code"], s["date"], horizon),
            ).fetchall()
        res = simulate(s, cache[key], cfg, costs)
        res.update({"date": s["date"], "code": s["code"], "name": s.get("name", ""), "strategy": s["strategy"]})
        trades.append(res)
    return trades


def _stats(done):
    n = len(done)
    wins = [t for t in done if t["pnl"] > 0]
    losses = [t for t in done if t["pnl"] <= 0]
    gw = sum(t["r"] for t in wins)
    gl = -sum(t["r"] for t in losses)
    cum = peak = dd = 0.0
    run = max_run = 0
    for t in sorted(done, key=lambda x: (x["exit_date"], x["date"], x["code"])):
        cum += t["r"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        if t["pnl"] <= 0:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    return {
        "n": n,
        "win_rate": round(len(wins) / n * 100, 1) if n else 0,
        "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss_pct": round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0,
        "avg_win_r": round(gw / len(wins), 2) if wins else 0,
        "avg_loss_r": round(-gl / len(losses), 2) if losses else 0,
        "profit_factor": (round(gw / gl, 2) if gl > 0 else None),
        "expectancy_r": round(sum(t["r"] for t in done) / n, 3) if n else 0,
        "total_r": round(sum(t["r"] for t in done), 2),
        "max_drawdown_r": round(dd, 2),
        "max_consecutive_losses": max_run,
    }


def summarize(trades, strategies):
    done = [t for t in trades if t["status"] in ("target", "stop", "time")]
    out = {
        "signals": len(trades),
        "nofill": sum(1 for t in trades if t["status"] == "nofill"),
        "open": sum(1 for t in trades if t["status"] in ("open", "pending")),
        "exits": {k: sum(1 for t in done if t["status"] == k) for k in ("target", "stop", "time")},
        "all": _stats(done),
        "by_strategy": {k: _stats([t for t in done if t["strategy"] == k]) for k in strategies},
    }
    cum = 0.0
    eq = []
    for t in sorted(done, key=lambda x: (x["exit_date"], x["date"], x["code"])):
        cum += t["r"]
        eq.append({"d": t["exit_date"], "r": round(cum, 2)})
    out["equity"] = eq
    out["recent"] = [
        {k: (round(v, 2) if isinstance(v, float) else v) for k, v in t.items()}
        for t in sorted(done, key=lambda x: (x["exit_date"], x["date"]), reverse=True)[:30]
    ]
    return out
