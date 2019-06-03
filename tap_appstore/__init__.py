#!/usr/bin/env python3

from datetime import datetime
from datetime import timedelta
import os
import json

import singer
from singer import utils, Transformer
from singer import metadata

from appstoreconnect import Api
import pytz

REQUIRED_CONFIG_KEYS = [
    'key_id',
    'key_file',
    'issuer_id',
    'vendor',
    'start_date'
]
STATE = {}

LOGGER = singer.get_logger()

BOOKMARK_DATE_FORMAT = '%Y-%m-%dT%H:%M:%SZ'


class Context:
    config = {}
    state = {}
    catalog = {}
    tap_start = None
    stream_map = {}
    new_counts = {}
    updated_counts = {}

    @classmethod
    def get_catalog_entry(cls, stream_name):
        if not cls.stream_map:
            cls.stream_map = {s["tap_stream_id"]: s for s in cls.catalog['streams']}
        return cls.stream_map.get(stream_name)

    @classmethod
    def get_schema(cls, stream_name):
        stream = [s for s in cls.catalog["streams"] if s["tap_stream_id"] == stream_name][0]
        return stream["schema"]

    @classmethod
    def is_selected(cls, stream_name):
        stream = cls.get_catalog_entry(stream_name)
        if stream is not None:
            stream_metadata = metadata.to_map(stream['metadata'])
            return metadata.get(stream_metadata, (), 'selected')
        return False

    @classmethod
    def print_counts(cls):
        LOGGER.info('------------------')
        for stream_name, stream_count in Context.new_counts.items():
            LOGGER.info('%s: %d new, %d updates',
                        stream_name,
                        stream_count,
                        Context.updated_counts[stream_name])
        LOGGER.info('------------------')


def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)


# Load schemas from schemas folder
def load_schemas():
    schemas = {}

    for filename in os.listdir(get_abs_path('schemas')):
        path = get_abs_path('schemas') + '/' + filename
        file_raw = filename.replace('.json', '')
        with open(path) as file:
            schemas[file_raw] = json.load(file)

    return schemas


def discover():
    raw_schemas = load_schemas()
    streams = []

    for schema_name, schema in raw_schemas.items():
        # create and add catalog entry
        catalog_entry = {
            'stream': schema_name,
            'tap_stream_id': schema_name,
            'schema': schema,
            # TODO Events may have a different key property than this. Change
            # if it's appropriate.
            'key_properties': [
                'line_id',  # artificial
                'begin_date',
                'end_date'
            ]
        }
        streams.append(catalog_entry)

    return {'streams': streams}


def tsv_to_list(tsv, column_name_modifier = None):
    lines = tsv.split('\n')
    header = [s.lower().replace(' ', '_') for s in lines[0].split('\t')]

    data = []
    for line in lines[1:]:
        if len(line) == 0:
            continue
        line_obj = {}
        line_cols = line.split('\t')
        for i, column in enumerate(header):
            if i < len(line_cols):
                line_obj[column] = line_cols[i].strip()
        data.append(line_obj)

    return data


def sync(api):
    # Write all schemas and init count to 0
    for catalog_entry in Context.catalog['streams']:
        stream_name = catalog_entry["tap_stream_id"]
        singer.write_schema(stream_name, catalog_entry['schema'], catalog_entry['key_properties'])

        Context.new_counts[stream_name] = 0
        Context.updated_counts[stream_name] = 0

    query_report(api)


def query_report(api):
    stream_name = 'summary_sales_report'
    catalog_entry = Context.get_catalog_entry(stream_name)
    stream_schema = catalog_entry['schema']

    # bookmark = datetime.fromisoformat(get_bookmark(stream_name)).replace(tzinfo=pytz.UTC)
    bookmark = datetime.strptime(get_bookmark(stream_name), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
    delta = timedelta(days=1)

    extraction_time = singer.utils.now()

    iterator = bookmark
    with Transformer(singer.UNIX_SECONDS_INTEGER_DATETIME_PARSING) as transformer:
        while iterator + delta <= extraction_time:

            iterator_str = iterator.strftime("%Y-%m-%d")
            rep_tsv = api.sales_report('SALES', 'SUMMARY', 'DAILY', Context.config['vendor'], iterator_str, '1_0')
            rep = tsv_to_list(rep_tsv)

            for index, line in enumerate(rep, start=1):
                data = line
                data['line_id'] = index
                rec = transformer.transform(data, stream_schema)

                singer.write_record(
                    stream_name,
                    rec,
                    time_extracted=extraction_time
                )

                Context.new_counts[stream_name] += 1

            singer.write_bookmark(
                Context.state,
                stream_name,
                'start_date',
                iterator.strftime(BOOKMARK_DATE_FORMAT)
            )

            singer.write_state(Context.state)

            iterator += delta

    singer.write_state(Context.state)


def get_bookmark(name):
    bookmark = singer.get_bookmark(Context.state, name, 'start_date')
    if bookmark is None:
        bookmark = Context.config['start_date']
    return bookmark


@utils.handle_top_exception(LOGGER)
def main():
    # Parse command line arguments
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)

    # If discover flag was passed, run discovery mode and dump output to stdout
    if args.discover:
        catalog = discover()
        print(json.dumps(catalog, indent=2))

    else:
        Context.tap_start = utils.now()
        if args.catalog:
            Context.catalog = args.catalog.to_dict()
        else:
            Context.catalog = discover()

        Context.config = args.config
        Context.state = args.state

        api = Api(
            Context.config['key_id'],
            Context.config['key_file'],
            Context.config['issuer_id']
        )

        sync(api)


if __name__ == '__main__':
    main()
