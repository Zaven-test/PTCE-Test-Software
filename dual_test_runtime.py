"""
dual_test_runtime.py

Modular runtime for two independent current-flow tests:
- HV Connectors test (Contactor 2 / HV enable)
- CCA Fuses test (Contactor 1 / fuse close)

Each test has editable runtime variables, separate Influx buckets, and
persistent cycle state so the script can pause/resume and recover after
restart.
"""

import csv
import json
import math
import os
import signal
import threading
import time
import traceback
from datetime import datetime

# ---------------------------------------------------------------------------
# Editable test configuration
# ---------------------------------------------------------------------------
SIMULATE = False
PSU_VISA_ADDRESS = 'USB0::0x2EC7::0x3900::805240031807340017::INSTR'
PSU_VOLTAGE_LIMIT = 10.0
RAMP_STEP_A = 10.0
RAMP_STEP_DELAY_S = 0.25
SHUNT_RESISTANCE_OHM = 50e-6
MAX_VOLTAGE_V = 10.0
LOG_INTERVAL = 10
STATE_SAVE_INTERVAL_S = 5
STATE_FILE = 'dual_test_state.json'
HISTORY_FILE = 'dual_test_history.log'
LOG_FILE_XLSX = 'dual_test_log.xlsx'
LOG_FILE_CSV_TEMPLATE = 'dual_test_log_{test}.csv'

# Set the contactor lines for each test.
TEST_DEFINITIONS = {
    'HV': {
        'label': 'HV Connectors',
        'contactor_channel': 'cDAQ2Mod1/line2',
        'enabled': True,
        'auto_start': False,
        'influx_bucket_env': 'INFLUX_BUCKET_HV',
        'max_current_a': 2.0,
        'flow_duration_s': 30.0,
        'pause_duration_s': 60.0,
        'bucket_tag': 'hv',
        'tc_channels': [
            ('cDAQ2Mod3/ai0', 'C01_SANTO'),
            ('cDAQ2Mod3/ai1', 'C02_SANTO'),
            ('cDAQ2Mod3/ai2', 'C03_SANTO'),
            ('cDAQ2Mod3/ai3', 'C04_SANTO'),
            ('cDAQ2Mod3/ai4', 'C05_SANTO'),
            ('cDAQ2Mod3/ai5', 'C06_SANTO'),
            ('cDAQ2Mod3/ai6', 'C07_SANTO'),
            ('cDAQ2Mod3/ai7', 'C08_SANTO'),
            ('cDAQ2Mod3/ai8', 'C09_SANTO'),
            ('cDAQ2Mod3/ai9', 'C10_SANTO'),
            ('cDAQ2Mod3/ai10', 'C11_SANTO'),
            ('cDAQ2Mod3/ai11', 'C12_SANTO'),
            ('cDAQ2Mod3/ai12', 'C13_SANTO'),
            ('cDAQ2Mod3/ai13', 'C14_SANTO'),
            ('cDAQ2Mod3/ai14', 'C15_SANTO'),
            ('cDAQ2Mod3/ai15', 'C16_SANTO'),
            ('cDAQ2Mod4/ai0', 'C17_SANTO'),
            ('cDAQ2Mod4/ai1', 'C18_COND'),
            ('cDAQ2Mod4/ai2', 'C19_SANTO'),
            ('cDAQ2Mod4/ai3', 'C20_COND'),
            ('cDAQ2Mod4/ai4', 'C21_SANTO'),
            ('cDAQ2Mod4/ai5', 'C22_COND'),
            ('cDAQ2Mod4/ai6', 'C23_COND'),
            ('cDAQ2Mod4/ai7', 'C24_SANTO'),
            ('cDAQ2Mod4/ai8', 'C25_SANTO'),
            ('cDAQ2Mod4/ai9', 'LUG-C26_SANTO'),
        ],
    },
    'FUSE': {
        'label': 'CCA Fuses',
        'contactor_channel': 'cDAQ2Mod1/line1',
        'enabled': True,
        'auto_start': False,
        'influx_bucket_env': 'INFLUX_BUCKET_FUSE',
        'max_current_a': 1.5,
        'flow_duration_s': 20.0,
        'pause_duration_s': 40.0,
        'bucket_tag': 'fuse',
        'tc_channels': [],
    },
}

