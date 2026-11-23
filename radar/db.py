from __future__ import annotations

import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices(
  date TEXT, code TEXT, name TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, value REAL,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS inst(date TEXT, code TEXT, net REAL, PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS trust(date TEXT, code TEXT, net REAL, PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS disposal(code TEXT, start TEXT, end TEXT);
CREATE TABLE IF NOT EXISTS no_data(date TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS signals(
  date TEXT, code TEXT, source TEXT, strategy TEXT, order_type TEXT,
  entry REAL, stop REAL, t1 REAL, t2 REAL, score REAL, payload TEXT,
  PRIMARY KEY(date, code, source));
"""


def connect(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def upsert_prices(conn, date, rows):
    conn.executemany(
        "INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?,?,?,?)",
        [(date, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows],
    )
    conn.commit()


def upsert_inst(conn, date, mapping):
    conn.executemany("INSERT OR REPLACE INTO inst VALUES(?,?,?)", [(date, c, v) for c, v in mapping.items()])
    conn.commit()


def upsert_trust(conn, date, mapping):
    conn.executemany("INSERT OR REPLACE INTO trust VALUES(?,?,?)", [(date, c, v) for c, v in mapping.items()])
    conn.commit()


def has_trust(conn, date):
    return conn.execute("SELECT 1 FROM trust WHERE date=? LIMIT 1", (date,)).fetchone() is not None


def load_trust(conn, since=None):
    q = "SELECT date,code,net FROM trust"
    args = ()
    if since:
        q += " WHERE date>=?"
        args = (since,)
    out = {}
    for d, code, net in conn.execute(q, args):
        out.setdefault(d, {})[code] = net
    return out


def replace_disposal(conn, rows):
    conn.execute("DELETE FROM disposal")
    conn.executemany("INSERT INTO disposal VALUES(?,?,?)", rows)
    conn.commit()


def load_disposal(conn):
    return [tuple(r) for r in conn.execute("SELECT code,start,end FROM disposal")]


def last_price_date(conn):
    r = conn.execute("SELECT MAX(date) FROM prices").fetchone()
    return r[0] if r else None


def has_prices(conn, date):
    return conn.execute("SELECT 1 FROM prices WHERE date=? LIMIT 1", (date,)).fetchone() is not None


def has_inst(conn, date):
    return conn.execute("SELECT 1 FROM inst WHERE date=? LIMIT 1", (date,)).fetchone() is not None


def mark_no_data(conn, date):
    conn.execute("INSERT OR IGNORE INTO no_data VALUES(?)", (date,))
    conn.commit()


def is_no_data(conn, date):
    return conn.execute("SELECT 1 FROM no_data WHERE date=?", (date,)).fetchone() is not None


def price_dates(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT date FROM prices ORDER BY date")]


def save_signals(conn, source, picks, asof):
    conn.execute("DELETE FROM signals WHERE date=? AND source=?", (asof, source))
    conn.executemany(
        "INSERT OR REPLACE INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            (asof, p["code"], source, p["strategy"], p["order_type"], p["entry"], p["stop"], p["t1"], p["t2"],
             p["score"], json.dumps({"name": p["name"]}, ensure_ascii=False))
            for p in picks
        ],
    )
    conn.commit()


def clear_signals(conn, source):
    conn.execute("DELETE FROM signals WHERE source=?", (source,))
    conn.commit()


def load_signals(conn, source):
    cur = conn.execute(
        "SELECT date,code,strategy,order_type,entry,stop,t1,t2,score,payload FROM signals WHERE source=? ORDER BY date,code",
        (source,),
    )
    out = []
    for r in cur:
        name = ""
        try:
            name = json.loads(r[9]).get("name", "")
        except Exception:
            pass
        out.append({"date": r[0], "code": r[1], "strategy": r[2], "order": r[3], "entry": r[4], "stop": r[5],
                    "t1": r[6], "t2": r[7], "score": r[8], "name": name})
    return out


def export_signals_json(conn, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(load_signals(conn, "live"), f, ensure_ascii=False)


def import_signals_json(conn, path):
    """資料庫被清空(例如快取失效)時,從已提交的 signals.json 還原實盤紀錄。"""
    if not os.path.exists(path):
        return 0
    if conn.execute("SELECT COUNT(*) FROM signals WHERE source='live'").fetchone()[0] > 0:
        return 0
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    conn.executemany(
        "INSERT OR REPLACE INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(r["date"], r["code"], "live", r["strategy"], r["order"], r["entry"], r["stop"], r["t1"], r["t2"],
          r.get("score", 0), json.dumps({"name": r.get("name", "")}, ensure_ascii=False)) for r in rows],
    )
    conn.commit()
    return len(rows)
