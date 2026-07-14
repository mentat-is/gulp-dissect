import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from gulp.api.opensearch.filters import GulpIngestionFilter

from gulp_dissect.cli import (
    _passes_ingestion_filter,
    AppConfig,
    ResolvedExtractSpec,
    build_config,
    collect_extract_specs,
    get_app_version,
    ingest_spec,
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
        concurrency=4,
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
    assert cfg.chunk_size == 10_000


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


def test_passes_ingestion_filter_model_extra_string_gte_lte_supports_iso8601_like_values():
    flt = GulpIngestionFilter.model_validate(
        {
            "event.created": {
                "gte": "2024-01-01T00:00:00Z",
                "lte": "2024-01-31T23:59:59Z",
            }
        }
    )

    assert (
        _passes_ingestion_filter({"event.created": "2024-01-15T12:00:00Z"}, flt) is True
    )
    assert (
        _passes_ingestion_filter({"event.created": "2023-12-31T23:59:59Z"}, flt)
        is False
    )
    assert (
        _passes_ingestion_filter({"event.created": "2024-02-01T00:00:00Z"}, flt)
        is False
    )


def test_passes_ingestion_filter_model_extra_string_gte_lte_rejects_mixed_types():
    flt = GulpIngestionFilter.model_validate(
        {
            "event.created": {
                "gte": "2024-01-01T00:00:00Z",
                "lte": "2024-01-31T23:59:59Z",
            }
        }
    )

    assert (
        _passes_ingestion_filter({"event.created": 1704067200000000000}, flt) is False
    )


def test_passes_ingestion_filter_invalid_timestamp_string_is_not_forced_to_zero():
    flt = GulpIngestionFilter.model_validate(
        {"time_range": [1704067200000000000, 1704153600000000000]}
    )

    # Parsing failures should not be treated as timestamp=0, otherwise records
    # could be incorrectly rejected when start > 0.
    assert _passes_ingestion_filter({"@timestamp": "not-a-timestamp"}, flt) is True


def test_resolve_specs_preserves_inline_mapping_parameters():
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mappings": {
                    "m1": {
                        "fields": {
                            "EventCode": {"ecs": ["event.code"]},
                        }
                    }
                },
                "mapping_id": "m1",
            },
        }
    ]

    resolved = _resolve_specs(specs_raw, _cfg())
    assert resolved[0].mapping_id == "m1"
    assert resolved[0].plugin == "evt"
    assert resolved[0].mapping_parameters["mapping_id"] == "m1"
    assert resolved[0].mapping_parameters["mappings"]["m1"]["fields"]["EventCode"][
        "ecs"
    ] == ["event.code"]


def test_resolve_specs_sets_default_excludes_on_every_mapping():
    cfg = _cfg()
    assert parse_args([]).default_excludes is True
    assert parse_args(["--no-default-excludes"]).default_excludes is False
    resolved = _resolve_specs(
        [
            {
                "plugin": "evt",
                "mapping_parameters": {
                    "mappings": {
                        "m1": {"fields": {}},
                        "m2": {"exclude": ["custom", "_source"], "fields": {}},
                    }
                },
            }
        ],
        cfg,
    )

    mappings = resolved[0].mapping_parameters["mappings"]
    defaults = ["_generated", "_version", "_source", "_classification"]
    assert mappings["m1"]["exclude"] == defaults
    assert mappings["m2"]["exclude"] == [
        "custom",
        "_source",
        "_generated",
        "_version",
        "_classification",
    ]


