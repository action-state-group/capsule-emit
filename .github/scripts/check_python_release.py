#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Require a namespaced Python release tag matching the package version."""
import argparse
import re
from pathlib import Path

import tomllib


def check(tag: str, metadata: Path) -> None:
    if not re.fullmatch(r"python/v[0-9]+\.[0-9]+\.[0-9]+(?:[ab]|rc)?[0-9]*", tag):
        raise ValueError("expected a python/v<version> release tag")
    with metadata.open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    if tag != f"python/v{version}":
        raise ValueError("release tag does not match the Python package version")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("metadata", type=Path)
    args = parser.parse_args()
    try:
        check(args.tag, args.metadata)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
