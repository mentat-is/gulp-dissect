import asyncio
import json
from pathlib import Path

import pytest
from gulp.api.opensearch.filters import GulpIngestionFilter
from gulp.api.mapping.mapping_utils import build_gulp_document_id

from gulp_dissect.cli import (
    _passes_ingestion_filter,
    AppConfig,
    build_config,
    collect_extract_specs,
    get_app_version,
    ingest_spec,
    map_record_to_gulp_document,
    map_record_to_gulp_documents,
    print_banner,
    normalize_timestamp,
    parse_args,
    resolve_specs,
)


def _cfg(context_name=None, source_name=None):
    return AppConfig(
        image_path="/tmp/image.img",
        username="u",
        password="p",
        gulp_url="http://localhost:8080",
        operation_id="test_operation",
        limit=0,
        chunk_size=1000,
        context_name=context_name,
        source_name=source_name,
        mapping_files_base_path=None,
        flt=None,
        reset_operation=False,
        verbose=False,
    )


def _resolve_specs(specs_raw, cfg):
    return asyncio.run(_resolve_specs_async(specs_raw, cfg))


async def _resolve_specs_async(specs_raw, cfg):
    return await resolve_specs(specs_raw, cfg)


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


def test_parse_args_version_prints_version_and_exits(
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as exc:
        parse_args(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == get_app_version()


def test_print_banner_includes_version(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setattr("gulp_dissect.cli.art.text2art", lambda text, font: "BANNER")

    print_banner()

    output = capsys.readouterr().out
    assert "BANNER" in output
    assert f"gulp-dissect v{get_app_version()}" in output


def test_collect_extract_specs_from_extract_rules(tmp_path: Path):
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
    extract_rules_file = tmp_path / "extracts.json"
    extract_rules_file.write_text(json.dumps(payload), encoding="utf-8")

    args = parse_args(["--extract_rules", str(extract_rules_file)])
    specs = collect_extract_specs(args)
    assert len(specs) == 1
    assert specs[0]["plugin"] == "evt"

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


def test_build_config_parses_mapping_files_base_path_from_cli():
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
            "--mapping_files_base_path",
            "/tmp/mappings",
        ]
    )

    cfg = build_config(args)
    assert cfg.mapping_files_base_path == "/tmp/mappings"


def test_build_config_parses_mapping_files_base_path_from_env(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GULP_DISSECT_MAPPING_FILES_BASE_PATH", "/env/mappings")

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
    assert cfg.mapping_files_base_path == "/env/mappings"


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


def test_build_config_parses_flt_json():
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
            "--flt",
            '{"time_range":[1704067200000000000,1704153600000000000]}',
        ]
    )
    cfg = build_config(args)
    assert isinstance(cfg.flt, GulpIngestionFilter)
    assert cfg.flt.time_range == (1704067200000000000, 1704153600000000000)


def test_build_config_rejects_non_object_flt():
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
            "--flt",
            '["not-an-object"]',
        ]
    )
    with pytest.raises(ValueError, match="flt"):
        build_config(args)


def test_build_config_rejects_invalid_flt_object_shape():
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
            "--flt",
            '{"time_range":{"start":1,"end":2}}',
        ]
    )
    with pytest.raises(ValueError, match="invalid flt"):
        build_config(args)


def test_passes_ingestion_filter_model_extra_string_and_number_ops_with_and():
    flt = GulpIngestionFilter.model_validate(
        {
            "time_range": [1704067200000000000, 1704067200000000000],
            "event.category": "authentication",
            "event.severity": {"gte": 3, "lte": 5},
            "event.risk_score": 7,
        }
    )

    good = {
        "@timestamp": "2024-01-01T00:00:00Z",
        "event.category": "authentication",
        "event.severity": 4,
        "event.risk_score": 7,
    }
    assert _passes_ingestion_filter(good, flt) is True

    bad_time = {
        "@timestamp": "2024-01-02T00:00:00Z",
        "event.category": "authentication",
        "event.severity": 4,
        "event.risk_score": 7,
    }
    assert _passes_ingestion_filter(bad_time, flt) is False

    bad_string = {
        "@timestamp": "2024-01-01T00:00:00Z",
        "event.category": "process",
        "event.severity": 4,
        "event.risk_score": 7,
    }
    assert _passes_ingestion_filter(bad_string, flt) is False

    bad_number = {
        "@timestamp": "2024-01-01T00:00:00Z",
        "event.category": "authentication",
        "event.severity": 6,
        "event.risk_score": 7,
    }
    assert _passes_ingestion_filter(bad_number, flt) is False


