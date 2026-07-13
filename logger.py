"""
logger.py

Always-on data logger for the dual HV/FUSE test rig. Runs completely
independently of dual_test_runtime.py -- start, stop, or restart either
process without affecting the other.

Owns the shunt current reader (the only thing that touches that hardware
now) and writes it to Influx/CSV/XLSX every LOG_INTERVAL seconds, tagged
with whatever phase/active_test/cycle dual_test_runtime.py last persisted
to STATE_FILE, and whatever PSU current/voltage it last published to
LIVE_READINGS_FILE. If dual_test_runtime.py isn't running, those files are
just stale/missing and rows get tagged idle -- logging never stops.

Run it in its own terminal alongside dual_test_runtime.py, from the same
working directory (so both see the same state/log files).
"""

import json
import os
import time

import dual_test_runtime as rt


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _read_sequence_state():
    data = _read_json(rt.STATE_FILE)
    seq = (data or {}).get('sequence', {})
    phase = seq.get('phase', rt.PHASE_IDLE)
    active_test = seq.get('active_test')
    raw_counts = seq.get('cycle_counts', {})
    cycle_counts = {name: raw_counts.get(name, 0) for name in rt.TEST_DEFINITIONS}
    return phase, active_test, cycle_counts


def _read_live_psu_reading():
    data = _read_json(rt.LIVE_READINGS_FILE) or {}
    return data.get('psu_current_a'), data.get('psu_voltage_v')


class DataLogger:
    def __init__(self):
        self._reader = rt.ShuntReader(rt.SHUNTS, rt.MAX_VOLTAGE_V, rt.SIMULATE)
        self._writers = {}
        self._load_writers()

    def _load_writers(self):
        url = os.environ[rt.INFLUX_URL_ENV]
        org = os.environ.get(rt.INFLUX_ORG_ENV, rt.DEFAULT_INFLUX_ORG)
        for name, cfg in rt.TEST_DEFINITIONS.items():
            bucket = os.environ.get(cfg['influx_bucket_env'])
            if not bucket:
                raise RuntimeError(f'Missing env var {cfg["influx_bucket_env"]} for {name}')
            token = os.environ.get(cfg['influx_token_env'])
            if not token:
                raise RuntimeError(f'Missing env var {cfg["influx_token_env"]} for {name}')
            if rt.SIMULATE:
                self._writers[name] = rt.NullWriter(cfg['bucket_tag'])
            else:
                self._writers[name] = rt.InfluxWriter(url, org, bucket, token, cfg['bucket_tag'])
            self._writers[f'{name}_csv'] = rt.CsvLogger(name, cfg['label'])
            self._writers[f'{name}_xlsx'] = rt.ExcelLogger(name, cfg['label'])

    def log_once(self):
        timestamp = time.time()
        phase, active_test, cycle_counts = _read_sequence_state()
        psu_current, psu_voltage = _read_live_psu_reading()
        current_owner = active_test if phase == rt.PHASE_FLOW else None
        try:
            currents = self._reader.read_currents()
        except Exception as exc:
            print(f'  [logger error] {type(exc).__name__}: {exc}')
            return
        for name in rt.TEST_DEFINITIONS:
            row_phase = phase if name == active_test else rt.PHASE_IDLE
            cycle = cycle_counts[name]
            self._writers[name].write_currents(currents, rt.SHUNTS, timestamp)
            self._writers[f'{name}_csv'].write_currents(
                currents, rt.SHUNTS, timestamp,
                cycle=cycle,
                phase=row_phase,
                current_owner=current_owner,
                psu_current=psu_current,
                psu_voltage=psu_voltage,
            )
            self._writers[f'{name}_xlsx'].write_currents(
                currents, rt.SHUNTS, timestamp,
                cycle=cycle,
                phase=row_phase,
                current_owner=current_owner,
                psu_current=psu_current,
                psu_voltage=psu_voltage,
            )

    def run(self):
        print('Data logger running. Ctrl+C to stop.')
        try:
            while True:
                self.log_once()
                time.sleep(rt.LOG_INTERVAL)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    def close(self):
        self._reader.close()
        for writer in self._writers.values():
            writer.close()
        print('Logger shutdown complete.')


def main():
    from dotenv import load_dotenv
    load_dotenv()
    DataLogger().run()


if __name__ == '__main__':
    main()
