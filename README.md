- [gulp-dissect](#gulp-dissect)
  - [Install](#install)
  - [CLI](#cli)
    - [configure the dissect plugin/s to use and provide mappings for gulp](#configure-the-dissect-plugins-to-use-and-provide-mappings-for-gulp)
      - [tuples input on the command line](#tuples-input-on-the-command-line)
      - [tuples input from a JSON file](#tuples-input-from-a-json-file)
  - [examples](#examples)
    - [filtering](#filtering)
  - [Mapping Behavior](#mapping-behavior)

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
usage: gulp-dissect [-h] [--version] [--image_path IMAGE_PATH] [--username USERNAME] [--password PASSWORD] [--gulp_url GULP_URL]
                    [--operation_id OPERATION_ID] [--limit LIMIT] [--chunk_size CHUNK_SIZE] [--context_name CONTEXT_NAME]
                    [--source_name SOURCE_NAME] [--mapping_files_base_path MAPPING_FILES_BASE_PATH] [--flt FLT]
                    [--reset-operation] [--verbose] [--plugin PLUGIN] [--mapping_parameters MAPPING_PARAMETERS]
                    [--extract_rules EXTRACT_RULES]

Extract data from a forensic image with Dissect and ingest mapped records into gULP via ingest_raw.

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
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

### configure the dissect plugin/s to use and provide mappings for gulp

`mapping_parameters` and related mapping format follows the same format as in [gulp](https://github.com/mentat-is/gulp/blob/master/docs/plugins_and_mapping.md#mapping-101) and are parsed using imported gulp's code.

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

> if multiple mappings are present (i.e. multiple `mappings` keys in both `--mapping_parameters.mappings` or `--mapping_parameters.mapping_file`), they are merged together and sent to backend as a single object with multiple mapping ids: thus, the desired `mapping_id` to be applied must be specified to, or gulp will use the first mapping id it finds in the merged mapping object, which may not be the intended one.

## examples

applying value aliases

~~~bash
gulp-dissect \
--image_path /gulp/img/SCHARDT.img \
--username admin --password admin \
--gulp_url http://localhost:8080 \
--operation_id test_operation \
--plugin evt \
--mapping_parameters '{"mappings":{"dissect_evt":{"value_aliases":{"event.code":{"default":{"1000":"bingo"}}},"fields":{"ts":{"ecs":["@timestamp"]},"EventCode":{"ecs":["event.code"]},"hostname":{"is_gulp_type":"context_name"},"SourceName":{"is_gulp_type":"source_name"}}}},"mapping_id":"dissect_evt"}' --limit 2 --reset-operation
~~~

### filtering

`--flt` is evaluated locally by `gulp-dissect` on **raw extracted records** before they are sent to backend **where the mapping is effectively applied**.

> so you have to use raw field names and values in the filter conditions, not gulp-mapped field names or values !!!

- all configured conditions are combined as AND.
- field matches are evaluated against the raw extracted record keys, not against mapped ECS fields.
- backend mapping still happens afterwards in gULP via `plugin_params.mapping_parameters`.

Supported comparisons:

- string equality: `{"key": "value"}`
- string ranges (lexicographic, useful for ISO8601): `{"key": {"gte": "2024-01-01T00:00:00Z", "lte": "2024-01-31T23:59:59Z"}}`
- numeric equality: `{"key": 42}`
- numeric ranges: `{"key": {"gte": 10, "lte": 20}}`
- numeric lower bound only: `{"key": {"gte": 10}}`
- numeric upper bound only: `{"key": {"lte": 20}}`
- time range on the default timestamp key "ts": `{"time_range": ["2024-01-01T00:00:00Z", "2024-01-31T23:59:59Z"]}` (evaluated against the raw "ts" field in extracted records, which is expected to be in ISO8601 format)

Some examples follow.

Filter by raw event code field value:

```bash
gulp-dissect \
--image_path /gulp/img/SCHARDT.img \
--username admin --password admin \
--gulp_url http://localhost:8080 \
--operation_id test_operation \
--plugin evt \
--mapping_parameters '{"mappings":{"dissect_evt":{"value_aliases":{"event.code":{"default":{"1000":"bingo"}}},"fields":{"ts":{"ecs":["@timestamp"]},"EventCode":{"ecs":["event.code"]},"hostname":{"is_gulp_type":"context_name"},"SourceName":{"is_gulp_type":"source_name"}}}},"mapping_id":"dissect_evt"}' --flt '{"EventCode":1000}' --reset-operation
```

> in the example above, filtering is applied locally on raw data, then value_aliases is applied by the backend. So if you want to filter by an aliased value, you need to use the original value in the filter condition, not the alias.

Filter by numeric equality:

```bash
--flt '{"Severity":3}'
```

Filter by numeric range:

```bash
--flt '{"Severity":{"gte":3,"lte":5}}'
```

Combine time range and raw fields (AND):

```bash
 --flt '{"time_range": ["2004-08-20T15:25:39+00:00","2004-08-20T15:45:39+00:00"],"Channel":"Security","Severity":{"gte":3}}'
```

> `time_range` is evaluated against "ts", which is the default timestamp key used by dissect, and **must be specified as an ISO8601 string**.

## Mapping Behavior

mapping is performed on the backend as usual, applying the given mapping to the `raw` plugin.

specifically:

1. any `mapping_file` or `additional_mapping_files` provided in `GulpMappingParameters` are cleared, converted to direct json mappings and set in `plugin_params.mapping_parameters.mappings` prior to sending to backend together with the provided `mapping_id`.
2. Extracted records are sent as raw payloads to backend and handled by the `raw` plugin for ingestion.
3. backend then applies the mapping as usual during ingestion.
