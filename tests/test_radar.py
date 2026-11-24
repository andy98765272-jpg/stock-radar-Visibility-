import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar import db, indicators as ind, perf, screen as scr, twse
from radar.__main__ import compute_stats, load_cfg, run_screen, backtest
from radar.util import Costs, add_ticks, parse_num, roc_dates, round_down_tick, round_up_tick, tick_size
from tests import synth

CFG = load_cfg()


class Basics(unittest.TestCase):
    def test_parse_num(self):
        self.assertEqual(parse_num("1,234.50"), 1234.5)
        self.assertIsNone(parse_num("--"))
        self.assertIsNone(parse_num(None))

    def test_roc_dates(self):
        self.assertEqual(roc_dates("115/11/20～115/12/03"), ["2026-11-20", "2026-12-03"])
        self.assertEqual(roc_dates("2026/11/20 ~ 2026/12/03"), ["2026-11-20", "2026-12-03"])

    def test_ticks(self):
        self.assertEqual(tick_size(9.9), 0.01)
        self.assertEqual(tick_size(100), 0.5)
        self.assertEqual(round_down_tick(96.97), 96.9)
        self.assertEqual(round_up_tick(100.2), 100.5)
        self.assertEqual(add_ticks(100.5, 1), 101.0)

    def test_sizing_matches_site_example(self):
        # 文件範例:進場 100、停損 97、最大風險 1,000 → 約 280 股,site 計算器為 279
        self.assertEqual(Costs(CFG).shares(100, 97, 1000), 279)

    def test_indicators(self):
        up = [float(i) for i in range(1, 60)]
        self.assertEqual(ind.rsi(up), 100.0)
        self.assertAlmostEqual(ind.sma(up, 5), 57.0)
        dif, dea, hist = ind.macd(up)
        self.assertGreater(dif, 0)
        a = ind.atr([x + 1 for x in up], [x - 1 for x in up], up)
        self.assertAlmostEqual(a, 2.0 + 0.0, delta=0.1)


class Parsing(unittest.TestCase):
    PRICE_FIELDS = ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"]

    def test_new_format(self):
        payload = {"stat": "OK", "date": "20261120", "tables": [
            {"title": "大盤統計資訊", "fields": ["成交統計", "成交金額"], "data": [["a", "1"]]},
            {"title": "每日收盤行情", "fields": self.PRICE_FIELDS, "data": [
                ["2330", "台積電", "1,000,000", "100", "900,000,000", "900.00", "910.00", "895.00", "905.00"],
                ["1234", "無成交", "0", "0", "0", "--", "--", "--", "--"]]}]}
        rows = twse.parse_prices(payload, "20261120")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "2330")
        self.assertEqual(rows[0][5], 905.0)
        self.assertEqual(rows[0][6], 1000000)

    def test_legacy_format_and_date_mismatch(self):
        payload = {"stat": "OK", "date": "20261120", "fields9": self.PRICE_FIELDS,
                   "data9": [["2330", "台積電", "1,000", "1", "905,000", "900", "910", "895", "905"]]}
        self.assertEqual(len(twse.parse_prices(payload, "20261120")), 1)
        self.assertEqual(twse.parse_prices(payload, "20261119"), [])
        self.assertEqual(twse.parse_prices({"stat": "很抱歉，沒有符合條件的資料!"}, "20261120"), [])

    def test_inst_and_disposal(self):
        inst = {"stat": "OK", "date": "20261120", "fields": ["證券代號", "證券名稱", "三大法人買賣超股數"],
                "data": [["2330", "台積電", "1,234"], ["2317", "鴻海", "-500"]]}
        self.assertEqual(twse.parse_inst(inst, "20261120"), {"2330": 1234.0, "2317": -500.0})
        disp = {"fields": ["編號", "證券代號", "證券名稱", "處置起迄時間"],
                "data": [["1", "3004", "某股", "115/11/17～115/11/30"]]}
        self.assertEqual(twse.parse_disposal(disp), [("3004", "2026-11-17", "2026-11-30")])


