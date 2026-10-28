"""A job dispatched inside the callback's transaction reaches the dispatcher only after commit."""

import pytest
from django.db import transaction

from patient_access.django_apps.jobs.dispatch import OnCommitDispatcher


class Recorder:
    def __init__(self):
        self.sent = []

    def dispatch(self, job_id):
        self.sent.append(job_id)


@pytest.mark.django_db(transaction=True)
def test_dispatch_waits_for_commit():
    inner = Recorder()
    with transaction.atomic():
        OnCommitDispatcher(inner).dispatch("j1")
        assert inner.sent == []
    assert inner.sent == ["j1"]


@pytest.mark.django_db(transaction=True)
def test_rolled_back_job_is_never_dispatched():
    inner = Recorder()
    with pytest.raises(RuntimeError), transaction.atomic():
        OnCommitDispatcher(inner).dispatch("j1")
        raise RuntimeError("identity already spent")
    assert inner.sent == []
