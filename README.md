- [gulp-dissect](#gulp-dissect)
  - [Install](#install)
  - [CLI](#cli)
    - [tuples input on the command line](#tuples-input-on-the-command-line)
    - [tuples input from a JSON file](#tuples-input-from-a-json-file)
  - [Filtering with --flt](#filtering-with---flt)
  - [Required Mapping Validation](#required-mapping-validation)
  - [Notes](#notes)

# gulp-dissect

`gulp-dissect` extracts records from a forensic disk image through the [Dissect](https://github.com/fox-it/dissect) API and ingests them into gULP with `/ingest_raw` and the `raw` plugin.

## Install

From this repository root:

```bash
cd gulp-dissect
/gulp/.venv/bin/pip install -e .
```

## CLI

Environment variables are supported only for:

- `--username` (or `GULP_DISSECT_USERNAME`)
- `--password` (or `GULP_DISSECT_PASSWORD`)
- `--gulp_url` (or `GULP_DISSECT_URL`)

All other options are command-line only:

- `--image_path`: path to the forensic disc image to be dissected
- `--operation_id`: the operation in gulp to ingest data into. this operation must already exist before running the tool.
- `--limit` (default: `0`) maximum number of records to ingest across all extract specs (`0` means no limit)
- `--reset-operation` clears existing data in the target operation before ingest while keeping the operation itself
- `--chunk_size` (default: `1000`)
- `--context_id`: optional, the id of an existing `GulpContext` to be used for ingested documents, to be set in each `GulpDocument` as `gulp.context_id`. if not provided, a field in the currently processed mapping must have `is_type` defined as `context_name`. if neither is provided, the tool should notify the error and fail.
- `--source_id`: optional, the id of an existing `GulpSource` to be used for ingested documents, to be set in each `GulpDocument` as `gulp.source_id`. if not provided, a field in the currently processed mapping must have `is_type` defined as `source_name`. if neither is provided, the tool should notify the error and fail.
- `--flt`: optional `GulpIngestionFilter` JSON object applied client-side before calling `/ingest_raw`
- `--verbose` prints each generated GulpDocument as JSON; by default the CLI shows a progress bar instead

You must provide one or more extract tuples (`plugin`, `GulpMappingParameters`) using one of these forms:

> GulpMappingParameters and related mapping format follows the same format as in [gulp](https://github.com/mentat-is/gulp/blob/master/docs/plugins_and_mapping.md#mapping-101) and are parsed using imported gulp's code.
> the only difference is not all flags are supported. specifically:
>
> 1. `is_gulp_type` is supported only for `context_name` and `source_name` to allow auto-assigning `gulp.context_id` and `gulp.source_id` respectively when the corresponding CLI flags are not provided.
> 2. `extra_doc_with_event_code` is currently not supported

### tuples input on the command line

One or more `--plugin` / `--mapping_parameters` pairs for multiple plugins (processed sequentially):

```bash
gulp-dissect \
  --image_path /gulp/img/SCHARDT.img \
  --username admin --password admin \
  --gulp_url http://localhost:8080 \
  --operation_id test_operation \
  --plugin evt --mapping_parameters '{"mappings":{"dissect_evt":{"exclude":["_generated","_version","_classification"],"fields":{"ts":{"ecs":["@timestamp"]},"EventCode":{"ecs":["event.code"]},"hostname":{"is_gulp_type":"context_name"},"SourceName":{"is_gulp_type":"source_name"},"_source":{"ecs":["log.file_path"]},"_version":{"ecs":["log.file_version"]}}}}}'
  # others here ...
  # --plugin mft --mapping_parameters '...'
```

### tuples input from a JSON file

a JSON file containing one tuple object or a list of tuple objects using `--extract_file /path/to/extracts.json`.

Example `extracts.json`:

```json
[
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
            "_source": {"ecs": ["log.file_path"]}
          }
        }
      },
      "mapping_id": "dissect_evt"
    }
  },
  {
    "plugin": "mft",
    "mapping_parameters": {
      "mapping_file": "/mapping_files/dissect_mft_mapping.json",
      "mapping_id": "dissect_mft"
    }
  }
]
```

Run with file-based tuples:

```bash
gulp-dissect \
  --image_path /gulp/img/SCHARDT.img \
  --username admin --password admin \
  --gulp_url http://localhost:8080 \
  --operation_id test_operation \
  --extract_file /tmp/extracts.json
```

`mapping_parameters` accepts either:

- `{ "mapping_file": "/abs/path/to/file.json", "mapping_id": "..." }`
- `{ "mappings": { "id": { ...GulpMapping... } }, "mapping_id": "id" }`

## Filtering with --flt

`--flt` is evaluated locally by `gulp-dissect` before documents are sent to ingest.

- all configured conditions are combined as AND.

Supported `model_extra` comparisons:

- string equality: `{"key": "value"}`
- numeric equality: `{"key": 42}`
- numeric ranges: `{"key": {"gte": 10, "lte": 20}}`
- numeric lower bound only: `{"key": {"gte": 10}}`
- numeric upper bound only: `{"key": {"lte": 20}}`

Examples:

Filter by aliased event code value:

```bash
gulp-dissect \
  --image_path /gulp/img/SCHARDT.img \
  --username admin --password admin \
  --gulp_url http://localhost:8080 \
  --operation_id test_operation \
  --plugin evt \
  --mapping_parameters '{"mappings":{"dissect_evt":{"value_aliases":{"event.code":{"default":{"1073748859":"bingo"}}},"fields":{"EventCode":{"ecs":["event.code"]},"hostname":{"is_gulp_type":"context_name"},"SourceName":{"is_gulp_type":"source_name"}}}},"mapping_id":"dissect_evt"}' \
  --flt '{"event.code":"bla"}'
```

Filter by numeric equality:

```bash
--flt '{"event.severity":3}'
```

Filter by numeric range:

```bash
--flt '{"event.severity":{"gte":3,"lte":5}}'
```

Combine time range and extra fields (AND):

```bash
--flt '{"time_range":[1704067200000000000,1704153600000000000],"event.category":"authentication","event.severity":{"gte":3}}'
```

## Required Mapping Validation

Before ingestion starts, each extract tuple is validated:

- `@timestamp` mapping is required (fallback to source field `ts` is applied only if no explicit `@timestamp` mapping exists and `ts` exists in mapping fields).
- `event.code` mapping is required.
- If `--context_id` is not provided, at least one mapping field must define `is_gulp_type: "context_name"`.
- If `--source_id` is not provided, at least one mapping field must define `is_gulp_type: "source_name"`.

## Notes

- Generated GulpDocuments preserves unmapped Dissect fields **as is**; fields referenced by the mapping are transformed into ECS / gULP targets and removed from the raw payload.
- `--limit` applies globally across all extract specs in one run.
- `--reset-operation` clears (recreates) the target operation before ingestion, allowing for a clean slate without needing to manually reset the gULP instance; use with caution as it deletes all existing data in the operation.
- The tool always attempts `logout`, even when extraction or ingestion fails.
