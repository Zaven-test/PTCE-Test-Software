"""
tc_mapping_check.py

Sidequest: verify the TC channel-to-connector name mapping used in
dual_test_runtime.py. Logs every configured thermocouple channel (HV +
FUSE) straight to InfluxDB, tagged into its own bucket
(INFLUX_BUCKET_SIDEQUEST / INFLUX_TOKEN_SIDEQUEST in .env) so it doesn't
mix into real test data.

No PSU output, no contactors -- purely a passive logging loop. Heat one
thermocouple at a time with a heat gun and watch which channel's reading
(and delta from its own baseline, printed alongside) rises, to confirm the
physical TC matches its configured name.

Ctrl+C to stop.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dual_test_runtime as dtr

# ---------------------------------------------------------------------------
# Editable test configuration
# ---------------------------------------------------------------------------
SIMULATE = False
BUCKET_TAG = 'tc_mapping_check'
LOG_INTERVAL = 0.25  # 10 s

INFLUX_TOKEN_ENV = 'INFLUX_TOKEN_SIDEQUEST'
INFLUX_BUCKET_ENV = 'INFLUX_BUCKET_SIDEQUEST'

# Same channel -> name mapping as dual_test_runtime.py's TEST_DEFINITIONS,
# copied here (not read live from dtr) so it can be freely edited for this
# sidequest without touching the main runtime config.
TC_CHANNELS = [
    ('cDAQ2Mod3/ai0',  'C01_SANTO'),
    ('cDAQ2Mod3/ai1',  'C02_COND'),
    ('cDAQ2Mod3/ai2',  'C03_SANTO'),
    ('cDAQ2Mod3/ai3',  'C04_COND'),
    ('cDAQ2Mod3/ai4',  'C05_SANTO'),
    ('cDAQ2Mod3/ai5',  'C06_COND'),
    ('cDAQ2Mod3/ai6',  'C07_SANTO'),
    ('cDAQ2Mod3/ai7',  'C08_COND'),
    ('cDAQ2Mod3/ai8',  'C09_SANTO'),
    ('cDAQ2Mod3/ai9',  'C10_COND'),
    ('cDAQ2Mod3/ai10', 'C11_SANTO'),
    ('cDAQ2Mod3/ai11', 'C12_COND'),
    ('cDAQ2Mod3/ai12', 'C13_SANTO'),
    ('cDAQ2Mod3/ai13', 'C14_COND'),
    ('cDAQ2Mod3/ai14', 'C15_SANTO'),
    ('cDAQ2Mod3/ai15', 'C16_COND'),
    ('cDAQ2Mod4/ai1',  'C18_COND'),
    ('cDAQ2Mod4/ai2',  'C19_SANTO'),
    ('cDAQ2Mod4/ai3',  'C20_SANTO'),
    ('cDAQ2Mod4/ai4',  'C21_SANTO'),
    ('cDAQ2Mod4/ai5',  'C22_SANTO'),
    ('cDAQ2Mod4/ai6',  'C23_COND'),
    ('cDAQ2Mod4/ai7',  'C24_SANTO'),
    ('cDAQ2Mod4/ai8',  'C25_SANTO'),
    ('cDAQ2Mod4/ai9',  'LUG-C26'),
    ('cDAQ2Mod4/ai10', 'TC27_CONTACTOR_AMB'),
    ('cDAQ2Mod4/ai11', 'C17_SANTO'),
    ('cDAQ2Mod4/ai12', 'TC29_CONTACTOR_2'),
    ('cDAQ2Mod4/ai13', 'TC30_CONTACTOR_3'),
    ('cDAQ2Mod4/ai15', 'TC31_TEST_AMB'),
    ('cDAQ2Mod4/ai14', 'TC28_CONTACTOR_1'),
]


def main():
    from dotenv import load_dotenv
    load_dotenv()

    tc_reader = dtr.TCReader(TC_CHANNELS, dtr.TC_TYPE, SIMULATE)
    names = tc_reader.names()

    if SIMULATE:
        writer = dtr.NullTCWriter(BUCKET_TAG)
    else:
        url = os.environ[dtr.INFLUX_URL_ENV]
        org = os.environ.get(dtr.INFLUX_ORG_ENV, dtr.DEFAULT_INFLUX_ORG)
        bucket = os.environ[INFLUX_BUCKET_ENV]
        token = os.environ[INFLUX_TOKEN_ENV]
        writer = dtr.InfluxWriter(url, org, bucket, token, BUCKET_TAG)

    baseline = None
    print(f'Logging {len(names)} TC channels to bucket tag "{BUCKET_TAG}" every {LOG_INTERVAL}s.')
    print('Heat one thermocouple at a time and watch which name shows the rising delta. Ctrl+C to stop.\n')

    try:
        while True:
            now = time.time()
            temperatures = tc_reader.read()
            writer.write_tcs(temperatures, names, now)

            if baseline is None:
                baseline = list(temperatures)

            ranked = sorted(
                zip(names, temperatures, (t - b for t, b in zip(temperatures, baseline))),
                key=lambda row: row[2],
                reverse=True,
            )
            print(f'--- {time.strftime("%H:%M:%S")} ---')
            for name, temp, delta in ranked:
                marker = '  <-- rising' if delta >= 5.0 else ''
                print(f'  {name:<22} {temp:7.2f}C  (delta {delta:+6.2f}C){marker}')
            print()

            time.sleep(LOG_INTERVAL)
    except KeyboardInterrupt:
        print('\nCtrl+C -- stopping.')
    finally:
        tc_reader.close()
        writer.close()
        print('Done: TC reader closed, InfluxDB client closed.')


if __name__ == '__main__':
    main()
