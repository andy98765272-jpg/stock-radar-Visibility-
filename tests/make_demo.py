"""用合成資料產生網站預覽用的 picks.json / stats.json(不會碰 docs/data,也不會寫 signals.json)。

用法:python -m tests.make_demo <輸出資料夾>
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

from radar import db
from radar.__main__ import backtest, compute_stats, load_cfg, run_screen
from tests import synth


def main(out_dir):
    cfg = load_cfg()
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "demo.db"))
    synth.build(conn)
    res = run_screen(conn, cfg)
    res["meta"]["synthetic"] = True
    backtest(conn, cfg, 60, log=lambda *_: None)
    stats = compute_stats(conn, cfg)
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    with open(os.path.join(out_dir, "data", "picks.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(out_dir, "data", "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, separators=(",", ":"))
    print("入選", len(res["picks"]), "檔;回測訊號", stats["backtest"]["signals"], "筆;已結算", stats["backtest"]["all"]["n"], "筆")
    print(json.dumps(stats["backtest"]["all"], ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "preview")
