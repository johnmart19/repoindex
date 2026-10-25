#!/usr/bin/env python3
"""Local standalone checks; no network, models, devices or real source scans."""
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BUDGETS = {"README.md": 4000, "AGENTS.md": 1500, "docs/USAGE.md": 10000}
sys.path.insert(0, str(ROOT / "src"))
from repoindex.checks import documentation_errors


def main():
    errors = documentation_errors(ROOT, BUDGETS)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    command = [sys.executable, "-W", "always::ResourceWarning", "-m", "unittest", "discover", "-s", "tests", "-v"]
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode or "ResourceWarning:" in result.stdout:
        print(result.stdout[-12000:])
        return result.returncode or 1
    summary = re.search(r"Ran \d+ tests.*", result.stdout, flags=re.S)
    print(summary.group(0).strip() if summary else "Fixture suite passed")
    if not shutil.which("git"):
        print("Documentation, entry-point budgets and local checks passed")
        return 0
    git = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT,
                         text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if git.returncode == 0:
        for extra in ([], ["--cached"]):
            check = subprocess.run(["git", "diff"] + extra + ["--check", "--", "."], cwd=ROOT)
            if check.returncode:
                return check.returncode
    print("Documentation, entry-point budgets and local checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
