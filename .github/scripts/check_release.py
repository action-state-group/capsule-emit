#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Require a namespaced release tag that matches its language's metadata.

Each publisher calls this before building, so a tag from another language's
namespace, or one that disagrees with the version it would publish, never
reaches PyPI or npm.
"""
import argparse
import json
import re
from pathlib import Path

import tomllib

SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
TAGS = {
    "python": rf"python/v{SEMVER}(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?",
    "ts": rf"ts/v{SEMVER}(?:-[0-9A-Za-z.-]+)?",
}


def check(language: str, tag: str, metadata: Path) -> None:
    pattern = TAGS.get(language)
    if pattern is None:
        raise ValueError(f"unknown release language {language!r}")
    if re.fullmatch(pattern, tag) is None:
        raise ValueError(f"expected a {language}/v<version> release tag")
    if language == "python":
        with metadata.open("rb") as stream:
            version = tomllib.load(stream)["project"]["version"]
    else:
        version = json.loads(metadata.read_text())["version"]
    if tag != f"{language}/v{version}":
        raise ValueError(f"release tag does not match the {language} package version")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("language", choices=sorted(TAGS))
    parser.add_argument("tag")
    parser.add_argument("metadata", type=Path)
    args = parser.parse_args()
    try:
        check(args.language, args.tag, args.metadata)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
