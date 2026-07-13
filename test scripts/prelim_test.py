"""
prelim_test.py

Preliminary high-current flow test. Reads voltage drop across 3 shunt
resistors (50 µΩ each), converts to current (I = V / R), logs to InfluxDB,
controls Contactor 2 (cDAQ2 Slot 3, LINE1), and drives an ITECH PSU via
USB/VISA.

Flow:
  1. Script connects to PSU — output OFF, current 0 A (safety reset).
  2. Operator enters target current level.
  3. Script closes Contactor 2 (zero-current switching — no arc/weld risk).
  4. Script turns PSU output ON and ramps current up to target level.
  5. Script begins logging shunt currents to InfluxDB.
  6. Press 'q' + Enter or Ctrl+C to stop:
       — PSU current set to 0 A, output OFF
       — Contactor 2 opened

.env vars required (non-simulate mode):
  INFLUX_URL=...
  INFLUX_TOKEN_A=...
  INFLUX_BUCKET_A=...
  INFLUX_ORG=...   (optional, defaults to "tesse")
"""

import os
import time
import threading
import random

# ---------------------------------------------------------------------------
# Config  — edit before running
# ---------------------------------------------------------------------------
SIMULATE = False

PSU_VISA_ADDRESS  = 'USB0::0x2EC7::0x3900::805240031807340017::INSTR'  # fill in PSU serial
PSU_VOLTAGE_LIMIT = 10.0    # V — OVP ceiling sent to PSU
RAMP_STEP_A       = 10.0    # A — current increment per ramp step
RAMP_STEP_DELAY_S = 0.5     # s — delay between ramp steps  (~10 A/s → 600 A in ~60 s)

SHUNT_RESISTANCE_OHM = 50e-6  # Ω — all three shunts are the same value

SHUNTS = [
    {"channel": "cDAQ2Mod2/ai0", "name": "I_in_1", "resistance_ohm": SHUNT_RESISTANCE_OHM},
    {"channel": "cDAQ2Mod2/ai1", "name": "I_in_2", "resistance_ohm": SHUNT_RESISTANCE_OHM},
    {"channel": "cDAQ2Mod2/ai2", "name": "I_out",  "resistance_ohm": SHUNT_RESISTANCE_OHM},
]

CONTACTOR_2_CH = "cDAQ2Mod1/line1"  # NI 9472 LINE1 — "Cont Close 2"

MAX_VOLTAGE_V = 10.0   # NI 9224 fixed ±10 V range
LOG_INTERVAL  = 0.5    # seconds between InfluxDB writes


# ---------------------------------------------------------------------------
# PSU controller (ITECH via USB/VISA)
# ---------------------------------------------------------------------------
class PSU:
    """Controls an ITECH PSU via PyVISA. Starts with output OFF, current 0 A."""

    def __init__(self, visa_address, voltage_limit, simulate):
        self.simulate       = simulate
        self._voltage_limit = voltage_limit
        self._current_sp    = 0.0
        self._psu           = None

        if simulate:
            print(f"  [sim] PSU initialised ({visa_address}) — output OFF, 0 A")
            return

        import pyvisa
        rm  = pyvisa.ResourceManager()
        psu = rm.open_resource(visa_address)
        psu.timeout = 5000
        idn = psu.query('*IDN?').strip()
        print(f"  PSU connected: {idn}")
        self._psu = psu
        psu.write(f'SOUR:VOLT {voltage_limit:.3f}')
        psu.write('SOUR:CURR 0')
        psu.write('OUTP OFF')

    def output_on(self):
        if self.simulate:
            print("  [sim] PSU output ON")
            return
        self._psu.write('OUTP ON')

    def set_current(self, amps):
        self._current_sp = float(amps)
        if self.simulate:
            print(f"  [sim] PSU → {amps:.2f} A")
            return
        self._psu.write(f'SOUR:CURR {amps:.3f}')

    def ramp_to(self, target_a):
        """Ramp from current setpoint to target_a in RAMP_STEP_A increments."""
        start     = self._current_sp
        target    = float(target_a)
        direction = 1.0 if target > start else -1.0
        level     = start + direction * abs(RAMP_STEP_A)

        while direction * level < direction * target:
            self.set_current(round(level, 6))
            time.sleep(RAMP_STEP_DELAY_S)
            level += direction * abs(RAMP_STEP_A)

        self.set_current(target)
        time.sleep(RAMP_STEP_DELAY_S)

    def measure(self):
        """Return (actual_current_A, actual_voltage_V)."""
        if self.simulate:
            return (self._current_sp + random.uniform(-0.3, 0.3),
                    3.0 + random.uniform(-0.05, 0.05))
        i = float(self._psu.query('MEAS:CURR?').strip())
        v = float(self._psu.query('MEAS:VOLT?').strip())
        return i, v

    def shutdown(self, reason=""):
        """Immediate zero + output OFF. Three retries on comms failure."""
        if self.simulate:
            print(f"  [sim] PSU shutdown{f' — {reason}' if reason else ''}")
            self._current_sp = 0.0
            return
        if self._psu is None:
            return
        for attempt in range(3):
            try:
                self._psu.write('SOUR:CURR 0')
                self._psu.write('OUTP OFF')
                self._current_sp = 0.0
                print(f"  PSU output OFF{f' — {reason}' if reason else ''}")
                return
            except Exception as e:
                print(f"  [PSU] shutdown attempt {attempt+1}/3 failed: {e}")
                time.sleep(0.5)
        print("  !! PSU SHUTDOWN FAILED — MANUALLY VERIFY OUTPUT IS OFF !!")

    def close(self):
        self.shutdown()
        if self._psu is not None:
            self._psu.close()
            self._psu = None


