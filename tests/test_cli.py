import json
from pathlib import Path

import pytest

from gulp_dissect.cli import (
    AppConfig,
    build_config,
    collect_extract_specs,
    map_record_to_gulp_document,
    normalize_timestamp,
    parse_args,
    resolve_specs,
)


def _cfg(context_id=None, source_id=None):
    return AppConfig(
        image_path="/tmp/image.img",
        username="u",
        password="p",
        gulp_url="http://localhost:8080",
        operation_id="test_operation",
        limit=0,
        chunk_size=1000,
        context_id=context_id,
        source_id=source_id,
        reset_operation=False,
        verbose=False,
    )


def test_collect_extract_specs_from_repeated_pairs():
    spec = {
        "mapping_parameters": {
            "mappings": {
                "dissect_evt": {
                    "fields": {
                        "ts": {"ecs": ["@timestamp"]},
                        "EventCode": {"ecs": ["event.code"]},
                        "hostname": {"is_gulp_type": "context_name"},
                        "SourceName": {"is_gulp_type": "source_name"},
                    }
                }
            },
            "mapping_id": "dissect_evt",
        },
    }
    args = parse_args(
        [
            "--plugin",
            "evt",
            "--mapping_parameters",
            json.dumps(spec["mapping_parameters"]),
        ]
    )
    specs = collect_extract_specs(args)
    assert len(specs) == 1
    assert specs[0]["plugin"] == "evt"


def test_collect_extract_specs_rejects_unpaired_plugin_mapping_parameters():
    args = parse_args(["--plugin", "evt"])

    with pytest.raises(ValueError, match="same number"):
        collect_extract_specs(args)


def test_collect_extract_specs_from_extract_file(tmp_path: Path):
    payload = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mappings": {
                    "dissect_evt": {
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "EventCode": {"ecs": ["event.code"]},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                        }
                    }
                },
                "mapping_id": "dissect_evt",
            },
        }
    ]
    extract_file = tmp_path / "extracts.json"
    extract_file.write_text(json.dumps(payload), encoding="utf-8")

    args = parse_args(["--extract_file", str(extract_file)])
    specs = collect_extract_specs(args)
    assert len(specs) == 1
    assert specs[0]["plugin"] == "evt"


def test_build_config_enables_verbose_flag():
    args = parse_args(
        [
            "--image_path",
            "/tmp/image.img",
            "--username",
            "u",
            "--password",
            "p",
            "--gulp_url",
            "http://localhost:8080",
            "--operation_id",
            "test_operation",
            "--verbose",
        ]
    )
    cfg = build_config(args)
    assert cfg.verbose is True


def test_build_config_uses_env_for_auth_and_url_only(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GULP_DISSECT_USERNAME", "env-user")
    monkeypatch.setenv("GULP_DISSECT_PASSWORD", "env-pass")
    monkeypatch.setenv("GULP_DISSECT_URL", "http://env:8080")

    args = parse_args(
        [
            "--image_path",
            "/tmp/image.img",
            "--operation_id",
            "test_operation",
        ]
    )
    cfg = build_config(args)

    assert cfg.username == "env-user"
    assert cfg.password == "env-pass"
    assert cfg.gulp_url == "http://env:8080"


def test_build_config_rejects_missing_image_path_even_if_env_present(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GULP_DISSECT_IMAGE_PATH", "/tmp/from_env.img")

    args = parse_args(
        [
            "--username",
            "u",
            "--password",
            "p",
            "--gulp_url",
            "http://localhost:8080",
            "--operation_id",
            "test_operation",
        ]
    )

    with pytest.raises(ValueError, match="image_path"):
        build_config(args)


def test_build_config_defaults_to_unlimited_limit():
    args = parse_args(
        [
            "--image_path",
            "/tmp/image.img",
            "--username",
            "u",
            "--password",
            "p",
            "--gulp_url",
            "http://localhost:8080",
            "--operation_id",
            "test_operation",
        ]
    )
    cfg = build_config(args)
    assert cfg.limit == 0


def test_build_config_sets_limit_from_cli():
    args = parse_args(
        [
            "--image_path",
            "/tmp/image.img",
            "--username",
            "u",
            "--password",
            "p",
            "--gulp_url",
            "http://localhost:8080",
            "--operation_id",
            "test_operation",
            "--limit",
            "5",
        ]
    )
    cfg = build_config(args)
    assert cfg.limit == 5


def test_build_config_rejects_negative_limit():
    args = parse_args(
        [
            "--image_path",
            "/tmp/image.img",
            "--username",
            "u",
            "--password",
            "p",
            "--gulp_url",
            "http://localhost:8080",
            "--operation_id",
            "test_operation",
            "--limit",
            "-1",
        ]
    )

    with pytest.raises(ValueError, match="limit"):
        build_config(args)


def test_build_config_enables_reset_operation_flag():
    args = parse_args(
        [
            "--image_path",
            "/tmp/image.img",
            "--username",
            "u",
            "--password",
            "p",
            "--gulp_url",
            "http://localhost:8080",
            "--operation_id",
            "test_operation",
            "--reset-operation",
        ]
    )
    cfg = build_config(args)
    assert cfg.reset_operation is True


def test_resolve_specs_enforces_context_source_without_overrides():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mappings": {
                    "m1": {
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "EventCode": {"ecs": ["event.code"]},
                        }
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]

    with pytest.raises(ValueError, match="context_name"):
        resolve_specs(specs_raw, _cfg())


def test_resolve_specs_accepts_context_source_overrides_and_injects_id_mapping():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mappings": {
                    "m1": {
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "EventCode": {"ecs": ["event.code"]},
                        }
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]

    resolved = resolve_specs(specs_raw, _cfg(context_id="ctx123", source_id="src456"))
    assert resolved[0].mapping_id == "m1"
    assert resolved[0].plugin == "evt"