def test_passes_ingestion_filter_model_extra_rejects_missing_or_invalid_conditions():
    missing_field_flt = GulpIngestionFilter.model_validate({"event.category": "auth"})
    assert (
        _passes_ingestion_filter(
            {"@timestamp": "2024-01-01T00:00:00Z"}, missing_field_flt
        )
        is False
    )

    unsupported_condition_flt = GulpIngestionFilter.model_validate(
        {"event.severity": {"gt": 3}}
    )
    assert (
        _passes_ingestion_filter(
            {"@timestamp": "2024-01-01T00:00:00Z", "event.severity": 4},
            unsupported_condition_flt,
        )
        is False
    )

    unsupported_equal_flt = GulpIngestionFilter.model_validate(
        {"event.severity": {"equal": 4}}
    )
    assert (
        _passes_ingestion_filter(
            {"@timestamp": "2024-01-01T00:00:00Z", "event.severity": 4},
            unsupported_equal_flt,
        )
        is False
    )


def test_passes_ingestion_filter_invalid_timestamp_string_is_not_forced_to_zero():
    flt = GulpIngestionFilter.model_validate(
        {"time_range": [1704067200000000000, 1704153600000000000]}
    )

    # Parsing failures should not be treated as timestamp=0, otherwise records
    # could be incorrectly rejected when start > 0.
    assert _passes_ingestion_filter({"@timestamp": "not-a-timestamp"}, flt) is True


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
        _resolve_specs(specs_raw, _cfg())


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

    resolved = _resolve_specs(
        specs_raw, _cfg(context_name="ctx123", source_name="src456")
    )
    assert resolved[0].mapping_id == "m1"
    assert resolved[0].plugin == "evt"


def test_resolve_specs_allows_missing_event_code_mapping_with_event_code_override():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "event_code": "4624",
                "mappings": {
                    "m1": {
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                        }
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]

    resolved = _resolve_specs(specs_raw, _cfg())
    assert resolved[0].mapping_id == "m1"
    assert resolved[0].event_code_fields == []


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

    resolved = _resolve_specs(specs_raw, _cfg())
    assert resolved[0].timestamp_fields == ["ts"]


def test_resolve_specs_supports_mapping_file_like_gulp(tmp_path: Path):
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(
        json.dumps(
            {
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
                "metadata": {"plugin": ["evt"]},
            }
        ),
        encoding="utf-8",
    )

    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mapping_file": str(mapping_file),
                "mapping_id": "m1",
            },
        }
    ]

    resolved = _resolve_specs(specs_raw, _cfg())
    assert resolved[0].mapping_id == "m1"
    assert resolved[0].timestamp_fields == ["ts"]
    assert resolved[0].event_code_fields == ["EventCode"]


def test_resolve_specs_mapping_file_allows_mapping_event_code_without_event_code_field(
    tmp_path: Path,
):
    mapping_file = tmp_path / "mapping_no_event_code_field.json"
    mapping_file.write_text(
        json.dumps(
            {
                "mappings": {
                    "m1": {
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                        },
                        "event_code": "dissect_mft",
                    }
                },
                "metadata": {"plugin": ["evt"]},
            }
        ),
        encoding="utf-8",
    )

    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mapping_file": str(mapping_file),
                "mapping_id": "m1",
            },
        }
    ]

    resolved = _resolve_specs(specs_raw, _cfg())
    assert resolved[0].mapping_id == "m1"
    assert resolved[0].event_code_fields == []


