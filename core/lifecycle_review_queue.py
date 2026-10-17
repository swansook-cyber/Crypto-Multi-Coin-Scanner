"""Bounded incremental market evidence in the existing research SQLite store.

The journal remains lifecycle authority. Terminal checkpoints are written only
after journal persistence; a crash between the two commits safely replays.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path

import pandas as pd

from core.durable_journal import record_key
from core.research_telemetry import ResearchTelemetryStore, utc_now
from core.signal_identity import normalize_float, normalize_timestamp

INTERVAL = pd.Timedelta(minutes=15)
MAX_CANDLES_PER_REVIEW = 1000
MAX_REVIEWS_PER_CYCLE = 100


class LifecycleReviewQueue:
    def __init__(self, db_path, journal_path):
        self.store = ResearchTelemetryStore(db_path)
        self.scope = str(Path(journal_path).resolve())

    @staticmethod
    def geometry(row):
        stop = row.get("stop_loss")
        if pd.isna(stop) or not str(stop).strip():
            stop = row.get("sl")
        values = {k: normalize_float(row.get(k), decimals=12) for k in ("entry", "tp1", "tp2")}
        values.update(stop=normalize_float(stop, decimals=12), timestamp=normalize_timestamp(row.get("timestamp")))
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def candles(self, session, row, fetch, now=None):
        key, geometry = record_key(row), self.geometry(row)
        with closing(self.store.connect(read_only=True)) as connection:
            prior = connection.execute("SELECT * FROM lifecycle_review_progress WHERE journal_scope=? AND canonical_signal_key=?",
                                       (self.scope, key)).fetchone()
        if prior and prior["geometry_hash"] != geometry:
            raise ValueError("Lifecycle geometry changed; cursor cannot be reused")
        cached = json.loads(prior["candles_json"]) if prior else []
        # Persist the attempt without advancing its cursor. Failed requests also
        # move to the back of the fair queue rather than starving other trades.
        with closing(self.store.connect()) as connection, connection:
            connection.execute("""INSERT INTO lifecycle_review_progress VALUES (?,?,?,?,?,0,?)
                ON CONFLICT(journal_scope,canonical_signal_key) DO UPDATE SET updated_at_utc=excluded.updated_at_utc""",
                (self.scope,key,geometry,json.dumps(cached),prior["cursor_utc"] if prior else None,utc_now()))
        start = pd.to_datetime(row["timestamp"], utc=True).ceil("15min")
        if cached:
            start = pd.to_datetime(cached[-1]["open_time"], utc=True) + INTERVAL
        now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
        cutoff = now.floor("15min") - pd.Timedelta(milliseconds=1)
        if start <= cutoff and not (prior and prior["terminal"]):
            end = min(cutoff, start + MAX_CANDLES_PER_REVIEW * INTERVAL - pd.Timedelta(milliseconds=1))
            new = fetch(session, row["symbol"], int(start.timestamp()*1000), int(end.timestamp()*1000))
            if not new.empty:
                new = new.copy()
                new["close_time"] = pd.to_datetime(new["close_time"], utc=True)
                new["open_time"] = pd.to_datetime(new["open_time"], utc=True)
                new = new[(new["open_time"] >= start) & (new["close_time"] <= end)].sort_values("open_time")
                if len(new) > MAX_CANDLES_PER_REVIEW or new["open_time"].duplicated().any():
                    raise ValueError("Unexpected candle count or duplicate candle evidence")
                expected = start
                for candle in new.to_dict("records"):
                    if candle["open_time"] != expected:
                        raise ValueError("Candle gap: retained pending for retry; no cursor advance")
                    expected += INTERVAL
                    cached.append({k: candle[k].isoformat() if isinstance(candle[k], pd.Timestamp) else candle[k]
                                   for k in ("open_time", "close_time", "high", "low", "close")})
        cursor = cached[-1]["close_time"] if cached else None
        with closing(self.store.connect()) as connection, connection:
            connection.execute("""INSERT INTO lifecycle_review_progress VALUES (?,?,?,?,?,0,?)
                ON CONFLICT(journal_scope,canonical_signal_key) DO UPDATE SET
                candles_json=excluded.candles_json,cursor_utc=excluded.cursor_utc,
                updated_at_utc=excluded.updated_at_utc""",
                (self.scope, key, geometry, json.dumps(cached), cursor, utc_now()))
        return pd.DataFrame(cached, columns=["open_time", "close_time", "high", "low", "close"])

    def acknowledge(self, row):
        """Call after successful journal commit, never after evaluation alone."""
        terminal = str(row.get("lifecycle_terminal", "")) in {"1", "1.0", "True"}
        with closing(self.store.connect()) as connection, connection:
            connection.execute("UPDATE lifecycle_review_progress SET terminal=?,updated_at_utc=? WHERE journal_scope=? AND canonical_signal_key=?",
                               (int(terminal), utc_now(), self.scope, record_key(row)))

    def ordered_indices(self, frame):
        """Unchecked/least-recently-checked first, so bounded batches are fair."""
        with closing(self.store.connect(read_only=True)) as connection:
            checked = dict(connection.execute("SELECT canonical_signal_key,updated_at_utc FROM lifecycle_review_progress WHERE journal_scope=?", (self.scope,)))
        return sorted(frame.index, key=lambda index: (checked.get(record_key(frame.loc[index].to_dict()), ""),
                                                      str(frame.loc[index].get("timestamp", "")), int(index)))
