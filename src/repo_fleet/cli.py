"""Command line entry point: ``repo-fleet [-c config.toml] status|pull|bundle|verify|all``."""
from __future__ import annotations

import argparse
import os
import sys

from . import core

DEFAULT_CONFIG = os.path.expanduser("~/.config/repo-fleet/config.toml")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="repo-fleet", description=__doc__)
    ap.add_argument("-c", "--config", default=os.environ.get("REPO_FLEET_CONFIG", DEFAULT_CONFIG))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="one line per repo: dirty / ahead / behind / no remote")
    s.add_argument("--fetch", action="store_true", help="fetch before reporting")
    sub.add_parser("pull", help="fetch everywhere; fast-forward clean repos on their default branch")
    sub.add_parser("bundle", help="verified git bundle per repo, copied to every destination")
    sub.add_parser("verify", help="check bundle checksums in the backup directory")
    sub.add_parser("all", help="pull + bundle + status (what a daily timer runs)")
    a = ap.parse_args(argv)
    try:
        cfg = core.load_config(a.config)
    except (OSError, ValueError, KeyError) as e:
        print(f"config error ({a.config}): {e}", file=sys.stderr)
        return 2
    if a.cmd == "status":
        return 1 if core.cmd_status(cfg, fetch=a.fetch) else 0
    if a.cmd == "pull":
        return 1 if core.cmd_pull(cfg) else 0
    if a.cmd == "bundle":
        return 1 if core.cmd_bundle(cfg) else 0
    if a.cmd == "verify":
        bad = [b for d in cfg.backup_dir.iterdir() if d.is_dir() for b in core.verify_checksums(d)]
        print("all bundles match their checksums" if not bad else "MISMATCH: " + ", ".join(bad))
        return 1 if bad else 0
    return core.cmd_all(cfg)


if __name__ == "__main__":
    sys.exit(main())