SHUNTS = [
    {'channel': 'cDAQ2Mod2/ai0', 'name': 'I_out',  'resistance_ohm': SHUNT_RESISTANCE_OHM},
]

TC_TYPE = 'T'
TC_INTERVAL = 10

INFLUX_URL_ENV = 'INFLUX_URL'
INFLUX_TOKEN_ENV = 'INFLUX_TOKEN_A'
INFLUX_ORG_ENV = 'INFLUX_ORG'
DEFAULT_INFLUX_ORG = 'tesse'

# ---------------------------------------------------------------------------
# Constants and helpers
# ---------------------------------------------------------------------------
PHASE_IDLE = 'idle'
PHASE_FLOW = 'flow'
PHASE_PAUSE = 'pause'

class TestState:
    def __init__(self, name, config):
        self.name = name
        self.label = config['label']
        self.config = config
        self.cycle = 0
        self.phase = PHASE_IDLE
        self.manual_pause = False
        self.phase_started_at = None
        self.phase_deadline = None
        self.remaining_s = None
        self.last_saved_at = None

    def to_dict(self):
        return {
            'name': self.name,
            'cycle': self.cycle,
            'phase': self.phase,
            'manual_pause': self.manual_pause,
            'phase_started_at': self.phase_started_at,
            'phase_deadline': self.phase_deadline,
            'remaining_s': self.remaining_s,
            'last_saved_at': self.last_saved_at,
        }

    @classmethod
    def from_dict(cls, config, data):
        state = cls(data['name'], config)
        state.cycle = data.get('cycle', 0)
        state.phase = data.get('phase', PHASE_IDLE)
        state.manual_pause = data.get('manual_pause', False)
        state.phase_started_at = data.get('phase_started_at')
        state.phase_deadline = data.get('phase_deadline')
        state.remaining_s = data.get('remaining_s')
        state.last_saved_at = data.get('last_saved_at')
        return state

    def is_active(self):
        return self.phase == PHASE_FLOW and not self.manual_pause

    def is_waiting_for_power(self):
        return self.phase == PHASE_FLOW and not self.manual_pause and self.phase_deadline is not None

    def is_paused(self):
        return self.manual_pause

    def is_flowing(self):
        return self.phase == PHASE_FLOW

    def is_in_pause(self):
        return self.phase == PHASE_PAUSE

    def duration_for_phase(self):
        if self.phase == PHASE_FLOW:
            return self.config['flow_duration_s']
        if self.phase == PHASE_PAUSE:
            return self.config['pause_duration_s']
        return 0.0

    def readable_status(self, active_owner_name=None):
        if self.phase == PHASE_IDLE:
            return 'idle'
        if self.is_paused():
            return f'paused ({self.phase})'
        if self.phase == PHASE_FLOW:
            if active_owner_name == self.name:
                return 'flowing'
            return 'waiting for power'
        return self.phase

    def current_bucket_tag(self):
        return self.config['bucket_tag']

    def to_status_line(self, active_owner_name=None):
        remaining = self.phase_deadline - time.time() if self.phase_deadline else self.remaining_s
        remaining_text = f'{remaining:.1f}s' if remaining is not None else 'n/a'
        active_text = 'owner' if active_owner_name == self.name else ''
        return (f'{self.label:<12} | cycle {self.cycle:3d} | phase {self.phase:<5} | '
                f'{"paused" if self.manual_pause else "running" :<7} | remain {remaining_text:<7} {active_text}')