def test_resolve_specs_supports_mapping_scoped_value_aliases():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mappings": {
                    "m1": {
                        "value_aliases": {
                            "event.code": {
                                "default": {
                                    "4624": "bingo",
                                }
                            }
                        },
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "EventCode": {"ecs": ["event.code"]},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                        },
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]

    resolved = _resolve_specs(specs_raw, _cfg())
    assert (
        resolved[0].mapping["value_aliases"]["event.code"]["default"]["4624"] == "bingo"
    )


@pytest.mark.asyncio
async def test_resolve_specs_passes_mapping_files_base_path_to_mapping_resolver(
    monkeypatch: pytest.MonkeyPatch,
):
    from gulp_dissect import cli as cli_module

    captured: dict[str, str | None] = {"mapping_base_path": None}

    class _FakeMappingModel:
        def model_dump(self):
            return {
                "fields": {
                    "ts": {"ecs": ["@timestamp"]},
                    "EventCode": {"ecs": ["event.code"]},
                    "hostname": {"is_gulp_type": "context_name"},
                    "SourceName": {"is_gulp_type": "source_name"},
                }
            }

    async def _fake_mapping_parameters_to_mapping(
        mapping_parameters,
        mapping_base_path=None,
    ):
        captured["mapping_base_path"] = mapping_base_path
        return ({"m1": _FakeMappingModel()}, "m1")

    monkeypatch.setattr(
        cli_module,
        "mapping_parameters_to_mapping",
        _fake_mapping_parameters_to_mapping,
    )

    cfg = _cfg()
    cfg.mapping_files_base_path = "/tmp/base"

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

    await _resolve_specs_async(specs_raw, cfg)

    assert captured["mapping_base_path"] == "/tmp/base"


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


class _FakeIngestApi:
    def __init__(self):
        self.calls = []

    async def raw(self, operation_id, plugin_name, data, params, wait, timeout):
        self.calls.append(
            {
                "operation_id": operation_id,
                "plugin_name": plugin_name,
                "data": list(data),
                "params": params,
                "wait": wait,
                "timeout": timeout,
            }
        )

        class _Result:
            status = "completed"

        return _Result()


class _FakeIngestClient:
    def __init__(self):
        self.ingest = _FakeIngestApi()


class _FakeFullClient(_FakeClient):
    def __init__(self):
        super().__init__()
        self.ingest = _FakeIngestApi()


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
    spec = (await _resolve_specs_async(specs_raw, _cfg()))[0]

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
    cfg = _cfg(context_name="ctx-fixed", source_name="src-fixed")
    spec = (await _resolve_specs_async(specs_raw, cfg))[0]

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

    assert doc["gulp.context_id"].startswith("ctx::test_operation::ctx-fixed")
    assert doc["gulp.source_id"].startswith("src::test_operation::")
    assert doc["gulp.source_id"].endswith("::src-fixed")


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
    spec = (await _resolve_specs_async(specs_raw, _cfg()))[0]

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
    spec = (await _resolve_specs_async(specs_raw, _cfg()))[0]

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


