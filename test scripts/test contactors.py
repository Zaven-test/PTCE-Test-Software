"""
Interactive contactor toggler for the TE Flex Logger chassis.

  1 + Enter  — C1 (LINE0) HIGH, C2 LOW
  2 + Enter  — C2 (LINE1) HIGH, C1 LOW
  Enter      — all contactors LOW
  q + Enter or Ctrl+C — exit and release LOW
"""

import os

SIMULATE = os.environ.get("SIMULATE", "False").lower() in ("1", "true", "yes", "y")

# LINE0 = C1, LINE1 = C2 (as seen in FlexLogger)
CONTACTOR_LINES = [
    "cDAQ2Mod3/port0/line0",
    "cDAQ2Mod3/port0/line1",
]


class ContactorController:
    def __init__(self, lines, simulate):
        self.simulate = simulate
        self.lines = lines
        self._states = [False] * len(lines)
        self._task = None

        if self.simulate:
            print("SIMULATE mode: contactor outputs will not be written.")
            return

        import nidaqmx
        from nidaqmx.constants import LineGrouping

        self._task = nidaqmx.Task()
        self._task.do_channels.add_do_chan(
            ",".join(lines),
            line_grouping=LineGrouping.CHAN_PER_LINE)
        self._write()

    def _write(self):
        if self.simulate:
            labels = ["HIGH" if s else "LOW" for s in self._states]
            print(f"SIMULATE: {', '.join(f'LINE{i}={l}' for i, l in enumerate(labels))}")
            return
        self._task.write(list(self._states))

    def set_one(self, index):
        """Set one line HIGH, all others LOW."""
        self._states = [i == index for i in range(len(self.lines))]
        self._write()

    def all_low(self):
        self._states = [False] * len(self.lines)
        self._write()

    def close(self):
        if self._task is not None:
            self._task.close()


def main():
    controller = ContactorController(CONTACTOR_LINES, SIMULATE)

    print("Connected to TE Flex Logger chassis.")
    print("  1 + Enter  -> C1 (LINE0) HIGH, C2 LOW")
    print("  2 + Enter  -> C2 (LINE1) HIGH, C1 LOW")
    print("  Enter      -> all LOW")
    print("  q + Enter or Ctrl+C to quit.")
    print(f"SIMULATE={SIMULATE}\n")

    try:
        while True:
            cmd = input(">> ").strip().lower()
            if cmd == "q":
                break
            elif cmd == "1":
                controller.set_one(0)
                print("  C1 HIGH, C2 LOW")
            elif cmd == "2":
                controller.set_one(1)
                print("  C2 HIGH, C1 LOW")
            elif cmd == "":
                controller.all_low()
                print("  All LOW")
            else:
                print("  Unknown command. Use 1, 2, or Enter.")
    except KeyboardInterrupt:
        print("\nCtrl+C.")
    finally:
        controller.all_low()
        controller.close()
        print("Stopped: contactors set LOW.")


if __name__ == "__main__":
    main()