class StateManager:
    def __init__(self, state_file):
        self.state_file = state_file
        self.lock = threading.RLock()
        self.states = {}

    def load(self, configs):
        if os.path.isfile(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                tests = raw.get('tests', {})
                for name, cfg in configs.items():
                    if name in tests:
                        self.states[name] = TestState.from_dict(cfg, tests[name])
                    else:
                        self.states[name] = TestState(name, cfg)
            except Exception:
                print('Warning: failed to load state file, starting fresh.')
                self.states = {name: TestState(name, cfg) for name, cfg in configs.items()}
        else:
            self.states = {name: TestState(name, cfg) for name, cfg in configs.items()}

        now = time.time()
        for state in self.states.values():
            if state.phase_deadline is not None and not state.manual_pause:
                remaining = state.phase_deadline - now
                if remaining <= 0:
                    state.phase_deadline = now
                else:
                    state.remaining_s = None
            if state.manual_pause and state.remaining_s is None:
                state.remaining_s = state.duration_for_phase()

    def save(self):
        with self.lock:
            output = {'tests': {name: state.to_dict() for name, state in self.states.items()},
                      'saved_at': time.time()}
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(output, f, indent=2)

    def get(self, name):
        return self.states[name]

    def all_states(self):
        return list(self.states.values())


# ---------------------------------------------------------------------------
# Instrument classes
# ---------------------------------------------------------------------------
class PSU:
    def __init__(self, visa_address, voltage_limit, simulate):
        self.simulate = simulate
        self._voltage_limit = voltage_limit
        self._current_sp = 0.0
        self._psu = None

        if simulate:
            print(f'  [sim] PSU initialised ({visa_address}) — output OFF, 0 A')
            return

        import pyvisa
        rm = pyvisa.ResourceManager()
        psu = rm.open_resource(visa_address)
        psu.timeout = 5000
        psu.write_termination = '\n'
        psu.read_termination = '\n'
        idn = psu.query('*IDN?').strip()
        print(f'  PSU connected: {idn}')
        self._psu = psu
        psu.write(f'SOUR:VOLT {voltage_limit:.3f}')
        psu.write('SOUR:CURR 0')
        psu.write('OUTP OFF')

    def output_on(self):
        if self.simulate:
            print('  [sim] PSU output ON')
            return
        self._psu.write('OUTP ON')

    def set_current(self, amps):
        self._current_sp = float(amps)
        if self.simulate:
            print(f'  [sim] PSU → {amps:.3f} A')
            return
        self._psu.write(f'SOUR:CURR {amps:.3f}')

    def measure(self):
        if self.simulate:
            return (self._current_sp + 0.1, 3.0)
        current_resp = self._psu.query('MEAS:CURR?').strip()
        voltage_resp = self._psu.query('MEAS:VOLT?').strip()
        try:
            i = float(current_resp)
            v = float(voltage_resp)
        except ValueError:
            raise ValueError(
                f"Unexpected PSU response during measurement: CURR?='{current_resp}', VOLT?='{voltage_resp}'"
            )
        return i, v

    def shutdown(self, reason=''):
        if self.simulate:
            print(f'  [sim] PSU shutdown{f" — {reason}" if reason else ""}')
            self._current_sp = 0.0
            return
        if self._psu is None:
            return
        for attempt in range(3):
            try:
                self._psu.write('SOUR:CURR 0')
                self._psu.write('OUTP OFF')
                self._current_sp = 0.0
                print(f'  PSU output OFF{f" — {reason}" if reason else ""}')
                return
            except Exception as exc:
                print(f'  [PSU] shutdown attempt {attempt + 1}/3 failed: {exc}')
                time.sleep(0.5)
        print('  !! PSU SHUTDOWN FAILED — MANUALLY VERIFY OUTPUT IS OFF !!')

    def close(self):
        self.shutdown()
        if self._psu is not None:
            self._psu.close()
            self._psu = None


class Contactor:
    def __init__(self, channel, simulate, label):
        self.channel = channel
        self.simulate = simulate
        self.label = label
        self._task = None

        if simulate:
            print(f'  [sim] Contactor {label} ({channel}) initialised — OPEN')
            return

        import nidaqmx
        self._task = nidaqmx.Task()
        self._task.do_channels.add_do_chan(channel)
        self._task.start()
        self._write(False)

    def energize(self):
        if self.simulate:
            print(f'  [sim] Contactor {self.label} CLOSED')
            return
        self._write(True)

    def de_energize(self):
        if self.simulate:
            print(f'  [sim] Contactor {self.label} OPENED')
            return
        self._write(False)

    def _write(self, state):
        self._task.write(state)

    def close(self):
        if self._task is not None:
            self._write(False)
            self._task.close()
            self._task = None


class ShuntReader:
    def __init__(self, shunts, max_voltage, simulate):
        self.shunts = shunts
        self.simulate = simulate
        self._t0 = time.time()
        self._task = None

        if simulate:
            return

        import nidaqmx
        from nidaqmx.constants import TerminalConfiguration
        self._task = nidaqmx.Task()
        for s in shunts:
            self._task.ai_channels.add_ai_voltage_chan(
                s['channel'],
                terminal_config=TerminalConfiguration.DIFF,
                min_val=-max_voltage,
                max_val=max_voltage,
            )
        self._task.start()

    def read_currents(self):
        if self.simulate:
            elapsed = time.time() - self._t0
            nominal = min(15.0, 15.0 * elapsed / 10.0)
            return [nominal + 0.1 * (i - 1) for i in range(len(self.shunts))]

        raw = self._task.read()
        voltages = raw if isinstance(raw, list) else [raw]
        return [v / s['resistance_ohm'] for v, s in zip(voltages, self.shunts)]

    def close(self):
        if self._task is not None:
            self._task.close()
            self._task = None


class TCReader:
    def __init__(self, channels, tc_type, simulate):
        self.channels = channels
        self.tc_type = tc_type
        self.simulate = simulate
        self._t0 = time.time()
        self._task = None

        if simulate:
            return

        import nidaqmx
        from nidaqmx.constants import ThermocoupleType, TemperatureUnits
        tmap = {
            'J': ThermocoupleType.J,
            'K': ThermocoupleType.K,
            'T': ThermocoupleType.T,
            'E': ThermocoupleType.E,
            'N': ThermocoupleType.N,
        }
        self._task = nidaqmx.Task()
        for channel, _ in channels:
            self._task.ai_channels.add_ai_thrmcpl_chan(
                channel,
                thermocouple_type=tmap[tc_type],
                units=TemperatureUnits.DEG_C,
            )
        self._task.start()

    def read(self):
        if self.simulate:
            elapsed = time.time() - self._t0
            return [25.0 + 2.0 * i + 0.5 * (math.sin(elapsed) if i % 2 == 0 else math.cos(elapsed))
                    for i in range(len(self.channels))]

        raw = self._task.read()
        return raw if isinstance(raw, list) else [raw]

    def names(self):
        return [name for _, name in self.channels]

    def close(self):
        if self._task is not None:
            self._task.close()
            self._task = None


class InfluxWriter:
    def __init__(self, url, org, bucket, token, test_tag):
        import influxdb_client
        from influxdb_client.client.write_api import SYNCHRONOUS
        self._ic = influxdb_client
        self._client = influxdb_client.InfluxDBClient(url=url, token=token, org=org)
        self._writer = self._client.write_api(write_options=SYNCHRONOUS)
        self._bucket = bucket
        self._org = org
        self._test_tag = test_tag

    def write_currents(self, currents, shunts, timestamp):
        ts_ms = round(timestamp * 1000.0)
        points = []
        for current, s in zip(currents, shunts):
            points.append(
                self._ic.Point('current')
                .tag('test', self._test_tag)
                .tag('shunt', s['name'])
                .field('amps', float(current))
                .time(ts_ms, self._ic.WritePrecision.MS)
            )
        self._writer.write(bucket=self._bucket, org=self._org, record=points)

    def write_tcs(self, temperatures, names, timestamp):
        ts_ms = round(timestamp * 1000.0)
        points = []
        for name, temp in zip(names, temperatures):
            points.append(
                self._ic.Point('temperature')
                .tag('test', self._test_tag)
                .tag('channel', name)
                .field('deg_c', float(temp))
                .time(ts_ms, self._ic.WritePrecision.MS)
            )
        self._writer.write(bucket=self._bucket, org=self._org, record=points)

    def close(self):
        self._client.close()


class NullTCWriter:
    def __init__(self, test_tag):
        self._test_tag = test_tag

    def write_tcs(self, temperatures, names, timestamp):
        values = ', '.join(f'{name}={temp:.2f}C' for name, temp in zip(names, temperatures))
        print(f'  [tc log {self._test_tag}] {values}')

    def close(self):
        pass


class CsvLogger:
    def __init__(self, test_name, test_label):
        self.test_name = test_name
        self.test_label = test_label
        self.filename = LOG_FILE_CSV_TEMPLATE.format(test=test_name.lower())
        self._ensure_file()

    def _ensure_file(self):
        if not os.path.isfile(self.filename):
            with open(self.filename, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp',
                    'cycle',
                    'test',
                    'phase',
                    'current_owner',
                    'psu_current_a',
                    'psu_voltage_v',
                    'shunt_name',
                    'shunt_amps',
                ])

    def write_currents(self, currents, shunts, timestamp, cycle, phase, current_owner, psu_current, psu_voltage):
        iso_ts = datetime.fromtimestamp(timestamp).isoformat(sep=' ')
        rows = []
        for current, s in zip(currents, shunts):
            rows.append([
                iso_ts,
                cycle,
                self.test_name,
                phase,
                current_owner or '',
                round(psu_current, 6) if psu_current is not None else '',
                round(psu_voltage, 6) if psu_voltage is not None else '',
                s['name'],
                round(current, 6),
            ])
        with open(self.filename, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(rows)

    def close(self):
        pass


class ExcelLogger:
    def __init__(self, test_name, test_label):
        self.test_name = test_name
        self.test_label = test_label
        self.filename = LOG_FILE_XLSX
        self.sheet_name = test_name.upper()
        self._workbook = None
        self._worksheet = None
        self._init_workbook()

    def _init_workbook(self):
        try:
            from openpyxl import Workbook, load_workbook
        except ImportError:
            self._workbook = None
            return

        if os.path.isfile(self.filename):
            try:
                self._workbook = load_workbook(self.filename)
            except Exception:
                self._workbook = Workbook()
        else:
            self._workbook = Workbook()

        if self.sheet_name in self._workbook.sheetnames:
            self._worksheet = self._workbook[self.sheet_name]
        else:
            self._worksheet = self._workbook.create_sheet(self.sheet_name)
            self._worksheet.append([
                'timestamp',
                'cycle',
                'test',
                'phase',
                'current_owner',
                'psu_current_a',
                'psu_voltage_v',
                'shunt_name',
                'shunt_amps',
            ])

    def write_currents(self, currents, shunts, timestamp, cycle, phase, current_owner, psu_current, psu_voltage):
        if self._workbook is None or self._worksheet is None:
            return
        iso_ts = datetime.fromtimestamp(timestamp).isoformat(sep=' ')
        for current, s in zip(currents, shunts):
            self._worksheet.append([
                iso_ts,
                cycle,
                self.test_name,
                phase,
                current_owner or '',
                round(psu_current, 6) if psu_current is not None else '',
                round(psu_voltage, 6) if psu_voltage is not None else '',
                s['name'],
                round(current, 6),
            ])

    def close(self):
        if self._workbook is not None:
            try:
                self._workbook.save(self.filename)
            except Exception as exc:
                print(f'  [ExcelLogger] failed to save {self.filename}: {exc}')


class NullWriter:
    def __init__(self, test_tag):
        self.test_tag = test_tag

    def write_currents(self, currents, shunts, timestamp, cycle=None, phase=None, current_owner=None):
        values = '  '.join(f"{s['name']}={c:.2f}A" for c, s in zip(currents, shunts))
        print(f'  [log {self.test_tag}] {values}')

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Runtime manager
# ---------------------------------------------------------------------------
class DualTestRuntime:
    def __init__(self):
        self._stop_event = threading.Event()
        self._state_manager = StateManager(STATE_FILE)
        self._state_manager.load(TEST_DEFINITIONS)
        self._writers = {}
        self._tc_readers = {}
        self._tc_writers = {}
        self._contactors = {}
        self._psu = PSU(PSU_VISA_ADDRESS, PSU_VOLTAGE_LIMIT, SIMULATE)
        self._reader = ShuntReader(SHUNTS, MAX_VOLTAGE_V, SIMULATE)
        self._last_state_save = time.time()
        self._last_tc_log = time.time()
        self._current_owner = None
        self._load_influx_writers()
        self._create_contactors()
        self._start_if_needed()

    def _load_influx_writers(self):
        url = os.environ[INFLUX_URL_ENV]
        token = os.environ[INFLUX_TOKEN_ENV]
        org = os.environ.get(INFLUX_ORG_ENV, DEFAULT_INFLUX_ORG)
        for name, cfg in TEST_DEFINITIONS.items():
            bucket = os.environ.get(cfg['influx_bucket_env'])
            if not bucket:
                raise RuntimeError(f'Missing env var {cfg["influx_bucket_env"]} for {name}')
            if SIMULATE:
                self._writers[name] = NullWriter(cfg['bucket_tag'])
            else:
                self._writers[name] = InfluxWriter(url, org, bucket, token, cfg['bucket_tag'])
            self._writers[f'{name}_csv'] = CsvLogger(name, cfg['label'])
            self._writers[f'{name}_xlsx'] = ExcelLogger(name, cfg['label'])
            if cfg.get('tc_channels'):
                if SIMULATE:
                    self._tc_writers[name] = NullTCWriter(cfg['bucket_tag'])
                else:
                    self._tc_writers[name] = InfluxWriter(url, org, bucket, token, cfg['bucket_tag'])
                self._tc_readers[name] = TCReader(cfg['tc_channels'], TC_TYPE, SIMULATE)

    def _create_contactors(self):
        for name, cfg in TEST_DEFINITIONS.items():
            self._contactors[name] = Contactor(cfg['contactor_channel'], SIMULATE, cfg['label'])

    def _start_if_needed(self):
        now = time.time()
        for state in self._state_manager.all_states():
            cfg = state.config
            if state.phase == PHASE_IDLE and cfg.get('auto_start', False):
                self._transition_to_flow(state, now)

    def _transition_to_flow(self, state, now):
        if state.phase != PHASE_FLOW:
            state.phase = PHASE_FLOW
            state.cycle += 1
            state.phase_started_at = now
            state.remaining_s = state.config['flow_duration_s']
            state.phase_deadline = now + state.remaining_s
            state.manual_pause = False
            self._append_history(state, 'flow_start')
            self._state_manager.save()

    def _transition_to_pause(self, state, now):
        state.phase = PHASE_PAUSE
        state.phase_started_at = now
        state.remaining_s = state.config['pause_duration_s']
        state.phase_deadline = now + state.remaining_s
        state.manual_pause = False
        self._append_history(state, 'pause_start')
        self._state_manager.save()

    def _transition_to_idle(self, state, now):
        state.phase = PHASE_IDLE
        state.phase_started_at = now
        state.phase_deadline = None
        state.remaining_s = None
        state.manual_pause = False
        self._append_history(state, 'idle')
        self._state_manager.save()

    def _append_history(self, state, event):
        entry = {
            'timestamp': time.time(),
            'test': state.name,
            'event': event,
            'cycle': state.cycle,
            'phase': state.phase,
            'manual_pause': state.manual_pause,
        }
        with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')

    def _choose_power_owner(self):
        candidates = [s for s in self._state_manager.all_states()
                      if s.phase == PHASE_FLOW and not s.manual_pause]
        if not candidates:
            return None
        return min(candidates, key=lambda s: s.phase_started_at or float('inf'))

    def _apply_scheduler(self, now):
        owner = self._choose_power_owner()
        self._current_owner = owner.name if owner else None

        for state in self._state_manager.all_states():
            contactor = self._contactors[state.name]
            if owner is state:
                contactor.energize()
            else:
                contactor.de_energize()

        if owner is None:
            self._psu.shutdown('no active test')
            return

        self._psu.output_on()
        self._psu.set_current(owner.config['max_current_a'])

    def _update_phase_for_state(self, state, now):
        if state.phase == PHASE_IDLE:
            return
        if state.manual_pause:
            if state.phase_deadline is not None:
                state.remaining_s = max(0.0, state.phase_deadline - now)
                state.phase_deadline = None
            return
        if state.phase_deadline is None and state.remaining_s is not None:
            state.phase_deadline = now + state.remaining_s
        if state.phase_deadline is None:
            return
        if state.phase == PHASE_FLOW and self._current_owner != state.name:
            return
        remaining = state.phase_deadline - now
        if remaining > 0:
            return
        if state.phase == PHASE_FLOW:
            self._transition_to_pause(state, now)
        elif state.phase == PHASE_PAUSE:
            self._transition_to_flow(state, now)

    def _update_states(self):
        now = time.time()
        for state in self._state_manager.all_states():
            if state.phase != PHASE_IDLE and state.phase_deadline is not None:
                if state.phase_deadline < now:
                    state.phase_deadline = now
        self._apply_scheduler(now)
        for state in self._state_manager.all_states():
            self._update_phase_for_state(state, now)
        if time.time() - self._last_state_save > STATE_SAVE_INTERVAL_S:
            self._state_manager.save()
            self._last_state_save = time.time()

    def _ensure_owner_power(self):
        owner = self._choose_power_owner()
        if owner is None:
            return
        if self._current_owner != owner.name:
            self._apply_scheduler(time.time())

    def _maybe_log_tcs(self, timestamp):
        hv_state = self._state_manager.get('HV')
        if hv_state.phase == PHASE_IDLE:
            return
        if 'HV' not in self._tc_readers or 'HV' not in self._tc_writers:
            return
        try:
            temperatures = self._tc_readers['HV'].read()
            names = self._tc_readers['HV'].names()
            self._tc_writers['HV'].write_tcs(temperatures, names, timestamp)
        except Exception as exc:
            print(f'  [tc logger error] {type(exc).__name__}: {exc}')

    def _write_status(self):
        owner = self._current_owner
        print('\nCurrent runtime status:')
        for state in self._state_manager.all_states():
            print('  ' + state.to_status_line(owner))
        print('  PSU owner:', owner or 'none')

    def start_test(self, name):
        state = self._state_manager.get(name)
        if state.phase == PHASE_IDLE:
            self._transition_to_flow(state, time.time())
            print(f'Started {state.label} cycle {state.cycle}')
            return
        if state.phase == PHASE_PAUSE:
            self._transition_to_flow(state, time.time())
            print(f'Restarted {state.label} at cycle {state.cycle}')
            return
        print(f'{state.label} is already {state.phase}')

    def pause_test(self, name):
        state = self._state_manager.get(name)
        if state.phase == PHASE_IDLE:
            print(f'{state.label} is already idle')
            return
        if state.manual_pause:
            print(f'{state.label} is already paused')
            return
        state.manual_pause = True
        if state.phase_deadline is not None:
            state.remaining_s = max(0.0, state.phase_deadline - time.time())
            state.phase_deadline = None
        self._state_manager.save()
        print(f'Paused {state.label} at cycle {state.cycle}')

    def resume_test(self, name):
        state = self._state_manager.get(name)
        if not state.manual_pause:
            print(f'{state.label} is not paused')
            return
        state.manual_pause = False
        if state.remaining_s is None:
            state.remaining_s = state.duration_for_phase()
        state.phase_deadline = time.time() + state.remaining_s
        self._state_manager.save()
        print(f'Resumed {state.label} at cycle {state.cycle}')

    def stop_test(self, name):
        state = self._state_manager.get(name)
        if state.phase == PHASE_IDLE:
            print(f'{state.label} is already idle')
            return
        self._transition_to_idle(state, time.time())
        self._state_manager.save()
        print(f'Stopped {state.label}')

    def dump_state(self):
        self._state_manager.save()
        print(f'State saved to {STATE_FILE}')

    def close(self):
        self._stop_event.set()
        self._psu.shutdown('application exit')
        for contactor in self._contactors.values():
            contactor.close()
        self._reader.close()
        for writer in self._writers.values():
            writer.close()
        for writer in self._tc_writers.values():
            writer.close()
        for reader in self._tc_readers.values():
            reader.close()
        self._state_manager.save()
        print('Shutdown complete.')

    def logging_loop(self):
        while not self._stop_event.is_set():
            active_owner = self._current_owner
            timestamp = time.time()
            if active_owner:
                try:
                    currents = self._reader.read_currents()
                    psu_current, psu_voltage = self._psu.measure()
                    state = self._state_manager.get(active_owner)
                    self._writers[active_owner].write_currents(currents, SHUNTS, timestamp)
                    self._writers[f'{active_owner}_csv'].write_currents(
                        currents, SHUNTS, timestamp,
                        cycle=state.cycle,
                        phase=state.phase,
                        current_owner=active_owner,
                        psu_current=psu_current,
                        psu_voltage=psu_voltage,
                    )
                    self._writers[f'{active_owner}_xlsx'].write_currents(
                        currents, SHUNTS, timestamp,
                        cycle=state.cycle,
                        phase=state.phase,
                        current_owner=active_owner,
                        psu_current=psu_current,
                        psu_voltage=psu_voltage,
                    )
                except Exception as exc:
                    print(f'  [logger error] {type(exc).__name__}: {exc}')
            if (timestamp - self._last_tc_log) >= TC_INTERVAL:
                self._maybe_log_tcs(timestamp)
                self._last_tc_log = timestamp
            self._stop_event.wait(LOG_INTERVAL)

    def command_loop(self):
        self._write_status()
        self.print_help()
        while not self._stop_event.is_set():
            try:
                command = input('> ').strip().lower()
            except EOFError:
                break
            if not command:
                continue
            self.handle_command(command)
            if self._stop_event.is_set():
                break

    def handle_command(self, command):
        tokens = command.split()
        if not tokens:
            return
        cmd = tokens[0]
        args = tokens[1:]
        if cmd in ('help', '?'):
            self.print_help()
        elif cmd == 'status':
            self._write_status()
        elif cmd == 'start' and args:
            self.start_test(args[0].upper())
        elif cmd == 'pause' and args:
            self.pause_test(args[0].upper())
        elif cmd == 'resume' and args:
            self.resume_test(args[0].upper())
        elif cmd == 'stop' and args:
            self.stop_test(args[0].upper())
        elif cmd == 'dump':
            self.dump_state()
        elif cmd in ('quit', 'exit', 'q'):
            self._stop_event.set()
        else:
            print(f'Unknown command: {command}')
            self.print_help()

    def print_help(self):
        print('\nCommands:')
        print('  status               - show current runtime state')
        print('  start <HV|FUSE>      - begin or resume a test cycle')
        print('  pause <HV|FUSE>      - pause a running test')
        print('  resume <HV|FUSE>     - resume a paused test')
        print('  stop <HV|FUSE>       - stop a test and reset to idle')
        print('  dump                 - save current state to disk')
        print('  quit / exit / q      - cleanly shutdown and exit')

    def run(self):
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        logger_thread = threading.Thread(target=self.logging_loop, daemon=True)
        logger_thread.start()
        try:
            while not self._stop_event.is_set():
                self._update_states()
                time.sleep(0.2)
        except KeyboardInterrupt:
            self._stop_event.set()
        finally:
            self.close()

    def _signal_handler(self, signum, frame):
        print(f'\nReceived signal {signum}, shutting down...')
        self._stop_event.set()


def main():
    from dotenv import load_dotenv
    load_dotenv()

    runtime = DualTestRuntime()
    print('Dual test runtime loaded. Type "help" for commands.')
    command_thread = threading.Thread(target=runtime.command_loop, daemon=True)
    command_thread.start()
    runtime.run()


if __name__ == '__main__':
    main()