class Simulate(unittest.TestCase):
    def sig(self, order="stop"):
        return {"entry": 100.0, "stop": 97.0, "t1": 106.0, "order": order}

    def sim(self, future, order="stop"):
        return perf.simulate(self.sig(order), future, CFG, Costs(CFG))

    def bar(self, d, o, h, l, c):
        return (d, o, h, l, c)

    def test_target(self):
        r = self.sim([self.bar("d1", 99, 101, 98, 100), self.bar("d2", 100, 107, 99, 106)])
        self.assertEqual(r["status"], "target")
        self.assertEqual(r["exit"], 106)
        self.assertGreater(r["r"], 1.5)

    def test_stop_priority_same_day(self):
        r = self.sim([self.bar("d1", 99, 101, 98, 100), self.bar("d2", 100, 107, 96, 100)])
        self.assertEqual(r["status"], "stop")

    def test_fill_day_only_checks_stop(self):
        # 成交當天最高價雖然超過停利價,但當天不算停利;之後也沒再碰到停利,應走到時間出場
        r = self.sim([self.bar("d1", 99, 108, 98, 107)] + [self.bar(f"d{i}", 105, 105.5, 103, 104) for i in range(2, 9)])
        self.assertEqual(r["status"], "time")
        # 成交隔天就碰到停利,才算停利
        r2 = self.sim([self.bar("d1", 99, 108, 98, 107), self.bar("d2", 105, 107, 104, 106)])
        self.assertEqual(r2["status"], "target")

    def test_gap_down_exit_at_open(self):
        r = self.sim([self.bar("d1", 99, 101, 98, 100), self.bar("d2", 94, 95, 93, 94)])
        self.assertEqual(r["status"], "stop")
        self.assertEqual(r["exit"], 94)

    def test_nofill_and_pending(self):
        far = [self.bar(f"d{i}", 90, 95, 88, 92) for i in range(5)]
        self.assertEqual(self.sim(far)["status"], "nofill")
        self.assertEqual(self.sim(far[:1])["status"], "pending")

    def test_time_exit(self):
        fut = [self.bar("d1", 99, 101, 98, 100)] + [self.bar(f"d{i}", 100, 102, 98, 101) for i in range(2, 9)]
        r = self.sim(fut)
        self.assertEqual(r["status"], "time")
        self.assertEqual(r["exit"], 101)

    def test_limit_order(self):
        r = self.sim([self.bar("d1", 103, 104, 99.5, 102)], order="limit")
        self.assertEqual(r.get("status"), "open")  # 成交後資料還不足以出場


