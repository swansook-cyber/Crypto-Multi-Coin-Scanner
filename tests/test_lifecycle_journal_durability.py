from __future__ import annotations

import json
import multiprocessing as mp
import os
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from core.durable_journal import merge_frame, read_frame
from core.lifecycle_review_queue import LifecycleReviewQueue
from core.post_tp1_lifecycle import evaluate_reporting_lifecycle


def signal(symbol="BTCUSDT"):
    return dict(timestamp="2026-10-10T00:00:00Z", symbol=symbol, side="LONG",
                entry=100, stop_loss=90, tp1=110, tp2=120,
                result="OPEN", signal_status="sent")


def writer(path, barrier, mode, number):
    frame = read_frame(path)
    barrier.wait(timeout=20)
    if mode == "add":
        frame = pd.DataFrame([signal(f"COIN{number}USDT")])
    else:
        index = number if mode == "different" else 0
        frame.loc[index, "tp1_alert_sent" if number == 0 else "outcome_alert_sent"] = 1
    merge_frame(path, frame)


@pytest.mark.parametrize("mode", ["add", "different", "same"])
def test_real_multiprocess_writers(tmp_path, mode):
    path = tmp_path / "signals.csv"
    merge_frame(path, pd.DataFrame([signal(), signal("ETHUSDT")]))
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(3)
    processes = [ctx.Process(target=writer, args=(path, barrier, mode, i)) for i in range(2)]
    for process in processes:
        process.start()
    barrier.wait(timeout=20)
    for process in processes:
        process.join(25)
        assert process.exitcode == 0
    rows = read_frame(path)
    assert len(rows) == (4 if mode == "add" else 2)
    if mode == "same":
        assert rows.loc[0, "tp1_alert_sent"] == "1.0"
        assert rows.loc[0, "outcome_alert_sent"] == "1.0"
    elif mode == "different":
        assert rows.loc[0, "tp1_alert_sent"] == "1.0"
        assert rows.loc[1, "outcome_alert_sent"] == "1.0"


def crash_writer(path):
    import core.durable_journal as journal
    journal.os.replace = lambda *_args: os._exit(79)
    merge_frame(path, pd.DataFrame([signal("CRASHUSDT")]))


def test_crash_before_replace_preserves_complete_file_and_releases_lock(tmp_path):
    path = tmp_path / "signals.csv"
    merge_frame(path, pd.DataFrame([signal()]))
    before = path.read_bytes()
    process = mp.get_context("spawn").Process(target=crash_writer, args=(path,))
    process.start()
    process.join(20)
    assert process.exitcode == 79
    assert path.read_bytes() == before
    merge_frame(path, pd.DataFrame([signal("AFTERUSDT")]))
    assert len(read_frame(path)) == 2


def test_stale_rewrite_and_canonical_idempotency(tmp_path):
    path = tmp_path / "signals.csv"
    merge_frame(path, pd.DataFrame([signal()]))
    stale = read_frame(path)
    merge_frame(path, pd.DataFrame([signal("ETHUSDT")]))
    stale.loc[0, "outcome_alert_sent"] = 1
    merge_frame(path, stale)
    equivalent = dict(signal(), timestamp="2026-10-10T00:00:00.100+00:00", entry="100.000000")
    for _ in range(3):
        merge_frame(path, pd.DataFrame([equivalent]))
    saved = read_frame(path)
    assert len(saved) == 2
    assert saved.loc[0, "outcome_alert_sent"] in {"1", "1.0"}


