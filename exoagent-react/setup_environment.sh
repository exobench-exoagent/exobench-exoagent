#!/usr/bin/env bash
set -eu

PYTHON_BIN="${PYTHON_BIN:-python3.10}"
VENV_DIR="${VENV_DIR:-.venv}"

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info[:2] != (3, 10):
    raise SystemExit(
        "Python 3.10 is required for EXOTIC compatibility. "
        f"Found Python {sys.version.split()[0]}. "
        "Run with PYTHON_BIN=python3.10 sh setup_environment.sh if needed."
    )
PY

if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

. "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python - <<'PY'
import os
import shutil
from importlib import metadata

for package_name in ["exotic", "astropy", "photutils", "pyraf", "Flask", "python-dotenv"]:
    print(f"{package_name}=={metadata.version(package_name)}")

import exotic
print(f"EXOTIC import OK: {exotic.__file__}")

exotic_cli = shutil.which("exotic")
exotic_gui_cli = shutil.which("exotic-gui")
print(f"EXOTIC CLI: {exotic_cli or 'not found'}")
print(f"EXOTIC GUI CLI: {exotic_gui_cli or 'not found'}")

if not exotic_cli:
    raise SystemExit("EXOTIC installed but the `exotic` command was not found on PATH.")

if not os.environ.get("iraf"):
    print(
        "WARNING: pyraf is installed, but IRAF may not be configured. "
        "Set the iraf environment variable to your IRAF installation directory "
        "before running IRAF tasks through PyRAF."
    )
PY
