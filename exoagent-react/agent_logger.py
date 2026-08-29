import os
import json
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_NAME = PROJECT_ROOT.name


def _timestamp():
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def log_path():
    log_dir = Path(os.environ.get("EXOAGENT_LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]
    return str(log_dir / f"{timestamp}.txt")


def log(log_file, label, value):
    if value is None:
        return

    if not isinstance(value, str):
        value = json.dumps(value, indent=2, default=str)

    with open(log_file, "a") as file:
        file.write(f"\n\n## {label}\n")
        file.write(f"Logged: {_timestamp()}\n")
        file.write(value)


def log_prompt(log_file, prompt):
    if isinstance(prompt, str):
        prompt_header = prompt
    else:
        prompt_header = json.dumps(prompt, default=str)

    separator = "\n\n" if os.path.exists(log_file) and os.path.getsize(log_file) else ""
    runtime_metadata = {
        "runtime": RUNTIME_NAME,
        "runtime_directory": str(PROJECT_ROOT),
        "process_cwd": str(Path.cwd()),
    }

    with open(log_file, "a") as file:
        file.write(f"{separator}# {prompt_header}\n")
        file.write(f"Started: {_timestamp()}\n")
        file.write("\n## Runtime\n")
        file.write(json.dumps(runtime_metadata, indent=2, default=str))
