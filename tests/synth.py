"""產生合成(假的)市場資料,只用來測試程式與預覽網站,不代表任何真實股票。"""
from __future__ import annotations

import random
from datetime import date, timedelta

from radar import db, screen as scr


def weekdays_ending(end, n):
    out = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def path(n, start, drift, noise, rng):
    p = [start]
    for _ in range(n - 1):
        p.append(p[-1] * (1 + drift + rng.gauss(0, noise)))
    return p


def bars_from(closes, vols, rng):
    bars = []
    prev = closes[0]
    for c, v in zip(closes, vols):
        o = prev * (1 + rng.gauss(0, 0.004))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.006)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.006)))
        bars.append([o, h, l, c, v])
        prev = c
    return bars


# ---- 刻意造出符合各策略型態的標的 ----
def make_breakout(n, rng):
    c = path(n, 60, 0.0018, 0.014, rng)
    v = [3e6 * rng.uniform(0.8, 1.2) for _ in range(n)]
    b = bars_from(c, v, rng)
    hi_prev = max(x[1] for x in b[-21:-1])
    b[-1][0] = c[-2] * 1.002
    b[-1][3] = hi_prev * 1.006
    b[-1][1] = b[-1][3] * 1.003
    b[-1][2] = b[-1][0] * 0.998
    b[-1][4] = 3e6 * 2.2
    return b


def make_pullback(n, rng):
    c = path(n, 55, 0.0035, 0.012, rng)
    for k, f in ((-3, 0.994), (-2, 0.995)):
        c[k] = c[k - 1] * f
    c[-1] = c[-2] * 1.003
    v = [3e6 * rng.uniform(0.9, 1.3) for _ in range(n)]
    v[-1] = 3e6 * 0.5
    b = bars_from(c, v, rng)
    b[-1][0] = c[-2] * 0.9995
    b[-1][2] = min(b[-1][0], b[-1][3]) * 0.999
    b[-1][1] = max(b[-1][0], b[-1][3]) * 1.001
    return b


def make_momentum(n, rng):
    c = path(n, 50, 0.003, 0.012, rng)
    for k, f in ((-3, 1.012), (-2, 1.013), (-1, 1.012)):
        c[k] = c[k - 1] * f
    v = [3e6 * rng.uniform(0.8, 1.1) for _ in range(n)]
    v[-3], v[-2], v[-1] = 3.4e6, 3.9e6, 4.4e6
    b = bars_from(c, v, rng)
    for k in (-3, -2, -1):
        o = b[k][0]
        b[k][1] = b[k][3] * 1.001
        b[k][2] = min(o, b[k][3]) * 0.997
    return b


def find_pattern(maker, want, n, rng, cfg):
    """重複抽樣,直到產生的標的確實符合指定策略,且通過基本條件。"""
    for _ in range(400):
        bars = maker(n, rng)
        rows = [("x", d, bb[0], bb[1], bb[2], bb[3], bb[4], bb[3] * bb[4]) for d, bb in zip(range(n), bars)]
        tup = [(d, bb[0], bb[1], bb[2], bb[3], bb[4], bb[3] * bb[4]) for d, bb in zip(range(n), bars)]
        f = scr.build_features(tup, cfg)
        if not f or scr.base_exclusions(f, "x", cfg, False):
            continue
        m = scr.match_strategies(f, {"median_ret3": 0.0}, cfg)
        if want in m:
            plan = scr.make_plan(f, want, cfg)
            if plan["rr"] >= cfg["filters"]["min_rr"]:
                return bars
    return maker(n, rng)  # 找不到就退回最後一次,測試會顯示結果


def build(conn, end=date(2026, 11, 20), n_days=130, n_stocks=160, seed=7, cfg=None):
    if cfg is None:
        import json, os
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
    rng = random.Random(seed)
    days = weekdays_ending(end, n_days)
    stocks = {}
    for i in range(n_stocks):
        code = str(2001 + i)
        start = rng.uniform(20, 250)
        closes = path(n_days, start, rng.gauss(0.0003, 0.0008), rng.uniform(0.012, 0.025), rng)
        base = rng.uniform(0.6e6, 5e6)
        vols = [base * rng.uniform(0.6, 1.5) for _ in range(n_days)]
        stocks[code] = (f"合成股{code}", bars_from(closes, vols, rng))

    stocks["3001"] = ("合成突破", find_pattern(make_breakout, "bk", n_days, rng, cfg))
    stocks["3002"] = ("合成拉回", find_pattern(make_pullback, "pb", n_days, rng, cfg))
    stocks["3003"] = ("合成續攻", find_pattern(make_momentum, "mo", n_days, rng, cfg))
    # 3004:型態與 3001 相同,但被列為處置股,應被排除
    stocks["3004"] = ("合成處置", [list(x) for x in stocks["3001"][1]])

    table = {}
    for code, (name, bars) in stocks.items():
        rows = []
        for (o, h, l, c_, v_) in bars:
            h = max(h, o, c_)
            l = min(l, o, c_)
            rows.append((code, name, round(o, 2), round(h, 2), round(l, 2), round(c_, 2), round(v_), round(c_ * v_)))
        table[code] = rows

    for idx, d in enumerate(days):
        db.upsert_prices(conn, d.isoformat(), [table[code][idx] for code in table])
        inst = {}
        for code in table:
            net = rng.gauss(0, 1) * 2e5
            if code in ("3001", "3002", "3003", "3004"):
                net = abs(net) + 3e5
            inst[code] = round(net)
        db.upsert_inst(conn, d.isoformat(), inst)
    db.replace_disposal(conn, [("3004", (days[-1] - timedelta(days=3)).isoformat(), (days[-1] + timedelta(days=8)).isoformat())])
    return days
