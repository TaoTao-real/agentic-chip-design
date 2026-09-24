#!/usr/bin/env python3
"""Terminate stale processes rooted in one controlled experiment install."""

from __future__ import annotations

import argparse
import json
import os

import psutil


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = os.path.realpath(args.install_root)
    protected = {os.getpid()}
    current = psutil.Process()
    protected.update(parent.pid for parent in current.parents())
    matches: list[psutil.Process] = []
    records = []
    for process in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            if process.pid in protected:
                continue
            cmdline = process.info.get("cmdline") or []
            if not any(root in argument for argument in cmdline):
                continue
            matches.append(process)
            records.append({"pid": process.pid, "cmdline": cmdline})
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    if not args.dry_run:
        for process in matches:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(matches, timeout=10)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(alive, timeout=10)
    print(json.dumps({"install_root": root, "dry_run": args.dry_run, "matches": records}))


if __name__ == "__main__":
    main()
