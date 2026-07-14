import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

_PLAN_MAPPING_PARAMETERS = {
    "mappings": {
        "dissect_evt": {
            "exclude": ["_generated", "_version", "_classification"],
            "fields": {
                "ts": {"ecs": ["@timestamp"]},
                "EventCode": {"ecs": ["event.code"]},
                "hostname": {"is_gulp_type": "context_name"},
                "SourceName": {"is_gulp_type": "source_name"},
                "_source": {"ecs": ["log.file_path"]},
            },
        }
    }
}

_LIMITED_EVT_DOC_COUNT = 25
_PLAN_OPERATION_ID = "test_operation"
_ROOT = Path(__file__).resolve().parents[1]


def _base_url() -> str:
    return os.getenv("GULP_BASE_URL", "http://localhost:8080")


def _username() -> str:
    return os.getenv("GULP_TEST_USER", "admin")


def _password() -> str:
    return os.getenv("GULP_TEST_PASSWORD", "admin")


@pytest.mark.integration
def test_cli_plan_evt_workflow():
    """Run the plan's SCHARDT.img + evt workflow against the plan's test_operation."""

    image_path = "/gulp/img/SCHARDT.img"
    if not os.path.exists(image_path):
        pytest.skip(f"Sample image missing: {image_path}")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(_ROOT / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gulp_dissect.cli",
            "--image_path",
            image_path,
            "--username",
            _username(),
            "--password",
            _password(),
            "--gulp_url",
            _base_url(),
            "--operation_id",
            _PLAN_OPERATION_ID,
            "--limit",
            str(_LIMITED_EVT_DOC_COUNT),
            "--reset-operation",
            "--chunk-size",
            "1000",
            "--plugin",
            "evt",
            "--mapping_parameters",
            json.dumps(_PLAN_MAPPING_PARAMETERS),
        ],
        cwd=_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    combined_output = result.stdout + result.stderr
    assert result.returncode == 0, combined_output
    assert f"ingested records={_LIMITED_EVT_DOC_COUNT}" in result.stdout
