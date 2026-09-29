"""SQLite trade journal + lessons. Every trade keeps full details; the analyst
reports and the decision that led to it are stored so mistakes can be traced."""
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL DEFAULT 'OPEN',          -- OPEN | CLOSED
  mode TEXT NOT NULL,                           -- paper | live
  symbol TEXT NOT NULL, security_id TEXT NOT NULL,
  side TEXT NOT NULL DEFAULT 'BUY',
  setup TEXT, entry_reason TEXT,
  entry_time TEXT, entry_price REAL, quantity INTEGER,
  stop_loss REAL, target REAL, initial_stop_loss REAL,
  confidence REAL, risk_amount REAL,
  exit_time TEXT, exit_price REAL, exit_reason TEXT,
  pnl REAL, pnl_pct REAL, r_multiple REAL,
  sl_order_id TEXT,
  analyst_reports TEXT, decision TEXT,
  review TEXT                                   -- post-trade reflection (JSON)
);
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_id INTEGER, created TEXT,
  mistake_type TEXT, what_went_wrong TEXT, rule TEXT,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS analyst_scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, analyst TEXT, stance TEXT,
  conviction INTEGER, correct INTEGER          -- correct: 1/0, NULL when stance was NEUTRAL
);
CREATE TABLE IF NOT EXISTS strategy_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, update_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, detail TEXT
);
"""

EXPORT_COLS = ["id", "mode", "symbol", "setup", "entry_time", "entry_price", "quantity", "stop_loss",
               "initial_stop_loss", "target", "entry_reason", "exit_time", "exit_price", "exit_reason",
               "pnl", "pnl_pct", "r_multiple", "confidence"]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- trades -----------------------------------------------------------
    def open_trade(self, *, mode, symbol, security_id, setup, entry_reason, entry_price, quantity,
                   stop_loss, target, confidence, analyst_reports, decision, sl_order_id=None) -> int:
        risk = (entry_price - stop_loss) * quantity
        cur = self.db.execute(
            """INSERT INTO trades (mode,symbol,security_id,setup,entry_reason,entry_time,entry_price,quantity,
               stop_loss,initial_stop_loss,target,confidence,risk_amount,sl_order_id,analyst_reports,decision)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mode, symbol, security_id, setup, entry_reason, now(), entry_price, quantity, stop_loss,
             stop_loss, target, confidence, risk, sl_order_id, json.dumps(analyst_reports), json.dumps(decision)))
        self.db.commit()
        return cur.lastrowid

    def close_trade(self, trade_id: int, exit_price: float, exit_reason: str):
        t = self.get(trade_id)
        pnl = (exit_price - t["entry_price"]) * t["quantity"]
        risk = t["risk_amount"] or 0
        self.db.execute(
            "UPDATE trades SET status='CLOSED', exit_time=?, exit_price=?, exit_reason=?, pnl=?, pnl_pct=?, r_multiple=? WHERE id=?",
            (now(), exit_price, exit_reason, round(pnl, 2), round(100 * (exit_price / t["entry_price"] - 1), 2),
             round(pnl / risk, 2) if risk else None, trade_id))
        self.db.commit()

    def update_levels(self, trade_id: int, stop_loss=None, target=None, sl_order_id=None):
        if stop_loss is not None:
            self.db.execute("UPDATE trades SET stop_loss=? WHERE id=?", (stop_loss, trade_id))
        if target is not None:
            self.db.execute("UPDATE trades SET target=? WHERE id=?", (target, trade_id))
        if sl_order_id is not None:
            self.db.execute("UPDATE trades SET sl_order_id=? WHERE id=?", (sl_order_id, trade_id))
        self.db.commit()

    def set_review(self, trade_id: int, review: dict):
        self.db.execute("UPDATE trades SET review=? WHERE id=?", (json.dumps(review), trade_id))
        self.db.commit()

    def get(self, trade_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()

    def open_trades(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY id").fetchall()

    def closed_trades(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE status='CLOSED' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def realized_pnl(self) -> float:
        return self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='CLOSED'").fetchone()[0]

    def deployed_capital(self) -> float:
        return self.db.execute("SELECT COALESCE(SUM(entry_price*quantity),0) FROM trades WHERE status='OPEN'").fetchone()[0]

    def stats_by_setup(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT COALESCE(setup,'?') setup, COUNT(*) n, SUM(pnl>0) wins, ROUND(SUM(pnl),0) pnl,
               ROUND(AVG(r_multiple),2) avg_r FROM trades WHERE status='CLOSED' GROUP BY setup ORDER BY n DESC""").fetchall()
        return [dict(r) for r in rows]

    # ---- lessons ----------------------------------------------------------
    def add_lesson(self, trade_id, mistake_type, what_went_wrong, rule):
        self.db.execute("INSERT INTO lessons (trade_id,created,mistake_type,what_went_wrong,rule) VALUES (?,?,?,?,?)",
                        (trade_id, now(), mistake_type, what_went_wrong, rule))
        self.db.commit()

    def active_lessons(self, limit: int = 30) -> list[dict]:
        rows = self.db.execute("SELECT * FROM lessons WHERE active=1 ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def retire_lessons(self, ids: list[int]):
        self.db.executemany("UPDATE lessons SET active=0 WHERE id=?", [(i,) for i in ids])
        self.db.commit()

    def closed_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED'").fetchone()[0]

    # ---- performance / scoring ---------------------------------------------
    def realized_between(self, month: str) -> float:
        """Realized P&L of trades closed in month 'YYYY-MM'."""
        return self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='CLOSED' AND substr(exit_time,1,7)=?",
                               (month,)).fetchone()[0]

    def realized_before(self, month: str) -> float:
        return self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='CLOSED' AND substr(exit_time,1,7)<?",
                               (month,)).fetchone()[0]

    def all_closed(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM trades WHERE status='CLOSED' ORDER BY id").fetchall()]

    def record_analyst_scores(self, trade_id: int, rows: list[tuple]):
        self.db.executemany("INSERT INTO analyst_scores (trade_id,analyst,stance,conviction,correct) VALUES (?,?,?,?,?)",
                            [(trade_id, *r) for r in rows])
        self.db.commit()

    def analyst_scorecard(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT analyst, COUNT(correct) calls, SUM(correct) hits,
               ROUND(1.0*SUM(correct)/NULLIF(COUNT(correct),0),2) hit_rate,
               ROUND(AVG(CASE WHEN correct=0 THEN conviction END),0) avg_conviction_when_wrong
               FROM analyst_scores GROUP BY analyst""").fetchall()
        return [dict(r) for r in rows]

    def log_strategy(self, update: dict):
        self.db.execute("INSERT INTO strategy_log (ts,update_json) VALUES (?,?)", (now(), json.dumps(update)))
        self.db.commit()

    # ---- misc -------------------------------------------------------------
    def event(self, kind: str, detail: str):
        self.db.execute("INSERT INTO events (ts,kind,detail) VALUES (?,?,?)", (now(), kind, detail))
        self.db.commit()

    def export_csv(self, path) -> Path:
        path = Path(path)
        rows = self.db.execute("SELECT * FROM trades ORDER BY id").fetchall()
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(EXPORT_COLS)
            for r in rows:
                w.writerow([r[c] for c in EXPORT_COLS])
        return path