@pytest.mark.asyncio
async def test_map_record_to_gulp_document_drops_generated_and_gulp_namespace_fields():
    specs_raw = [
        {
            "plugin": "mft",
            "mapping_parameters": {
                "mappings": {
                    "m1": {
                        "event_code": "dissect_mft",
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "filesize": {"ecs": ["file.size"]},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                            "_source": {"ecs": ["log.file_path"]},
                        },
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]

    spec = (await _resolve_specs_async(specs_raw, _cfg()))[0]

    doc = await map_record_to_gulp_document(
        _FakeClient(),
        _cfg(),
        spec,
        {
            "ts": "2024-01-01T00:00:00Z",
            "filesize": 123,
            "hostname": "host-a",
            "SourceName": "security",
            "_source": "/path/to/mft",
            "_generated": "drop-me",
            "gulp.context_id": "raw-ctx",
            "gulp.source_id": "raw-src",
            "OtherField": "keep-me",
        },
        1,
        {},
        {},
    )

    assert "_generated" not in doc
    assert "gulp.context_id" in doc
    assert doc["gulp.context_id"].startswith("ctx::test_operation::")
    assert "gulp.source_id" in doc
    assert doc["gulp.source_id"].startswith("src::test_operation::")
    assert "OtherField" in doc

    event_original = json.loads(doc["event.original"])
    assert "_generated" not in event_original
    assert "gulp.context_id" not in event_original
    assert "gulp.source_id" not in event_original


@pytest.mark.asyncio
async def test_map_record_to_gulp_document_applies_value_aliases_from_mapping_parameters():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mappings": {
                    "m1": {
                        "value_aliases": {
                            "event.code": {
                                "default": {
                                    "4624": "bingo",
                                }
                            }
                        },
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "EventCode": {"ecs": ["event.code"]},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                        },
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]

    spec = (await _resolve_specs_async(specs_raw, _cfg()))[0]

    doc = await map_record_to_gulp_document(
        _FakeClient(),
        _cfg(),
        spec,
        {
            "ts": "2024-01-01T00:00:00Z",
            "EventCode": 4624,
            "hostname": "host-a",
            "SourceName": "security",
        },
        1,
        {},
        {},
    )

    assert doc["event.code"] == "bingo"


@pytest.mark.asyncio
async def test_map_record_to_gulp_document_overrides_agent_type_from_mapping_parameters():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "agent_type": "override-agent",
                "mappings": {
                    "m1": {
                        "agent_type": "mapping-agent",
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "EventCode": {"ecs": ["event.code"]},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                        },
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]

    spec = (await _resolve_specs_async(specs_raw, _cfg()))[0]

    doc = await map_record_to_gulp_document(
        _FakeClient(),
        _cfg(),
        spec,
        {
            "ts": "2024-01-01T00:00:00Z",
            "EventCode": 4624,
            "hostname": "host-a",
            "SourceName": "security",
        },
        1,
        {},
        {},
    )

    assert doc["agent.type"] == "override-agent"


@pytest.mark.asyncio
async def test_map_record_to_gulp_document_overrides_event_code_from_mapping_parameters():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "event_code": "override-event",
                "mappings": {
                    "m1": {
                        "event_code": "mapping-event",
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "EventCode": {"ecs": ["event.code"]},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                        },
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]

    spec = (await _resolve_specs_async(specs_raw, _cfg()))[0]

    doc = await map_record_to_gulp_document(
        _FakeClient(),
        _cfg(),
        spec,
        {
            "ts": "2024-01-01T00:00:00Z",
            "EventCode": 4624,
            "hostname": "host-a",
            "SourceName": "security",
        },
        1,
        {},
        {},
    )

    assert doc["event.code"] == "override-event"


@pytest.mark.asyncio
async def test_map_record_to_gulp_documents_supports_extra_doc_with_event_code():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mappings": {
                    "m1": {
                        "event_code": "base-event",
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "ExtraTs": {"extra_doc_with_event_code": "extra-event"},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                            "Computername": {"ecs": ["host.name"]},
                        },
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]

    spec = (await _resolve_specs_async(specs_raw, _cfg()))[0]

    docs = await map_record_to_gulp_documents(
        _FakeClient(),
        _cfg(),
        spec,
        {
            "ts": "2024-01-01T00:00:00Z",
            "ExtraTs": "2024-01-02T00:00:00Z",
            "hostname": "host-a",
            "SourceName": "security",
            "Computername": "host-a",
        },
        1,
        {},
        {},
    )

    assert len(docs) == 2
    expected_base_id = build_gulp_document_id(
        event_original=docs[0]["event.original"],
        event_code=docs[0]["event.code"],
        operation_id="test_operation",
        context_id=docs[0]["gulp.context_id"],
        source_id=docs[0]["gulp.source_id"],
        event_sequence=docs[0]["event.sequence"],
        timestamp=docs[0]["@timestamp"],
    )
    assert docs[0]["_id"] == expected_base_id
    assert docs[0]["event.code"] == "base-event"
    assert docs[0]["@timestamp"] == "2024-01-01T00:00:00.000Z"
    assert docs[1]["event.code"] == "extra-event"
    assert docs[1]["@timestamp"] == "2024-01-02T00:00:00.000Z"
    assert docs[1]["host.name"] == "host-a"
    assert docs[1]["gulp.context_id"] == docs[0]["gulp.context_id"]
    assert docs[1]["gulp.source_id"] == docs[0]["gulp.source_id"]
    assert docs[1]["gulp.base_document_id"] == docs[0]["_id"]


@pytest.mark.asyncio
async def test_ingest_spec_applies_local_filter_and_does_not_forward_flt(monkeypatch):
    from gulp_dissect import cli as cli_module

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

    cfg = _cfg(context_name="ctx-fixed", source_name="src-fixed")
    cfg.flt = GulpIngestionFilter(time_range=(1704067200000000000, 1704067200000000000))
    cfg.chunk_size = 10
    cfg.verbose = True
    spec = (await _resolve_specs_async(specs_raw, cfg))[0]

    monkeypatch.setattr(
        cli_module,
        "iter_plugin_records",
        lambda target, plugin: iter([{"id": 1}, {"id": 2}]),
    )

    seq = {"value": 0}

    async def _fake_map_record_to_gulp_documents(
        client,
        cfg,
        spec,
        raw_record,
        event_sequence,
        context_cache,
        source_cache,
    ):
        seq["value"] += 1
        if seq["value"] == 1:
            ts = "2024-01-01T00:00:00Z"
        else:
            ts = "2024-01-02T00:00:00Z"
        return [
            {
                "@timestamp": ts,
                "event.code": "4624",
                "gulp.context_id": "ctx-fixed",
                "gulp.source_id": "src-fixed",
            }
        ]

    monkeypatch.setattr(
        cli_module,
        "map_record_to_gulp_documents",
        _fake_map_record_to_gulp_documents,
    )

    client = _FakeIngestClient()
    ingested = await ingest_spec(client, cfg, target=None, spec=spec, max_records=None)

    assert ingested == 1
    assert len(client.ingest.calls) == 1
    assert len(client.ingest.calls[0]["data"]) == 1
    assert "flt" not in client.ingest.calls[0]["params"]
    assert "plugin_params" in client.ingest.calls[0]["params"]


@pytest.mark.asyncio
async def test_ingest_spec_expands_extra_doc_with_event_code(monkeypatch):
    from gulp_dissect import cli as cli_module

    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mappings": {
                    "m1": {
                        "event_code": "base-event",
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                            "ExtraTs": {"extra_doc_with_event_code": "extra-event"},
                            "hostname": {"is_gulp_type": "context_name"},
                            "SourceName": {"is_gulp_type": "source_name"},
                            "Computername": {"ecs": ["host.name"]},
                        },
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]

    spec = (await _resolve_specs_async(specs_raw, _cfg()))[0]

    monkeypatch.setattr(
        cli_module,
        "iter_plugin_records",
        lambda target, plugin: iter(
            [
                {
                    "ts": "2024-01-01T00:00:00Z",
                    "ExtraTs": "2024-01-02T00:00:00Z",
                    "hostname": "host-a",
                    "SourceName": "security",
                    "Computername": "host-a",
                }
            ]
        ),
    )

    client = _FakeFullClient()
    ingested = await ingest_spec(
        client, _cfg(), target=None, spec=spec, max_records=None
    )

    assert ingested == 2
    assert len(client.ingest.calls) == 1
    assert len(client.ingest.calls[0]["data"]) == 2
    assert client.ingest.calls[0]["data"][0]["event.code"] == "base-event"
    assert client.ingest.calls[0]["data"][1]["event.code"] == "extra-event"
