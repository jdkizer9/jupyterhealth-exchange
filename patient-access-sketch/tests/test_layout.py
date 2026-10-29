from datetime import UTC, datetime

import pytest

from patient_access.core.models import Job, Participant
from patient_access.core.source_settings import ConfigError
from patient_access.destinations.layout import KeyLayout, LayoutError
from patient_access.testing.memory import selection, source


def _job(ref="P-1", attrs=None):
    return Job(
        id="j1",
        participant=Participant(ref, "x", attrs=attrs or {}),
        source=source(selection(vendor="epic", iss="https://e/fhir", customer_id="org-9"), ehr_patient_id="pat"),
        destinations=("d",),
        created_at=datetime(2026, 10, 22, tzinfo=UTC),
    )


def test_default_and_attrs_and_dates():
    assert KeyLayout().render(_job()) == "pulls/P-1/j1"
    layout = KeyLayout("{attrs[study]}/{source.vendor}/{participant.ref}/{created_at:%Y}/{created_at:%m}/{job.id}")
    assert layout.render(_job(attrs={"study": "bp"})) == "bp/epic/P-1/2026/10/j1"
    assert KeyLayout("{source.customer_id}/{job.id}").render(_job()) == "org-9/j1"


def test_source_id_is_stable_and_prefers_npi():
    job = _job()
    hashed = KeyLayout("{source.id}/{participant.ref}").render(job)
    assert hashed.startswith("epic-") and len(hashed.split("/")[0]) == len("epic-") + 12
    assert KeyLayout("{source.id}").render(_job()) == hashed.split("/")[0]  # same iss, same id
    job.source.brand_npi = "1234567890"
    assert KeyLayout("{source.id}").render(job) == "npi-1234567890"


def test_values_cannot_escape_their_segment():
    assert KeyLayout().render(_job(ref="../../etc/{job.id}")) == "pulls/.._.._etc__job.id_/j1"
    assert KeyLayout("{participant.ref}").render(_job(ref="..")) == "_"


def test_unknown_root_fails_at_config_and_missing_attr_fails_the_pull():
    with pytest.raises(ConfigError):
        KeyLayout("{patient.ref}/{job.id}")
    with pytest.raises(ConfigError):
        KeyLayout("pulls//{job.id}")
    with pytest.raises(LayoutError, match="study"):
        KeyLayout("{attrs[study]}/{job.id}").render(_job())
