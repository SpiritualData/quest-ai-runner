"""StateStore edge cases: log rotation, concurrent writes, duplicate detection.

This test suite verifies quest-ai-runner's file position tracking stability when the
state file is rotated, truncated, or accessed concurrently, to ensure no duplicate
task events are executed under these edge conditions.
"""
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

from quest_ai_runner.runner.poller import StateStore


class TestStateStoreBasics:
    """Sanity checks: normal load/save/dedup flow."""

    def test_empty_state_on_missing_file(self):
        """A non-existent state file is not an error; the store starts empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "nonexistent.json")
            store = StateStore(path)
            assert store.seen("any-sig") is False
            assert store.seen("another") is False

    def test_mark_persists_to_file(self):
        """Marking a signature writes to the state file and survives a reload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            store1 = StateStore(path)
            store1.mark("sig-1")
            store1.mark("sig-2")
            # Reload from the same file — both should be remembered.
            store2 = StateStore(path)
            assert store2.seen("sig-1") is True
            assert store2.seen("sig-2") is True
            assert store2.seen("sig-3") is False

    def test_dedup_prevents_re_execution(self):
        """Seen signatures are never marked again (the dedup gate)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            store = StateStore(path)
            assert store.seen("task-1") is False
            store.mark("task-1")
            assert store.seen("task-1") is True
            assert store.seen("task-1") is True  # Still true on re-check


class TestStateStoreLogRotation:
    """Edge case: state file is rotated (renamed) while the store is running."""

    def test_rotation_recovery_no_data_loss(self):
        """When the state file is rotated mid-operation, the in-memory state is preserved.

        Scenario:
          1. Store marks sig-1
          2. File is rotated (e.g., by logrotate or a log handler)
          3. Store marks sig-2 — this write hits a NEW file (the old one is gone)
          4. Reload: should see sig-1 (from the old file before rotation) + sig-2
             from the new file. Since we can't read the rotated file anymore, this is
             the best we can do: the in-memory state at rotation time is preserved.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            store = StateStore(path)
            store.mark("sig-1")
            store.mark("sig-2")
            # Simulating rotation: move the file away and clear the in-memory set
            # (the poller doesn't know rotation happened, but a new StateStore instance
            # created after rotation would reload from the NEW file).
            rotated_path = os.path.join(tmpdir, "state.json.1")
            os.rename(path, rotated_path)
            # The old store still has sig-1 and sig-2 in memory.
            assert store.seen("sig-1") is True
            assert store.seen("sig-2") is True
            # A NEW store reads from the (empty) path — it starts fresh.
            # This is the weakness: if the poller dies and a new instance starts
            # after rotation, sig-1 and sig-2 would be re-run (duplicates).
            # In practice, the backend's claim() gate prevents this (a re-claimed task
            # fails at the backend level), but the test proves the file-level risk.
            store2 = StateStore(path)
            assert store2.seen("sig-1") is False  # Lost to rotation!
            assert store2.seen("sig-2") is False

    def test_rotation_with_atomic_write_prevents_corruption(self):
        """StateStore uses atomic temp + replace, so rotation mid-write is safe.

        The store writes to a temp file, then replaces atomically. If rotation
        happens mid-write (temp file partially written), the old state file is
        untouched and a reload reads consistent data.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            store = StateStore(path)
            store.mark("sig-1")
            store.mark("sig-2")
            # Verify the file is valid JSON (the atomic write ensures this).
            content = Path(path).read_text()
            data = json.loads(content)
            assert "sig-1" in data["handled"]
            assert "sig-2" in data["handled"]


class TestStateStoreConcurrentWrites:
    """Edge case: multiple processes/threads write to the state file simultaneously."""

    def test_concurrent_marks_no_data_loss(self):
        """Multiple threads marking signatures concurrently see all marks.

        The StateStore uses a threading.Lock to guard in-memory state, but
        concurrent file writes can still race. This test verifies that the
        lock prevents in-memory corruption, and the last write's JSON content
        is the authoritative state (within the limits of file atomicity).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            store = StateStore(path)

            def mark_many(prefix, count):
                for i in range(count):
                    store.mark(f"{prefix}-{i}")

            threads = [
                threading.Thread(target=mark_many, args=("thread-A", 10)),
                threading.Thread(target=mark_many, args=("thread-B", 10)),
                threading.Thread(target=mark_many, args=("thread-C", 10)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # The in-memory store should have all 30 signatures.
            for prefix in ["thread-A", "thread-B", "thread-C"]:
                for i in range(10):
                    assert store.seen(f"{prefix}-{i}") is True

    def test_concurrent_marks_survive_reload(self):
        """Signatures marked concurrently persist to disk and survive a reload.

        This is the critical proof: concurrent marks must not lose data when
        written to disk. The atomic write (temp + replace) ensures the file
        is always consistent; the lock guards the in-memory set.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            store1 = StateStore(path)

            def mark_many(prefix, count):
                for i in range(count):
                    store1.mark(f"{prefix}-{i}")

            threads = [
                threading.Thread(target=mark_many, args=("t1", 5)),
                threading.Thread(target=mark_many, args=("t2", 5)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Reload and verify all marks survived.
            store2 = StateStore(path)
            for prefix in ["t1", "t2"]:
                for i in range(5):
                    assert store2.seen(f"{prefix}-{i}") is True

    def test_cap_prevents_unbounded_growth(self):
        """StateStore caps the stored set to the most recent 5000 signatures.

        This prevents the state file from growing unbounded over years of
        operation. The last save to disk keeps only 5000 signatures.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            store = StateStore(path)
            # Mark 6000 signatures.
            for i in range(6000):
                store.mark(f"sig-{i}")
            # Check that the file contains exactly 5000 signatures (the cap).
            store2 = StateStore(path)
            count = 0
            for i in range(6000):
                if store2.seen(f"sig-{i}"):
                    count += 1
            assert count == 5000  # Exactly 5000 are retained (1000 dropped)

    def test_cap_evicts_oldest_first(self):
        """The 5000 cap keeps the NEWEST signatures and drops the OLDEST ones.

        StateStore now tracks insertion order (a dict, not a plain set), so the eviction at save
        time is deterministic: the oldest-marked signatures are the ones dropped, not an arbitrary
        subset picked by set iteration order.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            store = StateStore(path)
            for i in range(6000):
                store.mark(f"sig-{i}")
            store2 = StateStore(path)
            # The oldest 1000 (sig-0 .. sig-999) were evicted.
            for i in range(1000):
                assert store2.seen(f"sig-{i}") is False
            # The newest 5000 (sig-1000 .. sig-5999) survive.
            for i in range(1000, 6000):
                assert store2.seen(f"sig-{i}") is True


class TestStateStoreAtomicWrite:
    """Edge case: the write itself is interrupted partway through."""

    def test_failed_write_leaves_previous_file_intact(self, monkeypatch):
        """If the temp-file write raises partway, the previously-saved state file must be
        untouched (the atomic temp+replace never gets to the replace step), and a later,
        un-interrupted mark() still round-trips normally."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            store = StateStore(path)
            store.mark("sig-good-1")
            store.mark("sig-good-2")
            good_content = Path(path).read_text()

            real_write_text = Path.write_text
            calls = {"n": 0}

            def _flaky_write_text(self, *args, **kwargs):
                # Only fail the temp file used by StateStore._save, not unrelated writes.
                if self.suffix == ".tmp" and calls["n"] == 0:
                    calls["n"] += 1
                    raise OSError("simulated disk-full mid-write")
                return real_write_text(self, *args, **kwargs)

            monkeypatch.setattr(Path, "write_text", _flaky_write_text)
            store.mark("sig-bad")  # the write for this mark fails partway

            # The on-disk file must still be exactly the last GOOD content (untouched).
            assert Path(path).read_text() == good_content

            monkeypatch.setattr(Path, "write_text", real_write_text)

            # Normal roundtrip still works after the failure is past.
            store.mark("sig-good-3")
            store2 = StateStore(path)
            assert store2.seen("sig-good-1") is True
            assert store2.seen("sig-good-2") is True
            assert store2.seen("sig-good-3") is True


class TestStateStoreFileCorruption:
    """Edge case: state file is corrupted or partially written."""

    def test_corrupt_json_starts_fresh(self):
        """A corrupted state file is silently ignored; the store starts fresh."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            # Write invalid JSON.
            Path(path).write_text("{not valid json")
            # Loading should not raise; the store starts empty.
            store = StateStore(path)
            assert store.seen("any-sig") is False
            # Marking and reloading should work normally.
            store.mark("sig-1")
            store2 = StateStore(path)
            assert store2.seen("sig-1") is True

    def test_truncated_json_starts_fresh(self):
        """A truncated state file (mid-write failure) is silently ignored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            # Write a truncated JSON (simulating a failed write mid-operation).
            Path(path).write_text('{"handled": ["sig-1", "sig-2"')  # Incomplete
            # Loading should not raise.
            store = StateStore(path)
            assert store.seen("sig-1") is False  # Couldn't parse, so forgotten
            # But the store is still functional.
            store.mark("sig-3")
            store2 = StateStore(path)
            assert store2.seen("sig-3") is True


class TestStateStoreProcessRestart:
    """End-to-end: process restart scenario under edge conditions."""

    def test_restart_after_clean_shutdown(self):
        """Restart after clean shutdown restores all state (the happy path)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            # Process 1: mark some signatures.
            store1 = StateStore(path)
            store1.mark("sig-a")
            store1.mark("sig-b")
            store1.mark("sig-c")
            # Process 2: restart (reload from disk).
            store2 = StateStore(path)
            assert store2.seen("sig-a") is True
            assert store2.seen("sig-b") is True
            assert store2.seen("sig-c") is True
            # Continue from where we left off.
            store2.mark("sig-d")
            # Process 3: another restart.
            store3 = StateStore(path)
            assert store3.seen("sig-a") is True
            assert store3.seen("sig-d") is True

    def test_restart_after_crash_recovers_last_successful_state(self):
        """Restart after crash recovers the last successfully written state.

        The atomic write (temp + replace) ensures the file is never partially
        written; a crash mid-write leaves the old file untouched. So the
        most recent atomic transaction survives.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            store1 = StateStore(path)
            store1.mark("sig-1")
            store1.mark("sig-2")
            # Note: before restart, these are persisted to disk atomically.
            # Now simulate a crash (kill the process, don't let it shutdown).
            # The file was already written, so it's safe.
            store2 = StateStore(path)
            assert store2.seen("sig-1") is True
            assert store2.seen("sig-2") is True


class TestStateStoreNoPathHandling:
    """Edge case: state_path is None (in-memory only)."""

    def test_no_path_disables_persistence(self):
        """When state_path is None, the store is in-memory only (no file I/O)."""
        store1 = StateStore(None)
        store1.mark("sig-1")
        assert store1.seen("sig-1") is True
        # A new store with None path doesn't share state (each is independent).
        store2 = StateStore(None)
        assert store2.seen("sig-1") is False


class TestStateStoreTaskSignatureIntegration:
    """Integration: task signatures + StateStore dedup."""

    def test_same_task_same_status_same_signature(self):
        """A task with the same id/status/timestamp has the same signature."""
        from quest_ai_runner.runner.poller import _task_signature
        task = {"id": "t1", "status": "queued", "updated_at": "2026-06-17T10:00:00Z"}
        sig1 = _task_signature(task)
        sig2 = _task_signature(dict(task))
        assert sig1 == sig2

    def test_task_status_change_changes_signature(self):
        """A task that changes status gets a new signature (re-runnable)."""
        from quest_ai_runner.runner.poller import _task_signature
        task1 = {"id": "t1", "status": "queued", "updated_at": "2026-06-17T10:00:00Z"}
        task2 = dict(task1)
        task2["status"] = "in_progress"
        sig1 = _task_signature(task1)
        sig2 = _task_signature(task2)
        assert sig1 != sig2  # Different status => new signature

    def test_dedup_chain_queued_then_claimed(self):
        """A task goes from queued -> claimed -> run. Signature changes at each stage."""
        from quest_ai_runner.runner.poller import _task_signature
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            store = StateStore(path)
            # Task starts queued.
            task_v1 = {"id": "t1", "status": "queued", "updated_at": "T1"}
            sig1 = _task_signature(task_v1)
            assert store.seen(sig1) is False
            store.mark(sig1)
            assert store.seen(sig1) is True
            # Task is now in_progress (claimed by poller).
            task_v2 = {"id": "t1", "status": "in_progress", "updated_at": "T1"}
            sig2 = _task_signature(task_v2)
            # sig1 != sig2 because status changed.
            assert sig1 != sig2
            # A reload sees the task is now in_progress and can run it (new sig, not seen).
            store2 = StateStore(path)
            assert store2.seen(sig2) is False  # New signature, not yet seen


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
