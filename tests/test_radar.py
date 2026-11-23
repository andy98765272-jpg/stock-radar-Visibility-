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