def test_stronger_terminal_and_disjoint_field_updates_survive(tmp_path):
    path = tmp_path / "signals.csv"
    merge_frame(path, pd.DataFrame([signal()]))
    checker = read_frame(path)
    watcher = read_frame(path)
    checker.loc[0, "lifecycle_state"] = "TP2_WIN"
    checker.loc[0, "lifecycle_r"] = 1.5
    checker.loc[0, "lifecycle_terminal"] = 1
    merge_frame(path, checker)
    watcher.loc[0, "tp1_alert_sent"] = 1
    merge_frame(path, watcher)
    merge_frame(path, pd.DataFrame([dict(signal(), lifecycle_state="ORIGINAL_SL", lifecycle_r=-1,
                                       lifecycle_source="LEGACY_FALLBACK")]))
    saved = read_frame(path).iloc[0]
    assert saved.lifecycle_state == "TP2_WIN"
    assert float(saved.lifecycle_r) == 1.5
    assert float(saved.tp1_alert_sent) == 1
    with pytest.raises(ValueError, match="Conflicting terminal"):
        merge_frame(path, pd.DataFrame([dict(signal(), lifecycle_state="ORIGINAL_SL", lifecycle_r=-1)]))


def candle_path(hours, terminal=True):
    times = pd.date_range("2026-10-10", periods=hours*4+1, freq="15min", tz="UTC")
    rows = pd.DataFrame(dict(open_time=times, close_time=times+pd.Timedelta(minutes=15)-pd.Timedelta(milliseconds=1),
                             high=105., low=95., close=100.))
    if terminal:
        rows.loc[0, "high"] = 111.
        rows.loc[len(rows)-1, "high"] = 121.
    return rows


@pytest.mark.parametrize("hours", [12, 30, 60, 96])
def test_incremental_resolution_beyond_age_limit_and_restart(tmp_path, hours):
    path = tmp_path / "signals.csv"
    db = tmp_path / "research.db"
    row = signal()
    all_candles = candle_path(hours)
    calls = []
    def fetch(_session, _symbol, start, end):
        calls.append(start)
        opens = all_candles.open_time.astype("int64")//10**6
        return all_candles[(opens >= start) & (opens <= end)]
    now = pd.Timestamp("2026-10-10T00:00:00Z") + pd.Timedelta(hours=hours/2)
    queue = LifecycleReviewQueue(db, path)
    first = queue.candles(None, row, fetch, now)
    assert not evaluate_reporting_lifecycle(row, first).terminal
    queue = LifecycleReviewQueue(db, path)  # simulate restart
    final = queue.candles(None, row, fetch, all_candles.close_time.max()+pd.Timedelta(seconds=1))
    result = evaluate_reporting_lifecycle(row, final)
    assert result.state == "TP2_WIN" and result.lifecycle_r == 1.5
    assert calls[1] > calls[0]
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT terminal FROM lifecycle_review_progress").fetchone()[0] == 0
    merge_frame(path, pd.DataFrame([dict(row, lifecycle_state=result.state, lifecycle_r=result.lifecycle_r, lifecycle_terminal=1)]))
    queue.acknowledge(dict(row, lifecycle_terminal=1))
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM lifecycle_review_progress WHERE terminal=0").fetchone()[0] == 0


def test_old_unresolved_and_gap_do_not_lose_work(tmp_path):
    queue = LifecycleReviewQueue(tmp_path/"research.db", tmp_path/"signals.csv")
    row = signal()
    data = candle_path(100, terminal=False)
    pending = queue.candles(None, row, lambda *_args: data, data.close_time.max()+pd.Timedelta(seconds=1))
    assert not evaluate_reporting_lifecycle(row, pending).terminal
    with sqlite3.connect(tmp_path/"research.db") as connection:
        assert connection.execute("SELECT terminal FROM lifecycle_review_progress").fetchone()[0] == 0
    other = signal("GAPUSDT")
    with pytest.raises(ValueError, match="Candle gap"):
        queue.candles(None, other, lambda *_args: data.iloc[1:], data.close_time.max()+pd.Timedelta(seconds=1))


