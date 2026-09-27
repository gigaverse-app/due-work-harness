"""
Fetch the upstream demo applications at the exact commits the demos were written against.

The demos never vendor upstream code: they run the harness against the demo
applications, and real open-source applications, exactly as their projects ship them. Run this once before the demo
tests; it clones into ``demos/.upstream/`` (ignored by git).
"""

import subprocess
import sys
from pathlib import Path

UPSTREAMS = {
    "procrastinate": (
        "https://github.com/procrastinate-org/procrastinate.git",
        "35f3ca98d979cf5afecb6df902e9a750680edea0",
    ),
    "dbos-demo-apps": ("https://github.com/dbos-inc/dbos-demo-apps.git", "45a68c2ce39838cf6853db4dd71c5a40dac70051"),
    "saleor": ("https://github.com/saleor/saleor.git", "5ff56489737c78a9a5631d528f699303c953696a"),
}

ROOT = Path(__file__).resolve().parent / ".upstream"


def fetch(name: str, url: str, commit: str) -> Path:
    target = ROOT / name
    if not target.exists():
        # Only the pinned commit: an application's full history can be hundreds of megabytes.
        subprocess.run(["git", "init", "--quiet", str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "remote", "add", "origin", url], check=True)
    subprocess.run(["git", "-C", str(target), "fetch", "--quiet", "--depth", "1", "origin", commit], check=True)
    subprocess.run(["git", "-C", str(target), "checkout", "--quiet", "--detach", commit], check=True)
    return target


def main() -> None:
    names = sys.argv[1:] or list(UPSTREAMS)
    for name in names:
        url, commit = UPSTREAMS[name]
        print(f"{name}: {fetch(name, url, commit)} @ {commit[:9]}")


if __name__ == "__main__":
    main()
