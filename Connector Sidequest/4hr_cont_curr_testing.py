"""
4hr_cont_curr_testing.py

Sidequest: hold a constant current through the HV connector test setup for
4 hours straight, logging shunt current and all HV thermocouples to
InfluxDB. Reuses the exact HV hardware config (staggered contactors, shunt,
31 TC channels, over-temp trip limits) from dual_test_runtime.py -- only
the InfluxDB destination is different: this logs to its own bucket
(INFLUX_BUCKET_SIDEQUEST / INFLUX_TOKEN_SIDEQUEST in .env) instead of the
shared HV bucket, so this sidequest run doesn't mix into the normal test
data.

Ctrl+C (or an over-temp trip) zeroes the PSU and opens the contactors
immediately, same as the main runtime.
"""

import os
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dual_test_runtime as dtr

# ---------------------------------------------------------------------------
# Editable test configuration
# ---------------------------------------------------------------------------
SIMULATE = False
HV_CFG = dtr.TEST_DEFINITIONS['HV']
HOLD_CURRENT_A = 450                         # A -- edit to set this test's hold current
DURATION_S = 4 * 60 * 60                     # 4 hours
LOG_INTERVAL = dtr.LOG_INTERVAL              # 10 s, current + status
TC_INTERVAL = dtr.TC_INTERVAL                # 10 s, thermocouples
BUCKET_TAG = 'amphenol_4hr'                  # tag value on every point written

# Thermocouple channels for this sidequest, as (cDAQ channel, Influx name)
# tuples. Edit freely -- this list is independent of dual_test_runtime.py's
# TEST_DEFINITIONS['HV']['tc_channels'], so changes here don't affect the
# main dual test runtime. The contactor-mounted TCs (formerly TC28/29/30)
# were removed for this run and repurposed onto connector C04, so only
# TC27_CONT_AMB still gets the tighter ambient limit below -- see
# _tc_temp_limit().
TC_CHANNELS = [
    ('cDAQ2Mod3/ai0',  'C01_SANTO'),
    ('cDAQ2Mod3/ai1',  'C02_COND'),
    ('cDAQ2Mod3/ai2',  'C03_SANTO'),
    ('cDAQ2Mod3/ai3',  'C04_COND'),
    ('cDAQ2Mod3/ai4',  'C05_SANTO'),
    ('cDAQ2Mod3/ai5',  'C06_COND'),
    ('cDAQ2Mod3/ai6',  'C07_SANTO'),
    ('cDAQ2Mod3/ai7',  'C08_SANTO'),
    ('cDAQ2Mod3/ai8',  'C09_SANTO'),
    ('cDAQ2Mod3/ai9',  'C10_COND'),
    ('cDAQ2Mod3/ai10', 'C11_SANTO'),
    ('cDAQ2Mod3/ai11', 'C12_COND'),
    ('cDAQ2Mod3/ai12', 'C13_SANTO'),
    ('cDAQ2Mod3/ai13', 'C14_COND'),
    ('cDAQ2Mod3/ai14', 'C15_SANTO'),
    ('cDAQ2Mod3/ai15', 'C16_SANTO'),
    ('cDAQ2Mod4/ai11', 'C17_SANTO'),
    ('cDAQ2Mod4/ai1',  'C18_COND'),
    ('cDAQ2Mod4/ai2',  'C19_SANTO'),
    ('cDAQ2Mod4/ai3',  'C20_SANTO'),
    ('cDAQ2Mod4/ai4',  'C21_SANTO'),
    ('cDAQ2Mod4/ai5',  'C22_SANTO'),
    ('cDAQ2Mod4/ai6',  'C23_COND'),
    ('cDAQ2Mod4/ai7',  'C24_SANTO'),
    ('cDAQ2Mod4/ai8',  'C25_SANTO'),
    ('cDAQ2Mod4/ai9',  'LUG-C26_SANTO'),
    ('cDAQ2Mod4/ai10', 'TC27_CONTACTOR_AMB'),
    ('cDAQ2Mod4/ai12', 'C04_SANTO'),
    ('cDAQ2Mod4/ai13', 'C04_RECEPTACLE'),
    ('cDAQ2Mod4/ai14', 'C04_CABLEINSULATION'),
    ('cDAQ2Mod4/ai15', 'TC31_TEST_AMB'),
]

INFLUX_TOKEN_ENV = 'INFLUX_TOKEN_SIDEQUEST'
INFLUX_BUCKET_ENV = 'INFLUX_BUCKET_SIDEQUEST'

# Over-temp trip limits for TC_CHANNELS above, defined locally instead of
# relying on dtr._tc_temp_limit()'s name matching against
# dual_test_runtime.py's own CONTACTOR_TC_NAMES set (which no longer applies
# now that the contactor-mounted TCs are gone from this list).
AMBIENT_TC_NAMES = {'TC27_CONT_AMB'}
AMBIENT_MAX_TEMP_C = dtr.CONTACTOR_AMBIENT_MAX_TEMP_C  # 75.0
SAMPLE_MAX_TEMP_C = dtr.SAMPLE_MAX_TEMP_C              # 155.0