# ---------------------------------------------------------------------------
# Contactor controller (NI 9472 digital output)
# ---------------------------------------------------------------------------
class Contactor:
    """Controls a single NI 9472 digital output line. Starts open."""

    def __init__(self, channel, simulate):
        self.channel  = channel
        self.simulate = simulate
        self._task    = None

        if simulate:
            print(f"  [sim] Contactor ({channel}) initialised — OPEN")
            return

        import nidaqmx
        self._task = nidaqmx.Task()
        self._task.do_channels.add_do_chan(channel)
        self._task.start()
        self._write(False)

    def energize(self):
        if self.simulate:
            print("  [sim] Contactor 2 CLOSED")
            return
        self._write(True)

    def de_energize(self):
        if self.simulate:
            print("  [sim] Contactor 2 OPENED")
            return
        self._write(False)

    def _write(self, state):
        self._task.write(state)

    def close(self):
        if self._task is not None:
            self._write(False)
            self._task.close()
            self._task = None


# ---------------------------------------------------------------------------
# Shunt current reader — NI 9224 voltage → current (I = V / R)
# ---------------------------------------------------------------------------
class ShuntReader:
    def __init__(self, shunts, max_voltage, simulate):
        self.shunts   = shunts
        self.simulate = simulate
        self._t0      = time.time()
        self._task    = None

        if simulate:
            return

        import nidaqmx
        from nidaqmx.constants import TerminalConfiguration

        self._task = nidaqmx.Task()
        for s in shunts:
            self._task.ai_channels.add_ai_voltage_chan(
                s["channel"],
                terminal_config=TerminalConfiguration.DIFF,
                min_val=-max_voltage,
                max_val=max_voltage,
            )
        self._task.start()

    def read_currents(self):
        if self.simulate:
            el   = time.time() - self._t0
            base = min(15, 15 * el / 10.0)
            return [base + random.uniform(-2, 2) for _ in self.shunts]

        raw      = self._task.read()
        voltages = raw if isinstance(raw, list) else [raw]
        return [v / s["resistance_ohm"] for v, s in zip(voltages, self.shunts)]

    def close(self):
        if self._task is not None:
            self._task.close()


