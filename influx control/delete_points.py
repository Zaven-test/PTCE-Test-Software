"""
delete_points.py

Delete data in a specific, narrow time window -- for surgically removing a
bad point or two (e.g. a glitch spike) rather than a whole measurement.

InfluxDB delete predicates can only match _measurement and tags, not field
values, so the way you target "one point" is a tight [START, STOP) time
window (and a tag filter if other series share that same timestamp).

START_LOCAL/STOP_LOCAL below are LOCAL time -- exactly what you'd read off
the Explorer graph/tooltips or find_bad_points.py's output. They're
converted to UTC automatically before being sent to InfluxDB.

Get exact timestamps first with find_bad_points.py or the Data Explorer's
"View Raw Data" toggle. Edit the constants below, then run:

    python delete_points.py
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

MEASUREMENT = 'current'
TAG_FILTERS = {'shunt': 'I_out', 'test': 'hv'}   # narrow to the affected series
#MEASUREMENT = 'temperature'
#TAG_FILTERS = {'channel': 'C18_COND', 'test': 'hv'}
# Bracket the bad point(s) as tightly as possible, in LOCAL time. STOP is
# exclusive. These cover the 8-point ~29,400A glitch found on the I_out
# shunt on 2026-07-10 (09:45:50.523 -> 09:48:14.691); confirmed the next
# good point is at 09:48:26.503 (~677A), so STOP is safely before it.
START_LOCAL = '2026-07-10T09:45:50.000'
STOP_LOCAL = '2026-07-10T09:48:15.000'


def local_to_utc_rfc3339(local_str):
    """'2026-07-07T13:34:08.000' (local wall-clock) -> RFC3339 UTC string."""
    naive = datetime.fromisoformat(local_str)
    return naive.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def main():
    start = local_to_utc_rfc3339(START_LOCAL)
    stop = local_to_utc_rfc3339(STOP_LOCAL)

    predicate_parts = [f'_measurement="{MEASUREMENT}"']
    predicate_parts += [f'{key}="{value}"' for key, value in TAG_FILTERS.items()]
    predicate = ' AND '.join(predicate_parts)

    print(f'Deleting bucket={BUCKET} predicate={predicate!r}')
    print(f'  local: {START_LOCAL} -> {STOP_LOCAL}')
    print(f'  utc:   {start} -> {stop}')
    with InfluxDBClient(url=URL, token=TOKEN, org=ORG) as client:
        client.delete_api().delete(start, stop, predicate, bucket=BUCKET, org=ORG)
    print('done.')


if __name__ == '__main__':
    main()
