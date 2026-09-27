#!/usr/bin/env python3
"""Read-only, exact-tree classification of the first isolated Tasks release.

This is not a deployment command. No network, DB access or Git mutation.
The one-release manifest must be regenerated/reviewed if ANY source changes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

MANIFEST = "ops/tasks-release-classification.json"
NON_SCHEMA_PATHS = ("app/auth.py", "app/schema_migrations.py")


def git(root, *arguments):
    return subprocess.check_output(["git"] + list(arguments), cwd=str(root),
                                   stderr=subprocess.PIPE)


def revision(value):
    if not re.match(r"^[0-9a-f]{40}$", value):
        raise ValueError("An exact Git object is required")
    return value


def tree_digest(root, candidate):
    records = git(root, "ls-tree", "-r", "-z", "--full-tree", revision(candidate)).split(b"\0")
    # Only the manifest itself is excluded, avoiding a self-referential hash.
    records = [record for record in records if record and
               record.split(b"\t", 1)[1] != MANIFEST.encode("ascii")]
    return hashlib.sha256(b"\0".join(sorted(records)) + b"\0").hexdigest()


def schema_changes(root, base, candidate):
    revision(base)
    revision(candidate)
    if git(root, "cat-file", "-t", base).strip() != b"commit":
        raise ValueError("Base must be a commit")
    changes = git(root, "diff", "--name-only", "-z", base, candidate).decode("utf-8").split("\0")
    changes = [path for path in changes if path]
    entries = git(root, "ls-tree", candidate, "--", MANIFEST)
    if not entries:
        return changes
    manifest = json.loads(git(root, "show", candidate + ":" + MANIFEST).decode("utf-8"))
    if (set(manifest) != {"version", "base_commit", "candidate_tree_sha256", "non_schema_paths"}
            or manifest["version"] != 1 or manifest["base_commit"] != base
            or manifest["candidate_tree_sha256"] != tree_digest(root, candidate)
            or manifest["non_schema_paths"] != list(NON_SCHEMA_PATHS)
            or not set(NON_SCHEMA_PATHS).issubset(set(changes))):
        raise ValueError("Tasks release classification does not match the exact reviewed trees")
    return [path for path in changes if path not in NON_SCHEMA_PATHS]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()
    try:
        paths = schema_changes(Path(args.repository), args.base, args.candidate)
    except (ValueError, TypeError, KeyError, OSError, subprocess.CalledProcessError):
        print("PRECHECK_FAILED: invalid Tasks release classification", file=sys.stderr)
        return 1
    print("\n".join(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
