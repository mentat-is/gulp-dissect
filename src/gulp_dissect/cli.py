from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from dateutil import parser as dateparser
from dissect.target.plugin import FunctionDescriptor, find_functions
from dissect.target.target import Target
from dissect.target.tools.utils.cli import execute_function_on_target
from gulp_sdk import GulpClient
from tqdm import tqdm


@dataclass
class AppConfig:
    image_path: str
    username: str
    password: str
    gulp_url: str
    operation_id: str
    limit: int
    chunk_size: int
    context_id: str | None
    source_id: str | None
    reset_operation: bool
    verbose: bool


@dataclass
class ResolvedExtractSpec:
    plugin: str
    mapping_id: str
    mapping: dict[str, Any]
    timestamp_fields: list[str]
    timestamp_formats: dict[str, str | None]
    event_code_fields: list[str]
    context_name_fields: list[str]
    source_name_fields: list[str]


def _read_json_arg(raw: str) -> Any:
    text = raw
    if raw.startswith("@"):
        text = Path(raw[1:]).read_text(encoding="utf-8")
    return json.loads(text)


def _env_or_arg(value: str | int | None, env_name: str, default: Any = None) -> Any:
    if value is not None:
        return value
    env_val = os.getenv(env_name)
    if env_val is not None:
        return env_val
    return default


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Extract data from a forensic image with Dissect and ingest mapped "
            "records into gULP via ingest_raw."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--image_path",
        help="absolute path to the forensic disk image to process",
    )
    p.add_argument(
        "--username",
        help="gULP username (or set GULP_DISSECT_USERNAME)",
    )
    p.add_argument(
        "--password",
        help="gULP password (or set GULP_DISSECT_PASSWORD)",
    )
    p.add_argument(
        "--gulp_url",
        help="gULP base URL, e.g. http://localhost:8080 (or set GULP_DISSECT_URL)",
    )
    p.add_argument(
        "--operation_id",
        help="existing gULP operation id where documents will be ingested",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "maximum number of records to ingest across all extract tuples; "
            "0 means no limit"
        ),
    )
    p.add_argument(
        "--chunk_size",
        type=int,
        default=None,
        help="number of mapped records sent per ingest_raw chunk",
    )
    p.add_argument(
        "--context_id",
        help=(
            "explicit existing context id; if omitted, mapping must provide "
            "an is_gulp_type=context_name field"
        ),
    )
    p.add_argument(
        "--source_id",
        help=(
            "explicit existing source id; if omitted, mapping must provide "
            "an is_gulp_type=source_name field"
        ),
    )
    p.add_argument(
        "--reset-operation",
        action="store_true",
        help=(
            "delete and recreate the target operation before ingestion " "(destructive)"
        ),
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "print each mapped GulpDocument as JSON instead of showing the "
            "progress bar"
        ),
    )

    # Tuple form: one plugin + one mapping_parameters, positionally paired.
    # Repeat both flags to run multiple extracts sequentially.
    p.add_argument(
        "--plugin",
        action="append",
        default=[],
        help=(
            "Dissect plugin/function name for one extract tuple; repeat with "
            "--mapping_parameters"
        ),
    )
    p.add_argument(
        "--mapping_parameters",
        action="append",
        default=[],
        help=(
            "JSON object (or @file.json) for one extract tuple mapping_parameters; "
            "must match --plugin occurrences"
        ),
    )

    p.add_argument(
        "--extract_file",
        action="append",
        default=[],
        help=(
            "JSON file containing one extract tuple object or a list of tuple "
            "objects with shape: {plugin, mapping_parameters}; repeatable"
        ),
    )
    return p.parse_args(argv)


