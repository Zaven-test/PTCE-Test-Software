"""
ptce_bench_test.py  (two-station, two-bucket, two-token version)

Two PTCE stations share one rig. Their thermocouples are read in a single
nidaqmx task, but each station's channels are logged to its OWN Influx bucket,
each with its OWN write token.

  * BACKGROUND: every second, read all TCs, split them by station, write each
    station's values to its own bucket.
  * FOREGROUND: press Enter to toggle the bus-select DO line HIGH / LOW.

    Run it               -> logging starts (both buckets), bus LOW
    Enter                -> bus HIGH  (Enter again -> LOW)
    'q' + Enter / Ctrl+C -> bus LOW, logging stops, both clients flushed

.env beside this script (a separate write token per bucket):

    INFLUX_URL=http://10.86.105.93:8086
    INFLUX_ORG=tesse
    INFLUX_TOKEN_A=<station A bucket write token>
    INFLUX_BUCKET_A=TESSE-745
    INFLUX_TOKEN_B=<station B bucket write token>
    INFLUX_BUCKET_B=<station B bucket name>

SIMULATE = True dry-runs with fake TCs and console "writes" (no hardware/Influx).
"""

import os
import time
import math
import random
import threading
import dataclasses

# -- Config ------------------------------------------------------------------
SIMULATE = False        


STATIONS = {
    "A": [
        ("cDAQ2Mod2/ai0", "TC1"),
        ("cDAQ2Mod2/ai1", "TC2"),
    ],
    "B": [
        ("cDAQ2Mod2/ai2", "TC3"),
        ("cDAQ2Mod2/ai3", "TC4"),
    ],
}
STATION_ENV = {
    "A": {"token": "INFLUX_TOKEN_A", "bucket": "INFLUX_BUCKET_A", "test": "TESSE-745"},
    "B": {"token": "INFLUX_TOKEN_B", "bucket": "INFLUX_BUCKET_B", "test": "TESSE-1890"},
}

TC_TYPE  = "T"
BUS_DO_LINE     = "cDAQ2Mod1/port0/line0" 
BUS_ACTIVE_HIGH = True
LOG_INTERVAL    = 1.0


# -- Influx record + recorder ------------------------------------------------
@dataclasses.dataclass
class TCRecord:
    timestamp: float = 0.0
    temperatures: list = dataclasses.field(default_factory=list)
    bus_state: int = 0


class InfluxRecorder:
    """One recorder per bucket (its own client + token).
    Field name -> measurement; list field -> 'index' tag."""
    def __init__(self, url, org, bucket, token, test_tag, station, channel_names):
        import influxdb_client
        from influxdb_client.client.write_api import SYNCHRONOUS
        self._ic = influxdb_client
        self._client = influxdb_client.InfluxDBClient(url=url, token=token, org=org)
        self._writer = self._client.write_api(write_options=SYNCHRONOUS)
        self._bucket, self._org = bucket, org
        self._test_tag, self._station, self._names = test_tag, station, channel_names

    def write(self, record):
        points = []
        d = dataclasses.asdict(record)
        ts_ms = round(d.pop("timestamp") * 1000.0)
        for name, data in d.items():
            if isinstance(data, (list, tuple)):
                for i, value in enumerate(data):
                    points.append(
                        self._ic.Point(name)
                        .tag("test", self._test_tag)
                        .tag("station", self._station)
                        .tag("index", i + 1)
                        .tag("channel", self._names[i] if i < len(self._names) else str(i + 1))
                        .field("value", float(value))
                        .time(ts_ms, self._ic.WritePrecision.MS))
            else:
                points.append(
                    self._ic.Point(name)
                    .tag("test", self._test_tag)
                    .tag("station", self._station)
                    .field("value", data)
                    .time(ts_ms, self._ic.WritePrecision.MS))
        self._writer.write(bucket=self._bucket, org=self._org, record=points)

    def close(self):
        self._client.close()


class NullRecorder:
    def __init__(self, station):
        self._station = station
    def write(self, record):
        t = record.temperatures
        hi = max(t) if t else float("nan")
        print(f"    [influx:{self._station}] t={record.timestamp:.0f} "
              f"bus={record.bus_state} TCmax={hi:.1f} n={len(t)}")
    def close(self):
        pass


