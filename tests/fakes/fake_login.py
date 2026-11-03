"""Stand-in for a vendor device-code login (tests only; never used by the app).

Prints a device URL and a one-time code the way `gh auth login --web` does, optionally
waits for a line on stdin when `--needs-input` is given, then exits 0. `--fail` exits 1.
"""

from __future__ import annotations

import sys
import time


def main() -> int:
    args = sys.argv[1:]
    print("! First copy your one-time code: ABCD-1234", flush=True)
    print("Open this URL to continue in your web browser: https://github.com/login/device", flush=True)
    if "--needs-input" in args:
        print("Enter the verification code:", flush=True)
        line = sys.stdin.readline().strip()
        print(f"received {len(line)} characters", flush=True)
    time.sleep(0.05)
    if "--fail" in args:
        print("error: the browser session was not completed", flush=True)
        return 1
    print("Logged in as fake-user", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
