"""
tests/test_report_corrupt_history.py — `--report` on a history it cannot read.

The report functions in history.py re-raise a read error on purpose: "no data"
is a claim that nothing was measured, and a database that cannot be opened has
made no such claim. Nothing above them caught it, so `python main.py --report`
on a file that is not a SQLite database (a bad restore, a truncated copy)
ended in a raw traceback. The operator gets one line naming the file and the
reason, a non-zero exit, and no table that could be mistaken for data.
"""

from __future__ import annotations

import main


def _one_target(*_args, **_kwargs):
    return [main.Target(url="https://a.example")]


def test_report_on_a_corrupt_history_is_one_line_and_exit_1(tmp_path, capsys, monkeypatch):
    db = tmp_path / "history.db"
    db.write_bytes(b"this is not a sqlite database" * 100)
    monkeypatch.setattr(main, "load_targets", _one_target)

    code = main._report_main(history_path=db)

    out = capsys.readouterr()
    assert code == 1
    assert "cannot be read" in out.err
    assert str(db) in out.err
    assert "Traceback" not in out.err
    assert out.out == "", "a table printed over an unreadable database reads as data"


def test_report_on_a_missing_history_still_prints_no_data_and_exits_0(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(main, "load_targets", _one_target)

    code = main._report_main(history_path=tmp_path / "absent.db")

    out = capsys.readouterr()
    assert code == 0
    assert "no data" in out.out
