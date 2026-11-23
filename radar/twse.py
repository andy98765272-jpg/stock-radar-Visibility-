"""證交所(上市)盤後資料讀取。

注意:這裡的網址與欄位名稱是依證交所公開介面的格式撰寫,所有解析都以「欄位名稱」比對而不是欄位位置,
以降低格式小改版的影響。第一次使用請先執行 `python -m radar doctor` 確認解析結果。
"""
from __future__ import annotations

import json
import time
import urllib.request

from .util import parse_num, roc_dates, ssl_context

BASE = "https://www.twse.com.tw/rwd/zh"
UA = "Mozilla/5.0 (compatible; StockRadar/1.0)"
_last = [0.0]
PAUSE = [3.5]  # 證交所會限制短時間內的請求量,每次請求之間至少間隔這麼多秒


class FetchError(Exception):
    pass


def http_json(url, retries=3):
    last = None
    for attempt in range(retries):
        wait = PAUSE[0] - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # 網路錯誤、被擋、非 JSON 都走這裡
            last = e
            time.sleep(2 * (attempt + 1))
    raise FetchError(f"{url} -> {last}")


def find_table(payload, must_have):
    """在回應中找出同時包含所有指定欄位名稱的表格,支援新版 tables 與舊版 fieldsN/dataN 格式。"""
    cands = []
    if isinstance(payload.get("tables"), list):
        for t in payload["tables"]:
            cands.append((t.get("fields"), t.get("data")))
    cands.append((payload.get("fields"), payload.get("data")))
    for k, v in payload.items():
        if k.startswith("fields") and k != "fields":
            cands.append((v, payload.get("data" + k[len("fields"):])))
    for fields, data in cands:
        if not fields or not data:
            continue
        if all(any(m in str(f) for f in fields) for m in must_have):
            return fields, data
    return None


def col(fields, name):
    for i, f in enumerate(fields):
        if str(f).strip() == name:
            return i
    for i, f in enumerate(fields):
        if name in str(f):
            return i
    return None


def parse_prices(payload, date_str):
    """回傳 [(code, name, open, high, low, close, volume(股), value(元))]"""
    pd = payload.get("date")
    if pd and str(pd) != date_str:
        return []
    if payload.get("stat") not in (None, "OK"):
        return []
    t = find_table(payload, ["證券代號", "收盤價"])
    if not t:
        return []
    fields, data = t
    i_code, i_name = col(fields, "證券代號"), col(fields, "證券名稱")
    i_vol, i_val = col(fields, "成交股數"), col(fields, "成交金額")
    i_o, i_h, i_l, i_c = col(fields, "開盤價"), col(fields, "最高價"), col(fields, "最低價"), col(fields, "收盤價")
    need = [i_code, i_vol, i_val, i_o, i_h, i_l, i_c]
    if any(x is None for x in need):
        return []
    out = []
    for row in data:
        try:
            code = str(row[i_code]).strip()
            vol, val = parse_num(row[i_vol]), parse_num(row[i_val])
            o, h, l, c = (parse_num(row[i]) for i in (i_o, i_h, i_l, i_c))
        except (IndexError, TypeError):
            continue
        if None in (vol, val, o, h, l, c) or not vol:
            continue
        name = str(row[i_name]).strip() if i_name is not None else ""
        out.append((code, name, o, h, l, c, vol, val))
    return out


def parse_inst(payload, date_str):
    """回傳 {code: 三大法人買賣超股數}"""
    pd = payload.get("date")
    if pd and str(pd) != date_str:
        return {}
    if payload.get("stat") not in (None, "OK"):
        return {}
    t = find_table(payload, ["證券代號", "三大法人買賣超股數"])
    if not t:
        return {}
    fields, data = t
    i_code, i_net = col(fields, "證券代號"), col(fields, "三大法人買賣超股數")
    out = {}
    for row in data:
        try:
            code = str(row[i_code]).strip()
            net = parse_num(row[i_net])
        except (IndexError, TypeError):
            continue
        if net is not None:
            out[code] = net
    return out


def parse_disposal(payload):
    """回傳 [(code, start_iso, end_iso)];欄位抓不到期間時,起迄皆為 None。"""
    t = find_table(payload, ["證券代號", "處置"])
    if not t:
        return []
    fields, data = t
    i_code = col(fields, "證券代號")
    i_per = col(fields, "處置起迄")
    out = []
    for row in data:
        try:
            code = str(row[i_code]).strip()
        except (IndexError, TypeError):
            continue
        s = e = None
        if i_per is not None:
            try:
                ds = roc_dates(str(row[i_per]))
            except IndexError:
                ds = []
            if len(ds) >= 2:
                s, e = ds[0], ds[1]
            elif len(ds) == 1:
                s, e = ds[0], None
        out.append((code, s, e))
    return out


def fetch_prices(d):
    ds = d.strftime("%Y%m%d")
    payload = http_json(f"{BASE}/afterTrading/MI_INDEX?date={ds}&type=ALLBUT0999&response=json")
    return parse_prices(payload, ds)


def fetch_inst(d):
    ds = d.strftime("%Y%m%d")
    payload = http_json(f"{BASE}/fund/T86?date={ds}&selectType=ALLBUT0999&response=json")
    return parse_inst(payload, ds)


def fetch_disposal():
    payload = http_json(f"{BASE}/announcement/punish?response=json")
    return parse_disposal(payload)
