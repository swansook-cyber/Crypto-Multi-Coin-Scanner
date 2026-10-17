import pandas as pd
import pytest
import position_watcher_state_cleanup as cleanup
import system_status

HEADER = "timestamp,symbol,side,entry,tp1,result\n"


@pytest.mark.parametrize("content,expected", [(None,"MISSING"),("","EMPTY"),("\n","EMPTY"),
    (HEADER,"EMPTY"),(HEADER+"2026-10-10T00:00:00Z,BTCUSDT,LONG,100,110,OPEN\n","HEALTHY")])
def test_missing_empty_and_populated_are_read_only(tmp_path, content, expected):
    path=tmp_path/"signals.csv"
    if content is not None: path.write_text(content,encoding="utf-8")
    before=path.read_bytes() if path.exists() else None
    frame=cleanup.load_journal(path)
    assert frame.attrs["journal_input_status"]==expected
    assert {"timestamp","symbol"}.issubset(frame.columns)
    assert frame.empty==(expected!="HEALTHY")
    classification=cleanup.classify_cleanup(path)
    assert classification.journal_input_status==expected
    assert classification.safe_to_remove==0
    assert (path.read_bytes() if path.exists() else None)==before


@pytest.mark.parametrize("content", [HEADER+'"unterminated', HEADER+"2026-10-10,BTCUSDT\n",
    HEADER+"2026-10-10,BTCUSDT,LONG,100,110,OPEN,extra\n", "garbage\n", "timestamp,symbol,symbol\n"])
def test_corrupt_csv_is_invalid_not_empty(tmp_path,monkeypatch,content):
    path=tmp_path/"signals.csv";path.write_text(content)
    with pytest.raises(cleanup.JournalInputError):cleanup.load_journal(path)
    monkeypatch.setattr(system_status,"JOURNAL",path)
    monkeypatch.setattr(system_status,"audit_status",lambda:"PASS")
    status=system_status.safety_status()
    assert status["watcher_journal_status"]=="INVALID"
    assert status["data_integrity"]=="FAIL"
    assert status["active_stale_watcher_state"] is None
    assert system_status.final_status([status["watcher_journal_check"]])==("NOT READY",2)


@pytest.mark.parametrize("content,state,check", [(None,"MISSING","WARNING"),("","EMPTY","WARNING"),
    (HEADER,"EMPTY","WARNING"),(HEADER+"2026-10-10T00:00:00Z,BTCUSDT,LONG,100,110,OPEN\n","HEALTHY","PASS")])
def test_system_status_distinguishes_input_states(tmp_path,monkeypatch,content,state,check):
    path=tmp_path/"signals.csv"
    if content is not None:path.write_text(content)
    monkeypatch.setattr(system_status,"JOURNAL",path)
    monkeypatch.setattr(system_status,"audit_status",lambda:"PASS")
    status=system_status.safety_status()
    assert status["watcher_journal_status"]==state
    assert status["watcher_journal_check"]==check
    assert system_status.final_status([check])[0]==("READY FOR PRODUCTION" if check=="PASS" else "READY WITH WARNINGS")


def test_linux_missing_input_fixture_is_zero_bytes(tmp_path,monkeypatch):
    # Same branch as smoke when ignored logs/signals.csv is absent in git archive.
    path=tmp_path/"signals.csv";path.touch()
    monkeypatch.setattr(system_status,"JOURNAL",path)
    status=system_status.safety_status()
    assert status["watcher_journal_status"]=="EMPTY"
    assert status["watcher_journal_check"]=="WARNING"
    assert path.stat().st_size==0
