"""ThreadDispatcher runs the job through the same claim/run/release as the other runners."""

import threading

from patient_access.dispatch.thread import ThreadDispatcher


class Claimer:
    def __init__(self, allow=True):
        self.allow, self.claimed, self.released = allow, [], []

    def claim(self, job_id, worker_id, lease):
        self.claimed.append((job_id, worker_id))
        return self.allow

    def release(self, job_id, worker_id):
        self.released.append((job_id, worker_id))


def _run_sync(**kw):
    """Run the thread body inline so the test is deterministic."""

    class Inline(threading.Thread):
        def start(self):
            self.run()

    return ThreadDispatcher(thread_factory=Inline, **kw)


def test_claims_runs_releases_and_cleans_up():
    ran, exits = [], []
    claimer = Claimer()
    d = _run_sync(run=ran.append, claimer=claimer, on_thread_exit=lambda: exits.append(1))
    d.dispatch("job-1")
    assert ran == ["job-1"] and len(claimer.released) == 1 and exits == [1]
    assert claimer.claimed[0][1].startswith("thread:")


def test_skips_when_already_leased_and_survives_run_errors():
    claimer = Claimer(allow=False)
    ran = []
    _run_sync(run=ran.append, claimer=claimer).dispatch("job-2")
    assert ran == [] and claimer.released == []

    def boom(job_id):
        raise RuntimeError("fetch exploded")

    claimer = Claimer()
    _run_sync(run=boom, claimer=claimer).dispatch("job-3")  # must not raise
    assert claimer.released == [("job-3", claimer.claimed[0][1])]


def test_release_all_frees_leases_held_by_running_threads():
    import threading as th

    claimer = Claimer()
    started, finish = th.Event(), th.Event()

    def slow(job_id):
        started.set()
        finish.wait(5)

    d = ThreadDispatcher(run=slow, claimer=claimer)
    d.dispatch("job-9")
    assert started.wait(5)
    d.release_all()  # what atexit does on a deploy
    assert claimer.released == [("job-9", claimer.claimed[0][1])]
    finish.set()
