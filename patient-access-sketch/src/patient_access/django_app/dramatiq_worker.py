"""Worker-side entry for DramatiqDispatcher: `dramatiq patient_access.django_app.dramatiq_worker`.

Sets up Django, builds the module, and hands its `run` to the actor. Each job is claimed through
the same lease the polling worker uses, so a duplicate message (or a polling worker running beside
this one) can't run a job twice at once.
"""

import os
from datetime import timedelta

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "server.settings")
django.setup()

from patient_access.dispatch import dramatiq_dispatcher  # noqa: E402
from patient_access.dispatch.db_polling import worker_id  # noqa: E402

from .stores import OrmJobClaimer  # noqa: E402
from .wiring import get_module  # noqa: E402

_module = get_module()
_claimer = OrmJobClaimer()


def _run(job_id: str) -> None:
    wid = worker_id()
    if not _claimer.claim(job_id, wid, timedelta(minutes=10)):
        return  # someone else has it, or it is already terminal
    try:
        _module.run(job_id)
    finally:
        _claimer.release(job_id, wid)


dramatiq_dispatcher.set_runner(_run)
