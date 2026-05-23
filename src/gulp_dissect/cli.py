"""Command-line entry point for extracting with Dissect and ingesting into gULP.

This module implements the full `gulp-dissect` runtime pipeline:

1. Parse CLI arguments and construct a validated runtime configuration.
2. Resolve one or more extract tuple specifications (`plugin`, `mapping_parameters`)
     using gULP's own mapping-resolution utilities.
3. Open a Dissect target image and stream records for each requested plugin.
4. Transform each Dissect record into a gULP-compatible raw document by applying
     mapping rules, timestamp normalization, type coercion, ECS projection, and
     context/source resolution.
5. Optionally filter mapped documents client-side via `GulpIngestionFilter`.
6. Send accepted documents to `/ingest_raw` in bounded chunks.

Design notes:

- Mapping parsing intentionally delegates to `mapping_parameters_to_mapping` so
    behavior stays aligned with the gULP backend.
- Client-side filtering is performed before ingestion API calls and is not
    forwarded inside `plugin_params`.
- The CLI supports multiple extract tuples and enforces a global ingestion limit
    (`--limit`) across all tuples in sequence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any, Iterable

import art
from dissect.target.plugin import FunctionDescriptor, find_functions
from dissect.target.target import Target
from dissect.target.tools.utils.cli import execute_function_on_target
from muty.log import MutyLogger
import muty.time
from gulp.api.mapping.mapping_utils import (
    apply_value_aliases,
    convert_special_timestamp,
    flatten_json_value,
    mapping_attr,
    mapping_parameters_to_mapping,
    normalize_timestamp,
    transform_scalar,
)
from gulp.api.opensearch.filters import GulpIngestionFilter
from gulp_sdk import GulpClient
from tqdm import tqdm

from gulp.structs import GulpMappingParameters

# ---------------------------------------------------------------------------
# Data classes for configuration and resolved extract specifications.
# ---------------------------------------------------------------------------


@dataclass
class AppConfig:
    """Runtime configuration for a `gulp-dissect` execution.

    The values in this dataclass are fully normalized and validated by
    :func:`build_config` before any extraction starts.

    Attributes:
        image_path: Absolute or container-local path to the forensic image.
        username: gULP login username.
        password: gULP login password.
        gulp_url: Base URL of the gULP server.
        operation_id: Target operation identifier where documents are ingested.
        limit: Global maximum number of accepted documents to ingest; `0` means
            unlimited and is translated to `None` during runtime.
        chunk_size: Maximum documents sent per `/ingest_raw` request.
        context_id: Optional explicit context id override.
        source_id: Optional explicit source id override.
        flt: Optional client-side ingestion filter (`GulpIngestionFilter`).
        reset_operation: Whether to delete/recreate the operation before ingest.
        verbose: Whether to print mapped documents instead of showing progress.
    """

    image_path: str
    username: str
    password: str
    gulp_url: str
    operation_id: str
    limit: int
    chunk_size: int
    context_id: str | None
    source_id: str | None
    flt: GulpIngestionFilter | None
    reset_operation: bool
    verbose: bool


@dataclass
class ResolvedExtractSpec:
    """Validated extract tuple used by the extraction/ingestion loop.

    A `ResolvedExtractSpec` is produced from user-provided tuple inputs after
    mapping resolution and static validation. Precomputed field lists are stored
    for downstream logic and diagnostics.
    """

    plugin: str
    mapping_id: str
    mapping: dict[str, Any]
    timestamp_fields: list[str]
    timestamp_formats: dict[str, str | None]
    event_code_fields: list[str]
    context_name_fields: list[str]
    source_name_fields: list[str]
    mapping_parameters: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Argument parsing and configuration helpers.
# ---------------------------------------------------------------------------


def _read_json_arg(raw: str) -> Any:
    """Parse JSON input from a string or reference file.

    If the argument starts with `@`, the remainder is treated as a file path and
    file contents are parsed as JSON. Otherwise, the argument itself is parsed as
    an inline JSON string.

    Args:
        raw: Raw CLI value.

    Returns:
        Parsed JSON value.

    Raises:
        FileNotFoundError: If `@path` points to a missing file.
        json.JSONDecodeError: If content is not valid JSON.
    """
    text = raw
    if raw.startswith("@"):
        text = Path(raw[1:]).read_text(encoding="utf-8")
    return json.loads(text)


def _env_or_arg(value: str | int | None, env_name: str, default: Any = None) -> Any:
    """Resolve a setting by precedence: CLI arg -> env var -> default.

    Args:
        value: Parsed CLI argument value.
        env_name: Environment variable name used as fallback.
        default: Default value used when both arg and env are unset.

    Returns:
        The resolved value from highest-precedence source.
    """
    if value is not None:
        return value
    env_val = os.getenv(env_name)
    if env_val is not None:
        return env_val
    return default


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Define and parse CLI arguments for the `gulp-dissect` command.

    Args:
        argv: Optional argument vector for tests or programmatic invocation.

    Returns:
        Parsed argparse namespace.
    """
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
        "--flt",
        help=(
            "optional GulpIngestionFilter JSON object applied "
            "client-side before ingest_raw calls"
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
    """Build and validate normalized runtime configuration.

    This function centralizes configuration validation, including required
    argument checks, numeric bound checks, and `--flt` schema validation through
    `GulpIngestionFilter`.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Fully validated :class:`AppConfig`.

    Raises:
        ValueError: For missing required parameters, invalid ranges, or malformed
            filter configuration.
    """
    image_path = args.image_path
    username = _env_or_arg(args.username, "GULP_DISSECT_USERNAME")
    password = _env_or_arg(args.password, "GULP_DISSECT_PASSWORD")
    gulp_url = _env_or_arg(args.gulp_url, "GULP_DISSECT_URL")
    operation_id = args.operation_id
    limit_raw = args.limit if args.limit is not None else 0
    chunk_size_raw = args.chunk_size if args.chunk_size is not None else 1000
    context_id = args.context_id
    source_id = args.source_id
    flt_raw = args.flt
    reset_operation_raw = args.reset_operation

    flt: GulpIngestionFilter | None = None
    if flt_raw is not None:
        parsed_flt = _read_json_arg(flt_raw)
        if not isinstance(parsed_flt, dict):
            raise ValueError("flt must be a JSON object")
        try:
            flt = GulpIngestionFilter.model_validate(parsed_flt)
        except Exception as exc:
            raise ValueError(f"invalid flt: {exc}") from exc

    # Required runtime settings must be present either as CLI args or env vars.
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
        flt=flt,
        reset_operation=str(reset_operation_raw).lower() in {"1", "true", "yes", "on"},
        verbose=bool(args.verbose),
    )


