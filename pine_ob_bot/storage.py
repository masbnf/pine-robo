from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Trade


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS trades (
          id TEXT PRIMARY KEY, payload TEXT NOT NULL, closed_time TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, kind TEXT NOT NULL,
          payload TEXT NOT NULL);
        """)

    def load(self) -> dict | None:
        row = self.conn.execute("SELECT value FROM state WHERE key='snapshot'").fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        trades = self.conn.execute("SELECT payload FROM trades ORDER BY closed_time,id").fetchall()
        if trades:
            payload["broker"]["trades"] = [json.loads(item[0]) for item in trades]
        return payload

    def save(self, engine: dict, broker: dict, context: dict | None = None) -> None:
        payload = json.dumps({"engine": engine, "broker": broker, "context": context}, separators=(",", ":"))
        with self.conn:
            self.conn.execute("INSERT INTO state(key,value) VALUES('snapshot',?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (payload,))

    def record_event(self, time: str, kind: str, payload: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO events(time,kind,payload) VALUES(?,?,?)",
                              (time, kind, json.dumps(payload, separators=(",", ":"))))

    def record_trade(self, trade: Trade) -> None:
        from dataclasses import asdict
        payload = json.dumps(asdict(trade), separators=(",", ":"))
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO trades(id,payload,closed_time) VALUES(?,?,?)",
                              (trade.id, payload, trade.closed_time))

    def event_counts(self, start_time: str, end_time: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) FROM events WHERE time>=? AND time<? GROUP BY kind",
            (start_time, end_time)).fetchall()
        return {str(kind): int(count) for kind, count in rows}

    def close(self) -> None:
        self.conn.close()