class TrustStrategy(unittest.TestCase):
    """投信隔日沖:條件、模擬與 T86 欄位解析。"""
    def _bars(self, n=25, last=None):
        base = [("2026-10-%02d" % (i + 1), 100, 101, 99, 100, 3_000_000, 3e8) for i in range(n - 1)]
        base.append(last or ("2026-10-30", 100, 103, 100, 102, 4_000_000, 4e8))
        return base

    def _U(self, bars, code="2330"):
        U = scr.Universe()
        U.bars[code] = bars
        U.dates[code] = [b[0] for b in bars]
        U.names[code] = "測試"
        U.mdates = [b[0] for b in bars]
        return U

    def test_pass_and_each_condition_blocks(self):
        from radar import ts
        U = self._U(self._bars())
        asof = "2026-10-30"
        ok = ts.screen_ts(U, {asof: {"2330": 500_000}}, [], asof, CFG)
        self.assertEqual([p["code"] for p in ok["picks"]], ["2330"])
        self.assertEqual(ok["picks"][0]["trust_lots"], 500)
        self.assertEqual(ts.screen_ts(U, {asof: {"2330": -1}}, [], asof, CFG)["picks"], [])      # 投信賣超
        self.assertFalse(ts.screen_ts(U, {}, [], asof, CFG)["ready"])                            # 還沒公布投信資料
        low = self._bars(last=("2026-10-30", 100, 103, 100, 102, 2_000_000, 2e8))
        self.assertEqual(ts.screen_ts(self._U(low), {asof: {"2330": 1}}, [], asof, CFG)["picks"], [])   # 量不足 2500 張
        flat = self._bars(last=("2026-10-30", 100, 103, 100, 102, 3_300_000, 3e8))
        self.assertEqual(ts.screen_ts(self._U(flat), {asof: {"2330": 1}}, [], asof, CFG)["picks"], [])  # 量未達 1.2 倍
        nohigh = self._bars()
        nohigh[5] = ("2026-10-06", 100, 110, 99, 100, 3_000_000, 3e8)
        self.assertEqual(ts.screen_ts(self._U(nohigh), {asof: {"2330": 1}}, [], asof, CFG)["picks"], [])  # 沒創 20 日新高
        lim = self._bars(last=("2026-10-30", 100, 110, 100, 109.9, 4_000_000, 4e8))
        self.assertEqual(ts.screen_ts(self._U(lim), {asof: {"2330": 1}}, [], asof, CFG)["picks"], [])    # 漲幅 ≥ 9.5%
        self.assertEqual(ts.screen_ts(U, {asof: {"2330": 1}}, [("2330", None, None)], asof, CFG)["picks"], [])  # 處置股

    def test_rank_by_trust_share(self):
        from radar import ts
        a, b = self._bars(), self._bars()
        U = self._U(a, "2330")
        U.bars["2317"], U.dates["2317"], U.names["2317"] = b, [x[0] for x in b], "乙"
        res = ts.screen_ts(U, {"2026-10-30": {"2330": 100_000, "2317": 900_000}}, [], "2026-10-30", CFG)
        self.assertEqual([p["code"] for p in res["picks"]], ["2317", "2330"])
        self.assertEqual([p["rank"] for p in res["picks"]], [1, 2])

    def test_simulate_modes_and_costs(self):
        from radar import ts
        costs = Costs(CFG)
        cfg = json.loads(json.dumps(CFG))
        cfg['ts'].update({'tp_pct': 1.5, 'sl_pct': 5.0})
        up = [("d1", 100, 102, 99.5, 101, 0), ("d2", 101, 102, 100, 101, 0)]
        r = ts.simulate(100, up, cfg, costs, "same_close")           # 當日 +1.5% 達標
        self.assertEqual(r["status"], "tp")
        self.assertAlmostEqual(r["gross_pct"], 1.5, places=6)
        self.assertLess(r["net_pct"], 1.0)                           # 扣掉手續費與證交稅後只剩約 0.9%
        self.assertAlmostEqual(r["net_pct"], 1.5 - 0.1425 * 2 - 0.3 - 0.0, delta=0.05)
        r = ts.simulate(100, up, cfg, costs, "next_open")            # 隔日開盤 100 進場,最高 102 達標
        self.assertEqual((r["status"], r["fill"]), ("tp", 100))
        both = [("d1", 100, 102, 94, 100, 0)]
        self.assertEqual(ts.simulate(100, both, cfg, costs, "next_open")["status"], "stop")   # 同日兩邊都碰到,以停損計
        gap = [("d1", 100, 100.5, 99.8, 100, 0), ("d2", 90, 91, 89, 90, 0)]
        r = ts.simulate(100, gap, cfg, costs, "same_close")
        self.assertEqual((r["status"], r["exit"]), ("stop", 90))     # 跳空越過停損,以開盤價出場
        flat = [("d%d" % i, 100, 100.8, 99.5, 100.2, 0) for i in range(5)]
        r = ts.simulate(100, flat, cfg, costs, "same_close")
        self.assertEqual((r["status"], r["exit_date"]), ("time", "d4"))   # 第 5 日收盤出場
        self.assertEqual(ts.simulate(100, flat[:3], cfg, costs, "same_close")["status"], "open")
        self.assertEqual(ts.simulate(100, [], cfg, costs, "same_close")["status"], "pending")

    def test_override_and_sweep(self):
        from radar import ts
        costs = Costs(CFG)
        bars = [("d1", 100, 103.5, 99, 101, 0)]
        self.assertEqual(ts.simulate(100, bars, CFG, costs, "next_open", 3.0, 3.0)["status"], "tp")
        self.assertEqual(ts.simulate(100, bars, CFG, costs, "next_open", 4.0, 3.0)["status"], "open")
        bars2 = [("d1", 100, 103.5, 96.5, 101, 0)]   # 同日兩邊都碰到,算停損
        self.assertEqual(ts.simulate(100, bars2, CFG, costs, "next_open", 3.0, 3.0)["status"], "stop")
        self.assertEqual(CFG["ts"]["tp_pct"], 3.0)
        self.assertIn([3.0, 3.0], CFG["ts"]["sweep"])

    def test_parse_trust(self):
        from radar import twse
        payload = {"stat": "OK", "date": "20261120", "fields": ["證券代號", "證券名稱", "投信買進股數", "投信買賣超股數", "三大法人買賣超股數"],
                   "data": [["2330", "台積電", "1,000", "2,500", "-3,000"], ["2317", "鴻海", "0", "-1,000", "500"]]}
        self.assertEqual(twse.parse_trust(payload, "20261120"), {"2330": 2500, "2317": -1000})
        self.assertEqual(twse.parse_inst(payload, "20261120"), {"2330": -3000, "2317": 500})
        self.assertEqual(twse.parse_trust(payload, "20261119"), {})

    def test_backtest_runs_on_synth(self):
        from radar import ts
        tmp = tempfile.mkdtemp()
        conn = db.connect(os.path.join(tmp, "t.db"))
        days = synth.build(conn)
        U = scr.Universe.load(conn)
        import random
        rng = random.Random(1)
        for d in days:   # 合成資料沒有投信資料,隨機給一些買超
            db.upsert_trust(conn, d.isoformat(), {c: rng.choice([-1, 1]) * rng.randint(1, 800) * 1000 for c in U.bars})
        trust = db.load_trust(conn)
        cfg = json.loads(json.dumps(CFG))
        cfg["ts"]["min_lots"] = 100
        res = ts.backtest(conn, U, trust, [], cfg, [d.isoformat() for d in days])
        json.dumps(res)
        self.assertEqual(len(res["sweep"]), len(cfg["ts"]["sweep"]))
        for m in ts.MODES:
            s = res["modes"][m]
            if s["n"]:
                self.assertLessEqual(s["avg_net_pct"], s["avg_gross_pct"])   # 成本只會讓報酬變差
                self.assertEqual(sum(s["exits"].values()), s["n"])