# -- TC reader: one task spanning ALL channels, in declared order ------------
class TCReader:
    def __init__(self, stations, tc_type, simulate):
        self.simulate = simulate
        # Flatten to a single ordered list; remember each channel's station + name.
        self.flat = [(st, phys, name)
                     for st, chans in stations.items()
                     for phys, name in chans]
        self._t0 = time.time()
        if simulate:
            self._task = None
            return
        import nidaqmx
        from nidaqmx.constants import ThermocoupleType, TemperatureUnits
        tmap = {"J": ThermocoupleType.J, "K": ThermocoupleType.K,
                "T": ThermocoupleType.T, "E": ThermocoupleType.E,
                "N": ThermocoupleType.N}
        self._task = nidaqmx.Task()
        for _, phys, _ in self.flat:
            self._task.ai_channels.add_ai_thrmcpl_chan(
                phys, thermocouple_type=tmap[tc_type], units=TemperatureUnits.DEG_C)
        self._task.start()

    def read(self):
        """Return readings in the same order as self.flat."""
        if self.simulate:
            el = time.time() - self._t0
            base = 25 + 40 * (1 - math.exp(-el / 30.0))
            return [base + 3 * i + random.uniform(-0.3, 0.3) for i in range(len(self.flat))]
        data = self._task.read()
        return data if isinstance(data, list) else [data]

    def split_by_station(self, readings):
        """Group a flat reading list into {station: [values in that station's order]}."""
        out = {}
        for (st, _, _), value in zip(self.flat, readings):
            out.setdefault(st, []).append(value)
        return out

    def close(self):
        if self._task is not None:
            self._task.close()


# -- Bus-select DO line ------------------------------------------------------
class BusController:
    def __init__(self, line, active_high, simulate):
        self.simulate, self.active_high = simulate, active_high
        self._high = False
        if simulate:
            self._task = None
            return
        import nidaqmx
        from nidaqmx.constants import LineGrouping
        self._task = nidaqmx.Task()
        self._task.do_channels.add_do_chan(line, line_grouping=LineGrouping.CHAN_PER_LINE)
        self.set(False)

    def set(self, high):
        self._high = high
        if not self.simulate:
            self._task.write(high if self.active_high else (not high))

    @property
    def is_high(self):
        return self._high

    def close(self):
        if self._task is not None:
            self._task.close()


# -- Background logging thread -----------------------------------------------
def logging_worker(reader, bus, recorders, stop_event, interval):
    while not stop_event.is_set():
        now = time.time()
        try:
            by_station = reader.split_by_station(reader.read())
            bus_state = 1 if bus.is_high else 0
            for station, temps in by_station.items():
                recorders[station].write(
                    TCRecord(timestamp=now, temperatures=temps, bus_state=bus_state))
        except Exception as e:
            print(f"    [log error] {type(e).__name__}: {e}")
        stop_event.wait(interval)


# -- Main --------------------------------------------------------------------
def main():
    reader = TCReader(STATIONS, TC_TYPE, SIMULATE)
    bus = BusController(BUS_DO_LINE, BUS_ACTIVE_HIGH, SIMULATE)

    # One recorder per station: each with its OWN bucket and OWN token.
    recorders = {}
    if SIMULATE:
        for st in STATIONS:
            recorders[st] = NullRecorder(st)
    else:
        from dotenv import load_dotenv
        load_dotenv()
        url = os.environ["INFLUX_URL"]
        org = os.environ.get("INFLUX_ORG", "tesse")
        for st, chans in STATIONS.items():
            env = STATION_ENV[st]
            recorders[st] = InfluxRecorder(
                url=url, org=org,
                bucket=os.environ[env["bucket"]],
                token=os.environ[env["token"]],   # per-station token
                test_tag=env["test"], station=st, # per-station test tag
                channel_names=[n for _, n in chans])

    stop_event = threading.Event()
    log_thread = threading.Thread(
        target=logging_worker,
        args=(reader, bus, recorders, stop_event, LOG_INTERVAL), daemon=True)
    log_thread.start()

    total = sum(len(c) for c in STATIONS.values())
    print(f"Logging {total} TCs across {len(STATIONS)} stations -> separate buckets "
          f"(SIMULATE={SIMULATE}).")
    print("Bus is LOW. Press Enter to toggle bus HIGH/LOW, 'q'+Enter to quit.\n")

    try:
        while True:
            cmd = input()
            if cmd.strip().lower() == "q":
                break
            bus.set(not bus.is_high)
            print(f"  >> BUS {'HIGH' if bus.is_high else 'LOW'}")
    except KeyboardInterrupt:
        print("\nCtrl+C.")
    finally:
        stop_event.set()
        log_thread.join(timeout=2 * LOG_INTERVAL)
        bus.set(False)
        bus.close()
        reader.close()
        for r in recorders.values():
            r.close()
        print("Stopped: bus LOW, logging halted, both buckets flushed.")


if __name__ == "__main__":
    main()