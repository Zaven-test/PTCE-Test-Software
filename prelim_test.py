"""
prelim_test.py

Preliminary 500 A current flow test. No TCs. Reads voltage drop across
3 shunt resistors, converts to current (I = V / R), and logs to InfluxDB.

Flow:
  1. Script starts — operator closes contactors via probe, then presses Enter.
  2. Operator ramps to 500 A, then presses Enter to begin logging.
  3. Script logs shunt currents to InfluxDB every LOG_INTERVAL seconds.
  4. Press 'q' + Enter or Ctrl+C to abort and stop logging.

Fill in SHUNTS and the .env vars before running:
  INFLUX_TOKEN_PRELIM=<your write token>
  INFLUX_BUCKET_PRELIM=<your bucket name>
"""

import os
import time
import threading
import random

# ---------------------------------------------------------------------------
# Config  — UPDATE THESE before running
# ---------------------------------------------------------------------------
SIMULATE = False

# One entry per shunt: DAQ differential channel, display name, resistance in ohms
SHUNTS = [
    {"channel": "cDAQ2Mod2/ai0", "name": "Shunt1", "resistance_ohm": 0.001},
    {"channel": "cDAQ2Mod2/ai1", "name": "Shunt2", "resistance_ohm": 0.001},
    {"channel": "cDAQ2Mod2/ai2", "name": "Shunt3", "resistance_ohm": 0.001},
]

# Voltage range expected across shunts (±MAX_VOLTAGE_V differential)
MAX_VOLTAGE_V = 0.1

LOG_INTERVAL = 0.5   # seconds between InfluxDB writes
TEST_TAG     = "PRELIM-500A"

# ---------------------------------------------------------------------------
# Shunt reader
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
        """Return list of currents (A) in the same order as SHUNTS."""
        if self.simulate:
            el = time.time() - self._t0
            # Simulate a ramp to ~500 A over 10 s with small noise per shunt
            base = min(500.0, 500.0 * (el / 10.0))
            return [base + random.uniform(-2, 2) for _ in self.shunts]

        raw = self._task.read()
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
                .field("value", float(current))
                .time(ts_ms, self._ic.WritePrecision.MS)
            )
        self._writer.write(bucket=self._bucket, org=self._org, record=points)

    def close(self):
        self._client.close()


class NullWriter:
    """Console-only writer used in SIMULATE mode."""
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
    # Step 1: wait for contactors
    print("=" * 60)
    print("PRELIM TEST — 500 A current flow")
    print("=" * 60)
    print()
    print("Step 1: Close contactors via probe.")
    input("         Press Enter when contactors are closed... ")
    print("  >> Contactors closed.\n")

    # Step 2: wait for 500 A
    print("Step 2: Ramp current up to 500 A.")
    input("         Press Enter when current is at 500 A... ")
    print("  >> At 500 A — starting logging.\n")

    # Set up DAQ reader
    reader = ShuntReader(SHUNTS, MAX_VOLTAGE_V, SIMULATE)

    # Set up InfluxDB writer
    if SIMULATE:
        writer = NullWriter()
    else:
        from dotenv import load_dotenv
        load_dotenv()
        writer = InfluxWriter(
            url      = os.environ["INFLUX_URL"],
            org      = os.environ.get("INFLUX_ORG", "tesse"),
            bucket   = os.environ["INFLUX_BUCKET_PRELIM"],
            token    = os.environ["INFLUX_TOKEN_PRELIM"],
            test_tag = TEST_TAG,
        )

    stop_event = threading.Event()
    log_thread = threading.Thread(
        target=logging_worker,
        args=(reader, writer, SHUNTS, stop_event, LOG_INTERVAL),
        daemon=True,
    )
    log_thread.start()

    shunt_names = ", ".join(s["name"] for s in SHUNTS)
    print(f"Logging current on: {shunt_names}")
    print(f"Interval: {LOG_INTERVAL} s   |   SIMULATE={SIMULATE}")
    print("Press 'q' + Enter or Ctrl+C to abort.\n")

    try:
        while True:
            cmd = input()
            if cmd.strip().lower() == "q":
                break
    except KeyboardInterrupt:
        print("\nCtrl+C.")
    finally:
        print("\nAborting — stopping logging...")
        stop_event.set()
        log_thread.join(timeout=2 * LOG_INTERVAL)
        reader.close()
        writer.close()
        print("Done: logging halted, InfluxDB client closed.")


if __name__ == "__main__":
    main()