def test_queue_batch_bound_and_numeric_serialization_restart(tmp_path):
    data = candle_path(300, terminal=False)
    row = signal()
    calls=[]
    def fetch(_session,_symbol,start,end):
        calls.append((start,end))
        milliseconds=data.open_time.astype("int64")//10**6
        return data[(milliseconds>=start)&(milliseconds<=end)]
    db=tmp_path/"research.db"
    journal=tmp_path/"signals.csv"
    queue=LifecycleReviewQueue(db,journal)
    now=data.close_time.max()+pd.Timedelta(seconds=1)
    first=queue.candles(None,row,fetch,now)
    assert len(first)==1000
    queue=LifecycleReviewQueue(db,journal)
    row={**row,"entry":"100.0","stop_loss":"90.000000","sl":"","timestamp":"2026-10-10T00:00:00+00:00"}
    second=queue.candles(None,row,fetch,now)
    assert len(second)==len(data)
    assert calls[1][0]>calls[0][1]


def restart_queue_worker(db, journal, result_path):
    data = candle_path(96)
    starts = []
    def fetch(_session,_symbol,start,end):
        starts.append(start)
        ms=data.open_time.astype("int64")//10**6
        return data[(ms>=start)&(ms<=end)]
    queue=LifecycleReviewQueue(db,journal)
    rows=queue.candles(None,signal(),fetch,data.close_time.max()+pd.Timedelta(seconds=1))
    result=evaluate_reporting_lifecycle(signal(),rows)
    Path(result_path).write_text(json.dumps({"state":result.state,"starts":starts}))


def test_queue_survives_a_fresh_os_process(tmp_path):
    db,journal=tmp_path/"research.db",tmp_path/"signals.csv"
    data=candle_path(96)
    def fetch(_session,_symbol,start,end):
        ms=data.open_time.astype("int64")//10**6
        return data[(ms>=start)&(ms<=end)]
    queue=LifecycleReviewQueue(db,journal)
    first=queue.candles(None,signal(),fetch,pd.Timestamp("2026-10-12T00:00:00Z"))
    assert not evaluate_reporting_lifecycle(signal(),first).terminal
    output=tmp_path/"restart.json"
    process=mp.get_context("spawn").Process(target=restart_queue_worker,args=(db,journal,output))
    process.start();process.join(20)
    assert process.exitcode==0
    result=json.loads(output.read_text())
    assert result["state"]=="TP2_WIN"
    assert result["starts"][0]==int(pd.Timestamp("2026-10-12T00:00:00Z").timestamp()*1000)


def test_failed_request_does_not_starve_other_pending_identity(tmp_path):
    queue=LifecycleReviewQueue(tmp_path/"research.db",tmp_path/"signals.csv")
    a,b=signal(),signal("ETHUSDT")
    def fail(*_args):
        raise OSError("network unavailable")
    with pytest.raises(OSError):
        queue.candles(None,a,fail)
    assert queue.ordered_indices(pd.DataFrame([a,b]))==[1,0]
    with sqlite3.connect(tmp_path/"research.db") as connection:
        assert connection.execute("SELECT cursor_utc FROM lifecycle_review_progress").fetchone()[0] is None


def test_lock_contention_is_bounded(tmp_path):
    from core.durable_journal import process_lock
    with process_lock(tmp_path/"held.lock"):
        with pytest.raises(TimeoutError):
            with process_lock(tmp_path/"held.lock",timeout=.05):
                pass


def test_prospective_priority_over_historical_and_legacy(tmp_path):
    path=tmp_path/"signals.csv"
    historical=dict(signal(),lifecycle_state="ORIGINAL_SL",lifecycle_r=-1,lifecycle_terminal=1,lifecycle_source="HISTORICAL_LIFECYCLE_RECONSTRUCTION_V1")
    merge_frame(path,pd.DataFrame([historical]))
    prospective=dict(signal(),lifecycle_state="TP2_WIN",lifecycle_r=1.5,lifecycle_terminal=1,lifecycle_source="PROSPECTIVE")
    merge_frame(path,pd.DataFrame([prospective]))
    merge_frame(path,pd.DataFrame([historical]))
    assert read_frame(path).iloc[0].lifecycle_state=="TP2_WIN"
    # Source-only representations must not downgrade retained provenance either.
    source_path=tmp_path/"source_history.csv"
    merge_frame(source_path,pd.DataFrame([dict(signal(),lifecycle_state="TP2_WIN",lifecycle_r=1.5,source="PROSPECTIVE")]))
    merge_frame(source_path,pd.DataFrame([dict(signal(),lifecycle_state="ORIGINAL_SL",lifecycle_r=-1,source="HISTORICAL_RECONSTRUCTION")]))
    assert read_frame(source_path).iloc[0].source=="PROSPECTIVE"


