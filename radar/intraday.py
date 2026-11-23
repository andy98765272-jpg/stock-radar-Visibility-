"""盤中監控:只盯盤後選出的候選名單,輪詢即時報價並判斷進場、跳空、量能與停損狀態。

注意:盤中報價使用證交所即時報價介面(上市個股)。這個介面沒有公開的穩定規格,欄位以我所知的格式
(c 代號、z 最近成交價、o 開盤、h 最高、l 最低、y 昨收、v 累計成交量(張)、t 時間)解析,
請先用 `python -m radar watch --once` 在開盤時間實測。報價有數秒延遲,僅供參考,不能用來下單決策的唯一依據。
"""
from __future__ import annotations

import http.cookiejar
import json
import urllib.request

from .util import now_taipei, parse_num

MIS_HOME = "https://mis.twse.com.tw/stock/index.jsp"
MIS_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
UA = "Mozilla/5.0 (compatible; StockRadar/1.0)"

# 一般 U 型量能:開盤後 30 分鐘約 22%、10:30 約 45%、12:00 約 70%、收盤 100%(分鐘數從 09:00 起算)
CURVE = [(0, 0.0), (30, 0.22), (90, 0.45), (180, 0.70), (270, 1.0)]


def minutes_since_open(now):
    return max(0, min(270, now.hour * 60 + now.minute - 9 * 60))


def expected_fraction(minutes):
    if minutes <= 0:
        return 0.0
    for (m0, f0), (m1, f1) in zip(CURVE, CURVE[1:]):
        if minutes <= m1:
            return f0 + (f1 - f0) * (minutes - m0) / (m1 - m0)
    return 1.0


def parse_quotes(payload):
    """回傳 {code: {price, open, high, low, prev_close, volume(股), time}}"""
    out = {}
    for m in payload.get("msgArray", []) or []:
        code = str(m.get("c", "")).strip()
        if not code:
            continue
        price = parse_num(m.get("z"))
        if price is None:  # 尚無成交時 z 是 "-",改用揭示買價的第一檔作參考
            bid = str(m.get("b", "")).split("_")[0]
            price = parse_num(bid)
        lots = parse_num(m.get("v"))
        out[code] = {
            "price": price, "open": parse_num(m.get("o")), "high": parse_num(m.get("h")),
            "low": parse_num(m.get("l")), "prev_close": parse_num(m.get("y")),
            "volume": lots * 1000 if lots is not None else None, "time": m.get("t"),
        }
    return out


def fetch_quotes(codes):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", UA)]
    op.open(MIS_HOME, timeout=20).read()  # 先取得 session cookie
    ex = "|".join(f"tse_{c}.tw" for c in codes)
    with op.open(f"{MIS_API}?ex_ch={ex}&json=1&delay=0", timeout=20) as r:
        return parse_quotes(json.loads(r.read().decode("utf-8")))


def evaluate(pick, q, minutes, cfg):
    """依盤後計畫與即時報價判斷目前狀態。回傳 dict(含 status 與 level: wait/go/warn/dead)。"""
    base = {"code": pick["code"], "name": pick["name"], "strategy_name": pick["strategy_name"],
            "entry": pick["entry"], "stop": pick["stop"], "t1": pick["t1"], "price": None,
            "change_pct": None, "vr": None, "gap_pct": None}
    if not q or q.get("price") is None:
        return dict(base, status="尚無成交", level="wait")
    price, y = q["price"], q.get("prev_close")
    base["price"] = price
    base["change_pct"] = round((price / y - 1) * 100, 2) if y else None
    gap = round((q["open"] / y - 1) * 100, 2) if (q.get("open") and y) else None
    base["gap_pct"] = gap
    avg5 = pick.get("avg_vol5") or 0
    frac = expected_fraction(minutes)
    vr = None
    if q.get("volume") is not None and avg5 > 0 and minutes >= 5 and frac > 0:
        vr = round(q["volume"] / (avg5 * frac), 2)
    base["vr"] = vr
    if price <= pick["stop"]:
        return dict(base, status="跌破停損,訊號失效", level="dead")
    if gap is not None and gap > cfg["filters"]["gap_max_pct"]:
        return dict(base, status=f"跳空 {gap:+.1f}% 過大,不追", level="warn")
    reached = price >= pick["entry"] if pick["order_type"] == "stop" else price <= pick["entry"]
    if not reached:
        return dict(base, status="等待觸及進場價", level="wait")
    if pick["strategy"] == "bk":
        need = cfg["strategies"]["bk"]["min_vol_ratio"]
        if vr is None or vr < need:
            return dict(base, status=f"已觸價,但量比未達 {need}", level="warn")
    return dict(base, status="已觸價,符合進場條件", level="go")


def run_once(picks, cfg, fetch=fetch_quotes, now=None):
    now = now or now_taipei()
    minutes = minutes_since_open(now)
    quotes = fetch([p["code"] for p in picks]) if picks else {}
    items = [evaluate(p, quotes.get(p["code"]), minutes, cfg) for p in picks]
    return {"updated_at": now.strftime("%Y-%m-%d %H:%M:%S"), "minutes": minutes, "items": items}
