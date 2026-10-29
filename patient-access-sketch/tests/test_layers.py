"""The import-linter contracts in pyproject.toml run as part of pytest (plan §6: a failure is a finding)."""

import subprocess
from pathlib import Path


def test_import_contracts():
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(["lint-imports"], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