class LiveScreen(unittest.TestCase):
    """盤中即時選股:收盤時(推估比例 100%)的結果應該和盤後正式選股一致。"""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.conn = db.connect(os.path.join(cls.tmp, "t.db"))
        cls.days = synth.build(cls.conn)
        cls.last = cls.days[-1].isoformat()

    def _hist_universe(self):
        U = scr.Universe.load(self.conn)
        full = {c: list(b) for c, b in U.bars.items()}
        for c in list(U.bars):
            U.bars[c] = [b for b in full[c] if b[0] < self.last]
            U.dates[c] = [b[0] for b in U.bars[c]]
        U.mdates = [d for d in U.mdates if d < self.last]
        return U, full

    def _fake_fetch(self, full, scale=1.0):
        def fetch(codes):
            out = {}
            for c in codes:
                b = full[c][-1]
                out[c] = {"price": b[4], "open": b[1], "high": b[2], "low": b[3], "prev_close": full[c][-2][4],
                          "volume": b[5] * scale, "time": "13:30:00"}
            return out
        return fetch

    def test_matches_after_close_screen_at_full_day(self):
        from datetime import datetime
        from radar import livescreen
        U, full = self._hist_universe()
        now = datetime.fromisoformat(self.last + "T13:30:00")
        live = livescreen.run(U, [], CFG, fetch=self._fake_fetch(full), now=now)
        ref = run_screen(self.conn, CFG)
        self.assertTrue(live["meta"]["live"])
        codes = {p["code"] for p in live["picks"]} | {p["code"] for p in live["watchlist"]}
        self.assertIn("3001", codes)                                   # 刻意造出的突破型標的,盤中預估也要抓得到
        self.assertIn("3001", {p["code"] for p in ref["picks"]})
        for p in live["picks"]:
            self.assertGreaterEqual(p["score"], round(CFG["score"]["min_score"] * 0.8))
            self.assertIsNone(p["chips"])
        self.assertTrue(any("按比例調降" in n for n in live["meta"]["notes"]))
        self.assertGreater(live["meta"]["quoted"], 0)
        json.dumps(live)

    def test_guards(self):
        from datetime import datetime
        from radar import livescreen
        U, full = self._hist_universe()
        early = livescreen.run(U, [], CFG, fetch=self._fake_fetch(full), now=datetime.fromisoformat(self.last + "T09:10:00"))
        self.assertIn("error", early["meta"])           # 開盤太早,量推估不準
        Uf = scr.Universe.load(self.conn)
        done = livescreen.run(Uf, [], CFG, fetch=self._fake_fetch(full), now=datetime.fromisoformat(self.last + "T11:00:00"))
        self.assertIn("error", done["meta"])            # 資料庫已有今天的收盤資料

    def test_projection_uses_time_fraction(self):
        from datetime import datetime
        from radar import intraday, livescreen
        U, full = self._hist_universe()
        q = self._fake_fetch(full, scale=intraday.expected_fraction(90))(["3001"])
        V, used = livescreen.with_today(U, q, self.last, 90)
        self.assertAlmostEqual(V.bars["3001"][-1][5], full["3001"][-1][5], delta=full["3001"][-1][5] * 0.001)


