#!/usr/bin/env python3
"""Run the complete NearbyCast automated lab matrix."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


SUITES = [
    ("virtual E2E", [sys.executable, str(ROOT / "e2e_test.py")]),
    ("production path", [sys.executable, str(ROOT / "production_path_test.py")]),
    ("failure injection", [sys.executable, str(ROOT / "failure_test.py")]),
    ("latency", [sys.executable, str(ROOT / "latency_test.py")]),
    ("stress", [sys.executable, str(ROOT / "stress_test.py")]),
]


def main() -> int:
    print("NearbyCast test:all\n", flush=True)
    failed: list[str] = []
    for name, command in SUITES:
        print(f"── {name} ──", flush=True)
        proc = subprocess.run(command)
        if proc.returncode != 0:
            failed.append(name)
            print(f"SUITE FAIL: {name}\n", flush=True)
        else:
            print(f"SUITE PASS: {name}\n", flush=True)
    if failed:
        print(f"RESULT: FAIL ({', '.join(failed)})")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
