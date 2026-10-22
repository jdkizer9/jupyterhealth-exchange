"""Worker side of the DbPollingDispatcher. Standalone: a long-running service. JHE: `--once` from cron."""

import signal

from django.core.management.base import BaseCommand, CommandError

from patient_access.dispatch.db_polling import DbPollingDispatcher, poll_forever, poll_once, sweep, worker_id

from ...wiring import get_module


class Command(BaseCommand):
    help = "Claim and run queued jobs (DbPollingDispatcher worker side)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run every runnable job, then exit (cron).")

    def handle(self, *args, **options):
        module = get_module()
        dispatcher = module.dispatcher
        if not isinstance(dispatcher, DbPollingDispatcher):
            raise CommandError(f"run_jobs is the DbPollingDispatcher worker; configured: {type(dispatcher).__name__}")
        if options["once"]:
            wid = worker_id()
            while poll_once(module.claimer, module.run, dispatcher.lease, wid):
                pass
            sweep(module.claimer, module.spool, dispatcher.spool_max_age)
            return
        stopping = {"flag": False}

        def stop(*_):
            stopping["flag"] = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        poll_forever(dispatcher, module.claimer, module.run, module.spool, should_stop=lambda: stopping["flag"])
