"""
delete_old_measurements.py

One-off cleanup for the TESSE-745 (HV) bucket: removes the stale
`temperatures` (plural) and `bus_state` measurements left over from the old
station-based test influx.py script. Leaves `temperature` (singular) and
`current` -- the measurements dual_test_runtime.py actively writes -- alone.

Reads connection info from .env (INFLUX_URL, INFLUX_ORG, INFLUX_TOKEN_HV,
INFLUX_BUCKET_HV). Deletion is permanent and cannot be undone.
"""

import os

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient

load_dotenv()

URL = os.environ['INFLUX_URL']
ORG = os.environ.get('INFLUX_ORG', 'tesse')
TOKEN = os.environ['INFLUX_TOKEN_HV']
BUCKET = os.environ['INFLUX_BUCKET_HV']

START = '1970-01-01T00:00:00Z'
STOP = '2100-01-01T00:00:00Z'
MEASUREMENTS_TO_DELETE = ['temperatures', 'bus_state']


def main():
    with InfluxDBClient(url=URL, token=TOKEN, org=ORG) as client:
        delete_api = client.delete_api()
        for measurement in MEASUREMENTS_TO_DELETE:
            predicate = f'_measurement="{measurement}"'
            print(f'Deleting bucket={BUCKET} predicate={predicate!r} ...')
            delete_api.delete(START, STOP, predicate, bucket=BUCKET, org=ORG)
            print('  done.')


if __name__ == '__main__':
    main()
