"""A host that installs only the jobs app (JHE) gets its two tables and nothing else.

Runs in a fresh interpreter because INSTALLED_APPS is fixed once Django is set up.
"""

import json
import subprocess
import sys
from pathlib import Path

PROBE = r"""
import json
import django
from django.conf import settings

settings.configure(
    INSTALLED_APPS=["django.contrib.contenttypes", "patient_access.django_apps.jobs"],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    USE_TZ=True,
)
django.setup()
from django.apps import apps
from django.core.management import get_commands

import patient_access.django_apps.jobs.wiring  # noqa: F401  the host's wiring loads

out = {
    "tables": sorted(m._meta.db_table for m in apps.get_app_config("pa_jobs").get_models()),
    "run_jobs": get_commands().get("run_jobs"),
    "optional_commands": [c for c in ("seed_directory", "mint_deeplink") if c in get_commands()],
}
try:
    import patient_access.django_apps.directory.stores  # noqa: F401
    out["directory"] = "imported"
except RuntimeError as e:
    out["directory"] = "refused: " + str(e)[:40]
print(json.dumps(out))
"""


def test_jobs_only_host_gets_two_tables_and_no_optional_plugs():
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=root, capture_output=True, text=True, env={"PYTHONPATH": str(root / "src")}
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["tables"] == ["pa_jobs_authattemptrecord", "pa_jobs_jobrecord"]
    assert out["run_jobs"] == "patient_access.django_apps.jobs"
    assert out["optional_commands"] == []
    # Naming the module's directory plug without installing its app fails at startup, not quietly.
    assert out["directory"].startswith("refused: Model class")
