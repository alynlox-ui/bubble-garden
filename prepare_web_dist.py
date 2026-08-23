#!/usr/bin/env python3
"""Deterministic publish build for Bubble Garden (泡泡花园).

Copies ONLY browser-runtime files into web-dist/ (allowlisted), emits
build-report.json, and fails loudly on missing inputs. Render runs this
via `buildCommand: python3 prepare_web_dist.py`.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "web-dist"

# Allowlist: browser-runtime files only (no test scripts, screenshots, reports)
ALLOWLIST = ["index.html"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    missing = [name for name in ALLOWLIST if not (ROOT / name).is_file()]
    if missing:
        print("FATAL: missing required inputs:", missing)
        return 1

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()

    report = {"site": "bubble-garden", "files": []}
    for name in ALLOWLIST:
        src = ROOT / name
        dst = DIST / name
        dst.write_bytes(src.read_bytes())
        digest = sha256(dst)
        if digest != sha256(src):
            print(f"FATAL: hash mismatch after copy: {name}")
            return 1
        report["files"].append({"name": name, "bytes": dst.stat().st_size, "sha256": digest})

    # Conservative static headers
    (DIST / "_headers").write_text(
        "/*\n"
        "  X-Content-Type-Options: nosniff\n"
        "  Referrer-Policy: strict-origin-when-cross-origin\n"
        "  Permissions-Policy: camera=(), microphone=(), geolocation=()\n"
        "\n"
        "/index.html\n"
        "  Cache-Control: no-cache\n",
        encoding="utf-8",
    )
    report["files"].append({"name": "_headers", "bytes": (DIST / "_headers").stat().st_size})

    (DIST / "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"OK: {len(ALLOWLIST)} runtime file(s) -> {DIST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
