#!/usr/bin/env python3
"""Start/stop/inspect the dedicated debuggable Chrome used for CNKI.

Same idea as pkulaw-browser: a plain Google Chrome with its own profile and a
debugging port. The user logs in by hand once; scripts attach over CDP.
Chrome >= 136 refuses --remote-debugging-port on the Default profile, so the
profile must be dedicated.
"""
import argparse, json, pathlib, subprocess, sys, time, urllib.request

STATE = pathlib.Path.home() / ".agents/state/cnki-browser"
PROFILE = STATE / "chrome-profile"
PORT = 9334
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HOME_URL = "https://www.cnki.net/"


def alive(port=PORT):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as r:
            return json.load(r)
    except Exception:
        return None


def start(url=HOME_URL, port=PORT):
    if alive(port):
        print(f"ok  already running on :{port}")
        return 0
    PROFILE.mkdir(parents=True, exist_ok=True)
    log = open(STATE / "chrome.log", "ab")
    subprocess.Popen([CHROME, f"--remote-debugging-port={port}", f"--user-data-dir={PROFILE}",
                      "--no-first-run", "--no-default-browser-check", url],
                     stdout=log, stderr=log, start_new_session=True)
    for _ in range(40):
        time.sleep(0.5)
        if alive(port):
            print(f"ok  started on :{port}  profile={PROFILE}")
            return 0
    print("error  Chrome did not open the debugging port", file=sys.stderr)
    return 4


def stop(port=PORT):
    subprocess.run(["pkill", "-f", f"--user-data-dir={PROFILE}"])
    print("ok  stopped")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["start", "stop", "status"])
    ap.add_argument("--url", default=HOME_URL)
    a = ap.parse_args()
    if a.action == "start":
        return start(a.url)
    if a.action == "stop":
        return stop()
    info = alive()
    print(f"{'running' if info else 'not running'} :{PORT} {info.get('Browser','') if info else ''}")
    return 0 if info else 3


if __name__ == "__main__":
    sys.exit(main())
