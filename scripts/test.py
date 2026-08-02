#!/usr/bin/env python3
"""Run the deterministic Hermes Nimbus regression suite.

No live Halo or Hermes process is required.  Web-handler tests run when the
selected interpreter provides aiohttp; detector and plugin tests always run.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(Path(__file__).parent))

from check_xss_hardening import main as check_static_hardening  # noqa: E402


def main() -> int:
    suite = unittest.defaultTestLoader.discover(str(TESTS))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    static_status = check_static_hardening()
    return 0 if result.wasSuccessful() and static_status == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
