#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check relocated Python source, editable, wheel and sdist installs externally."""
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import tomllib

PROJECT = Path(__file__).resolve().parents[2] / "python"
SMOKE = """
import importlib.metadata as metadata
from capsule_emit import seal
from capsule_emit.verification import verify_capsule
from capsule_emit.adapters import ext_mcp_pb2
assert ext_mcp_pb2.McpRequest is not None
capsule = seal({'stage':'external-package'}, action='test', operator='test',
               developer='test', ledger='external.jsonl', witness=False, anchor=False)
assert verify_capsule(capsule.capsule).ok
entrypoints = {e.name:e.value for e in metadata.distribution('capsule-emit').entry_points}
assert entrypoints == {
 'capsule-emit':'capsule_emit.cli:main',
 'capsule-emit-server':'capsule_emit.server:main',
 'capsule-emit-agentgateway':'capsule_emit.adapters.agentgateway:main'}
print('External import/seal/verification/entrypoints OK')
"""


def run(*args, cwd, env=None):
    subprocess.run(args, cwd=cwd, env=env, check=True)


def inventory(wheel):
    expected = {str(p.relative_to(PROJECT)): p.read_bytes()
                for p in (PROJECT / "capsule_emit").rglob("*.py")}
    with zipfile.ZipFile(wheel) as archive:
        actual = {n: archive.read(n) for n in archive.namelist() if n.startswith("capsule_emit/")}
        assert actual == expected, "wheel module contents differ from source"
        assert any(n.endswith("/LICENSE") for n in archive.namelist())
        assert any(n.endswith("/NOTICE") for n in archive.namelist())
    print(f"Wheel inventory matches all {len(expected)} source modules")


def main():
    with tempfile.TemporaryDirectory(prefix="emit-python-package-") as directory:
        work = Path(directory)
        dist = work / "dist"
        run(sys.executable, "-m", "build", str(PROJECT), "--outdir", str(dist), cwd=work)
        wheel = next(dist.glob("*.whl"))
        inventory(wheel)
        unpacked = work / "unpacked"
        with tarfile.open(next(dist.glob("*.tar.gz"))) as archive:
            archive.extractall(unpacked, filter="data")
        rebuilt = work / "rebuilt"
        run(sys.executable, "-m", "build", "--wheel", str(next(unpacked.iterdir())),
            "--outdir", str(rebuilt), cwd=work)
        inventory(next(rebuilt.glob("*.whl")))
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["CAPSULE_WITNESS"] = "off"
        consumer = work / "consumer"
        run(sys.executable, "-m", "venv", str(consumer), cwd=work)
        py = consumer / "bin" / "python"
        # Generated protobuf modules are part of the agentgateway extra contract.
        for name, package in (("editable", f"{PROJECT}[agentgateway]"),
                              ("wheel", f"{wheel}[agentgateway]"),
                              ("sdist-wheel", f"{next(rebuilt.glob('*.whl'))}[agentgateway]")):
            args = [str(py), "-m", "pip", "install", "--force-reinstall"]
            if name == "editable":
                args.append("-e")
            run(*args, package, cwd=work, env=env)
            smoke = work / name
            smoke.mkdir()
            run(str(py), "-c", SMOKE, cwd=smoke, env=env)
            run(str(consumer / "bin" / "capsule-emit"), "--help", cwd=smoke, env=env)
        env["PYTHONPATH"] = str(PROJECT)
        source = work / "source"
        source.mkdir()
        run(str(py), "-c", SMOKE, cwd=source, env=env)
        with (PROJECT / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)["project"]
        print(json.dumps({"name": project["name"], "version": project["version"],
                          "stages": ["source", "editable", "wheel", "sdist-wheel"]}))


if __name__ == "__main__":
    main()
