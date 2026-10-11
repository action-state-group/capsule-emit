#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Require a namespaced release tag that matches its language's metadata.

Each publisher calls this before building, so a tag from another language's
namespace, or one that disagrees with the version or module path it would
publish, never reaches a registry.
"""
import argparse
import json
import re
from pathlib import Path

import tomllib

GO_MODULE = "github.com/action-state-group/capsule-emit/go"
SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
TAGS = {
    "python": rf"python/v{SEMVER}(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?",
    "ts": rf"ts/v{SEMVER}(?:-[0-9A-Za-z.-]+)?",
    "go": rf"go/v{SEMVER}(?:-[0-9A-Za-z.-]+)?",
}


def check(language: str, tag: str, metadata: Path) -> None:
    pattern = TAGS.get(language)
    if pattern is None:
        raise ValueError(f"unknown release language {language!r}")
    match = re.fullmatch(pattern, tag)
    if match is None:
        raise ValueError(f"expected a {language}/v<version> release tag")
    if language == "python":
        with metadata.open("rb") as stream:
            version = tomllib.load(stream)["project"]["version"]
    elif language == "ts":
        version = json.loads(metadata.read_text())["version"]
    else:
        module = re.search(r"^module\s+(\S+)\s*$", metadata.read_text(), re.MULTILINE)
        if module is None or module.group(1) != GO_MODULE:
            raise ValueError(f"go.mod must declare module {GO_MODULE}")
        # A major version of 2 or more needs a /vN module path suffix, which
        # this module does not declare.
        if int(match.group(1)) >= 2:
            raise ValueError("go/v2+ tags need a /vN module path")
        return
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