def _tc_temp_limit(name):
    if name in AMBIENT_TC_NAMES:
        return AMBIENT_MAX_TEMP_C
    return SAMPLE_MAX_TEMP_C


def main():
    from dotenv import load_dotenv
    load_dotenv()

    stop_event = threading.Event()

    def handle_signal(signum, frame):
        print(f'\nReceived signal {signum}, shutting down...')
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    psu = dtr.PSU(dtr.PSU_VISA_ADDRESS, dtr.PSU_VOLTAGE_LIMIT, SIMULATE)
    contactors = dtr.ContactorGroup(
        [dtr.Contactor(ch, SIMULATE, f"{HV_CFG['label']} #{i + 1}")
         for i, ch in enumerate(HV_CFG['contactor_channel'])],
        stagger_s=dtr.CONTACTOR_STAGGER_S,
    )
    shunt_reader = dtr.ShuntReader(dtr.SHUNTS, dtr.MAX_VOLTAGE_V, SIMULATE)
    tc_reader = dtr.TCReader(TC_CHANNELS, dtr.TC_TYPE, SIMULATE)

    if SIMULATE:
        writer = dtr.NullWriter(BUCKET_TAG)
        tc_writer = dtr.NullTCWriter(BUCKET_TAG)
    else:
        url = os.environ[dtr.INFLUX_URL_ENV]
        org = os.environ.get(dtr.INFLUX_ORG_ENV, dtr.DEFAULT_INFLUX_ORG)
        bucket = os.environ[INFLUX_BUCKET_ENV]
        token = os.environ[INFLUX_TOKEN_ENV]
        writer = dtr.InfluxWriter(url, org, bucket, token, BUCKET_TAG)
        tc_writer = writer

    tc_over_counts = {}

    def check_tc_safety(names, temperatures):
        tripped = []
        for name, temp in zip(names, temperatures):
            limit = _tc_temp_limit(name)
            if temp >= limit:
                count = tc_over_counts.get(name, 0) + 1
                tc_over_counts[name] = count
                if count >= dtr.OVER_TEMP_TRIP_READINGS:
                    tripped.append((name, temp, limit))
            else:
                tc_over_counts[name] = 0
        return tripped

    try:
        print(f"Closing {HV_CFG['label']} contactors...")
        contactors.energize()
        print('Contactors closed.\n')

        print(f'PSU output on, setting {HOLD_CURRENT_A} A...')
        psu.output_on()
        psu.set_current(HOLD_CURRENT_A)

        start = time.time()
        deadline = start + DURATION_S
        last_tc_log = 0.0
        print(f'Flowing {HOLD_CURRENT_A} A for {DURATION_S / 3600:.1f} h. Ctrl+C to stop early.\n')

        while not stop_event.is_set() and time.time() < deadline:
            now = time.time()
            try:
                currents = shunt_reader.read_currents()
                psu_current, psu_voltage = psu.measure()
                writer.write_currents(currents, dtr.SHUNTS, now)
                remaining_min = (deadline - now) / 60.0
                print(f'  t={now - start:7.1f}s  I_out={currents[0]:7.2f}A  '
                      f'PSU={psu_current:7.2f}A/{psu_voltage:5.2f}V  remain={remaining_min:6.1f}min')
            except Exception as exc:
                print(f'  [logger error] {type(exc).__name__}: {exc}')

            if now - last_tc_log >= TC_INTERVAL:
                try:
                    temperatures = tc_reader.read()
                    names = tc_reader.names()
                    tc_writer.write_tcs(temperatures, names, now)
                    tripped = check_tc_safety(names, temperatures)
                    if tripped:
                        details = ', '.join(f'{n}={t:.1f}C (limit {lim:.1f}C)' for n, t, lim in tripped)
                        print(f'\n!! OVER-TEMP TRIP: {details} — stopping test !!')
                        stop_event.set()
                except Exception as exc:
                    print(f'  [tc logger error] {type(exc).__name__}: {exc}')
                last_tc_log = now

            stop_event.wait(LOG_INTERVAL)

        if time.time() >= deadline:
            print('\n4-hour duration complete.')
    except KeyboardInterrupt:
        print('\nCtrl+C.')
    finally:
        print('\nStopping — zeroing PSU, opening contactors...')
        psu.shutdown('test stopped')
        contactors.de_energize()
        contactors.close()
        shunt_reader.close()
        tc_reader.close()
        writer.close()
        psu.close()
        print('Done: PSU off, contactors open, InfluxDB client closed.')


if __name__ == '__main__':
    main()