# ---------------------------------------------------------------------------
# InfluxDB writer
# ---------------------------------------------------------------------------
class InfluxWriter:
    def __init__(self, url, org, bucket, token, test_tag):
        import influxdb_client
        from influxdb_client.client.write_api import SYNCHRONOUS
        self._ic     = influxdb_client
        self._client = influxdb_client.InfluxDBClient(url=url, token=token, org=org)
        self._writer = self._client.write_api(write_options=SYNCHRONOUS)
        self._bucket = bucket
        self._org    = org
        self._test   = test_tag

    def write_currents(self, currents, shunts, timestamp):
        ts_ms  = round(timestamp * 1000.0)
        points = []
        for current, s in zip(currents, shunts):
            points.append(
                self._ic.Point("current")
                .tag("test",  self._test)
                .tag("shunt", s["name"])
                .field("amps", float(current))
                .time(ts_ms, self._ic.WritePrecision.MS)
            )
        self._writer.write(bucket=self._bucket, org=self._org, record=points)

    def close(self):
        self._client.close()


class NullWriter:
    def write_currents(self, currents, shunts, timestamp):
        vals = "  ".join(f"{s['name']}={c:.1f}A" for c, s in zip(currents, shunts))
        print(f"  [influx] {vals}")

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Background logging thread
# ---------------------------------------------------------------------------
def logging_worker(reader, writer, shunts, stop_event, interval):
    while not stop_event.is_set():
        now = time.time()
        try:
            currents = reader.read_currents()
            writer.write_currents(currents, shunts, now)
        except Exception as exc:
            print(f"  [log error] {type(exc).__name__}: {exc}")
        stop_event.wait(interval)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 60)
    print("PRELIM TEST — ITECH PSU + NI DAQ")
    print("=" * 60)
    print()

    while True:
        try:
            target_a = float(input("Enter target current (A): ").strip())
            if target_a <= 0:
                print("  Current must be > 0 A.")
                continue
            break
        except ValueError:
            print("  Enter a valid number.")

    test_tag = f"PRELIM-{target_a:.0f}A"
    print(f"  Target: {target_a} A   Tag: {test_tag}\n")

    psu       = PSU(PSU_VISA_ADDRESS, PSU_VOLTAGE_LIMIT, SIMULATE)
    contactor = Contactor(CONTACTOR_2_CH, SIMULATE)
    reader    = ShuntReader(SHUNTS, MAX_VOLTAGE_V, SIMULATE)

    if SIMULATE:
        writer = NullWriter()
    else:
        from dotenv import load_dotenv
        load_dotenv()
        writer = InfluxWriter(
            url      = os.environ["INFLUX_URL"],
            org      = os.environ.get("INFLUX_ORG", "tesse"),
            bucket   = os.environ["INFLUX_BUCKET_A"],
            token    = os.environ["INFLUX_TOKEN_A"],
            test_tag = test_tag,
        )

    stop_event = threading.Event()
    log_thread = threading.Thread(
        target=logging_worker,
        args=(reader, writer, SHUNTS, stop_event, LOG_INTERVAL),
        daemon=True,
    )

    try:
        print("Step 1: Closing Contactor 2 at zero current...")
        contactor.energize()
        print("        Contactor 2 CLOSED.\n")

        print("Step 2: Starting continuous logging.\n")
        log_thread.start()

        print(f"Step 3: Ramping PSU to {target_a} A "
              f"({RAMP_STEP_A} A/step, {RAMP_STEP_DELAY_S} s/step, ~{target_a/RAMP_STEP_A*RAMP_STEP_DELAY_S:.0f} s)...")
        psu.output_on()
        psu.ramp_to(target_a)
        actual_i, actual_v = psu.measure()
        print(f"        Ramp complete — actual: {actual_i:.2f} A, {actual_v:.3f} V\n")

        shunt_names = ", ".join(s["name"] for s in SHUNTS)
        print(f"Logging: {shunt_names}")
        print(f"Interval: {LOG_INTERVAL} s   |   SIMULATE={SIMULATE}")
        print("Press 'q' + Enter or Ctrl+C to stop.\n")

        while True:
            cmd = input()
            if cmd.strip().lower() == "q":
                break

    except KeyboardInterrupt:
        print("\nCtrl+C.")
    finally:
        print("\nStopping — zeroing PSU, opening Contactor 2, halting logging...")
        stop_event.set()
        if log_thread.is_alive():
            log_thread.join(timeout=2 * LOG_INTERVAL)
        psu.shutdown("test stopped")
        contactor.close()
        reader.close()
        writer.close()
        psu.close()
        print("Done: PSU off, Contactor 2 open, InfluxDB client closed.")


if __name__ == "__main__":
    main()
