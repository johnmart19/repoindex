#!/usr/bin/env python3
"""Build an sdist/wheel and test isolated installed use. No network or real sources."""
from pathlib import Path
import json
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def run(args, cwd, env=None):
    result = subprocess.run([str(x) for x in args], cwd=cwd, env=env,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError("Command failed: " + " ".join(map(str, args)) + "\n" + (result.stdout + result.stderr)[-6000:])
    return result.stdout


def build(source, destination, kind):
    code = ("import os,sys; os.chdir(sys.argv[1]); "
            "from setuptools import build_meta; "
            "getattr(build_meta, 'build_' + sys.argv[3])(sys.argv[2])")
    run([sys.executable, "-I", "-c", code, source, destination, kind], ROOT)


def main():
    with tempfile.TemporaryDirectory(prefix="repoindex-installed-") as tmp:
        temp = Path(tmp)
        dist = temp / "dist"
        dist.mkdir()
        build(ROOT, dist, "sdist")
        source_archive = next(dist.glob("*.tar.gz"))
        unpacked = temp / "unpacked"
        unpacked.mkdir()
        # The archive is locally built, but still validate every destination/link.
        with tarfile.open(source_archive) as archive:
            for member in archive.getmembers():
                target = (unpacked / member.name).resolve()
                if unpacked not in target.parents or not (member.isfile() or member.isdir()):
                    raise ValueError("Unexpected sdist member: " + member.name)
            archive.extractall(unpacked)
        source = next(unpacked.iterdir())
        build(source, dist, "wheel")
        wheel = next(dist.glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            metadata = archive.read(next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))).decode()
            if "Requires-Dist:" in metadata:
                raise ValueError("Unexpected runtime dependency")
            if "repoindex/workspace.py" not in archive.namelist():
                raise ValueError("Missing workspace implementation")
        environment = temp / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / "bin/python"
        command = environment / "bin/repoindex"
        run([python, "-I", "-m", "pip", "--disable-pip-version-check", "install",
             "--no-index", "--no-deps", wheel], temp)
        version = run([command, "--version"], temp).strip()
        if run([python, "-I", "-m", "repoindex", "--version"], temp).strip() != version:
            raise ValueError("Console/module versions differ")
        legacy = temp / "legacy"
        legacy.mkdir()
        (legacy / "mode.h").write_text("struct SharedMode {};\n")
        legacy_db = temp / "legacy.db"
        run([command, "index", legacy, "--db", legacy_db, "--jobs", "1"], temp)
        commands = [
            ["search", "Mode", "--paths-only", "--limit", "1"],
            ["lookup", "SharedMode"], ["symbols", "--out", temp / "symbols.md"],
            ["status", "--json"], ["stats"], ["tree"],
            ["dupes", "--root", legacy],
            ["agentsmd", legacy, "--out", temp / "guide.md"],
        ]
        for arguments in commands:
            run([command] + arguments + ["--db", legacy_db], temp)
        run([command, "headers", legacy, "--out", temp / "headers.md"], temp)
        workspace = temp / "workspace"
        workspace.mkdir()
        (workspace / "repoindex.json").write_text(json.dumps({"trees": ["alpha", "beta"]}))
        for tree in ("alpha", "beta"):
            directory = workspace / tree
            directory.mkdir()
            (directory / "mode.h").write_text("struct SharedMode {};\n")
            (directory / "impl.cpp").write_text("int dispatch() {\n  return run_body();\n}\n")
        run([command, "workspace", "index", "--workspace", workspace, "--jobs", "1"], temp)
        # Hide live sources and restored caches: subsequent lookups must restore bundles.
        for name in ("alpha", "beta", ".cache"):
            (workspace / name).rename(temp / ("hidden-" + name))
        rows = [json.loads(line) for line in run([command, "workspace", "impl", "run_body",
                "--workspace", workspace, "--json"], temp).splitlines()]
        if {row["tree"] for row in rows} != {"alpha", "beta"}:
            raise ValueError("Installed multi-tree body query failed")
        single = run([command, "workspace", "files", "impl.cpp", "--workspace", workspace,
                      "--tree", "alpha", "--json"], temp).splitlines()
        if len(single) != 1 or json.loads(single[0])["tree"] != "alpha":
            raise ValueError("Installed tree selection failed")
        source = json.loads(run([command, "workspace", "source", "beta/impl.cpp", "--line", "2",
                                "--limit", "1", "--workspace", workspace, "--json"], temp))
        if source["text"] != "  return run_body();":
            raise ValueError("Installed offline line retrieval failed")
        api = ("import pathlib,repoindex; from repoindex import workspace; "
               "assert str(pathlib.Path(repoindex.__file__).resolve()).startswith(" + repr(str(environment)) + "); "
               "assert len(list(workspace.query(pathlib.Path(" + repr(str(workspace)) + "), None, 'symbol', 'SharedMode'))) == 2")
        run([python, "-I", "-c", api], temp)
        print(version + ": sdist-to-wheel, isolated console/module/API, all legacy commands and source-free queries passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
