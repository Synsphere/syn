"""Prepare/test only the explicit disposable loopback syn_ci database."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from syn.migrations import upgradeDatabase


def main() -> None:
    url = os.environ["SYN_CI_DATABASE_URL"]
    parsed = make_url(url)
    if parsed.host not in {"127.0.0.1", "localhost", "::1"} or parsed.database != "syn_ci":
        raise SystemExit("CI preparation requires the disposable loopback syn_ci database")
    config = Path(os.environ["SYN_CONFIG"])
    if "--test" not in sys.argv:
        engine = create_engine(url)
        try:
            upgradeDatabase(engine)
        finally:
            engine.dispose()
        import json

        config.write_text(
            "[database]\nurl = " + json.dumps(url) + "\n[p2p]\nenabled = false\n", encoding="utf-8"
        )
        return
    if not config.is_file() or os.environ.get("SYN_COMMUNICATION_TEST_URL") != url:
        raise SystemExit("Database acceptance requires both explicit fixture configurations")
    report = config.with_suffix(".junit.xml")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests",
            "-m",
            "database",
            "-o",
            "cache_dir=.tmp-pytest/database-cache",
            "--junitxml=" + str(report),
        ],
        check=False,
    )
    if result.returncode:
        raise SystemExit(result.returncode)
    suites = list(ElementTree.parse(report).getroot().iter("testsuite"))
    if (
        not suites
        or sum(int(s.get("tests", "0")) for s in suites) == 0
        or any(int(s.get("skipped", "0")) for s in suites)
    ):
        raise SystemExit("Required PostgreSQL acceptance must execute without skipped tests")


if __name__ == "__main__":
    main()
