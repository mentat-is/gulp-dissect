- [gulp-dissect](#gulp-dissect)
  - [Install](#install)
  - [CLI](#cli)
    - [configure the dissect plugin/s to use and provide mappings for gulp](#configure-the-dissect-plugins-to-use-and-provide-mappings-for-gulp)
      - [mapping input via the command line](#mapping-input-via-the-command-line)
      - [mapping input via a JSON file](#mapping-input-via-a-json-file)
  - [examples](#examples)
    - [filtering](#filtering)

# gulp-dissect

`gulp-dissect` extracts records from a forensic disk image through the [Dissect](https://github.com/fox-it/dissect) API and ingests them into gULP with `/ingest_raw` and the `raw` plugin.

## Install

From this repository root, in a Python 3.12+ environment:

```bash
cd gulp-dissect
pip install -e .
```

> `dissect` itself seems not working with Python 3.14 at the moment, so we recommend using Python 3.13 for now until that is resolved.

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
                    [--operation_id OPERATION_ID] [--limit LIMIT] [--chunk-size CHUNK_SIZE] [--concurrency CONCURRENCY]
                    [--context_name CONTEXT_NAME]
                    [--source_name SOURCE_NAME] [--mapping_files_base_path MAPPING_FILES_BASE_PATH] [--flt FLT]
                    [--no-default-excludes] [--reset-operation] [--verbose] [--plugin PLUGIN]
                    [--mapping_parameters MAPPING_PARAMETERS]
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
  --chunk-size CHUNK_SIZE
                        number of mapped records sent per ingest_raw chunk (default: 1000)
  --concurrency CONCURRENCY
                        maximum concurrent non-final ingest_raw chunks (default: None)
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
  --no-default-excludes
                        do not add the standard internal-field exclusions to mappings (default: True)
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

Default exclusions ensure every mapping's `exclude` list contains `_generated`, `_version`, `_source`, and `_classification` before the mapping is sent to gULP, preserving any existing exclusions. Use `--no-default-excludes` to disable this behavior.

When `--context_name` and/or `--source_name` are provided, each value is treated as a context/source name override: gULP resolves it via `context_create` / `source_create` (creating it if missing), and the resulting ids are used in generated documents.

### configure the dissect plugin/s to use and provide mappings for gulp

`gulp-dissect` works similar as when calling the gulp ingestion API, basically it needs a `--plugin` parameter to tell which `plugin` (intended here as one of the [dissect plugins](https://docs.dissect.tools/en/stable/plugins/index.html)) to use for extraction, and a `--mapping_parameters` parameter to provide the corresponding mapping for that plugin, so that extracted records are then mapped by the backend and ingested into gULP as usual.

> `--mapping_parameters` follows the same exact format as when passed in [gulp](https://github.com/mentat-is/gulp/blob/master/docs/plugins_and_mapping.md#mapping-101) via `plugin_params`, and basically it is just forwarwed to the gulp `raw` plugin to perform ingestion with the given mapping.
>
> the only differences are the `mapping_file` and `additional_mapping_files` keys in the provided `mapping_parameters`:
>
> they are intented to be file paths in the local filesystem of `gulp-dissect` (since `gulp-dissect` is the one reading the mapping files and sending the mapping content to backend), so they are resolved and converted to direct JSON mappings by `gulp-dissect` before being sent to backend together with the provided `mapping_id`.

~~~mermaid
flowchart 
  A[gulp-dissect CLI] -->|process mapping_parameters| B[extract with dissect plugin]
  B --> C[generate GulpDocuments]
  C --> D[call gulp]
  D -->|ingest_raw| E[gULP backend]
  E -->|apply mapping| F[mapped records in gULP]
~~~

`--plugin` and `--mapping_parameters` can be provided in two ways:

#### mapping input via the command line

One or more `--plugin` / `--mapping_parameters` pairs to perform extraction of (possibly) multiple data in one shot (processed sequentially):

```bash
gulp-dissect \
  --image_path /gulp/img/SCHARDT.img \
  --username admin --password admin \
  --gulp_url http://localhost:8080 \
  --operation_id test_operation \
  --plugin evt \
  --mapping_parameters '{
    "mappings":{
      "dissect_evt":{
        "exclude":[
          "_generated","_version","_classification"
        ],
        "fields":{
          "ts":{
            "ecs":[
              "@timestamp"
            ]
          },
          "EventCode":{
            "ecs":[
              "event.code"
            ]
          },
          "hostname":{
            "is_gulp_type":"context_name"
          },
          "SourceName":{
            "is_gulp_type":"source_name"
          },
          "_source":{
            "ecs":["log.file_path"]
          },
          "_version":{
            "ecs":["log.file_version"]
          }
        }
      }
    }
  }'
  # others here ...
  # --plugin mft --mapping_parameters '...'
```

#### mapping input via a JSON file

a JSON file containing an array of `plugin` and `mapping_parameters` objects (processed sequentially), to be passed via `--extract_rules` argument

~~~json
[
  {
    "plugin": "dissect_plugin_1", 
    "mapping_parameters": { 
      // ... 
    }
  },
  {
    "plugin": "dissect_plugin_2", 
    "mapping_parameters": { 
      // ... 
    }
  }
]
~~~

[Example extract_rules](./extract_rules_sample.json)

> if multiple mappings are specified (i.e. multiple mappings in `--mapping_parameters.mappings` and/or `--mapping_parameters.mapping_file`), they are merged together and sent to backend as a single object with multiple mapping ids.
> thus, **it is important to specify the desired `mapping_id` to be applied**, or gulp will use the first mapping id it finds in the merged mapping object, which may not be the intended one.

## examples

provide a base directory for local mapping files, to look for `mapping_file` and `additional_mapping_files` paths in the provided mapping_parameters.

> either, if set, they must be absolute parameters!

```bash
gulp-dissect \
  --image_path /gulp/img/SCHARDT.img \
  --username admin --password admin \
  --gulp_url http://localhost:8080 \
  --operation_id test_operation \
  --plugin mft \
  --mapping_parameters '{
    "mapping_file":"dissect_mft.json",
    "mapping_id":"mft"
  }' --mapping_files_base_path /gulp/gulp-dissect/mapping_file_samples
```

mapping using [value_alieses](https://github.com/mentat-is/gulp/blob/master/docs/plugins_and_mapping.md#mapping-file-example) (processed by gulp)

~~~bash
gulp-dissect \
--image_path /gulp/img/SCHARDT.img \
--username admin --password admin \
--gulp_url http://localhost:8080 \
--operation_id test_operation \
--plugin evt \
--mapping_parameters '{
  "mappings": {
    "dissect_evt":{
      "value_aliases":{
        "event.code":{
          "default":{
            "1000":"bingo"
          }
        }
      },
      "exclude":[
        "_generated","_version","_classification", "_source"
      ],
      "fields":{
        "ts":{
          "ecs":[
            "@timestamp"
          ]
        },
        "EventCode":{
          "ecs":[
            "event.code"
          ]
        },
        "hostname":{
          "is_gulp_type":"context_name"
        },
        "SourceName":{
          "is_gulp_type":"source_name"
        }
      }
    }
  },
  "mapping_id":"dissect_evt"
}' --limit 2 --reset-operation
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
--mapping_parameters '{
  "mappings":{
    "dissect_evt":{
      "value_aliases":{
        "event.code":{
          "default":{
            "1000":"bingo"
          }
        }
      },
      "exclude":[
        "_generated","_version","_classification", "_source"
      ],
      "fields":{
        "ts":{
          "ecs":[
            "@timestamp"
          ]
        },
        "EventCode":{
          "ecs":[
            "event.code"
          ]
        },
        "hostname":{
          "is_gulp_type":"context_name"
        },
        "SourceName":{
          "is_gulp_type":"source_name"
        }
      }
    }
  },
  "mapping_id":"dissect_evt"
}' --flt '{"EventCode":1000}' --reset-operation
```

> in the example above, filtering is applied locally on raw data, then `value_aliases` is applied by the backend.
>
> So if you want to filter by an aliased value, you need to use the original value in the filter condition, not the alias.

Filter by numeric equality:

```bash
--flt '{
  "Severity":3
}'
```

Filter by numeric range:

```bash
--flt '{
  "Severity":{
    "gte":3,
    "lte":5
  }
}'
```

Combine time range and raw fields (AND):

```bash
 --flt '{
  "time_range": [
    "2004-08-20T15:25:39+00:00",
    "2004-08-20T15:45:39+00:00"
  ],
  "Channel":"Security",
  "Severity":{
    "gte":3
  }
}'
```

> `time_range` is evaluated against "ts", which is the default timestamp key used by dissect, and **must be specified as an ISO8601 string or directly as a nanoseconds-from-unix-epoch value**.