class Ssl(unittest.TestCase):
    def test_context_keeps_verification(self):
        import ssl
        from radar.util import ssl_context
        c = ssl_context()
        self.assertEqual(c.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(c.check_hostname)
        strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
        self.assertEqual(c.verify_flags & strict, 0)


class Intraday(unittest.TestCase):
    PICK = {"code": "3001", "name": "合成突破", "strategy": "bk", "strategy_name": "帶量突破型", "entry": 100.0,
            "stop": 97.0, "t1": 106.0, "order_type": "stop", "avg_vol5": 3_000_000}

    def test_curve(self):
        from radar import intraday as it
        self.assertEqual(it.expected_fraction(0), 0.0)
        self.assertAlmostEqual(it.expected_fraction(30), 0.22)
        self.assertEqual(it.expected_fraction(270), 1.0)
        self.assertLess(it.expected_fraction(60), it.expected_fraction(120))

    def test_parse_quotes(self):
        from radar import intraday as it
        q = it.parse_quotes({"msgArray": [
            {"c": "3001", "z": "100.50", "o": "99.00", "h": "101.00", "l": "98.50", "y": "98.00", "v": "1,200", "t": "09:45:00"},
            {"c": "3002", "z": "-", "b": "55.10_55.00_", "y": "54.00", "v": "10"}]})
        self.assertEqual(q["3001"]["price"], 100.5)
        self.assertEqual(q["3001"]["volume"], 1_200_000)
        self.assertEqual(q["3002"]["price"], 55.1)

    def test_parse_real_response_sample(self):
        # 2026-11-23 收盤後從 mis.twse.com.tw 取得的真實欄位(聚陽)
        from radar import intraday as it
        q = it.parse_quotes({"msgArray": [{"c": "1477", "n": "聚陽", "z": "221.5000", "pz": "222.5000", "o": "220.0000",
            "h": "223.0000", "l": "216.5000", "y": "220.0000", "v": "1832", "t": "13:30:00", "tv": "187"}]})["1477"]
        self.assertEqual(q["price"], 221.5)
        self.assertEqual(q["prev_close"], 220.0)
        self.assertEqual(q["volume"], 1_832_000)
        self.assertEqual(q["time"], "13:30:00")

    def ev(self, price, vol, open_=99.0, minutes=60, pick=None):
        from radar import intraday as it
        q = {"price": price, "open": open_, "prev_close": 98.0, "volume": vol}
        return it.evaluate(pick or self.PICK, q, minutes, CFG)

    def test_status_levels(self):
        # 60 分鐘預期累計約 33%:3,000,000 × 0.33 ≈ 990,000;量比 1.5 需要約 1,485,000 股
        self.assertEqual(self.ev(99.0, 2_000_000)["level"], "wait")
        self.assertEqual(self.ev(100.5, 2_000_000)["level"], "go")
        self.assertEqual(self.ev(100.5, 1_000_000)["level"], "warn")      # 觸價但量不足
        self.assertEqual(self.ev(96.5, 2_000_000)["level"], "dead")
        self.assertEqual(self.ev(104.0, 2_000_000, open_=103.0)["level"], "warn")  # 跳空過大
        self.assertEqual(self.ev(None, None)["level"], "wait")

    def test_run_once_with_fake_fetch(self):
        from datetime import datetime
        from radar import intraday as it
        fake = lambda codes: {"3001": {"price": 100.5, "open": 99.0, "prev_close": 98.0, "volume": 2_000_000}}
        res = it.run_once([self.PICK], CFG, fetch=fake, now=datetime(2026, 11, 23, 10, 0))
        self.assertEqual(res["minutes"], 60)
        self.assertEqual(res["items"][0]["level"], "go")
        json.dumps(res, ensure_ascii=False)


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.conn = db.connect(os.path.join(cls.tmp, "t.db"))
        cls.days = synth.build(cls.conn)
        cls.res = run_screen(cls.conn, CFG)

    def test_asof_is_last_day(self):
        self.assertEqual(self.res["meta"]["asof"], self.days[-1].isoformat())

    def test_pick_invariants(self):
        picks = self.res["picks"]
        self.assertGreater(len(picks), 0, json.dumps(self.res["meta"]["counts"], ensure_ascii=False))
        for p in picks:
            self.assertGreaterEqual(p["score"], CFG["score"]["min_score"])
            self.assertGreaterEqual(p["rr"], CFG["filters"]["min_rr"])
            self.assertLess(p["stop"], p["entry"])
            self.assertLess(p["entry"], p["t1"])
            self.assertLess(p["t1"], p["t2"])
            self.assertTrue(CFG["filters"]["price_min"] <= p["close"] <= CFG["filters"]["price_max"])
            self.assertGreaterEqual(p["turnover20_wan"], CFG["filters"]["min_turnover20_wan"])
            self.assertIn(p["strategy"], ("bk", "pb", "mo"))
            self.assertEqual(sum(p["breakdown"].values()), p["score"])
            self.assertGreater(p["shares"], 0)
        scores = [p["score"] for p in picks]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_history_and_info(self):
        for p in self.res["picks"]:
            h = p["history"]
            self.assertEqual(len(h), 60)
            self.assertEqual(h[-1][0], self.res["meta"]["asof"])
            self.assertEqual(h[-1][4], p["close"])
            self.assertAlmostEqual(h[-1][6], p["ma5"], places=1)
            self.assertAlmostEqual(h[-1][8], p["ma20"], places=1)
            self.assertTrue(all(x[6] is not None for x in h[-10:]))
            self.assertIn("ret5", p["info"])

    def test_watchlist_is_separate(self):
        res = self.res
        self.assertIn("watchlist", res)
        picks = {p["code"] for p in res["picks"]}
        for w in res["watchlist"]:
            self.assertNotIn(w["code"], picks)
            self.assertTrue(w["watch_reason"])
            self.assertGreaterEqual(w["score"], CFG["score"]["watch_min"])
            self.assertEqual(len(w["history"]), 60)
        for p in res["picks"]:
            self.assertNotIn("watch_reason", p)
        self.assertEqual(res["meta"]["counts"]["qualified"], len(res["picks"]))

    def test_disposal_excluded(self):
        self.assertNotIn("3004", [p["code"] for p in self.res["picks"]])

    def test_engineered_stocks(self):
        got = {p["code"]: p["strategy"] for p in self.res["picks"]}
        print("\n合成標的入選結果:", got, self.res["meta"]["counts"])
        self.assertIn("3001", got)

    def test_backtest_and_stats_serialize(self):
        backtest(self.conn, CFG, 40, log=lambda *_: None)
        db.save_signals(self.conn, "live", self.res["picks"], self.res["meta"]["asof"])
        st = compute_stats(self.conn, CFG)
        json.dumps(st, ensure_ascii=False)
        bt = st["backtest"]
        self.assertGreaterEqual(bt["signals"], bt["all"]["n"] + bt["nofill"] + bt["open"] - 1)
        self.assertEqual(st["live"]["all"]["n"], 0)  # 最後一天的訊號還沒有後續行情,不應被計入

    def test_signals_roundtrip(self):
        path = os.path.join(self.tmp, "signals.json")
        db.save_signals(self.conn, "live", self.res["picks"], self.res["meta"]["asof"])
        db.export_signals_json(self.conn, path)
        c2 = db.connect(os.path.join(self.tmp, "t2.db"))
        n = db.import_signals_json(c2, path)
        self.assertEqual(n, len(self.res["picks"]))


if __name__ == "__main__":
    unittest.main()
