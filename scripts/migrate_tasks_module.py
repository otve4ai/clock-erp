#!/usr/bin/env python3
"""Explicit offline new Tasks schema migration; never imports ERP or legacy tasks."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tasks.migrations import migrate_database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--app-commit", default="")
    arguments = parser.parse_args()
    print(json.dumps(migrate_database(arguments.database, arguments.app_commit), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