def collect_extract_specs(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Collect extract tuple payloads from CLI pair flags and/or JSON files.

    Supported forms:

    - repeated `--plugin` + `--mapping_parameters` pairs (1:1)
    - one or more `--extract_file` JSON files containing one tuple object or a
      list of tuple objects.

    Args:
        args: Parsed CLI arguments.

    Returns:
        List of raw tuple dictionaries with keys `plugin` and
        `mapping_parameters`.

    Raises:
        ValueError: If pair flags are unbalanced or no specs are provided.
    """

    specs: list[dict[str, Any]] = []

    if args.plugin or args.mapping_parameters:
        # The plugin and mapping_parameters flags must be paired 1:1.
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
        # Accept either a single tuple object or a list of tuple objects.
        loaded = json.loads(Path(extract_file).read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            specs.extend(loaded)
        else:
            specs.append(loaded)

    if not specs:
        raise ValueError("At least one extract tuple is required")

    return specs


def _ecs_contains(field_mapping: dict[str, Any], ecs_name: str) -> bool:
    """Return whether a mapping field routes to the requested ECS destination.

    The `ecs` attribute can be either a single string or a list of strings.
    """
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
    """Validate mapping requirements and derive routing-related metadata.

    Validation rules enforced here are runtime-critical and intentionally strict:

    - mapping must define a non-empty `fields` object
    - a mapping to `@timestamp` must exist (with fallback injection to `ts`)
    - a mapping to `event.code` must exist
    - when CLI overrides are absent, mapping must provide
      `is_gulp_type=context_name` and `is_gulp_type=source_name`

    Args:
        mapping_id: Mapping identifier selected from resolved mappings.
        mapping: Mapping dictionary as produced by gULP model dump.
        context_id: Optional explicit context override from CLI.
        source_id: Optional explicit source override from CLI.

    Returns:
        Normalized specification with precomputed field metadata.

    Raises:
        ValueError: If mapping structure or required semantic contracts are
            invalid.
    """

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

    if not timestamp_fields:
        # default mapping of @timestamp to "ts" if no other timestamp mapping is provided
        ts_mapping = fields.setdefault("ts", {})
        if not isinstance(ts_mapping, dict):
            ts_mapping = {}
            fields["ts"] = ts_mapping

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
        raise ValueError(f"mapping '{mapping_id}' is missing @timestamp mapping")
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


async def resolve_specs(
    specs_raw: list[dict[str, Any]], cfg: AppConfig
) -> list[ResolvedExtractSpec]:
    """Resolve user tuple inputs into validated, normalized extract specs.

    Mapping resolution is delegated to gULP's
    `mapping_parameters_to_mapping` helper to guarantee parity with backend
    behavior for inline mappings, mapping files, and additional mapping imports.

    Args:
        specs_raw: Raw tuple dictionaries collected from CLI/file inputs.
        cfg: Runtime configuration, used for context/source validation rules.

    Returns:
        List of validated :class:`ResolvedExtractSpec` entries.

    Raises:
        ValueError: If tuple format, plugin, or mapping payloads are invalid.
    """
    resolved: list[ResolvedExtractSpec] = []
    for idx, spec in enumerate(specs_raw, start=1):
        if not isinstance(spec, dict):
            raise ValueError(f"extract spec #{idx} must be an object")
        plugin = spec.get("plugin")
        if not plugin:
            raise ValueError(f"extract spec #{idx} is missing plugin")

        mp_dict = spec.get("mapping_parameters")
        if not isinstance(mp_dict, dict):
            raise ValueError(
                f"extract spec #{idx} is missing mapping_parameters object"
            )

        # Parse mappings using the same helper as gULP so inline mappings and
        # mapping file inputs resolve with identical rules.
        mapping_parameters = GulpMappingParameters.model_validate(mp_dict)
        resolved_mappings, mapping_id = await mapping_parameters_to_mapping(
            mapping_parameters
        )
        mapping = resolved_mappings[mapping_id].model_dump()

        normalized = _normalize_mapping(
            mapping_id, mapping, cfg.context_id, cfg.source_id
        )
        normalized.plugin = str(plugin)
        normalized.mapping_parameters = deepcopy(mp_dict)
        resolved.append(normalized)

    return resolved


def _should_process_source_field(mapping: dict[str, Any], source_field: str) -> bool:
    """Apply mapping include/exclude constraints to one source field name."""

    include_fields = mapping.get("include") or []
    exclude_fields = mapping.get("exclude") or []

    if include_fields and source_field not in include_fields:
        return False
    if source_field in exclude_fields:
        return False
    return True


def _should_preserve_source_field(mapping: dict[str, Any], source_field: str) -> bool:
    """Decide whether an original source field should be preserved in output.

    Preservation is intended for unmapped payload context while avoiding:

    - OpenSearch metadata keys
    - fields explicitly mapped to ECS/gULP targets
    - fields removed by include/exclude constraints
    """

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
    """Resolve a context id from context name with in-run memoization.

    Context creation in gULP is idempotent for existing names under operation,
    but memoization avoids duplicate API calls during large ingest loops.
    """

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
    """Resolve a source id from source name with per-context memoization."""

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
    """Map one Dissect record into a gULP raw document payload.

    This performs all per-record transformations required for ingest compatibility:

    - preserve selected unmapped fields
    - generate `event.original` and `event.sequence`
    - map source fields to ECS targets
    - coerce/transform values according to mapping directives
    - normalize timestamp output
    - apply value aliases
    - resolve/assign `gulp.context_id` and `gulp.source_id`

    Args:
        client: Active gULP client used for context/source resolution.
        cfg: Runtime configuration.
        spec: Validated mapping specification for this plugin stream.
        raw_record: Source Dissect record as plain dictionary.
        event_sequence: Sequence value assigned to `event.sequence`.
        context_cache: In-memory cache for context id lookups.
        source_cache: In-memory cache for source id lookups.

    Returns:
        JSON-serializable mapped document dictionary.

    Raises:
        ValueError: If required mapped values cannot be resolved.
    """

    fields = spec.mapping.get("fields", {})
    # Preserve unmapped original fields while still filtering source metadata and
    # respecting include/exclude rules. This keeps event.original informative.
    mapped: dict[str, Any] = {
        key: deepcopy(value)
        for key, value in raw_record.items()
        if _should_preserve_source_field(spec.mapping, key)
    }

    # Remove extraction-only metadata from the canonical event.original payload.
    raw_record.pop("_generated", None)
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
    value_aliases = spec.mapping.get("value_aliases") or {}

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
        transformed = transform_scalar(
            raw_value,
            force_type=mapping_attr(field_mapping, "force_type"),
            multiplier=mapping_attr(field_mapping, "multiplier"),
        )

        if mapping_attr(field_mapping, "flatten_json"):
            mapped.update(flatten_json_value(transformed))

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
                mapped[ecs_field] = convert_special_timestamp(
                    raw_value,
                    timestamp_kind=mapping_attr(field_mapping, "is_timestamp"),
                    timestamp_format=mapping_attr(field_mapping, "timestamp_format"),
                    output="iso8601",
                )
            else:
                mapped[ecs_field] = transformed

            # Apply aliases to each mapped ECS field using the same helper as gULP.
            if value_aliases:
                alias_target = {ecs_field: mapped[ecs_field]}
                apply_value_aliases(ecs_field, alias_target, value_aliases)
                mapped[ecs_field] = alias_target[ecs_field]

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
    """Recursively convert arbitrary values into JSON-safe primitives.

    This utility is used when normalizing Dissect records that may expose custom
    objects, bytes, sets, or datetimes.
    """
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
    """Convert a Dissect record container into a plain dictionary.

    Supported record shapes:

    - dictionaries
    - namedtuple-like objects exposing `_asdict()`
    - objects exposing `__dict__` (excluding private attributes)

    Raises:
        ValueError: If record type cannot be converted safely.
    """

    if isinstance(record, dict):
        return _to_jsonable(record)
    if hasattr(record, "_asdict"):
        return _to_jsonable(record._asdict())
    if hasattr(record, "__dict__"):
        data = {k: v for k, v in vars(record).items() if not k.startswith("_")}
        return _to_jsonable(data)
    raise ValueError(f"Unsupported record type: {type(record)!r}")


def _passes_ingestion_filter(
    mapped_record: dict[str, Any],
    flt: GulpIngestionFilter | None,
) -> bool:
    """Evaluate one mapped record against client-side ingestion filtering rules.

    Filter contract:

    - if no filter is provided, every document is accepted
    - if `storage_ignore_filter` is true, every document is accepted
    - all configured conditions are combined as logical AND
    - `time_range` is applied to `gulp.timestamp` when present, otherwise to a
      converted `@timestamp`
    - extra filter keys (`model_extra`) support:
      - string equality: `{ "field": "value" }`
      - numeric equality: `{ "field": 42 }`
      - numeric range: `{ "field": {"gte": 10, "lte": 20} }`

    Args:
        mapped_record: Fully mapped document candidate.
        flt: Optional parsed ingestion filter.

    Returns:
        `True` when record should be ingested, otherwise `False`.
    """

    def _is_number(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def _matches_model_extra(value: Any, condition: Any) -> bool:
        # string comparison: strict equality
        if isinstance(condition, str):
            return isinstance(value, str) and value == condition

        # number comparison: direct equality
        if _is_number(condition):
            return _is_number(value) and value == condition

        # number comparison with explicit operators
        if isinstance(condition, dict):
            if not _is_number(value):
                return False

            allowed = {"gte", "lte"}
            if not condition or any(k not in allowed for k in condition):
                return False

            for op, op_value in condition.items():
                if not _is_number(op_value):
                    return False
                if op == "gte" and value < op_value:
                    return False
                if op == "lte" and value > op_value:
                    return False

            return True

        return False

    if flt is None or flt.storage_ignore_filter:
        return True

    # 1) apply time_range when present
    if flt.time_range and len(flt.time_range) == 2:
        start, end = flt.time_range
        if not (start == 0 and end == 0):
            ts_nanos = mapped_record.get("gulp.timestamp")
            if not isinstance(ts_nanos, int):
                try:
                    ts_nanos = muty.time.string_to_nanos_from_unix_epoch(
                        mapped_record.get("@timestamp"),
                        throw_on_invalid=True,
                    )
                except Exception:
                    ts_nanos = None
            if ts_nanos is not None:
                if start > 0 and ts_nanos < start:
                    return False
                if end > 0 and ts_nanos > end:
                    return False

    # 2) apply extra field filters from model_extra
    extras = flt.model_extra or {}
    for field_name, condition in extras.items():
        if field_name not in mapped_record:
            return False
        if not _matches_model_extra(mapped_record[field_name], condition):
            return False

    return True


def _pick_function(plugin_name: str, target: Target) -> FunctionDescriptor:
    """Resolve the requested Dissect plugin/function name to one executable descriptor."""

    descriptors, invalid = find_functions(plugin_name, target)
    if invalid and not descriptors:
        raise ValueError(f"No dissect plugin function found for '{plugin_name}'")
    if not descriptors:
        raise ValueError(f"No dissect plugin function found for '{plugin_name}'")

    # Prefer an exact name match, then a path-based match, otherwise fallback.
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
    """Extract, transform, filter, and ingest records for a single spec.

    Processing order:

    1. iterate Dissect records for selected plugin
    2. map each record into gULP raw-document shape
    3. apply optional client-side ingestion filter
    4. append accepted documents to chunk buffer
    5. flush chunks to `/ingest_raw` using a shared request id

    Limit semantics:

    - `max_records` is applied to accepted records only
    - filtered-out records do not count toward the limit

    Args:
        client: Active gULP client.
        cfg: Runtime configuration.
        target: Opened Dissect target.
        spec: Resolved extract specification.
        max_records: Optional cap of accepted records for this spec.

    Returns:
        Number of accepted records ingested for this spec.

    Raises:
        RuntimeError: If `/ingest_raw` reports a failing status.
    """

    req_id = str(uuid.uuid4())
    chunk: list[dict[str, Any]] = []
    total = 0
    accepted_total = 0
    context_cache: dict[str, str] = {}
    source_cache: dict[tuple[str, str], str] = {}
    progress = None if cfg.verbose else _make_progress_bar(spec)

    async def _flush(last: bool) -> None:
        """Send the current chunk to `/ingest_raw` and clear local buffer."""
        if not chunk:
            return
        result = await client.ingest.raw(
            operation_id=cfg.operation_id,
            plugin_name="raw",
            data=chunk,
            params={
                "req_id": req_id,
                "last": last,
                "plugin_params": {
                    "mapping_parameters": deepcopy(spec.mapping_parameters),
                },
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
            if max_records is not None and accepted_total >= max_records:
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

            if not _passes_ingestion_filter(mapped_record, cfg.flt):
                continue

            accepted_total += 1
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

    return accepted_total


async def _reset_operation(client: GulpClient, operation_id: str) -> None:
    """Delete and recreate the target operation before ingestion.

    This is destructive and intended for clean-slate ingest runs.
    """

    await client.operations.delete(operation_id, force=True)
    await client.operations.create(name=operation_id)


async def run(cfg: AppConfig, specs: list[ResolvedExtractSpec]) -> None:
    """Execute the full end-to-end `gulp-dissect` workflow.

    High-level flow:

    - open target image
    - authenticate to gULP and ensure websocket session
    - optionally reset operation
    - ingest each extract spec sequentially while enforcing global `--limit`
    - attempt logout in finally block
    """

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
    """Entry point for the CLI.

    Wrap execution with banner/log setup and top-level error handling.

    Returns:
        Process-style exit code (`0` for success, `1` for failure).
    """
    banner = art.text2art("gulp-dissect", font="random")
    print(banner)
    MutyLogger.get_instance(name="gulp-dissect")
    try:
        args = parse_args(argv)
        cfg = build_config(args)
        specs_raw = collect_extract_specs(args)
        specs = asyncio.run(resolve_specs(specs_raw, cfg))
        asyncio.run(run(cfg, specs))
        return 0
    except Exception as exc:
        print(f"[gulp-dissect] error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
