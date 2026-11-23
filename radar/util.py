from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8))


def now_taipei() -> datetime:
    return datetime.now(TAIPEI)


def today_taipei():
    return now_taipei().date()


def parse_num(s):
    """把 '1,234.50'、'--'、None 之類的字串轉成 float 或 None。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).replace(",", "").strip()
    if t in ("", "--", "---", "-", "X", "x"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def roc_dates(text):
    """從文字中抓出日期(支援民國 115/11/20 與西元 2026/11/20),回傳 ISO 字串清單。"""
    text = text or ""
    out = []
    found = re.findall(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if found:
        for y, m, d in found:
            out.append(f"{int(y):04d}-{int(m):02d}-{int(d):02d}")
        return out
    for y, m, d in re.findall(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", text):
        out.append(f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}")
    return out


# ---- 升降單位 ----
def tick_size(p: float) -> float:
    if p < 10:
        return 0.01
    if p < 50:
        return 0.05
    if p < 100:
        return 0.1
    if p < 500:
        return 0.5
    if p < 1000:
        return 1.0
    return 5.0


def round_down_tick(p: float) -> float:
    t = tick_size(p)
    return round(math.floor(p / t + 1e-9) * t, 2)


def round_up_tick(p: float) -> float:
    t = tick_size(p)
    return round(math.ceil(p / t - 1e-9) * t, 2)


def add_ticks(p: float, k: int = 1) -> float:
    return round(p + k * tick_size(p), 2)


# ---- 交易成本 ----
class Costs:
    def __init__(self, cfg):
        c = cfg["costs"]
        self.fee = c["fee_rate"] * c["fee_discount"]
        self.tax = c["tax_rate"]
        self.slip = c["slippage"]

    def per_share_risk(self, entry: float, stop: float) -> float:
        """停損時每股實際虧損(含手續費、證交稅、滑價)。"""
        return (entry - stop) + 2 * self.slip + entry * self.fee + stop * self.fee + stop * self.tax

    def shares(self, entry: float, stop: float, max_risk: float) -> int:
        per = self.per_share_risk(entry, stop)
        return int(max_risk // per) if per > 0 else 0

    def net_pnl(self, entry: float, exit_: float) -> float:
        """每股淨損益。"""
        return (exit_ - entry) - 2 * self.slip - entry * self.fee - exit_ * self.fee - exit_ * self.tax
