#!/usr/bin/env python3
"""Static regression checks for Hermes Nimbus XSS hardening.

This intentionally checks the local static files for the dangerous patterns we fixed:
rendering WebSocket-controlled values through innerHTML, and building URLs from raw
instance ids.
"""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

CHECKS = [
    (
        STATIC / "index.html",
        [
            (r"\binnerHTML\b", "index.html should not use innerHTML for WebSocket-rendered instance fields"),
            (r"detail\.html\?id=['\"]?\s*\+\s*instance\.id", "index.html should not build detail URLs from raw instance.id"),
        ],
        ["safeText", "safeInstanceId", "safeColor", "safeIcon", "updateStateLabel"],
    ),
    (
        STATIC / "detail.html",
        [
            (r"\binnerHTML\b", "detail.html should not use innerHTML for state history"),
        ],
        ["safeText", "safeInstanceId", "safeColor", "safeIcon", "safeState"],
    ),
    (
        STATIC / "fullscreen.html",
        [
            (r"detailPage\s*=\s*['\"]detail\.html\?id=['\"]\s*\+\s*instanceId", "fullscreen.html should encode/sanitize instanceId in detailPage"),
        ],
        ["safeText", "safeInstanceId", "safeIcon", "safeState"],
    ),
]


def main() -> int:
    failures = []
    for path, forbidden_patterns, required_tokens in CHECKS:
        text = path.read_text(encoding="utf-8")
        for pattern, message in forbidden_patterns:
            if re.search(pattern, text):
                failures.append(f"{path.relative_to(ROOT)}: {message} (matched {pattern!r})")
        for token in required_tokens:
            if token not in text:
                failures.append(f"{path.relative_to(ROOT)}: missing hardening helper/token {token!r}")

    if failures:
        print("XSS hardening checks FAILED:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("XSS hardening checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
