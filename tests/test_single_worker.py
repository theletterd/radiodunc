"""Tests for app/single_worker.py — the single-worker pidfile guard."""

import logging
import os

from app.single_worker import ensure_single_worker


def test_missing_pidfile_claims_it(tmp_path):
    pidfile = tmp_path / "radiodunc.pid"

    assert ensure_single_worker(pidfile) is True
    assert pidfile.read_text() == str(os.getpid())


def test_stale_pidfile_is_overwritten(tmp_path):
    """A pidfile left behind by a dead process (kill -9, crash) must not
    block the next boot — the guard checks liveness, not mere existence."""
    pidfile = tmp_path / "radiodunc.pid"
    # Max PID on macOS is 99998 and on Linux defaults to 2^22; this can't
    # be a live process on either.
    pidfile.write_text("99999999")

    assert ensure_single_worker(pidfile) is True
    assert pidfile.read_text() == str(os.getpid())


def test_own_pid_in_file_is_fine(tmp_path):
    """Re-running the guard in the same process (module re-import under
    --reload) is a no-op, not a false alarm."""
    pidfile = tmp_path / "radiodunc.pid"
    pidfile.write_text(str(os.getpid()))

    assert ensure_single_worker(pidfile) is True
    assert pidfile.read_text() == str(os.getpid())


def test_live_other_process_logs_critical_and_leaves_file(tmp_path, caplog):
    """PID 1 (launchd/init) is always alive and never us — the guard must
    refuse the claim, log CRITICAL naming the consequences, and leave the
    original owner's pidfile untouched (a pytest run importing app.main
    while the dev server is up must not steal the server's claim)."""
    pidfile = tmp_path / "radiodunc.pid"
    pidfile.write_text("1")

    with caplog.at_level(logging.CRITICAL, logger="app.single_worker"):
        result = ensure_single_worker(pidfile)

    assert result is False
    assert pidfile.read_text() == "1"
    assert any(
        "Another RadioDunc process" in rec.message and "process-local" in rec.message
        for rec in caplog.records
    )


def test_garbage_pidfile_content_is_overwritten(tmp_path):
    """Corrupt content can't be a live claim; treat it like a stale file."""
    pidfile = tmp_path / "radiodunc.pid"
    pidfile.write_text("not-a-pid")

    assert ensure_single_worker(pidfile) is True
    assert pidfile.read_text() == str(os.getpid())


def test_unwritable_pidfile_does_not_raise(tmp_path):
    """The guard must never take the app down with it — an OSError on the
    pidfile path degrades to 'assume sole worker' rather than crashing."""
    pidfile = tmp_path / "no-such-dir-allowed" / "radiodunc.pid"
    # Make the parent un-creatable by shadowing it with a file.
    (tmp_path / "no-such-dir-allowed").write_text("a file, not a dir")

    assert ensure_single_worker(pidfile) is True
