- [gulp-dissect](#gulp-dissect)
  - [Install](#install)
  - [CLI](#cli)
    - [set dissect plugin/s and provide mappings](#set-dissect-plugins-and-provide-mappings)
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

~~~bash
# show usage
 gulp-dissect --help
                       .;                                                                  .   
                      .;'                    .'     .-.                                ...;... 
  ,:.,'    ,  :      .;   `..:.         .-..'       `-'      .      .   .-.   .-.       .'     
 :   ;    ;   ;     ::     ;;  : `;;;. :   ;       ;'      .';    .'; .;.-'  ;        .;       
  `-:'  .'`..:;._ _;;_.-   ;;_.`       `:::'`.  _.;:._.  .' .'  .' .'  `:::' `;;;;' .;         
-._:'                     .;'                           '      '                               

2026-05-23 17:28:24,623|gulp-dissect||DEBUG|28599,28599|_reconfigure|"muty/log.py", line 245|logger "<TraceLogger gulp-dissect (DEBUG)>" configured!
usage: gulp-dissect [-h] [--image_path IMAGE_PATH] [--username USERNAME] [--password PASSWORD] [--gulp_url GULP_URL]
                    [--operation_id OPERATION_ID] [--limit LIMIT] [--chunk_size CHUNK_SIZE] [--context_name CONTEXT_NAME]
                    [--source_name SOURCE_NAME] [--mapping_files_base_path MAPPING_FILES_BASE_PATH] [--flt FLT]
                    [--reset-operation] [--verbose] [--plugin PLUGIN] [--mapping_parameters MAPPING_PARAMETERS]
                    [--extract_rules EXTRACT_RULES]

Extract data from a forensic image with Dissect and ingest mapped records into gULP via ingest_raw.

options:
  -h, --help            show this help message and exit
  --image_path IMAGE_PATH
                        absolute path to the forensic disk image to process (default: None)
  --username USERNAME   gULP username (or set GULP_DISSECT_USERNAME) (default: None)
  --password PASSWORD   gULP password (or set GULP_DISSECT_PASSWORD) (default: None)
  --gulp_url GULP_URL   gULP base URL, e.g. http://localhost:8080 (or set GULP_DISSECT_URL) (default: None)
  --operation_id OPERATION_ID
                        existing gULP operation id where documents will be ingested (default: None)
  --limit LIMIT         maximum number of records to ingest across all extract tuples; 0 means no limit (default: None)
  --chunk_size CHUNK_SIZE
                        number of mapped records sent per ingest_raw chunk (default: None)
  --context_name CONTEXT_NAME
                        explicit context name override; if omitted, mapping must provide an is_gulp_type=context_name field
                        (default: None)
  --source_name SOURCE_NAME
                        explicit source name override; if omitted, mapping must provide an is_gulp_type=source_name field
                        (default: None)
  --mapping_files_base_path MAPPING_FILES_BASE_PATH
                        base path used to resolve relative mapping file paths (or set GULP_DISSECT_MAPPING_FILES_BASE_PATH)
                        (default: None)
  --flt FLT             optional GulpIngestionFilter JSON object applied client-side before ingest_raw calls (default: None)
  --reset-operation     delete and recreate the target operation before ingestion (destructive) (default: False)
  --verbose             print each mapped GulpDocument as JSON instead of showing the progress bar (default: False)
  --plugin PLUGIN       Dissect plugin/function name for one extract tuple; repeat with --mapping_parameters (default: [])
  --mapping_parameters MAPPING_PARAMETERS
                        JSON object (or @file.json) for one extract tuple mapping_parameters; must match --plugin occurrences,
                        all paths inside must be absolute paths unless --mapping_files_base_path is set (default: [])
  --extract_rules EXTRACT_RULES
                        JSON file containing one extract tuple object or a list of tuple objects with shape: {plugin,
                        mapping_parameters}; repeatable (default: [])
~~~

Environment variables are supported only for:

- `--username` (env `GULP_DISSECT_USERNAME`)
- `--password` (env `GULP_DISSECT_PASSWORD`)
- `--gulp_url` (env `GULP_DISSECT_URL`)
- `--mapping_files_base_path` (env `GULP_DISSECT_MAPPING_FILES_BASE_PATH`)

All other options are command-line only.  

When `--context_name` and/or `--source_name` are provided, each value is treated as a context/source name override: gULP resolves it via `context_create` / `source_create` (creating it if missing), and the resulting ids are used in generated documents.

### set dissect plugin/s and provide mappings

`mapping_parameters` and related mapping format follows the same format as in [gulp](https://github.com/mentat-is/gulp/blob/master/docs/plugins_and_mapping.md#mapping-101) and are parsed using imported gulp's code.

the only difference is not all flags are supported. specifically:

1. `is_gulp_type` is supported only for `context_name` and `source_name` to allow auto-assigning `gulp.context_id` and `gulp.source_id` respectively when the corresponding CLI flags are not provided.
2. `extra_doc_with_event_code` is currently not supported (and possibly never will be)

`--plugin` and `--mapping_parameters` must be provided using one of these forms:

#### tuples input on the command line

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

#### tuples input from a JSON file

a JSON file containing one or more tuples using `--extract_rules /path/to/extracts.json`.

[Example extract_rules](./extract_rules_sample.json)

Run with file-based tuples:

```bash
gulp-dissect \
  --image_path /gulp/img/SCHARDT.img \
  --username admin --password admin \
  --gulp_url http://localhost:8080 \
  --operation_id test_operation \
  --extract_rules ./extract_rules_sample.json
```

Run with relative mapping files resolved from an explicit base path:

```bash
gulp-dissect \
  --image_path /gulp/img/SCHARDT.img \
  --username admin --password admin \
  --gulp_url http://localhost:8080 \
  --operation_id test_operation \
  --plugin mft \
  --mapping_parameters '{"mapping_file":"dissect_mft.json","mapping_id":"mft"}' \
  --mapping_files_base_path /gulp/gulp-dissect/mapping_files
```

`mapping_parameters` accepts either:

- `{ "mapping_file": "/path/to/file.json", "mapping_id": "..." }`
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
- If `--context_name` is not provided, at least one mapping field must define `is_gulp_type: "context_name"`.
- If `--source_name` is not provided, at least one mapping field must define `is_gulp_type: "source_name"`.
- If `--context_name` and/or `--source_name` are provided, those overrides bypass mapping-based context/source extraction and are resolved/created using the provided values as names.

## Notes

- Generated GulpDocuments preserves unmapped Dissect fields **as is**; fields referenced by the mapping are transformed into ECS / gULP targets and removed from the raw payload.
- `--limit` applies globally across all extract specs in one run.
- `--reset-operation` clears (recreates) the target operation before ingestion, allowing for a clean slate without needing to manually reset the gULP instance; use with caution as it deletes all existing data in the operation.
- The tool always attempts `logout`, even when extraction or ingestion fails.