def build_config(args: argparse.Namespace) -> AppConfig:
    image_path = args.image_path
    username = _env_or_arg(args.username, "GULP_DISSECT_USERNAME")
    password = _env_or_arg(args.password, "GULP_DISSECT_PASSWORD")
    gulp_url = _env_or_arg(args.gulp_url, "GULP_DISSECT_URL")
    operation_id = args.operation_id
    limit_raw = args.limit if args.limit is not None else 0
    chunk_size_raw = args.chunk_size if args.chunk_size is not None else 1000
    context_id = args.context_id
    source_id = args.source_id
    reset_operation_raw = args.reset_operation

    required = {
        "image_path": image_path,
        "username": username,
        "password": password,
        "gulp_url": gulp_url,
        "operation_id": operation_id,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"Missing required parameter(s): {', '.join(missing)}")

    limit = int(limit_raw)
    if limit < 0:
        raise ValueError("limit must be >= 0")

    chunk_size = int(chunk_size_raw)
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")

    return AppConfig(
        image_path=str(image_path),
        username=str(username),
        password=str(password),
        gulp_url=str(gulp_url),
        operation_id=str(operation_id),
        limit=limit,
        chunk_size=chunk_size,
        context_id=str(context_id) if context_id else None,
        source_id=str(source_id) if source_id else None,
        reset_operation=str(reset_operation_raw).lower() in {"1", "true", "yes", "on"},
        verbose=bool(args.verbose),
    )


