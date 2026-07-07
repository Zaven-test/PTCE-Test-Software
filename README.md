# Dual Test Runtime

This repository includes a modular dual-test runtime for two independent current-flow tests:

- `HV` — TESSE 745 HV connector test
- `FUSE` — CCA fuse test

The runtime is controlled entirely from the terminal and supports:

- start/pause/resume/stop per test
- independent cycle state for each test
- persistent runtime state across restarts
- CSV and Excel logging per test
- InfluxDB write support per test

---

## Files

- `dual_test_runtime.py` — main runtime script
- `dual_test_state.json` — saved runtime state (generated)
- `dual_test_history.log` — event history log (generated)
- `dual_test_log_hv.csv` — HV test CSV log (generated)
- `dual_test_log_fuse.csv` — FUSE test CSV log (generated)
- `dual_test_log.xlsx` — Excel log workbook with `HV` and `FUSE` worksheets (generated)

---

## Prerequisites

Install required Python packages:

```powershell
pip install pyvisa nidaqmx influxdb-client python-dotenv openpyxl
```

If you only want CSV logging and do not need Excel, `openpyxl` is optional.

---

## Environment variables

Create a `.env` file in the repository root with at least:

```text
INFLUX_URL=https://your-influx-host:8086
INFLUX_TOKEN_A=your_token_here
INFLUX_BUCKET_HV=hv_bucket_name
INFLUX_BUCKET_FUSE=fuse_bucket_name
INFLUX_ORG=tesse
```

---

## Running the script

```powershell
python dual_test_runtime.py
```

The script launches an interactive terminal session.

---

## Terminal commands

Use these commands while the script is running:

- `status`
  - print current runtime state for both tests
- `start HV`
  - start or resume the HV connector test
- `start FUSE`
  - start or resume the CCA fuse test
- `pause HV`
  - pause the HV test and preserve remaining phase time
- `pause FUSE`
  - pause the FUSE test and preserve remaining phase time
- `resume HV`
  - resume a manually paused HV test
- `resume FUSE`
  - resume a manually paused FUSE test
- `stop HV`
  - stop HV and reset it to idle
- `stop FUSE`
  - stop FUSE and reset it to idle
- `dump`
  - immediately save runtime state to disk
- `quit`, `exit`, `q`
  - cleanly shut down and exit the script

---

## How the runtime behaves

- The script supports running HV only, FUSE only, or both together.
- Only one test can draw PSU power at once.
- Each test keeps its own flow/pause cycle and phase state.
- If a test is paused, its remaining time is saved and restored on resume.
- If the script shuts down unexpectedly, the state file is reloaded on restart.

---

## Timing guarantees

- Flow and pause durations are controlled by wall-clock deadlines.
- Each test phase uses a strict `phase_deadline` so transitions happen at the configured time.
- Manual pause saves the remaining time and restores it when resumed.

---

## Test configuration

Open `dual_test_runtime.py` and edit `TEST_DEFINITIONS` to change:

- `max_current_a`
- `flow_duration_s`
- `pause_duration_s`
- `contactor_channel`
- `bucket_tag`

For example, `TESSE 745` is the `HV` test and is currently configured as `HV`.

---

## Logs

- CSV files are created at `dual_test_log_hv.csv` and `dual_test_log_fuse.csv`.
- Excel workbook is written to `dual_test_log.xlsx` with sheets `HV` and `FUSE`.
- Each log row includes:
  - timestamp
  - cycle number
  - test name
  - phase
  - current owner
  - PSU current
  - PSU voltage
  - shunt name
  - shunt amps

---

## Notes

- If only the HV test is active, FUSE remains idle until you issue `start FUSE`.
- The `enabled` flag is currently informational; control is handled by terminal commands.
- Always verify the correct contactor channel mapping before running the hardware.
