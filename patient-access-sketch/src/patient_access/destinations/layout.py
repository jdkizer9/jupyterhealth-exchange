"""KeyLayout: where a pull lands in an object store, as a template the consumer configures.

    "pulls/{participant.ref}/{job.id}"                       (default)
    "{attrs[study]}/{participant.ref}/{created_at:%Y}/{created_at:%m}/{job.id}"

Available roots: `participant` (ref, provider), `attrs` (whatever the identity provider put in
Participant.attrs, e.g. the deep link's `attrs` claim), `job` (id), `source` (`id`: a stable,
path-safe health-system identifier, NPI-based when known; also vendor, brand_name, location_id,
ehr_patient_id), `created_at` (datetime). Each `/`-separated segment is rendered and
sanitised on its own, so no value can add or escape a prefix. Unknown roots fail at
configuration time; a missing attribute fails the pull before anything is written.
"""

from __future__ import annotations

import re
import string
from datetime import UTC, datetime
from typing import Any

from patient_access.core.models import Job
from patient_access.core.vendors import ConfigError

_SAFE = re.compile(r"[^A-Za-z0-9._-]")
ROOTS = {"participant", "attrs", "job", "source", "created_at"}


def safe(part: str) -> str:
    """One path segment: filename-safe characters only, never empty, never `.` or `..`."""
    out = _SAFE.sub("_", part)[:128]
    return out if out and out.strip(".") else "_"


class LayoutError(Exception):
    """The template needs something this job does not have. Safe to show an operator."""


class KeyLayout:
    DEFAULT = "pulls/{participant.ref}/{job.id}"

    def __init__(self, template: str = DEFAULT):
        self.template = template.strip("/")
        self.segments = self.template.split("/")
        fmt = string.Formatter()
        for seg in self.segments:
            if not seg:
                raise ConfigError(f"layout {template!r}: empty segment")
            for _, field, _, _ in fmt.parse(seg):
                if field is None:
                    continue
                root = re.split(r"[.\[]", field, maxsplit=1)[0]
                if root not in ROOTS:
                    raise ConfigError(f"layout {template!r}: unknown field {field!r} (roots: {sorted(ROOTS)})")

    def render(self, job: Job) -> str:
        ctx: dict[str, Any] = {
            "participant": job.participant,
            "attrs": job.participant.attrs,
            "job": job,
            "source": job.source,
            "created_at": job.created_at or datetime.now(UTC),
        }
        out = []
        for seg in self.segments:
            try:
                out.append(safe(seg.format_map(ctx)))
            except (KeyError, AttributeError, IndexError) as e:
                raise LayoutError(f"layout segment {seg!r} needs {e} which this pull does not have") from e
        return "/".join(out)
