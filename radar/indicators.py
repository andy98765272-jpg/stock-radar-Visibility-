from __future__ import annotations


def sma(vals, n, end=None):
    """取到 end(不含)為止最近 n 筆的平均;資料不足回傳 None。"""
    e = len(vals) if end is None else end
    if e - n < 0 or n <= 0:
        return None
    return sum(vals[e - n:e]) / n


def ema_series(vals, n):
    a = 2.0 / (n + 1)
    out = []
    prev = None
    for v in vals:
        prev = v if prev is None else a * v + (1 - a) * prev
        out.append(prev)
    return out


def macd(closes, fast=12, slow=26, signal=9):
    """回傳 (DIF, DEA, 柱狀體)。"""
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema_series(dif, signal)
    return dif[-1], dea[-1], dif[-1] - dea[-1]


def rsi(closes, n=14):
    """Wilder RSI。"""
    if len(closes) <= n:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def atr(highs, lows, closes, n=14):
    """Wilder ATR。"""
    if len(closes) <= n:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    a = sum(trs[:n]) / n
    for i in range(n, len(trs)):
        a = (a * (n - 1) + trs[i]) / n
    return a
