"""Command-line entry point for extracting with Dissect and ingesting into gULP.

This module implements the full `gulp-dissect` runtime pipeline:

1. Parse CLI arguments and construct a validated runtime configuration.
2. Resolve one or more extract tuple specifications (`plugin`, `mapping_parameters`)
    using gULP's own mapping-resolution utilities.
3. Open a Dissect target image and stream records for each requested plugin.
4. Optionally filter raw extracted records client-side via `GulpIngestionFilter`.
5. Send accepted documents to `/ingest_raw` in bounded chunks.

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
    mapping_parameters_to_mapping,
    normalize_timestamp,
)
from gulp.api.opensearch.filters import GulpIngestionFilter
from gulp_sdk import GulpClient
from tqdm import tqdm

from gulp_dissect import __version__
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
        context_name: Optional explicit context name override resolved (or
            created) on gULP and written as `gulp.context_id`.
        source_name: Optional explicit source name override resolved (or
            created) on gULP and written as `gulp.source_id`.
        mapping_files_base_path: Optional base path used to resolve relative
            mapping file paths in mapping parameters.
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
    context_name: str | None
    source_name: str | None
    mapping_files_base_path: str | None
    flt: GulpIngestionFilter | None
    reset_operation: bool
    verbose: bool


@dataclass
class ResolvedExtractSpec:
    """Validated extract tuple used by the extraction/ingestion loop."""

    plugin: str
    mapping_id: str
    mapping_parameters: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Argument parsing and configuration helpers.
# ---------------------------------------------------------------------------


def get_app_version() -> str:
    """Return the gulp-dissect package version."""

    return __version__


def print_banner() -> None:
    """Print the startup banner together with the current version."""

    banner = art.text2art("gulp-dissect", font="random")
    print(banner)
    print(f"gulp-dissect v{get_app_version()}")


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
        "--version",
        action="version",
        version=get_app_version(),
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
        "--context_name",
        help=(
            "explicit context name override; if omitted, mapping must provide "
            "an is_gulp_type=context_name field"
        ),
    )
    p.add_argument(
        "--source_name",
        help=(
            "explicit source name override; if omitted, mapping must provide "
            "an is_gulp_type=source_name field"
        ),
    )
    p.add_argument(
        "--mapping_files_base_path",
        help=(
            "base path used to resolve relative mapping file paths "
            "(or set GULP_DISSECT_MAPPING_FILES_BASE_PATH)"
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
            "must match --plugin occurrences, all paths inside must be absolute paths unless --mapping_files_base_path is set"
        ),
    )

    p.add_argument(
        "--extract_rules",
        dest="extract_rules",
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
    context_name = args.context_name
    source_name = args.source_name
    mapping_files_base_path = _env_or_arg(
        args.mapping_files_base_path,
        "GULP_DISSECT_MAPPING_FILES_BASE_PATH",
    )
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
        context_name=str(context_name) if context_name else None,
        source_name=str(source_name) if source_name else None,
        mapping_files_base_path=(
            str(mapping_files_base_path) if mapping_files_base_path else None
        ),
        flt=flt,
        reset_operation=str(reset_operation_raw).lower() in {"1", "true", "yes", "on"},
        verbose=bool(args.verbose),
    )


def collect_extract_specs(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Collect extract tuple payloads from CLI pair flags and/or JSON files.

    Supported forms:

    - repeated `--plugin` + `--mapping_parameters` pairs (1:1)
    - one or more `--extract_rules` JSON files containing one tuple object or a
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

    for extract_rules in args.extract_rules:
        # Accept either a single tuple object or a list of tuple objects.
        loaded = json.loads(Path(extract_rules).read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            specs.extend(loaded)
        else:
            specs.append(loaded)

    if not specs:
        raise ValueError("At least one extract tuple is required")

    return specs


def _resolve_mapping_path(path: str, base_path: str | None) -> str:
    """Resolve one mapping file path against the optional base path."""

    if not path or not base_path or os.path.isabs(path):
        return path

    resolved = str((Path(base_path) / path).resolve())
    if not Path(resolved).exists():
        raise FileNotFoundError(f"mapping file {resolved} does not exist")
    return resolved


async def _normalize_mapping_parameters(
    mp_dict: dict[str, Any],
    mapping_files_base_path: str | None,
) -> tuple[dict[str, Any], str]:
    """Validate mapping parameters and inline any file-based mappings."""

    mapping_parameters = GulpMappingParameters.model_validate(mp_dict)

    if mapping_parameters.mapping_file:
        mapping_parameters.mapping_file = _resolve_mapping_path(
            mapping_parameters.mapping_file,
            mapping_files_base_path,
        )

    if mapping_parameters.additional_mapping_files:
        mapping_parameters.additional_mapping_files = [
            (
                _resolve_mapping_path(file_path, mapping_files_base_path),
                mapping_id,
            )
            for file_path, mapping_id in mapping_parameters.additional_mapping_files
        ]

    if mapping_parameters.mapping_file:
        resolved_mappings, mapping_id = await mapping_parameters_to_mapping(
            mapping_parameters,
        )
        mapping_parameters.mappings = resolved_mappings
        mapping_parameters.mapping_id = mapping_id
        mapping_parameters.mapping_file = None
        mapping_parameters.additional_mapping_files = []
        mapping_parameters.additional_mappings = {}
    else:
        mapping_id = mapping_parameters.mapping_id or next(
            iter(mapping_parameters.mappings.keys()),
            "default",
        )

    return mapping_parameters.model_dump(exclude_none=True), str(mapping_id)


async def resolve_specs(
    specs_raw: list[dict[str, Any]], cfg: AppConfig
) -> list[ResolvedExtractSpec]:
    """Validate extract specs and normalize mapping parameter file paths."""
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

        normalized_mapping_parameters, mapping_id = await _normalize_mapping_parameters(
            mp_dict,
            cfg.mapping_files_base_path,
        )
        resolved.append(
            ResolvedExtractSpec(
                plugin=str(plugin),
                mapping_id=mapping_id,
                mapping_parameters=normalized_mapping_parameters,
            )
        )

    return resolved


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
        # treat as string
        return str(value)
        # return [_to_jsonable(v) for v in value]
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
    raw_record: dict[str, Any],
    flt: GulpIngestionFilter | None,
) -> bool:
    """Evaluate one raw extracted record against local ingestion filtering rules.

    Filter contract:

    - if no filter is provided, every document is accepted
    - if `storage_ignore_filter` is true, every document is accepted
    - all configured conditions are combined as logical AND
        - `time_range` is applied to the first available raw timestamp candidate in
            this order: `gulp.timestamp`, `@timestamp`, `ts`
    - extra filter keys (`model_extra`) support:
      - string equality: `{ "field": "value" }`
      - numeric equality: `{ "field": 42 }`
      - numeric range: `{ "field": {"gte": 10, "lte": 20} }`

    Args:
        raw_record: Extracted record candidate before backend mapping.
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

        # ordered comparison with explicit operators
        if isinstance(condition, dict):
            allowed = {"gte", "lte"}
            if not condition or any(k not in allowed for k in condition):
                return False

            # numeric ordered comparison
            if _is_number(value):
                for op, op_value in condition.items():
                    if not _is_number(op_value):
                        return False
                    if op == "gte" and value < op_value:
                        return False
                    if op == "lte" and value > op_value:
                        return False

                return True

            # string ordered comparison (lexicographic), useful for ISO8601 dates
            if isinstance(value, str):
                for op, op_value in condition.items():
                    if not isinstance(op_value, str):
                        return False
                    if op == "gte" and value < op_value:
                        return False
                    if op == "lte" and value > op_value:
                        return False

                return True

            return False

        return False

    if flt is None or flt.storage_ignore_filter:
        return True

    # 1) apply time_range when present
    if flt.time_range and len(flt.time_range) == 2:
        # turn start/end/ into nanoseconds
        start, end = flt.time_range
        if isinstance(start, str) and not start.isnumeric():
            start = muty.time.string_to_nanos_from_unix_epoch(start)
        if isinstance(end, str) and not end.isnumeric():
            end = muty.time.string_to_nanos_from_unix_epoch(end)

        ts_nanos: int = None
        if not (start == 0 and end == 0):
            timestamp = raw_record.get("ts")
            if timestamp:
                try:
                    ts_nanos = muty.time.string_to_nanos_from_unix_epoch(
                        timestamp,
                        throw_on_invalid=True,
                    )
                    # print(f"normalized timestamp 'ts' in nanos: {ts_nanos}")
                except Exception as ex:
                    ts_nanos = None
            if ts_nanos is not None:
                if start > 0 and ts_nanos < start:
                    return False
                if end > 0 and ts_nanos > end:
                    return False

    # 2) apply extra field filters from model_extra
    extras = flt.model_extra or {}
    for field_name, condition in extras.items():
        if field_name not in raw_record:
            return False
        if not _matches_model_extra(raw_record[field_name], condition):
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
    """Extract raw records and ingest them for a single spec.

    Processing order:

    1. iterate Dissect records for selected plugin
    2. optionally pre-map records locally for client-side filtering decisions
    3. inject optional CLI context/source overrides into each raw record
    4. append accepted records to chunk buffer
    5. flush chunks to `/ingest_raw` using a shared request id

    Limit semantics:

        - `max_records` is applied to records accepted by local filtering and
            submitted by this client

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
    accepted_total = 0
    total = 0
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
            to_send = deepcopy(raw_record)
            if cfg.context_name is not None:
                to_send["gulp.context_id"] = str(cfg.context_name)
            if cfg.source_name is not None:
                to_send["gulp.source_id"] = str(cfg.source_name)

            if cfg.flt is not None:
                if not _passes_ingestion_filter(to_send, cfg.flt):
                    continue

            accepted_total += 1
            if cfg.verbose:
                print(json.dumps(to_send, sort_keys=True, default=str))
            elif progress is not None:
                progress.update(1)

            chunk.append(to_send)
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
    try:
        args = parse_args(argv)
        print_banner()
        MutyLogger.get_instance(name="gulp-dissect")
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
