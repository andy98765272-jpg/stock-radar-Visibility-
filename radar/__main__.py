from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta

from . import db, livescreen, perf, screen as scr, ts as tsm, twse
from .util import now_taipei, today_taipei

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "radar.db")
OUT_DIR = os.path.join(ROOT, "docs", "data")


def load_cfg(path=None):
    with open(path or os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def write_json(name, obj):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


# ---------------- 資料更新 ----------------
def update(conn, cfg, history_days=None, log=print):
    twse.PAUSE[0] = cfg["data"]["request_pause_sec"]
    history_days = history_days or cfg["data"]["history_days"]
    today = today_taipei()
    first = today - timedelta(days=history_days)
    last = db.last_price_date(conn)
    start = max(first, date.fromisoformat(last) + timedelta(days=1)) if last else first
    d = start
    new_days = 0
    failures = 0
    while d <= today:
        iso = d.isoformat()
        if d.weekday() < 5 and not db.has_prices(conn, iso) and not db.is_no_data(conn, iso):
            try:
                rows = twse.fetch_prices(d)
            except twse.FetchError as e:
                failures += 1
                log(f"  ! {iso} 抓取失敗:{e}")
                if failures >= 3 and new_days == 0:
                    raise SystemExit("連續抓取失敗,可能是網路被擋或證交所暫時無法連線。請先執行 `python -m radar doctor`。")
                d += timedelta(days=1)
                continue
            if rows:
                db.upsert_prices(conn, iso, rows)
                new_days += 1
                log(f"  {iso} 行情 {len(rows)} 檔")
            elif d < today:
                db.mark_no_data(conn, iso)  # 非交易日(假日),之後不再重試
        d += timedelta(days=1)
    # 補抓法人與投信資料:最近 5 個交易日一定檢查(資料通常比行情晚發布);
    # 投信買賣超另外回補到 trust_backfill_days 個交易日,供「投信隔日沖」回測使用
    dates_all = db.price_dates(conn)
    back = cfg["data"].get("trust_backfill_days", 140)
    todo = [iso for i, iso in enumerate(dates_all[-back:])
            if not db.has_trust(conn, iso) or (iso in dates_all[-5:] and not db.has_inst(conn, iso))]
    if len(todo) > 10:
        log(f"  回補 {len(todo)} 個交易日的投信資料(每日約 {cfg['data']['request_pause_sec']:.1f} 秒,請稍候)")
    for iso in todo:
        try:
            m_inst, m_trust = twse.fetch_inst_trust(date.fromisoformat(iso))
        except twse.FetchError as e:
            log(f"  ! {iso} 法人資料抓取失敗:{e}")
            continue
        if m_inst:
            db.upsert_inst(conn, iso, m_inst)
        if m_trust:
            db.upsert_trust(conn, iso, m_trust)
            log(f"  {iso} 法人 {len(m_inst)} 檔、投信 {len(m_trust)} 檔")
    try:
        disp = twse.fetch_disposal()
        db.replace_disposal(conn, disp)
        log(f"  處置股 {len(disp)} 檔")
    except twse.FetchError as e:
        log(f"  ! 處置股清單抓取失敗(沿用舊資料):{e}")
    return new_days


# ---------------- 選股與績效 ----------------
def run_screen(conn, cfg, asof=None):
    dates = db.price_dates(conn)
    if not dates:
        raise SystemExit("資料庫沒有行情資料,請先執行 update 或 backfill。")
    asof = asof or dates[-1]
    since = dates[max(0, len(dates) - 140)]
    U = scr.Universe.load(conn, since)
    inst = scr.load_inst(conn, since)
    res = scr.screen(U, inst, db.load_disposal(conn), asof, cfg)
    return res


def compute_stats(conn, cfg):
    out = {"generated_at": now_taipei().isoformat(timespec="seconds"), "assumptions": perf.__doc__.strip()}
    for source in ("live", "backtest"):
        sigs = db.load_signals(conn, source)
        out[source] = perf.summarize(perf.evaluate(conn, sigs, cfg), list(scr.STRAT_ORDER))
    return out


def backtest(conn, cfg, days, log=print):
    dates = db.price_dates(conn)
    since = dates[max(0, len(dates) - 140 - days)]
    U = scr.Universe.load(conn, since)
    inst = scr.load_inst(conn, since)
    disp = db.load_disposal(conn)
    target = dates[-days:] if days < len(dates) else dates
    target = [d for i, d in enumerate(dates) if d in set(target) and i >= cfg["filters"]["min_history"]]
    db.clear_signals(conn, "backtest")
    total = 0
    for d in target:
        res = scr.screen(U, inst, disp, d, cfg)
        db.save_signals(conn, "backtest", res["picks"], d)
        total += len(res["picks"])
    log(f"回測 {len(target)} 個交易日,共 {total} 筆訊號")


def run_ts(conn, cfg, days):
    dates = db.price_dates(conn)
    since = dates[max(0, len(dates) - 140)]
    U = scr.Universe.load(conn, since)
    trust = db.load_trust(conn, since)
    disp = db.load_disposal(conn)
    today = tsm.screen_ts(U, trust, disp, dates[-1], cfg)
    bt = tsm.backtest(conn, U, trust, disp, cfg, dates[-days:])
    return {"generated_at": now_taipei().isoformat(timespec="seconds"), "params": cfg["ts"], "doc": tsm.__doc__.strip(),
            "today": today, "backtest": bt}


def cmd_ts(args):
    cfg = load_cfg(args.config)
    conn = db.connect(DB_PATH)
    res = run_ts(conn, cfg, args.days)
    write_json("ts.json", res)
    t = res["today"]
    print(f"資料日期 {t['asof']},投信隔日沖入選 {len(t['picks'])} 檔(資料就緒:{t['ready']})")
    for p in t["picks"]:
        print(f"  {p['rank']:>2}. {p['code']} {p['name']:<8} 收 {p['close']} 量 {p['vol_lots']} 張 投信買 {p['trust_lots']} 張 ({p['trust_share_pct']}%)")
    for m, s in res["backtest"]["modes"].items():
        print(f"  [{m}] {s.get('n', 0)} 筆 毛勝率 {s.get('win_rate_gross')} 淨勝率 {s.get('win_rate_net')} 每筆淨 {s.get('avg_net_pct')}%")


def cmd_livescreen(args):
    cfg = load_cfg(args.config)
    conn = db.connect(DB_PATH)
    dates = db.price_dates(conn)
    if not dates:
        raise SystemExit("資料庫沒有行情資料,請先執行 update。")
    U = scr.Universe.load(conn, dates[max(0, len(dates) - 140)])
    res = livescreen.run(U, db.load_disposal(conn), cfg)
    write_json("live_picks.json", res)
    m = res["meta"]
    if m.get("error"):
        print(m["error"])
        return 0
    print(f"{m['updated_at']} 盤中預估:查詢 {m['queried']} 檔、有報價 {m['quoted']} 檔,入選 {len(res['picks'])} 檔、觀察 {len(res['watchlist'])} 檔")
    for p in res["picks"]:
        print(f"  {p['code']} {p['name']:<8} {p['strategy_name']} {p['score']}分 現價 {p['close']}")


def cmd_daily(args):
    cfg = load_cfg(args.config)
    conn = db.connect(DB_PATH)
    sig_json = os.path.join(OUT_DIR, "signals.json")
    n = db.import_signals_json(conn, sig_json)
    if n:
        print(f"從 signals.json 還原 {n} 筆實盤訊號")
    print("更新資料…")
    update(conn, cfg)
    print("執行選股…")
    res = run_screen(conn, cfg)
    asof = res["meta"]["asof"]
    db.save_signals(conn, "live", res["picks"], asof)
    db.export_signals_json(conn, sig_json)
    write_json("picks.json", res)
    print(f"資料日期 {asof},入選 {len(res['picks'])} 檔 → docs/data/picks.json")
    if len(db.price_dates(conn)) >= cfg["filters"]["min_history"] + 10:
        backtest(conn, cfg, args.backtest_days)
    write_json("stats.json", compute_stats(conn, cfg))
    print("績效統計已更新 → docs/data/stats.json")
    write_json("ts.json", run_ts(conn, cfg, 140))
    print("投信隔日沖已更新 → docs/data/ts.json")


def cmd_update(args):
    cfg = load_cfg(args.config)
    conn = db.connect(DB_PATH)
    print(f"新增 {update(conn, cfg, args.days)} 個交易日")


def cmd_screen(args):
    cfg = load_cfg(args.config)
    conn = db.connect(DB_PATH)
    res = run_screen(conn, cfg, args.date)
    write_json("picks.json", res)
    for p in res["picks"]:
        print(f"{p['code']} {p['name']:<8} {p['strategy_name']} {p['score']}分  進 {p['entry']} 損 {p['stop']} 利 {p['t1']}/{p['t2']}")
    print(json.dumps(res["meta"]["counts"], ensure_ascii=False))


def cmd_backtest(args):
    cfg = load_cfg(args.config)
    conn = db.connect(DB_PATH)
    backtest(conn, cfg, args.days)
    write_json("stats.json", compute_stats(conn, cfg))


def cmd_doctor(args):
    cfg = load_cfg(args.config)
    twse.PAUSE[0] = cfg["data"]["request_pause_sec"]
    print("檢查證交所資料來源…")
    d = today_taipei()
    ok = False
    for _ in range(8):
        if d.weekday() < 5:
            try:
                rows = twse.fetch_prices(d)
            except twse.FetchError as e:
                print(f"  ✗ 無法連線:{e}")
                print("    可能被擋(境外 IP)或網路問題。見 README「如果證交所擋掉雲端 IP」。")
                return 1
            if rows:
                print(f"  ✓ {d} 行情:{len(rows)} 檔,範例:{rows[0]}")
                try:
                    inst = twse.fetch_inst(d)
                    print(f"  {'✓' if inst else '✗'} {d} 三大法人:{len(inst)} 檔")
                except twse.FetchError as e:
                    print(f"  ✗ 法人資料:{e}")
                try:
                    disp = twse.fetch_disposal()
                    print(f"  {'✓' if disp else '△'} 處置股清單:{len(disp)} 檔,範例:{disp[:2]}")
                except twse.FetchError as e:
                    print(f"  ✗ 處置股:{e}")
                ok = True
                break
        d -= timedelta(days=1)
    if not ok:
        print("  ✗ 最近 8 天都解析不到行情,證交所回應格式可能已改版,請把錯誤回報給維護者。")
        return 1
    return 0


def cmd_import(args):
    """匯入由瀏覽器抓下來的 JSON(prices / inst / disposal),用在無法直接連線證交所的環境。"""
    conn = db.connect(DB_PATH)
    with open(args.file, encoding="utf-8") as f:
        data = json.load(f)
    n = 0
    for ds, rows in sorted(data["prices"].items()):
        iso = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
        db.upsert_prices(conn, iso, [tuple(r) for r in rows])
        n += 1
    for ds, m in sorted(data.get("inst", {}).items()):
        db.upsert_inst(conn, f"{ds[:4]}-{ds[4:6]}-{ds[6:]}", m)
    disp = data.get("disposal")
    if disp and disp.get("data"):
        rows = twse.parse_disposal({"fields": disp["fields"], "data": disp["data"]})
        db.replace_disposal(conn, rows)
        print(f"處置股 {len(rows)} 檔")
    print(f"匯入 {n} 個交易日行情、{len(data.get('inst', {}))} 個交易日法人資料")


def cmd_watch(args):
    import time
    from . import intraday
    cfg = load_cfg(args.config)
    path = os.path.join(OUT_DIR, "picks.json")
    if not os.path.exists(path):
        raise SystemExit("找不到 docs/data/picks.json,請先執行 `python -m radar daily` 產生候選名單。")
    with open(path, encoding="utf-8") as f:
        picks = json.load(f)["picks"]
    print(f"監控 {len(picks)} 檔候選股,每 {args.interval} 秒向證交所抓一次報價(Ctrl+C 結束)")
    if args.interval < 3:
        print("  ! 間隔低於 3 秒:證交所報價本身更新不快,過於頻繁可能被限流或暫時封鎖 IP,建議用 5 秒以上。")
    while True:
        now = now_taipei()
        try:
            res = intraday.run_once(picks, cfg, now=now)
        except Exception as e:  # 網路或格式問題不要讓整個監控中斷
            print(f"[{now:%H:%M:%S}] 抓取失敗:{e}")
            if args.once:
                return 1
        else:
            write_json("intraday.json", res)
            print(f"\n[{res['updated_at']}] 盤中量比預期比例依時間估計")
            for x in res["items"]:
                px = "—" if x["price"] is None else f"{x['price']:.2f}"
                vr = "—" if x["vr"] is None else f"{x['vr']:.2f}"
                print(f"  {x['code']} {x['name']:<8} 現價 {px:>8} 量比 {vr:>5} 進 {x['entry']} 損 {x['stop']}  {x['status']}")
        if args.once or (now.hour, now.minute) >= (13, 35):
            return 0
        time.sleep(args.interval)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="radar", description="STOCK RADAR 台股短線選股")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="檢查資料來源是否可用").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("update", help="更新行情與法人資料")
    p.add_argument("--days", type=int, default=None, help="往回補幾個日曆天")
    p.set_defaults(fn=cmd_update)
    p = sub.add_parser("screen", help="以資料庫現有資料選股")
    p.add_argument("--date", default=None)
    p.set_defaults(fn=cmd_screen)
    p = sub.add_parser("backtest", help="重播過去 N 個交易日的選股並統計績效")
    p.add_argument("--days", type=int, default=60)
    p.set_defaults(fn=cmd_backtest)
    p = sub.add_parser("livescreen", help="盤中即時選股(預估版)")
    p.set_defaults(fn=cmd_livescreen)
    p = sub.add_parser("ts", help="投信隔日沖選股與回測")
    p.add_argument("--days", type=int, default=140)
    p.set_defaults(fn=cmd_ts)
    p = sub.add_parser("import", help="匯入瀏覽器抓下來的資料檔(JSON)")
    p.add_argument("file")
    p.set_defaults(fn=cmd_import)
    p = sub.add_parser("watch", help="盤中監控候選名單(在本機執行)")
    p.add_argument("--interval", type=int, default=5, help="向證交所抓報價的間隔秒數(預設 5)")
    p.add_argument("--once", action="store_true", help="只抓一次,用來測試")
    p.set_defaults(fn=cmd_watch)
    p = sub.add_parser("daily", help="更新 + 選股 + 績效統計(排程用)")
    p.add_argument("--backtest-days", type=int, default=60)
    p.set_defaults(fn=cmd_daily)
    args = ap.parse_args(argv)
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
