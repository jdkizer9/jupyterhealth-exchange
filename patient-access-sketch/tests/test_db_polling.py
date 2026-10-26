"""The polling loop survives infrastructure failures and heartbeats only on healthy polls."""

from datetime import timedelta

from patient_access.dispatch.db_polling import DbPollingDispatcher, poll_forever
from patient_access.testing.memory import InMemorySpool


class FlakyClaimer:
    """Fails the first `failures` claims the way a database in recovery would, then is empty."""

    def __init__(self, failures):
        self.failures, self.calls = failures, 0

    def claim_next(self, worker_id, lease: timedelta):
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("the database system is in recovery mode")
        return None

    def release(self, job_id, worker_id):
        pass

    def live_spool_refs(self):
        return set()


def test_loop_survives_claim_failures_with_backoff():
    claimer = FlakyClaimer(failures=3)
    slept, beats = [], []
    polls = {"n": 0}

    def should_stop():
        polls["n"] += 1
        return polls["n"] > 6

    poll_forever(
        DbPollingDispatcher(interval_seconds=1),
        claimer,
        run=lambda job_id: None,
        spool=InMemorySpool(),
        should_stop=should_stop,
        heartbeat=lambda: beats.append(1),
        sleep=slept.append,
    )
    assert claimer.calls == 6
    assert slept[:3] == [2, 4, 8]  # backoff while failing
    assert len(beats) == 3  # only the healthy polls heartbeat
    assert slept[3:] == [1, 1, 1]  # back to the normal interval
