"""
Interactive contactor toggler for the TE Flex Logger chassis.

Press Enter to toggle all configured contactor DO lines HIGH/LOW.
Press q + Enter or Ctrl+C to exit and release contactors LOW.
"""

import os

SIMULATE = os.environ.get("SIMULATE", "False").lower() in ("1", "true", "yes", "y")

# Update these lines to match the TE Flex Logger chassis contactor outputs.
CONTACTOR_LINES = [
    "cDAQ2Mod1/port0/line0",
    "cDAQ2Mod1/port0/line1",
    "cDAQ2Mod1/port0/line2",
]


class ContactorController:
    def __init__(self, lines, simulate):
        self.simulate = simulate
        self.lines = lines
        self._state = False
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
        self.set(False)

    def set(self, high):
        self._state = high
        if self.simulate:
            print(f"SIMULATE: setting contactors {'HIGH' if high else 'LOW'}")
            return

        values = [high] * len(self.lines)
        self._task.write(values)

    @property
    def is_high(self):
        return self._state

    def close(self):
        if self._task is not None:
            self._task.close()


def main():
    controller = ContactorController(CONTACTOR_LINES, SIMULATE)

    print("Connected to TE Flex Logger chassis.")
    print("Press Enter to toggle contactor lines HIGH/LOW.")
    print("Press q + Enter or Ctrl+C to quit.")
    print(f"SIMULATE={SIMULATE}\n")

    try:
        while True:
            cmd = input()
            if cmd.strip().lower() == "q":
                break
            controller.set(not controller.is_high)
            print(f"  >> CONTACTORS {'HIGH' if controller.is_high else 'LOW'}")
    except KeyboardInterrupt:
        print("\nCtrl+C.")
    finally:
        controller.set(False)
        controller.close()
        print("Stopped: contactors set LOW.")


if __name__ == "__main__":
    main()