def test_resolve_specs_fallbacks_timestamp_to_ts():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mappings": {
                    "m1": {
                        "fields": {
                            "ts": {},
                            "EventCode": {"ecs": ["event.code"]},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                        }
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]

    resolved = resolve_specs(specs_raw, _cfg())
    assert resolved[0].timestamp_fields == ["ts"]


def test_normalize_timestamp_to_iso_utc():
    val = normalize_timestamp("2024-01-01 12:00:00+02:00")
    assert val.endswith("Z")
    assert val.startswith("2024-01-01T10:00:00")


class _FakeOperations:
    async def context_create(self, operation_id, context_name):
        return {"id": f"ctx::{operation_id}::{context_name}", "name": context_name}

    async def source_create(self, operation_id, context_id, source_name, plugin=None):
        return {
            "id": f"src::{operation_id}::{context_id}::{source_name}",
            "name": source_name,
        }


class _FakeClient:
    def __init__(self):
        self.operations = _FakeOperations()


@pytest.mark.asyncio
async def test_map_record_to_gulp_document_builds_raw_doc_from_mapping():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mappings": {
                    "m1": {
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "EventCode": {"ecs": ["event.code"]},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                            "Computername": {"ecs": ["host.name"]},
                        }
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]
    spec = resolve_specs(specs_raw, _cfg())[0]

    doc = await map_record_to_gulp_document(
        _FakeClient(),
        _cfg(),
        spec,
        {
            "ts": "2024-01-01T00:00:00Z",
            "EventCode": 4624,
            "hostname": "host-a",
            "SourceName": "security",
            "Computername": "host-a",
        },
        7,
        {},
        {},
    )

    assert doc["@timestamp"] == "2024-01-01T00:00:00.000Z"
    assert doc["event.code"] == "4624"
    assert doc["event.sequence"] == 7
    assert doc["host.name"] == "host-a"
    assert "ts" not in doc
    assert "EventCode" not in doc
    assert "hostname" not in doc
    assert "SourceName" not in doc
    assert "Computername" not in doc
    assert doc["gulp.context_id"].startswith("ctx::test_operation::host-a")
    assert doc["gulp.source_id"].startswith("src::test_operation::")


@pytest.mark.asyncio
async def test_map_record_to_gulp_document_uses_explicit_ids_over_names():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mappings": {
                    "m1": {
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "EventCode": {"ecs": ["event.code"]},
                        }
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]
    cfg = _cfg(context_id="ctx-fixed", source_id="src-fixed")
    spec = resolve_specs(specs_raw, cfg)[0]

    doc = await map_record_to_gulp_document(
        _FakeClient(),
        cfg,
        spec,
        {
            "ts": "2024-01-01T00:00:00Z",
            "EventCode": "1",
        },
        1,
        {},
        {},
    )

    assert doc["gulp.context_id"] == "ctx-fixed"
    assert doc["gulp.source_id"] == "src-fixed"


@pytest.mark.asyncio
async def test_map_record_to_gulp_document_honors_exclude_and_maps_plan_fields():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mappings": {
                    "dissect_evt": {
                        "exclude": ["_generated"],
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "EventCode": {"ecs": ["event.code"]},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                            "_source": {"ecs": ["log.file_path"]},
                            "_version": {"ecs": ["log.file_version"]},
                        },
                    }
                },
                "mapping_id": "dissect_evt",
            },
        }
    ]
    spec = resolve_specs(specs_raw, _cfg())[0]

    doc = await map_record_to_gulp_document(
        _FakeClient(),
        _cfg(),
        spec,
        {
            "ts": "2024-01-01T00:00:00Z",
            "EventCode": 4624,
            "hostname": "host-a",
            "SourceName": "security",
            "_source": "/Windows/System32/winevt/Logs/Security.evtx",
            "_version": 2,
            "_generated": "drop-me",
        },
        3,
        {},
        {},
    )

    assert "_generated" not in doc
    assert "_source" not in doc
    assert "_version" not in doc
    assert "ts" not in doc
    assert "EventCode" not in doc
    assert "hostname" not in doc
    assert "SourceName" not in doc
    assert doc["log.file_path"] == "/Windows/System32/winevt/Logs/Security.evtx"
    assert doc["log.file_version"] == 2


@pytest.mark.asyncio
async def test_map_record_to_gulp_document_preserves_only_unmapped_fields():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mappings": {
                    "m1": {
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "EventCode": {"ecs": ["event.code"]},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                        }
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]
    spec = resolve_specs(specs_raw, _cfg())[0]

    doc = await map_record_to_gulp_document(
        _FakeClient(),
        _cfg(),
        spec,
        {
            "ts": "2024-01-01T00:00:00Z",
            "EventCode": 4624,
            "hostname": "host-a",
            "SourceName": "security",
            "UnmappedField": "keep-me",
        },
        9,
        {},
        {},
    )

    assert doc["UnmappedField"] == "keep-me"
    assert "ts" not in doc
    assert "EventCode" not in doc
    assert "hostname" not in doc
    assert "SourceName" not in doc
