"""Stand-in for a vendor device-code login (tests only; never used by the app).

Prints a device URL and a one-time code the way `gh auth login --web` does, optionally
waits for a line on stdin when `--needs-input` is given, then exits 0. `--fail` exits 1.
`--console` mimics `aws login --remote` instead: a URL, then a prompt without a newline that
waits for the authorization code on stdin.
"""

from __future__ import annotations

import sys
import time


def main() -> int:
    args = sys.argv[1:]
    if "--console" in args:
        print("Browser will not be automatically opened.", flush=True)
        print("Please visit the following URL:", flush=True)
        print(flush=True)
        print("https://signin.example.test/v1/authorize?response_type=code&state=abc", flush=True)
        print(flush=True)
        print("Enter the authorization code displayed in your browser: ", end="", flush=True)
        line = sys.stdin.readline().strip()
        print(f"received {len(line)} characters", flush=True)
        print("Successfully logged into console session", flush=True)
        return 1 if "--fail" in args else 0
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