def test_failed_journal_commit_does_not_acknowledge_resolution(tmp_path,monkeypatch):
    import review_signals as review
    path=tmp_path/"signals.csv"
    monkeypatch.setattr(review,"JOURNAL",path)
    def fail(*_args):
        raise OSError("disk full")
    monkeypatch.setattr(review,"merge_frame",fail)
    with pytest.raises(RuntimeError,match="commit failed"):
        review.persist_journal(pd.DataFrame([signal()]))


def test_atomic_replace_failure_and_malformed_evidence(tmp_path, monkeypatch):
    import core.durable_journal as journal
    path = tmp_path/"signals.csv"
    merge_frame(path, pd.DataFrame([signal()]))
    old = path.read_bytes()
    def fail(*_args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(journal.os, "replace", fail)
    with pytest.raises(OSError):
        merge_frame(path, pd.DataFrame([signal("ETHUSDT")]))
    assert path.read_bytes() == old
    path.write_text("timestamp,symbol\n2026-10-10\n")
    with pytest.raises(ValueError, match="Malformed"):
        merge_frame(path, pd.DataFrame([signal()]))


def test_five_captured_cases_through_review_pipeline(tmp_path, monkeypatch):
    import review_signals as review
    from core.research_telemetry import ResearchTelemetryStore

    fixture = json.loads((Path(__file__).parent / "fixtures/lifecycle_durability_production_v1.json").read_text())
    journal, history, db = tmp_path/"signals.csv", tmp_path/"signals_history.csv", tmp_path/"research.db"
    pd.DataFrame(fixture["journal"]).to_csv(journal, index=False)
    pd.DataFrame(fixture["history"]).to_csv(history, index=False)
    store = ResearchTelemetryStore(db)
    with store.connect() as connection:
        connection.execute("INSERT INTO shadow_boundaries VALUES (?,?,?)", ("post_tp1_delayed_quarter_r_v1", fixture["boundary"], fixture["boundary"]))
        for row in fixture["candidates"]:
            connection.execute("INSERT INTO scanner_runs(run_id,scan_started_at_utc,source_mode,source_name) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                               (row["run_id"],row["timestamp_utc"],"PROSPECTIVE","scanner"))
            connection.execute(f"INSERT INTO candidates ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
        # A preexisting shadow record must not be mutated by retrospective work.
        connection.execute("INSERT INTO shadow_decisions VALUES (?,?,?,?,?,?,?)", (fixture["candidates"][0]["candidate_key"],"post_tp1_delayed_quarter_r_v1","sentinel","KEEP","immutable",'{}',fixture["boundary"]))
        connection.execute("INSERT INTO execution_truth_links(canonical_signal_key,execution_status,last_enriched_at_utc) VALUES (?,?,?)",
                           (fixture["candidates"][0]["canonical_signal_key"],"OWNERSHIP_SENTINEL",fixture["boundary"]))
    with store.connect(read_only=True) as connection:
        before = connection.execute("SELECT * FROM shadow_decisions").fetchall()
        boundary = connection.execute("SELECT * FROM shadow_boundaries").fetchall()
        ownership = connection.execute("SELECT * FROM execution_truth_links").fetchall()
    monkeypatch.setattr(review,"JOURNAL",journal)
    monkeypatch.setattr(review,"HISTORY",history)
    monkeypatch.setattr(review,"EXTERNAL_SIGNALS",tmp_path/"absent.csv")
    monkeypatch.setenv("RESEARCH_TELEMETRY_DB",str(db))
    monkeypatch.setattr(review.time,"sleep",lambda *_args: None)
    calls=[]
    def fetch(_session,symbol,start,end):
        calls.append((symbol,start,end))
        raw=[r for r in fixture["klines"][symbol] if start<=r[0]<=end]
        return pd.DataFrame([dict(open_time=pd.to_datetime(r[0],unit="ms",utc=True),close_time=pd.to_datetime(r[6],unit="ms",utc=True),
                                  high=float(r[2]),low=float(r[3]),close=float(r[4])) for r in raw])
    monkeypatch.setattr(review,"fetch_klines",fetch)
    # Restore first without market review to test the crash/failure interval:
    # recovered orphans must never appear as newly actionable live positions.
    restored = review.restore_review_population(review.reload_journal(),db,fixture["boundary"])
    recovered = restored[restored["lifecycle_recovery"].fillna(0).astype(str).str.startswith("1")]
    assert recovered["result"].eq("UNKNOWN").all()
    from position_watcher import open_positions
    assert open_positions(recovered).empty
    review.run_review_cycle(False, object(), 24, print_report=False)
    result=read_frame(journal)
    assert len(result)==25
    assert pd.to_numeric(result.lifecycle_terminal).eq(1).all()
    assert pd.to_numeric(result.lifecycle_r).sum()==pytest.approx(5.799126,abs=1e-6)
    equity=peak=0.;drawdown=0.
    for row in result.sort_values(["remainder_resolution_time_utc","symbol"]).to_dict("records"):
        equity+=float(row["lifecycle_r"]);peak=max(peak,equity);drawdown=min(drawdown,equity-peak)
    assert drawdown==pytest.approx(-4.4,abs=1e-6)
    # The normal checker loop now needs no market request for these terminals.
    before_calls=len(calls)
    review.run_review_cycle(False, object(),24,print_report=False)
    assert len(calls)==before_calls
    with store.connect(read_only=True) as connection:
        assert [tuple(r) for r in connection.execute("SELECT * FROM shadow_decisions")] == [tuple(r) for r in before]
        assert [tuple(r) for r in connection.execute("SELECT * FROM shadow_boundaries")] == [tuple(r) for r in boundary]
        assert connection.execute("SELECT COUNT(*) FROM lifecycle_review_progress WHERE terminal=0").fetchone()[0]==0
        assert [tuple(r) for r in connection.execute("SELECT * FROM execution_truth_links")] == [tuple(r) for r in ownership]


def test_captured_historical_benchmark_and_execution_contract():
    fixture=json.loads((Path(__file__).parent/"fixtures/lifecycle_durability_production_v1.json").read_text())
    history=fixture["historical"]
    assert len(history)==287
    from collections import Counter
    assert Counter(r["lifecycle_state"] for r in history)=={"TP2_WIN":113,"TP1_THEN_ORIGINAL_SL":39,"ORIGINAL_SL":135}
    assert not any(r["ambiguity_flag"] for r in history)
    assert sum(r["lifecycle_r"] for r in history)==pytest.approx(49.6850525483,abs=1e-9)
    equity=peak=0.;dd=0.
    # Historical certified benchmark uses signal order (existing reporting
    # contract), distinct from the requested prospective terminal-order curve.
    for row in sorted(history,key=lambda r:(r["timestamp_utc"],r["canonical_signal_key"])):
        equity+=row["lifecycle_r"];peak=max(peak,equity);dd=min(dd,equity-peak)
    assert dd==pytest.approx(-12.4009009009,abs=1e-9)
    authoritative=[r for r in fixture["execution"] if r["execution_r"]]
    assert len(authoritative)==14
    assert len({r["canonical_signal_key"] for r in authoritative})==14
    assert sum(float(r["execution_pnl_usdt"]) for r in authoritative)==pytest.approx(83.08073946,abs=1e-8)
