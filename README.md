# gulp-dissect

`gulp-dissect` extracts records from a forensic disk image through the Dissect API and ingests them into gULP with `/ingest_raw` and the `raw` plugin.

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
- `--verbose` prints each generated GulpDocument as JSON; by default the CLI shows a progress bar instead

You must provide one or more extract tuples (`plugin`, `GulpMappingParameters`) using one of these forms:

1. Repeated pair options:

```bash
gulp-dissect \
  --image_path /gulp/img/SCHARDT.img \
  --username admin --password admin \
  --gulp_url http://localhost:8080 \
  --operation_id test_operation \
  --plugin evt \
  --mapping_parameters @/tmp/dissect_evt_mapping_params.json
```

1. Repeated `--plugin` / `--mapping_parameters` pairs for multiple plugins (processed sequentially):

```bash
gulp-dissect \
  --image_path /gulp/img/SCHARDT.img \
  --username admin --password admin \
  --gulp_url http://localhost:8080 \
  --operation_id test_operation \
  --plugin evt --mapping_parameters @/tmp/dissect_evt_mapping_params.json \
  --plugin mft --mapping_parameters @/tmp/dissect_mft_mapping_params.json
```

1. JSON file containing one tuple object or a list of tuple objects using `--extract_file /path/to/extracts.json`.

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

`mapping_file` and `mappings` are mutually exclusive.

## Required Mapping Validation

Before ingestion starts, each extract tuple is validated:

- `@timestamp` mapping is required (fallback to source field `ts` is applied only if no explicit `@timestamp` mapping exists and `ts` exists in mapping fields).
- `event.code` mapping is required.
- If `--context_id` is not provided, at least one mapping field must define `is_gulp_type: "context_name"`.
- If `--source_id` is not provided, at least one mapping field must define `is_gulp_type: "source_name"`.

## End-to-end Example

```bash
gulp-dissect \
  --image_path /gulp/img/SCHARDT.img \
  --username admin \
  --password admin \
  --gulp_url http://localhost:8080 \
  --operation_id test_operation \
  --limit 500 \
  --reset-operation \
  --chunk_size 1000 \
  --plugin evt \
  --mapping_parameters '{
    "mappings": {
      "dissect_evt": {
        "fields": {
          "ts": {"ecs": ["@timestamp"]},
          "EventCode": {"ecs": ["event.code"]},
          "hostname": {"is_gulp_type": "context_name"},
          "SourceName": {"is_gulp_type": "source_name"}
        }
      }
    },
    "mapping_id": "dissect_evt"
  }'
```

## Notes

- Timestamp values mapped to `@timestamp` are normalized to UTC ISO8601 (`YYYY-MM-DDTHH:MM:SS.sssZ`) before upload.
- Generated GulpDocuments preserve only unmapped Dissect fields; fields referenced by the mapping are transformed into ECS / gULP targets and removed from the raw payload.
- `--limit` applies globally across all extract specs in one run.
- `--reset-operation` clears (recreates) the target operation before ingestion, allowing for a clean slate without needing to manually reset the gULP instance; use with caution as it deletes all existing data in the operation.
- The tool always attempts `logout`, even when extraction or ingestion fails.