def test_resolve_specs_normalizes_mapping_file_paths(tmp_path: Path):
    base_path = tmp_path / "mappings"
    base_path.mkdir()
    main_file = base_path / "main.json"
    extra_file = base_path / "extra.json"
    main_file.write_text(
        json.dumps(
            {
                "metadata": {"plugin": ["evt"]},
                "mappings": {
                    "m1": {
                        "fields": {
                            "EventCode": {"ecs": ["event.code"]},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    extra_file.write_text(
        json.dumps(
            {
                "metadata": {"plugin": ["evt"]},
                "mappings": {
                    "extra_mapping": {
                        "fields": {
                            "ts": {"ecs": ["@timestamp"]},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = _cfg()
    cfg.mapping_files_base_path = str(base_path)
    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mapping_file": "main.json",
                "mapping_id": "m1",
                "additional_mapping_files": [["extra.json", "extra_mapping"]],
            },
        }
    ]

    resolved = _resolve_specs(specs_raw, cfg)
    assert "mapping_file" not in resolved[0].mapping_parameters
    assert resolved[0].mapping_parameters["additional_mapping_files"] == []
    assert resolved[0].mapping_parameters["mappings"]["m1"]["fields"]["EventCode"] == {
        "ecs": ["event.code"],
        "flatten_json": False,
    }
    assert resolved[0].mapping_parameters["mappings"]["m1"]["fields"]["ts"] == {
        "ecs": ["@timestamp"],
        "flatten_json": False,
    }


def test_resolve_specs_raises_for_missing_relative_mapping_file(tmp_path: Path):
    cfg = _cfg()
    cfg.mapping_files_base_path = str(tmp_path)

    specs_raw = [
        {
            "plugin": "evt",
            "mapping_parameters": {
                "mapping_file": "missing.json",
                "mapping_id": "m1",
            },
        }
    ]

    with pytest.raises(FileNotFoundError, match="missing.json"):
        _resolve_specs(specs_raw, cfg)


def test_normalize_timestamp_to_iso_utc():
    val = normalize_timestamp("2024-01-01 12:00:00+02:00")
    assert val.endswith("Z")
    assert val.startswith("2024-01-01T10:00:00")


class _FakeIngestApi:
    def __init__(self):
        self.calls = []

    async def raw(
        self,
        operation_id,
        plugin_name,
        data,
        params,
        wait_for_worker,
        wait,
        timeout,
    ):
        self.calls.append(
            {
                "operation_id": operation_id,
                "plugin_name": plugin_name,
                "data": list(data),
                "params": params,
                "wait_for_worker": wait_for_worker,
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


class _FakeFullClient:
    def __init__(self):
        self.ingest = _FakeIngestApi()


@pytest.mark.asyncio
async def test_ingest_spec_bounds_parallel_chunks_and_sends_last_after_barrier(
    monkeypatch,
):
    from gulp_dissect import cli as cli_module

    active = 0
    max_active = 0
    completed_nonfinal = 0
    last_flags: list[bool] = []

    class _Ingest:
        async def raw(self, **kwargs):
            nonlocal active, max_active, completed_nonfinal
            last = kwargs["params"]["last"]
            last_flags.append(last)
            active += 1
            max_active = max(max_active, active)
            if last:
                assert completed_nonfinal == 4
            await asyncio.sleep(0)
            active -= 1
            if not last:
                completed_nonfinal += 1
            return SimpleNamespace(status="success")

    monkeypatch.setattr(
        cli_module,
        "iter_plugin_records",
        lambda target, plugin: iter({"id": i} for i in range(5)),
    )
    cfg = _cfg()
    cfg.chunk_size = 1
    cfg.concurrency = 2
    cfg.verbose = True

    ingested = await ingest_spec(
        SimpleNamespace(ingest=_Ingest()),
        cfg,
        target=None,
        spec=ResolvedExtractSpec(plugin="evt", mapping_id="m1"),
    )

    assert ingested == 5
    assert max_active == 2
    assert last_flags == [False, False, False, False, True]


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
        lambda target, plugin: iter(
            [
                {"id": 1, "ts": "2024-01-01T00:00:00Z"},
                {"id": 2, "ts": "2024-01-02T00:00:00Z"},
            ]
        ),
    )

    client = _FakeIngestClient()
    ingested = await ingest_spec(client, cfg, target=None, spec=spec, max_records=None)

    assert ingested == 1
    assert len(client.ingest.calls) == 1
    assert len(client.ingest.calls[0]["data"]) == 1
    assert client.ingest.calls[0]["data"][0]["id"] == 1
    assert client.ingest.calls[0]["data"][0]["gulp.context_id"] == "ctx-fixed"
    assert client.ingest.calls[0]["data"][0]["gulp.source_id"] == "src-fixed"
    assert "flt" not in client.ingest.calls[0]["params"]
    assert "plugin_params" in client.ingest.calls[0]["params"]
    assert client.ingest.calls[0]["params"]["ws_id"] is None
    assert client.ingest.calls[0]["wait_for_worker"] is True


@pytest.mark.asyncio
async def test_ingest_spec_passes_mapping_parameters_for_backend_mapping(monkeypatch):
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

    assert ingested == 1
    assert len(client.ingest.calls) == 1
    assert len(client.ingest.calls[0]["data"]) == 1
    assert client.ingest.calls[0]["data"][0]["ExtraTs"] == "2024-01-02T00:00:00Z"
    mp = client.ingest.calls[0]["params"]["plugin_params"]["mapping_parameters"]
    assert mp["mapping_id"] == "m1"
    assert (
        mp["mappings"]["m1"]["fields"]["ExtraTs"]["extra_doc_with_event_code"]
        == "extra-event"
    )
