"""
find_bad_points.py

Query a bucket for points outside a normal value range so you can grab their
exact timestamps before deleting them with delete_points.py. Prints
_time / _measurement / tags / _value for anything matching the filter.

All times here are LOCAL time (this machine's timezone), matching what the
Data Explorer graph shows -- RANGE_START (if given as an absolute time
rather than a relative duration) and the printed results are both local.
Conversion to/from UTC for the InfluxDB API happens automatically.

Edit the constants below, then run:  python find_bad_points.py
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient

load_dotenv()

URL = os.environ['INFLUX_URL']
ORG = os.environ.get('INFLUX_ORG', 'tesse')
TOKEN = os.environ['INFLUX_TOKEN_HV']
BUCKET = os.environ['INFLUX_BUCKET_HV']

# Flux relative duration (e.g. '-24h'), OR an absolute LOCAL time (e.g.
# '2026-07-07T10:00:00.000') -- whatever you'd read off the Explorer graph.
RANGE_START = '-24h'
MEASUREMENT = 'current'
#FIELD = '_field'
VALUE_ABOVE = 1000              # well above normal (~677A) but below the ~23kA glitch -- only show points with _value > this
TAG_FILTERS = {'test': 'hv'}  # e.g. {'shunt': 'I_out'} to narrow to one series


def local_to_utc_rfc3339(local_str):
    """'2026-07-07T13:34:08.000' (local wall-clock) -> RFC3339 UTC string."""
    naive = datetime.fromisoformat(local_str)
    return naive.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def utc_to_local_display(dt):
    return dt.astimezone().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


def main():
    range_start = RANGE_START if RANGE_START.startswith('-') else local_to_utc_rfc3339(RANGE_START)

    tag_filter = ''.join(
        f' |> filter(fn: (r) => r.{key} == "{value}")'
        for key, value in TAG_FILTERS.items()
    )
    query = f'''
from(bucket: "{BUCKET}")
  |> range(start: {range_start})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")
  |> filter(fn: (r) => r._value > {VALUE_ABOVE}){tag_filter}
  |> sort(columns: ["_time"])
'''
    with InfluxDBClient(url=URL, token=TOKEN, org=ORG) as client:
        tables = client.query_api().query(query, org=ORG)
        count = 0
        for table in tables:
            for record in table.records:
                count += 1
                tags = {k: v for k, v in record.values.items()
                        if k not in ('_time', '_value', '_field', '_measurement',
                                     '_start', '_stop', 'result', 'table')}
                print(f'{utc_to_local_display(record.get_time())} (local)  '
                      f'value={record.get_value()}  tags={tags}')
        print(f'\n{count} point(s) matched.')


if __name__ == '__main__':
    main()
