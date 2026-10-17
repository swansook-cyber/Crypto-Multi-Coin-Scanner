from datetime import datetime, timezone, timedelta
import sqlite3
import pandas as pd
import pytest

from core.utc_time import (parse_utc_timestamp, timestamp_to_epoch_ms,
                           timestamp_from_epoch_ms, canonical_utc_iso)
from core.lifecycle_review_queue import LifecycleReviewQueue
from core.durable_journal import record_key, merge_frame, read_frame
from tests.test_lifecycle_journal_durability import signal, candle_path
from core.post_tp1_lifecycle import evaluate_reporting_lifecycle


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
@pytest.mark.parametrize("fraction", ["", ".123", ".123456", ".123456789"])
def test_explicit_storage_unit_and_precision(unit, fraction):
    original = parse_utc_timestamp("2026-10-10T00:00:00" + fraction + "Z")
    stamp = original.as_unit(unit)
    serialized = canonical_utc_iso(stamp)
    assert parse_utc_timestamp(serialized) == stamp
    assert timestamp_to_epoch_ms(stamp) == timestamp_to_epoch_ms(serialized)
    assert timestamp_from_epoch_ms(timestamp_to_epoch_ms(stamp)) == stamp.floor("ms")


@pytest.mark.parametrize("value", ["2026-10-10T00:00:00Z", "2026-10-10T07:00:00+07:00",
    "2026-10-10 00:00:00", datetime(2026,10,10), datetime(2026,10,10,tzinfo=timezone.utc),
    datetime(2026,10,10,7,tzinfo=timezone(timedelta(hours=7)))])
def test_equivalent_utc_identity_and_epoch(value):
    assert timestamp_to_epoch_ms(value) == 1791590400000
    assert canonical_utc_iso(value) == "2026-10-10T00:00:00.000000000Z"
    assert record_key(dict(signal(), timestamp=value)) == record_key(signal())


def test_missing_or_implicit_epoch_rejected_and_negative_epoch_floors():
    for value in (None, pd.NaT):
        with pytest.raises(ValueError): parse_utc_timestamp(value)
    with pytest.raises(TypeError): parse_utc_timestamp(1791590400000)
    assert timestamp_to_epoch_ms("1969-12-31T23:59:59.999999999Z") == -1


@pytest.mark.parametrize("hours", [24,48,72])
@pytest.mark.parametrize("offset", [-1,0,1])
def test_age_boundaries_remain_pending_and_fetchable(tmp_path,hours,offset):
    start=parse_utc_timestamp(signal()["timestamp"])
    now=start+pd.Timedelta(hours=hours,seconds=offset)
    assert (now-start > pd.Timedelta(hours=hours)) == (offset>0)
    queue=LifecycleReviewQueue(tmp_path/"r.db",tmp_path/"signals.csv")
    calls=[]
    def fetch(_session,_symbol,begin,end):
        calls.append((begin,end))
        return pd.DataFrame()
    queue.candles(None,signal(),fetch,now)
    assert calls == [(timestamp_to_epoch_ms(start),timestamp_to_epoch_ms(now.floor("15min")-pd.Timedelta(milliseconds=1)))]
    with sqlite3.connect(tmp_path/"r.db") as db:
        assert db.execute("SELECT terminal,cursor_utc FROM lifecycle_review_progress").fetchone()==(0,None)


def test_mixed_precision_queue_roundtrip_and_order(tmp_path):
    queue=LifecycleReviewQueue(tmp_path/"r.db",tmp_path/"signals.csv")
    data=candle_path(1)
    data["open_time"]=data.open_time.map(canonical_utc_iso).astype(object)
    data["close_time"]=data.close_time.map(canonical_utc_iso).astype(object)
    data.loc[0,"open_time"]="2026-10-10T07:00:00+07:00"
    data.loc[1,"open_time"]="2026-10-10T00:15:00.000Z"
    now="2026-10-10T02:00:00Z"
    first=queue.candles(None,signal(),lambda *_: data,now)
    second=LifecycleReviewQueue(tmp_path/"r.db",tmp_path/"signals.csv").candles(None,signal(),lambda *_: pd.DataFrame(),now)
    assert first.equals(second)
    assert evaluate_reporting_lifecycle(signal(),first).state=="TP2_WIN"
    other=signal("ETHUSDT")
    queue.candles(None,other,lambda *_: pd.DataFrame(),now)
    with sqlite3.connect(tmp_path/"r.db") as db:
        db.execute("UPDATE lifecycle_review_progress SET updated_at_utc=? WHERE canonical_signal_key=?",("2026-10-10T07:00:00.000000001+07:00",record_key(signal())))
        db.execute("UPDATE lifecycle_review_progress SET updated_at_utc=? WHERE canonical_signal_key=?",("2026-10-10T00:00:00.000001Z",record_key(other)))
    assert queue.ordered_indices(pd.DataFrame([other,signal()]))==[1,0]


def test_unit_equivalent_merge_retains_stronger_evidence(tmp_path):
    path=tmp_path/"signals.csv"
    for unit in ("s","ms","us","ns"):
        row=dict(signal(),timestamp=parse_utc_timestamp(signal()["timestamp"]).as_unit(unit),
                 lifecycle_state="TP2_WIN",lifecycle_r=1.5,lifecycle_terminal=1,lifecycle_source="PROSPECTIVE")
        merge_frame(path,pd.DataFrame([row]))
        merge_frame(path,pd.DataFrame([dict(signal(),lifecycle_state="ORIGINAL_SL",lifecycle_r=-1,
                                          lifecycle_terminal=1,lifecycle_source="LEGACY_FALLBACK")]))
    saved=read_frame(path)
    assert len(saved)==1
    assert saved.iloc[0].lifecycle_state=="TP2_WIN"


def test_nanosecond_journal_serialization_and_unchecked_queue_order(tmp_path):
    path=tmp_path/"signals.csv"
    stamp="2026-10-10T00:00:00.123456789Z"
    merge_frame(path,pd.DataFrame([dict(signal(),timestamp=stamp)]))
    assert parse_utc_timestamp(read_frame(path).iloc[0].timestamp)==parse_utc_timestamp(stamp)
    queue=LifecycleReviewQueue(tmp_path/"r.db",path)
    later=dict(signal("ETHUSDT"),timestamp="2026-10-10T00:00:00.000000001Z")
    earlier=dict(signal(),timestamp="2026-10-10T07:00:00+07:00")
    assert queue.ordered_indices(pd.DataFrame([later,earlier]))==[1,0]


def test_terminal_timestamp_equivalence_is_not_a_merge_conflict(tmp_path):
    path=tmp_path/"signals.csv"
    row=dict(signal(),lifecycle_state="TP2_WIN",lifecycle_r=1.5,lifecycle_terminal=1,
             remainder_resolution_time_utc="2026-10-10T01:00:00.123456789Z")
    merge_frame(path,pd.DataFrame([row]))
    equivalent=dict(row,remainder_resolution_time_utc="2026-10-10T08:00:00.123456789+07:00")
    merge_frame(path,pd.DataFrame([equivalent]))
    assert read_frame(path).iloc[0].remainder_resolution_time_utc==row["remainder_resolution_time_utc"]
    with pytest.raises(ValueError,match="Conflicting terminal"):
        merge_frame(path,pd.DataFrame([dict(row,remainder_resolution_time_utc="2026-10-10T01:00:00.123456790Z")]))
