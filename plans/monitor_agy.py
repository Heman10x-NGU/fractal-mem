"""Observe AGY lifecycle without sending input or changing focus.

Run inside Herdr: python3 plans/monitor_agy.py
The orchestrator owns this process and stops it when the build ends.
"""

import json
import os
import subprocess
import time


def main() -> None:
    if os.environ.get("HERDR_ENV") != "1":
        raise SystemExit("This monitor must run in a Herdr pane")
    previous = ""
    while True:
        result = subprocess.run(
            ["herdr", "agent", "get", "agy-fractal"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode:
            state = "unavailable"
        else:
            info = json.loads(result.stdout)["result"]["agent"]
            state = str(info["agent_status"])
        if state != previous:
            print(json.dumps({"time": time.time(), "agent": "agy-fractal", "state": state}), flush=True)
            previous = state
        time.sleep(10)


if __name__ == "__main__":
    main()