def collect_extract_specs(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Collect extract tuples from repeated CLI pairs and/or JSON files."""

    specs: list[dict[str, Any]] = []

    if args.plugin or args.mapping_parameters:
        if len(args.plugin) != len(args.mapping_parameters):
            raise ValueError(
                "--plugin and --mapping_parameters must be provided the same number of times"
            )
        for plugin, mapping_raw in zip(
            args.plugin, args.mapping_parameters, strict=True
        ):
            specs.append(
                {"plugin": plugin, "mapping_parameters": _read_json_arg(mapping_raw)}
            )

    for extract_file in args.extract_file:
        # Accept either one tuple object or a list of tuple objects in one file.
        loaded = json.loads(Path(extract_file).read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            specs.extend(loaded)
        else:
            specs.append(loaded)

    if not specs:
        raise ValueError("At least one extract tuple is required")

    return specs


def _select_mapping(mapping_parameters: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    has_file = bool(mapping_parameters.get("mapping_file"))
    has_mappings = bool(mapping_parameters.get("mappings"))
    if has_file == has_mappings:
        raise ValueError(
            "mapping_parameters must define exactly one of mapping_file or mappings"
        )

    mappings: dict[str, Any]
    if has_file:
        mapping_file = Path(str(mapping_parameters["mapping_file"]))
        if not mapping_file.exists():
            raise ValueError(f"mapping_file not found: {mapping_file}")
        loaded = json.loads(mapping_file.read_text(encoding="utf-8"))
        mappings = loaded.get("mappings", loaded)
    else:
        mappings = mapping_parameters["mappings"]

    if not isinstance(mappings, dict) or not mappings:
        raise ValueError("resolved mappings must be a non-empty object")

    mapping_id = mapping_parameters.get("mapping_id") or next(iter(mappings.keys()))
    if mapping_id not in mappings:
        raise ValueError(f"mapping_id '{mapping_id}' not found in mappings")

    mapping = mappings[mapping_id]
    if not isinstance(mapping, dict):
        raise ValueError("selected mapping must be an object")

    return str(mapping_id), deepcopy(mapping)


def _ecs_contains(field_mapping: dict[str, Any], ecs_name: str) -> bool:
    ecs = field_mapping.get("ecs")
    if isinstance(ecs, str):
        return ecs == ecs_name
    if isinstance(ecs, list):
        return ecs_name in ecs
    return False


def _normalize_mapping(
    mapping_id: str,
    mapping: dict[str, Any],
    context_id: str | None,
    source_id: str | None,
) -> ResolvedExtractSpec:
    """Validate one mapping block and precompute the fields needed during transformation."""

    fields = mapping.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError(
            f"mapping '{mapping_id}' must contain a non-empty fields object"
        )

    timestamp_fields: list[str] = []
    timestamp_formats: dict[str, str | None] = {}
    event_code_fields: list[str] = []
    context_name_fields: list[str] = []
    source_name_fields: list[str] = []

    for source_field, raw_field_mapping in fields.items():
        if not isinstance(raw_field_mapping, dict):
            continue
        if _ecs_contains(raw_field_mapping, "@timestamp"):
            timestamp_fields.append(source_field)
            timestamp_formats[source_field] = raw_field_mapping.get("timestamp_format")
        if _ecs_contains(raw_field_mapping, "event.code"):
            event_code_fields.append(source_field)
        field_type = raw_field_mapping.get("is_gulp_type")
        if field_type == "context_name":
            context_name_fields.append(source_field)
        if field_type == "source_name":
            source_name_fields.append(source_field)

    # Compatibility default: map @timestamp to ts if not explicitly set and ts exists.
    if not timestamp_fields and "ts" in fields:
        ts_mapping = fields.get("ts")
        if isinstance(ts_mapping, dict):
            ecs = ts_mapping.get("ecs")
            if ecs is None:
                ts_mapping["ecs"] = ["@timestamp"]
            elif isinstance(ecs, str):
                if ecs != "@timestamp":
                    ts_mapping["ecs"] = [ecs, "@timestamp"]
            elif isinstance(ecs, list) and "@timestamp" not in ecs:
                ecs.append("@timestamp")
            timestamp_fields.append("ts")
            timestamp_formats["ts"] = ts_mapping.get("timestamp_format")

    if not timestamp_fields:
        raise ValueError(
            f"mapping '{mapping_id}' is missing @timestamp mapping (and no fallback field 'ts' is available)"
        )
    if not event_code_fields:
        raise ValueError(f"mapping '{mapping_id}' is missing event.code mapping")

    if context_id is None and not context_name_fields:
        raise ValueError(
            f"mapping '{mapping_id}' must define a field with is_gulp_type='context_name' when --context_id is not provided"
        )
    if source_id is None and not source_name_fields:
        raise ValueError(
            f"mapping '{mapping_id}' must define a field with is_gulp_type='source_name' when --source_id is not provided"
        )

    return ResolvedExtractSpec(
        plugin="",
        mapping_id=mapping_id,
        mapping=mapping,
        timestamp_fields=timestamp_fields,
        timestamp_formats=timestamp_formats,
        event_code_fields=event_code_fields,
        context_name_fields=context_name_fields,
        source_name_fields=source_name_fields,
    )


def resolve_specs(
    specs_raw: list[dict[str, Any]], cfg: AppConfig
) -> list[ResolvedExtractSpec]:
    resolved: list[ResolvedExtractSpec] = []
    for idx, spec in enumerate(specs_raw, start=1):
        if not isinstance(spec, dict):
            raise ValueError(f"extract spec #{idx} must be an object")
        plugin = spec.get("plugin")
        if not plugin:
            raise ValueError(f"extract spec #{idx} is missing plugin")

        mapping_parameters = spec.get("mapping_parameters")
        if not isinstance(mapping_parameters, dict):
            raise ValueError(
                f"extract spec #{idx} is missing mapping_parameters object"
            )

        mapping_id, mapping = _select_mapping(mapping_parameters)
        normalized = _normalize_mapping(
            mapping_id, mapping, cfg.context_id, cfg.source_id
        )
        normalized.plugin = str(plugin)
        resolved.append(normalized)

    return resolved


def normalize_timestamp(value: Any, timestamp_format: str | None = None) -> str:
    """Normalize supported timestamp inputs into the UTC ISO8601 format required by gULP."""

    dt: datetime

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        # Heuristic for unix timestamps: seconds/ms/us/ns.
        abs_v = abs(float(value))
        if abs_v >= 1e18:
            dt = datetime.fromtimestamp(float(value) / 1e9, tz=timezone.utc)
        elif abs_v >= 1e15:
            dt = datetime.fromtimestamp(float(value) / 1e6, tz=timezone.utc)
        elif abs_v >= 1e12:
            dt = datetime.fromtimestamp(float(value) / 1e3, tz=timezone.utc)
        else:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        if timestamp_format:
            dt = datetime.strptime(value, timestamp_format)
        else:
            dt = dateparser.parse(value)
    else:
        raise ValueError(f"Unsupported timestamp value type: {type(value)!r}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    # Keep millisecond precision and force UTC Z suffix.
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _flatten_json_value(value: Any) -> dict[str, Any]:
    """Flatten a JSON object so nested keys can be emitted as dotted document fields."""

    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("flatten_json requires a JSON object value")

    flattened: dict[str, Any] = {}

    def _walk(prefix: str, current: Any) -> None:
        if isinstance(current, dict):
            for key, child in current.items():
                next_prefix = f"{prefix}.{key}" if prefix else str(key)
                _walk(next_prefix, child)
            return
        flattened[prefix] = current

    _walk("", value)
    return flattened


def _convert_special_timestamp(value: Any, field_mapping: dict[str, Any]) -> str:
    """Handle mapping-specific timestamp encodings before writing @timestamp."""

    timestamp_kind = field_mapping.get("is_timestamp")
    timestamp_format = field_mapping.get("timestamp_format")

    if timestamp_kind == "windows_filetime":
        base = datetime(1601, 1, 1, tzinfo=timezone.utc)
        dt = base + timedelta(microseconds=int(value) / 10)
        return normalize_timestamp(dt)
    if timestamp_kind == "chrome":
        base = datetime(1601, 1, 1, tzinfo=timezone.utc)
        dt = base + timedelta(microseconds=float(value))
        return normalize_timestamp(dt)
    if timestamp_kind == "generic":
        return normalize_timestamp(value, timestamp_format)
    return normalize_timestamp(value, timestamp_format)


def _transform_scalar(value: Any, field_mapping: dict[str, Any]) -> Any:
    """Apply lightweight scalar transforms declared in the mapping for one source field."""

    transformed = value

    if field_mapping.get("multiplier") is not None and transformed is not None:
        transformed = float(transformed) * float(field_mapping["multiplier"])

    force_type = field_mapping.get("force_type")
    if force_type == "int":
        transformed = int(transformed)
    elif force_type == "float":
        transformed = float(transformed)
    elif force_type == "str":
        transformed = str(transformed)

    return transformed


def _should_process_source_field(mapping: dict[str, Any], source_field: str) -> bool:
    """Apply mapping-level include/exclude rules to one source field name."""

    include_fields = mapping.get("include") or []
    exclude_fields = mapping.get("exclude") or []

    if include_fields and source_field not in include_fields:
        return False
    if source_field in exclude_fields:
        return False
    return True


def _should_preserve_source_field(mapping: dict[str, Any], source_field: str) -> bool:
    """Keep only unmapped source fields in the preserved raw payload."""

    opensearch_metadata_fields = {
        "_id",
        "_ignored",
        "_index",
        "_primary_term",
        "_routing",
        "_seq_no",
        "_source",
        "_type",
        "_version",
    }
    mapped_fields = mapping.get("fields") or {}

    if not _should_process_source_field(mapping, source_field):
        return False
    if source_field in opensearch_metadata_fields:
        return False
    if source_field in mapped_fields:
        return False
    return True


async def _ensure_context_id(
    client: GulpClient,
    operation_id: str,
    context_name: str,
    cache: dict[str, str],
) -> str:
    """Create or fetch a context once and cache its ID for subsequent records."""

    if context_name in cache:
        return cache[context_name]
    context = await client.operations.context_create(operation_id, context_name)
    context_id = str(context["id"])
    cache[context_name] = context_id
    return context_id


async def _ensure_source_id(
    client: GulpClient,
    operation_id: str,
    context_id: str,
    source_name: str,
    cache: dict[tuple[str, str], str],
) -> str:
    """Create or fetch a source once per (context, source_name) tuple."""

    key = (context_id, source_name)
    if key in cache:
        return cache[key]
    source = await client.operations.source_create(
        operation_id=operation_id,
        context_id=context_id,
        source_name=source_name,
        plugin="raw",
    )
    source_id = str(source["id"])
    cache[key] = source_id
    return source_id


async def map_record_to_gulp_document(
    client: GulpClient,
    cfg: AppConfig,
    spec: ResolvedExtractSpec,
    raw_record: dict[str, Any],
    event_sequence: int,
    context_cache: dict[str, str],
    source_cache: dict[tuple[str, str], str],
) -> dict[str, Any]:
    """Convert one Dissect record into a raw GulpDocument payload.

    The raw plugin does not apply arbitrary source mappings for us, so the CLI must
    preserve the original Dissect fields, materialize ECS targets locally, and resolve
    context/source names into stable IDs before upload.
    """

    fields = spec.mapping.get("fields", {})
    # Start from the full original record so unmapped Dissect fields are still present
    # in the final GulpDocument, but still honor mapping include/exclude rules.
    mapped: dict[str, Any] = {
        key: deepcopy(value)
        for key, value in raw_record.items()
        if _should_preserve_source_field(spec.mapping, key)
    }
    raw_record.pop(
        "_generated", None
    )  # either it will generated different documents depending on ingestion time
    mapped.update(
        {
            "event.original": json.dumps(raw_record, sort_keys=True, default=str),
            "event.sequence": event_sequence,
            "agent.type": spec.mapping.get("agent_type") or spec.plugin,
        }
    )

    context_id = cfg.context_id
    source_id = cfg.source_id
    context_name = spec.mapping.get("default_context") if context_id is None else None
    source_name = spec.mapping.get("default_source") if source_id is None else None

    for source_field, field_mapping in fields.items():
        if (
            source_field not in raw_record
            or not isinstance(field_mapping, dict)
            or not _should_process_source_field(spec.mapping, source_field)
        ):
            continue

        raw_value = raw_record[source_field]
        if raw_value is None:
            continue

        gulp_type = field_mapping.get("is_gulp_type")
        transformed = _transform_scalar(raw_value, field_mapping)

        if field_mapping.get("flatten_json"):
            mapped.update(_flatten_json_value(transformed))

        if gulp_type == "context_id" and context_id is None:
            context_id = str(transformed)
        elif gulp_type == "context_name" and context_id is None:
            context_name = str(transformed)
        elif gulp_type == "source_id" and source_id is None:
            source_id = str(transformed)
        elif gulp_type == "source_name" and source_id is None:
            source_name = str(transformed)

        ecs_targets = field_mapping.get("ecs")
        if isinstance(ecs_targets, str):
            ecs_targets = [ecs_targets]
        if not ecs_targets:
            continue

        for ecs_field in ecs_targets:
            if ecs_field == "@timestamp":
                mapped[ecs_field] = _convert_special_timestamp(raw_value, field_mapping)
            else:
                mapped[ecs_field] = transformed

    if "@timestamp" not in mapped:
        raise ValueError(f"record missing mapped @timestamp for plugin '{spec.plugin}'")

    event_code = mapped.get("event.code") or spec.mapping.get("event_code")
    if event_code is None:
        raise ValueError(f"record missing mapped event.code for plugin '{spec.plugin}'")
    mapped["event.code"] = str(event_code)

    if context_id is None:
        if context_name is None:
            raise ValueError(
                "Unable to resolve gulp.context_id: missing context_id and context_name"
            )
        context_id = await _ensure_context_id(
            client, cfg.operation_id, context_name, context_cache
        )

    if source_id is None:
        if source_name is None:
            raise ValueError(
                "Unable to resolve gulp.source_id: missing source_id and source_name"
            )
        source_id = await _ensure_source_id(
            client, cfg.operation_id, context_id, source_name, source_cache
        )

    mapped["gulp.context_id"] = context_id
    mapped["gulp.source_id"] = source_id

    return mapped


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]
    return str(value)


def record_to_dict(record: Any) -> dict[str, Any]:
    """Convert a Dissect record object into a plain JSON-safe dictionary."""

    if isinstance(record, dict):
        return _to_jsonable(record)
    if hasattr(record, "_asdict"):
        return _to_jsonable(record._asdict())
    if hasattr(record, "__dict__"):
        data = {k: v for k, v in vars(record).items() if not k.startswith("_")}
        return _to_jsonable(data)
    raise ValueError(f"Unsupported record type: {type(record)!r}")


def _pick_function(plugin_name: str, target: Target) -> FunctionDescriptor:
    """Resolve the requested Dissect plugin/function name to one executable descriptor."""

    descriptors, invalid = find_functions(plugin_name, target)
    if invalid and not descriptors:
        raise ValueError(f"No dissect plugin function found for '{plugin_name}'")
    if not descriptors:
        raise ValueError(f"No dissect plugin function found for '{plugin_name}'")

    for desc in descriptors:
        if desc.name == plugin_name:
            return desc
    for desc in descriptors:
        if desc.path.endswith(f".{plugin_name}"):
            return desc
    return descriptors[0]


def iter_plugin_records(target: Target, plugin_name: str) -> Iterable[dict[str, Any]]:
    """Yield normalized record dictionaries from one Dissect plugin stream."""

    descriptor = _pick_function(plugin_name, target)
    output_type, result = execute_function_on_target(target, descriptor, [])
    if output_type != "record":
        raise ValueError(
            f"Dissect plugin '{plugin_name}' produced output type '{output_type}', expected 'record'"
        )

    for record in result:
        yield record_to_dict(record)


def _make_progress_bar(spec: ResolvedExtractSpec) -> tqdm:
    """Create an indeterminate progress bar for one plugin stream."""

    return tqdm(
        desc=f"{spec.plugin}:{spec.mapping_id}",
        unit="event",
        dynamic_ncols=True,
        leave=True,
    )


async def ingest_spec(
    client: GulpClient,
    cfg: AppConfig,
    target: Target,
    spec: ResolvedExtractSpec,
    max_records: int | None = None,
) -> int:
    """Extract, transform, and ingest one Dissect plugin stream in raw chunks."""

    req_id = str(uuid.uuid4())
    chunk: list[dict[str, Any]] = []
    total = 0
    context_cache: dict[str, str] = {}
    source_cache: dict[tuple[str, str], str] = {}
    progress = None if cfg.verbose else _make_progress_bar(spec)

    async def _flush(last: bool) -> None:
        # Keep request tracking stable across all chunks for this extract spec.
        if not chunk:
            return
        result = await client.ingest.raw(
            operation_id=cfg.operation_id,
            plugin_name="raw",
            data=chunk,
            params={
                "req_id": req_id,
                "last": last,
            },
            wait=last,
            timeout=3600,
        )
        status = str(getattr(result, "status", "")).lower()
        if status in {"failed", "canceled", "error"}:
            raise RuntimeError(
                f"ingest_raw request failed for plugin '{spec.plugin}' with status='{status}', req_id='{req_id}'"
            )
        chunk.clear()

    try:
        for raw_record in iter_plugin_records(target, spec.plugin):
            if max_records is not None and total >= max_records:
                break

            total += 1
            mapped_record = await map_record_to_gulp_document(
                client,
                cfg,
                spec,
                raw_record,
                total,
                context_cache,
                source_cache,
            )
            if cfg.verbose:
                print(json.dumps(mapped_record, sort_keys=True, default=str))
            elif progress is not None:
                progress.update(1)
            chunk.append(mapped_record)
            if len(chunk) >= cfg.chunk_size:
                await _flush(last=False)

        if chunk:
            await _flush(last=True)
    finally:
        if progress is not None:
            progress.close()

    return total


async def _reset_operation(client: GulpClient, operation_id: str) -> None:
    """Clear (recreate) operation"""

    await client.operations.delete(operation_id, force=True)
    await client.operations.create(name=operation_id)


async def run(cfg: AppConfig, specs: list[ResolvedExtractSpec]) -> None:
    """Run the full extraction workflow for all requested plugin/mapping tuples."""

    target = Target.open(cfg.image_path)

    async with GulpClient(cfg.gulp_url) as client:
        await client.auth.login(cfg.username, cfg.password)
        await client.ensure_websocket()
        try:
            remaining_limit: int | None = cfg.limit if cfg.limit > 0 else None

            if cfg.reset_operation:
                print(f"[gulp-dissect] clearing operation='{cfg.operation_id}'")
                await _reset_operation(client, cfg.operation_id)

            for spec in specs:
                if remaining_limit is not None and remaining_limit <= 0:
                    print(
                        f"[gulp-dissect] limit reached ({cfg.limit}), skipping remaining extract specs"
                    )
                    break

                print(
                    f"[gulp-dissect] extracting plugin='{spec.plugin}' mapping_id='{spec.mapping_id}'"
                )
                ingested = await ingest_spec(
                    client,
                    cfg,
                    target,
                    spec,
                    max_records=remaining_limit,
                )
                print(
                    f"[gulp-dissect] plugin='{spec.plugin}' ingested records={ingested}"
                )

                if remaining_limit is not None:
                    remaining_limit -= ingested
        finally:
            try:
                await client.auth.logout()
            except Exception as exc:
                print(f"[gulp-dissect] warning: logout failed: {exc}")


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        cfg = build_config(args)
        specs_raw = collect_extract_specs(args)
        specs = resolve_specs(specs_raw, cfg)
        asyncio.run(run(cfg, specs))
        return 0
    except Exception as exc:
        print(f"[gulp-dissect] error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
