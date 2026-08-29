import fnmatch
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin
import requests
from agent_logger import log as agent_log, log_path as agent_log_path

PROJECT_ROOT = Path(__file__).resolve().parent
SHARED_ENV_PATH = PROJECT_ROOT.parent / ".env"

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(dotenv_path=".env"):
        env_path = Path(dotenv_path).expanduser()

        if not env_path.exists():
            return False

        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

        return True

load_dotenv(dotenv_path=SHARED_ENV_PATH)

EXOTIC_IDLE_TIMEOUT_CAP_SECONDS = 900.0


def _positive_float_env(name, default):
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default

    try:
        value = float(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive number of seconds") from error

    if value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds")

    return value


DEFAULT_EXOTIC_IDLE_TIMEOUT_SECONDS = min(
    _positive_float_env("EXOAGENT_EXOTIC_IDLE_TIMEOUT_SECONDS", 300.0),
    EXOTIC_IDLE_TIMEOUT_CAP_SECONDS,
)


def _resolve_exotic_idle_timeout(value):
    timeout = DEFAULT_EXOTIC_IDLE_TIMEOUT_SECONDS if value is None else float(value)
    if timeout <= 0:
        raise ValueError("idle_timeout_seconds must be a positive number of seconds")
    return min(timeout, EXOTIC_IDLE_TIMEOUT_CAP_SECONDS)

TODO_ITEMS = []
SHELL_SESSION = None
WORKSPACE_BASE = Path(
    os.environ.get("EXOAGENT_WORKSPACE_ROOT", PROJECT_ROOT / "exoagent-workspace")
).expanduser().resolve()
INPUT_ROOT = Path(
    os.environ.get("EXOAGENT_INPUT_ROOT", PROJECT_ROOT.parent / "input")
).expanduser().resolve()
ACTIVE_RUN_ID = os.environ.get("EXOAGENT_RUN_ID")
WORKSPACE_ROOT = None


def _new_run_id():
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _safe_run_id(value):
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or _new_run_id())).strip("._-")
    return safe_name or _new_run_id()


def _run_root(base, run_id):
    return (Path(base).expanduser().resolve() / "runs" / _safe_run_id(run_id)).resolve()


def _ensure_workspace_layout(root):
    for dirname in ("scratch", "output", "logs", "tmp"):
        (root / dirname).mkdir(parents=True, exist_ok=True)


def _ensure_active_workspace():
    if WORKSPACE_ROOT is None:
        return configure_workspace()
    return WORKSPACE_ROOT


def configure_workspace(run_id=None, workspace_base=None):
    """Create/select the active run workspace and reset process-local shell state."""
    global WORKSPACE_BASE, ACTIVE_RUN_ID, WORKSPACE_ROOT, SHELL_SESSION

    WORKSPACE_BASE = Path(workspace_base or WORKSPACE_BASE).expanduser().resolve()
    ACTIVE_RUN_ID = _safe_run_id(run_id or ACTIVE_RUN_ID or _new_run_id())
    WORKSPACE_ROOT = _run_root(WORKSPACE_BASE, ACTIVE_RUN_ID)
    _ensure_workspace_layout(WORKSPACE_ROOT)

    os.environ["EXOAGENT_RUN_ID"] = ACTIVE_RUN_ID
    os.environ["EXOAGENT_WORKSPACE_ROOT"] = str(WORKSPACE_BASE)
    os.environ["EXOAGENT_INPUT_ROOT"] = str(INPUT_ROOT)
    os.environ["EXOAGENT_LOG_DIR"] = str(WORKSPACE_ROOT / "logs")

    if SHELL_SESSION is not None and SHELL_SESSION.process.poll() is None:
        _terminate_process(SHELL_SESSION.process)
    SHELL_SESSION = None
    return WORKSPACE_ROOT

os.environ.setdefault("EXOAGENT_WORKSPACE_ROOT", str(WORKSPACE_BASE))
os.environ.setdefault("EXOAGENT_INPUT_ROOT", str(INPUT_ROOT))


def get_default_input_path():
    return os.environ.get("EXOAGENT_DEFAULT_INPUT_PATH") or str(INPUT_ROOT)


def _error_dict(error):
    return {
        "ok": False,
        "status": "error",
        "error": str(error),
        "traceback": traceback.format_exc()
    }


def _is_relative_to(path, root):
    return path == root or root in path.parents


def _path(path, access="read"):
    workspace_root = _ensure_active_workspace()
    candidate_path = Path(path).expanduser()

    if candidate_path.is_absolute():
        resolved_path = candidate_path.resolve()
    else:
        resolved_path = (workspace_root / candidate_path).resolve()

    if _is_relative_to(resolved_path, workspace_root):
        return resolved_path

    if access == "read" and _is_relative_to(resolved_path, INPUT_ROOT):
        return resolved_path

    if access != "read":
        raise PermissionError(
            f"Write/execute access outside exoagent-workspace is blocked: {resolved_path}. "
            f"Allowed writable workspace: {workspace_root}. "
            f"Read-only input root: {INPUT_ROOT}"
        )

    raise PermissionError(
        f"Path access outside exoagent-workspace or read-only input root is blocked: {resolved_path}. "
        f"Allowed workspace: {workspace_root}. "
        f"Read-only input root: {INPUT_ROOT}"
    )


def _copy_into_workspace(source_path, destination_dir):
    source = Path(source_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(str(source))

    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / _safe_filename(source.name, default="input")

    if source.is_dir():
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    return destination.resolve()


def prepare_run_workspace(input_path=None, run_id=None, workspace_base=None, copy_input=False):
    """Prepare a per-run workspace and reference input data in place by default."""
    run_root = configure_workspace(run_id=run_id, workspace_base=workspace_base)
    prepared_input = None

    if input_path:
        if re.match(r"^https?://", str(input_path).strip(), flags=re.IGNORECASE):
            raise ValueError(
                "Google Drive web URLs are not readable as FITS directories. "
                "Mount Google Drive first, then use the mounted folder path as EXOAGENT_INPUT_ROOT."
            )
        candidate = Path(input_path).expanduser()
        resolved_candidate = candidate.resolve()

        if not resolved_candidate.exists():
            raise FileNotFoundError(str(resolved_candidate))

        if resolved_candidate == run_root or run_root in resolved_candidate.parents:
            prepared_input = resolved_candidate
        elif copy_input:
            prepared_input = _copy_into_workspace(resolved_candidate, run_root / "input")
        else:
            prepared_input = _path(resolved_candidate, access="read")

    return {
        "run_id": ACTIVE_RUN_ID,
        "workspace_base": str(WORKSPACE_BASE),
        "workspace_root": str(run_root),
        "input_path": str(prepared_input) if prepared_input else None,
        "input_root": str(INPUT_ROOT),
        "logs_dir": str(run_root / "logs"),
        "output_dir": str(run_root / "output"),
        "scratch_dir": str(run_root / "scratch"),
        "tmp_dir": str(run_root / "tmp"),
    }


def _detect_encoding(raw_content):
    encodings = [
        ("utf-8-sig", raw_content.startswith(b"\xef\xbb\xbf")),
        ("utf-16", raw_content.startswith(b"\xff\xfe") or raw_content.startswith(b"\xfe\xff")),
        ("utf-8", True),
        ("cp1252", True),
        ("latin-1", True)
    ]

    for encoding, should_try in encodings:
        if not should_try:
            continue

        try:
            return raw_content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue

    return raw_content.decode("latin-1", errors="replace"), "latin-1"


def _guess_language(path):
    extension_map = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".jsx": "javascript",
        ".tsx": "typescript",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".md": "markdown",
        ".html": "html",
        ".css": "css",
        ".sh": "shell",
        ".sql": "sql",
        ".xml": "xml",
        ".ini": "ini",
        ".cfg": "ini",
        ".csv": "csv"
    }
    return extension_map.get(Path(path).suffix.lower(), "text")


def _line_range(content, start_line=None, end_line=None):
    lines = content.splitlines()
    start_index = max((start_line or 1) - 1, 0)
    end_index = end_line if end_line is not None else len(lines)
    selected_lines = lines[start_index:end_index]

    return "\n".join(
        f"{line_number}: {line}"
        for line_number, line in enumerate(selected_lines, start=start_index + 1)
    )


def _safe_filename(value, default="image"):
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return safe_name or default


def _truncate_text(value, max_chars=4000):
    if value is None or len(value) <= max_chars:
        return value, False

    return value[:max_chars], True


def _subprocess_env(env=None):
    venv_bin = PROJECT_ROOT / ".venv" / "bin"
    path_parts = [str(venv_bin)]
    if os.environ.get("PATH"):
        path_parts.append(os.environ["PATH"])

    inherited_env = dict(os.environ)
    inherited_env.update({
        "PATH": os.pathsep.join(path_parts),
        "PYTHONUNBUFFERED": "1",
        "EXOAGENT_RUN_ID": ACTIVE_RUN_ID,
        "EXOAGENT_WORKSPACE_ROOT": str(WORKSPACE_BASE),
        "EXOAGENT_INPUT_ROOT": str(INPUT_ROOT),
        "EXOAGENT_LOG_DIR": str(WORKSPACE_ROOT / "logs"),
    })

    inherited_env.update({
        str(key): str(value)
        for key, value in (env or {}).items()
        if value is not None
    })
    return inherited_env


def _compact_aavso_chart_json(chart_json, max_photometry_rows=200):
    if not isinstance(chart_json, dict):
        return chart_json, None, False

    compact_json = dict(chart_json)
    photometry = compact_json.get("photometry")

    if not isinstance(photometry, list):
        return compact_json, None, False

    photometry_count = len(photometry)
    if photometry_count > max_photometry_rows:
        compact_json["photometry"] = photometry[:max_photometry_rows]
        compact_json["photometry_truncated"] = True
        compact_json["photometry_count"] = photometry_count
        return compact_json, photometry_count, True

    compact_json["photometry_count"] = photometry_count
    return compact_json, photometry_count, False


def _reject_workspace_escape(value):
    if value is None:
        return

    text = str(value)
    allowed_prefixes = (
        str(WORKSPACE_ROOT),
        str(WORKSPACE_BASE),
        str(INPUT_ROOT),
    )
    blocked_patterns = [
        "../",
        "..\\",
        "cd ..",
        "cd /",
        " ~",
        "~/",
        "/etc/",
        "/root/",
        ".env",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ASTROMETRY_API_KEY",
    ]

    for pattern in blocked_patterns:
        if pattern in text and not any(prefix in text for prefix in allowed_prefixes):
            raise PermissionError(
                f"Command/code appears to access outside exoagent-workspace and was blocked: {pattern}"
            )


def ReadFileTool(path, start_line=None, end_line=None, max_chars=None, highlight=True):
    """Read a workspace-contained text file with encoding detection and line numbering."""
    try:
        file_path = _path(path)

        with file_path.open("rb") as file:
            raw_content = file.read()

        content, encoding = _detect_encoding(raw_content)
        selected_content = _line_range(content, start_line=start_line, end_line=end_line)

        if max_chars is not None:
            selected_content = selected_content[:max_chars]

        return {
            "ok": True,
            "status": "success",
            "path": str(file_path),
            "encoding": encoding,
            "language": _guess_language(file_path) if highlight else None,
            "content": selected_content,
            "error": None,
            "traceback": None
        }
    except Exception as error:
        return _error_dict(error)


def WriteFileTool(path, content, encoding="utf-8", backup=True, create_dirs=True):
    """Write text inside the workspace, optionally backing up an existing file."""
    try:
        file_path = _path(path, access="write")

        if create_dirs:
            file_path.parent.mkdir(parents=True, exist_ok=True)

        backup_path = None
        if backup and file_path.exists():
            backup_path = file_path.with_name(f"{file_path.name}.bak.{int(time.time())}")
            shutil.copy2(file_path, backup_path)

        file_path.write_text(content, encoding=encoding)

        return {
            "ok": True,
            "status": "success",
            "path": str(file_path),
            "bytes_written": len(content.encode(encoding)),
            "backup_path": str(backup_path) if backup_path else None,
            "error": None,
            "traceback": None
        }
    except Exception as error:
        return _error_dict(error)


def EditFileTool(path, edits, encoding=None, backup=True):
    """Apply ordered literal replacements to a workspace file and report each edit."""
    try:
        file_path = _path(path, access="write")
        raw_content = file_path.read_bytes()
        content, detected_encoding = _detect_encoding(raw_content)
        file_encoding = encoding or detected_encoding
        updated_content = content
        applied_edits = []

        for edit in edits:
            old_text = edit.get("old_text", "")
            new_text = edit.get("new_text", "")
            count = edit.get("count", 1)

            if old_text not in updated_content:
                applied_edits.append({
                    "old_text": old_text,
                    "applied": 0,
                    "error": "old_text not found"
                })
                continue

            updated_content, applied_count = re.subn(
                re.escape(old_text),
                lambda _: new_text,
                updated_content,
                count=count
            )
            applied_edits.append({
                "old_text": old_text,
                "applied": applied_count,
                "error": None
            })

        backup_path = None
        if backup:
            backup_path = file_path.with_name(f"{file_path.name}.bak.{int(time.time())}")
            shutil.copy2(file_path, backup_path)

        file_path.write_text(updated_content, encoding=file_encoding)

        return {
            "ok": True,
            "status": "success",
            "path": str(file_path),
            "encoding": file_encoding,
            "backup_path": str(backup_path) if backup_path else None,
            "edits": applied_edits,
            "error": None,
            "traceback": None
        }
    except Exception as error:
        return _error_dict(error)


def ListDirectoryTool(path, recursive=True, pattern=None, include_dirs=True, include_files=True, max_results=200):
    """List workspace directory entries with optional recursion and glob filtering."""
    try:
        root_path = _path(path)
        results = []

        if not root_path.exists():
            raise FileNotFoundError(str(root_path))

        iterator = root_path.rglob("*") if recursive else root_path.iterdir()

        for entry in iterator:
            relative_path = str(entry.relative_to(root_path))
            entry_type = "directory" if entry.is_dir() else "file"

            if entry_type == "directory" and not include_dirs:
                continue

            if entry_type == "file" and not include_files:
                continue

            if pattern and not fnmatch.fnmatch(relative_path, pattern):
                continue

            results.append({
                "path": str(entry),
                "relative_path": relative_path,
                "type": entry_type
            })

            if len(results) >= max_results:
                return {
                    "ok": True,
                    "status": "success",
                    "path": str(root_path),
                    "truncated": True,
                    "results": results,
                    "error": None,
                    "traceback": None
                }

        return {
            "ok": True,
            "status": "success",
            "path": str(root_path),
            "truncated": False,
            "results": results,
            "error": None,
            "traceback": None
        }
    except Exception as error:
        return _error_dict(error)


def FileSearchTool(pattern, path, file_pattern="*", context_lines=2, max_results=100):
    """Search workspace files with a regex and return matched lines with context."""
    try:
        root_path = _path(path)
        regex = re.compile(pattern)
        matches = []

        for file_path in root_path.rglob(file_pattern):
            if not file_path.is_file():
                continue

            try:
                content, _ = _detect_encoding(file_path.read_bytes())
            except Exception as error:
                matches.append({
                    "path": str(file_path),
                    "line_number": None,
                    "line": None,
                    "context": [],
                    "error": str(error),
                    "traceback": traceback.format_exc()
                })
                continue

            lines = content.splitlines()
            for index, line in enumerate(lines):
                if not regex.search(line):
                    continue

                context_start = max(index - context_lines, 0)
                context_end = min(index + context_lines + 1, len(lines))
                matches.append({
                    "path": str(file_path),
                    "line_number": index + 1,
                    "line": line,
                    "context": [
                        {
                            "line_number": line_number + 1,
                            "content": lines[line_number]
                        }
                        for line_number in range(context_start, context_end)
                    ],
                    "error": None,
                    "traceback": None
                })

                if len(matches) >= max_results:
                    return {
                        "ok": True,
                        "status": "success",
                        "truncated": True,
                        "matches": matches,
                        "error": None,
                        "traceback": None
                    }

        return {
            "ok": True,
            "status": "success",
            "truncated": False,
            "matches": matches,
            "error": None,
            "traceback": None
        }
    except Exception as error:
        return _error_dict(error)


def FindFilesTool(pattern, path, max_results=200):
    """Find workspace files using a glob pattern."""
    try:
        root_path = _path(path)
        matches = [
            str(file_path)
            for file_path in root_path.glob(pattern)
        ]

        return {
            "ok": True,
            "status": "success",
            "pattern": pattern,
            "path": str(root_path),
            "truncated": len(matches) > max_results,
            "files": matches[:max_results],
            "error": None,
            "traceback": None
        }
    except Exception as error:
        return _error_dict(error)


def WorkspaceInfoTool():
    """Return the active run workspace paths visible to agent tools."""
    try:
        return {
            "ok": True,
            "status": "success",
            "run_id": ACTIVE_RUN_ID,
            "workspace_base": str(WORKSPACE_BASE),
            "workspace_root": str(WORKSPACE_ROOT),
            "read_only_input_root": str(INPUT_ROOT),
            "scratch_dir": str(WORKSPACE_ROOT / "scratch"),
            "output_dir": str(WORKSPACE_ROOT / "output"),
            "logs_dir": str(WORKSPACE_ROOT / "logs"),
            "tmp_dir": str(WORKSPACE_ROOT / "tmp"),
            "isolation": "workspace_writes_with_read_only_input_root_and_scrubbed_subprocess_env",
            "error": None,
            "traceback": None,
        }
    except Exception as error:
        return _error_dict(error)


class _ShellSession:
    """Persistent Bash subprocess used by RunCommandTool."""

    def __init__(self, cwd=None, env=None):
        self.cwd = str(_path(cwd, access="write")) if cwd else str(WORKSPACE_ROOT)
        self.process = subprocess.Popen(
            ["/bin/bash"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.cwd,
            env=_subprocess_env(env),
            text=True,
            bufsize=1
        )
        os.set_blocking(self.process.stdout.fileno(), False)

    def run(self, command, timeout=30):
        sentinel = f"__EXOAGENT_COMMAND_DONE_{uuid.uuid4().hex}__"
        command_block = f"{command}\nprintf '\\n{sentinel}:%s\\n' \"$?\"\n"

        self.process.stdin.write(command_block)
        self.process.stdin.flush()

        output = ""
        started_at = time.monotonic()

        while time.monotonic() - started_at < timeout:
            try:
                chunk = os.read(self.process.stdout.fileno(), 4096).decode("utf-8", errors="replace")
            except BlockingIOError:
                time.sleep(0.05)
                continue

            if not chunk:
                time.sleep(0.05)
                continue

            output += chunk
            if sentinel in output:
                break

        match = re.search(rf"\n{sentinel}:(\d+)\n", output)

        if not match:
            return {
                "ok": False,
                "status": "timeout",
                "output": output,
                "exit_code": None,
                "timed_out": True,
                "cwd": self.cwd,
                "workspace_root": str(WORKSPACE_ROOT),
                "error": f"Command timed out after {timeout} seconds",
                "traceback": None
            }

        return {
            "ok": True,
            "status": "success",
            "output": output[:match.start()].strip(),
            "exit_code": int(match.group(1)),
            "timed_out": False,
            "cwd": self.cwd,
            "workspace_root": str(WORKSPACE_ROOT),
            "error": None,
            "traceback": None
        }


def RunCommandTool(command, timeout=30, cwd=None, env=None):
    """Run a shell command in the persistent workspace-contained Bash session."""
    try:
        global SHELL_SESSION

        _reject_workspace_escape(command)
        resolved_cwd = str(_path(cwd, access="write")) if cwd else str(WORKSPACE_ROOT)
        if (
            SHELL_SESSION is None
            or SHELL_SESSION.process.poll() is not None
            or SHELL_SESSION.cwd != resolved_cwd
        ):
            SHELL_SESSION = _ShellSession(cwd=cwd, env=env)

        return SHELL_SESSION.run(command, timeout=timeout)
    except Exception as error:
        return _error_dict(error)


def RunPythonTool(code, timeout=30, cwd=None, env=None):
    """Execute Python code in a workspace-contained subprocess with common imports."""
    try:
        _reject_workspace_escape(code)
        python_code = "\n".join([
            "import json, math, statistics, collections, itertools, functools, re, os, sys",
            f"os.chdir({json.dumps(str(_path(cwd, access='write') if cwd else WORKSPACE_ROOT))})",
            "try:",
            "    import pandas as pd",
            "except Exception:",
            "    pd = None",
            "try:",
            "    import numpy as np",
            "except Exception:",
            "    np = None",
            code
        ])
        result = subprocess.run(
            [sys.executable, "-c", python_code],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(_path(cwd, access="write")) if cwd else str(WORKSPACE_ROOT),
            env=_subprocess_env(env)
        )

        return {
            "ok": result.returncode == 0,
            "status": "success" if result.returncode == 0 else "error",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "cwd": str(_path(cwd, access="write")) if cwd else str(WORKSPACE_ROOT),
            "workspace_root": str(WORKSPACE_ROOT),
            "error": None if result.returncode == 0 else result.stderr,
            "traceback": None
        }
    except Exception as error:
        return _error_dict(error)


def WebSearchTool(query, max_results=5, timeout_seconds=20):
    """Query DuckDuckGo instant answers and return compact structured results."""
    try:
        url = "https://api.duckduckgo.com/"
        response = requests.get(
            url,
            params={
                "q": query,
                "format": "json",
                "no_html": 1,
                "skip_disambig": 1
            },
            timeout=timeout_seconds
        )

        if response.status_code != 200:
            return {
                "ok": False,
                "status": "http_error",
                "query": query,
                "status_code": response.status_code,
                "response_text": response.text,
                "results": [],
                "error": response_text,
                "traceback": None
            }

        data = response.json()
        results = []

        if data.get("AbstractText"):
            results.append({
                "title": data.get("Heading"),
                "url": data.get("AbstractURL"),
                "snippet": data.get("AbstractText")
            })

        for topic in data.get("RelatedTopics", []):
            topics = topic.get("Topics", []) if "Topics" in topic else [topic]

            for item in topics:
                if "Text" not in item:
                    continue

                results.append({
                    "title": item.get("Text", "").split(" - ")[0],
                    "url": item.get("FirstURL"),
                    "snippet": item.get("Text")
                })

                if len(results) >= max_results:
                    return {
                        "ok": True,
                        "status": "success",
                        "query": query,
                        "results": results,
                        "error": None,
                        "traceback": None
                    }

        if not results:
            results.append({
                "title": "DuckDuckGo search results",
                "url": f"https://duckduckgo.com/html/?q={quote_plus(query)}",
                "snippet": "No instant-answer results were returned. Open the URL for full search results."
            })

        return {
            "ok": True,
            "status": "success",
            "query": query,
            "results": results[:max_results],
            "error": None,
            "traceback": None
        }
    except Exception as error:
        return _error_dict(error)


def TodoRead():
    try:
        return {
            "ok": True,
            "status": "success",
            "todos": TODO_ITEMS,
            "error": None,
            "traceback": None
        }
    except Exception as error:
        return _error_dict(error)


def TodoWrite(todos):
    try:
        global TODO_ITEMS

        TODO_ITEMS = todos
        return {
            "ok": True,
            "status": "success",
            "todos": TODO_ITEMS,
            "error": None,
            "traceback": None
        }
    except Exception as error:
        return _error_dict(error)


def DisplayImageTool(path):
    """Return image metadata for interfaces that can render workspace images inline."""
    try:
        image_path = _path(path)
        return {
            "ok": True,
            "status": "success",
            "path": str(image_path),
            "exists": image_path.exists(),
            "message": "Display this image inline if the interface supports image rendering.",
            "error": None,
            "traceback": None
        }
    except Exception as error:
        return _error_dict(error)


def _stream_reader(stream, label, output_queue):
    try:
        for line in iter(stream.readline, ""):
            if line == "":
                break
            output_queue.put((label, line))
    except Exception as error:
        output_queue.put((label, f"reader error: {error}\n{traceback.format_exc()}"))


def _terminate_process(process):
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        return None, None
    except Exception as error:
        return str(error), traceback.format_exc()


def _log_tool_progress(log_file, event, details=None):
    if not log_file:
        return

    try:
        agent_log(log_file, event, details or {})
    except Exception:
        pass


def _agent_log_file(log_file=None):
    workspace_root = _ensure_active_workspace()

    if log_file is None:
        return agent_log_path()

    log_candidate = Path(log_file)
    logs_root = (workspace_root / "logs").resolve()
    resolved_log = log_candidate.resolve() if log_candidate.is_absolute() else (Path.cwd() / log_candidate).resolve()

    if resolved_log != logs_root and logs_root not in resolved_log.parents:
        raise PermissionError(f"Tool log_file must be inside {logs_root}")

    return str(resolved_log)


def RUN_EXOTIC_UNTIL_IDLE(
    inits_json,
    cwd=None,
    idle_timeout_seconds=None,
    tail_chars=6000,
    hard_timeout_seconds=None,
    env=None,
    log_file=None,
    input_text=None,
    session_id=None,
    prompt_quiet_seconds=0.5,
):
    """Run or resume EXOTIC reduction until completion, idle timeout, or input prompt."""
    started_at = time.monotonic()
    requested_idle_timeout_seconds = idle_timeout_seconds
    idle_timeout_seconds = _resolve_exotic_idle_timeout(idle_timeout_seconds)
    process = None
    command = None

    try:
        log_file = _agent_log_file(log_file)

        if not inits_json or not isinstance(inits_json, str):
            raise ValueError("inits_json must be a non-empty string")

        inits_json_path = _path(inits_json)
        resolved_cwd = str(_path(cwd, access="write")) if cwd else str(WORKSPACE_ROOT)

        sessions = getattr(RUN_EXOTIC_UNTIL_IDLE, "_sessions", None)
        if sessions is None:
            sessions = {}
            RUN_EXOTIC_UNTIL_IDLE._sessions = sessions

        def _read_stream_until_prompt_or_line(stream, label, output_queue):
            buffer = []
            try:
                while True:
                    char = stream.read(1)
                    if char == "":
                        if buffer:
                            output_queue.put((label, "".join(buffer)))
                        break

                    buffer.append(char)
                    if char == "\n" or char == ":":
                        output_queue.put((label, "".join(buffer)))
                        buffer = []
            except Exception as error:
                if buffer:
                    output_queue.put((label, "".join(buffer)))
                output_queue.put((label, f"reader error: {error}\n{traceback.format_exc()}"))

        def _looks_like_interactive_prompt(text):
            if not text:
                return False

            last_line = text.splitlines()[-1]
            stripped_line = last_line.strip()
            if not stripped_line:
                return False

            has_prompt_marker = last_line.endswith(": ") or stripped_line.endswith(":")
            if not has_prompt_marker:
                return False

            prompt_body = stripped_line[:-1].strip().lower()
            question_or_choice = (
                "?" in stripped_line
                or re.search(r"[\[(]\s*y\s*/\s*n\s*[\])]", prompt_body) is not None
                or "(y/n)" in prompt_body
            )
            value_request_prefixes = (
                "would you",
                "do you",
                "enter",
                "please select",
                "please enter",
                "select",
                "choose",
                "input",
                "provide",
                "type",
            )
            value_request_terms = (
                "filename",
                "file name",
                "directory",
                "path",
                "value",
                "option",
                "choice",
            )

            return (
                question_or_choice
                or prompt_body.startswith(value_request_prefixes)
                or any(term in prompt_body for term in value_request_terms)
            )

        def _append_output(session, label, text):
            if label == "stdout":
                session["stdout_chars"] += len(text)
            else:
                session["stderr_chars"] += len(text)

            session["combined_parts"].append(f"[{label}] {text}")
            session["plain_parts"].append(text)
            session["last_output_at"] = time.monotonic()
            _log_tool_progress(log_file, "exotic_output", {
                "stream": label,
                "text": text.rstrip("\n")
            })

        def _drain_output(session, wait_timeout=0.1):
            try:
                label, text = session["output_queue"].get(timeout=wait_timeout)
            except queue.Empty:
                return False

            _append_output(session, label, text)

            while True:
                try:
                    label, text = session["output_queue"].get_nowait()
                except queue.Empty:
                    break

                _append_output(session, label, text)

            return True

        def _session_result(session, status, terminate_error=None, terminate_traceback=None):
            combined_output = "".join(session["combined_parts"])
            plain_output = "".join(session["plain_parts"])
            elapsed_seconds = time.monotonic() - started_at
            session_process = session["process"]
            prompt_text = None
            if status == "awaiting_input" and plain_output.rstrip():
                prompt_text = plain_output.rstrip().splitlines()[-1].strip()

            _log_tool_progress(log_file, "exotic_process_finished", {
                "status": status,
                "session_id": session["session_id"],
                "returncode": session_process.returncode,
                "elapsed_seconds": elapsed_seconds,
                "stdout_chars": session["stdout_chars"],
                "stderr_chars": session["stderr_chars"],
                "combined_chars": len(combined_output),
                "awaiting_input": status == "awaiting_input"
            })

            ok = None if status == "awaiting_input" else (
                status == "completed" and session_process.returncode == 0
            )

            return {
                "ok": ok,
                "status": status,
                "awaiting_input": status == "awaiting_input",
                "interactive_prompt": prompt_text,
                "session_id": session["session_id"],
                "returncode": session_process.returncode,
                "tail": combined_output[-tail_chars:],
                "tail_chars": tail_chars,
                "requested_idle_timeout_seconds": requested_idle_timeout_seconds,
                "idle_timeout_seconds": idle_timeout_seconds,
                "max_idle_timeout_seconds": EXOTIC_IDLE_TIMEOUT_CAP_SECONDS,
                "hard_timeout_seconds": hard_timeout_seconds,
                "inits_json": str(session["inits_json_path"]),
                "command": session["command"],
                "cwd": session["cwd"],
                "workspace_root": str(WORKSPACE_ROOT),
                "elapsed_seconds": elapsed_seconds,
                "stdout_chars": session["stdout_chars"],
                "stderr_chars": session["stderr_chars"],
                "combined_chars": len(combined_output),
                "log_file": str(log_file) if log_file else None,
                "resume_instructions": (
                    "Call RUN_EXOTIC_UNTIL_IDLE again with the same session_id and input_text. "
                    "The EXOTIC process is still alive."
                ) if status == "awaiting_input" else None,
                "error": terminate_error,
                "traceback": terminate_traceback
            }

        requested_session_id = session_id or str(inits_json_path)
        session = sessions.get(requested_session_id)

        if session and session["process"].poll() is not None:
            sessions.pop(requested_session_id, None)
            session = None

        if session is None:
            command = ["exotic", f"-red={inits_json_path}", "-ov"]
            _log_tool_progress(log_file, "exotic_process_starting", {
                "command": command,
                "cwd": resolved_cwd,
                "inits_json": str(inits_json_path),
                "requested_idle_timeout_seconds": requested_idle_timeout_seconds,
                "idle_timeout_seconds": idle_timeout_seconds,
                "max_idle_timeout_seconds": EXOTIC_IDLE_TIMEOUT_CAP_SECONDS,
                "hard_timeout_seconds": hard_timeout_seconds
            })

            process = subprocess.Popen(
                command,
                cwd=resolved_cwd,
                env=_subprocess_env(env),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=0
            )

            output_queue = queue.Queue()
            stdout_thread = threading.Thread(
                target=_read_stream_until_prompt_or_line,
                args=(process.stdout, "stdout", output_queue),
                daemon=True
            )
            stderr_thread = threading.Thread(
                target=_read_stream_until_prompt_or_line,
                args=(process.stderr, "stderr", output_queue),
                daemon=True
            )
            stdout_thread.start()
            stderr_thread.start()

            session = {
                "session_id": requested_session_id,
                "process": process,
                "output_queue": output_queue,
                "stdout_thread": stdout_thread,
                "stderr_thread": stderr_thread,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "combined_parts": [],
                "plain_parts": [],
                "command": command,
                "cwd": resolved_cwd,
                "inits_json_path": inits_json_path,
                "last_output_at": time.monotonic(),
            }
            sessions[requested_session_id] = session
        else:
            process = session["process"]
            command = session["command"]
            _log_tool_progress(log_file, "exotic_process_resuming", {
                "session_id": session["session_id"],
                "cwd": session["cwd"],
                "inits_json": str(session["inits_json_path"]),
                "has_input_text": input_text is not None
            })

        if input_text is not None:
            if session["process"].stdin is None or session["process"].stdin.closed:
                raise RuntimeError("EXOTIC stdin is not available for this session")

            text_to_send = str(input_text)
            if not text_to_send.endswith("\n"):
                text_to_send += "\n"

            session["process"].stdin.write(text_to_send)
            session["process"].stdin.flush()
            session["last_output_at"] = time.monotonic()
            _log_tool_progress(log_file, "exotic_input_sent", {
                "session_id": session["session_id"],
                "input_chars": len(text_to_send)
            })

        while True:
            now = time.monotonic()

            if _drain_output(session, wait_timeout=0.1):
                continue

            if session["process"].poll() is not None:
                while _drain_output(session, wait_timeout=0):
                    pass

                sessions.pop(session["session_id"], None)
                return _session_result(session, "completed")

            combined_plain_output = "".join(session["plain_parts"])
            seconds_since_last_output = time.monotonic() - session["last_output_at"]
            if (
                combined_plain_output
                and seconds_since_last_output >= prompt_quiet_seconds
                and _looks_like_interactive_prompt(combined_plain_output)
            ):
                _log_tool_progress(log_file, "exotic_waiting_for_interactive_input", {
                    "session_id": session["session_id"],
                    "seconds_since_last_output": seconds_since_last_output,
                    "prompt": combined_plain_output.rstrip().splitlines()[-1].strip()
                })
                return _session_result(session, "awaiting_input")

            if hard_timeout_seconds is not None and now - started_at >= hard_timeout_seconds:
                _log_tool_progress(log_file, "exotic_hard_timeout", {
                    "elapsed_seconds": now - started_at,
                    "hard_timeout_seconds": hard_timeout_seconds
                })
                terminate_error, terminate_traceback = _terminate_process(session["process"])
                sessions.pop(session["session_id"], None)
                return _session_result(session, "hard_timeout", terminate_error, terminate_traceback)

            if seconds_since_last_output >= idle_timeout_seconds:
                _log_tool_progress(log_file, "exotic_idle_timeout", {
                    "elapsed_seconds": time.monotonic() - started_at,
                    "idle_timeout_seconds": idle_timeout_seconds,
                    "seconds_since_last_output": seconds_since_last_output
                })
                terminate_error, terminate_traceback = _terminate_process(session["process"])
                sessions.pop(session["session_id"], None)
                return _session_result(session, "idle_timeout", terminate_error, terminate_traceback)
    except Exception as error:
        if process and process.poll() is None:
            _terminate_process(process)

        elapsed_seconds = time.monotonic() - started_at
        combined_output = ""
        stdout_chars = 0
        stderr_chars = 0

        if "session" in locals() and session:
            combined_output = "".join(session.get("combined_parts", []))
            stdout_chars = session.get("stdout_chars", 0)
            stderr_chars = session.get("stderr_chars", 0)
            if "sessions" in locals():
                sessions.pop(session.get("session_id"), None)

        _log_tool_progress(log_file, "exotic_process_error", {
            "error": str(error),
            "elapsed_seconds": elapsed_seconds
        })

        return {
            "ok": False,
            "status": "error",
            "awaiting_input": False,
            "interactive_prompt": None,
            "session_id": session_id,
            "returncode": process.returncode if process else None,
            "tail": combined_output[-tail_chars:],
            "tail_chars": tail_chars,
            "requested_idle_timeout_seconds": requested_idle_timeout_seconds,
            "idle_timeout_seconds": idle_timeout_seconds,
            "max_idle_timeout_seconds": EXOTIC_IDLE_TIMEOUT_CAP_SECONDS,
            "hard_timeout_seconds": hard_timeout_seconds,
            "inits_json": inits_json,
            "command": command,
            "cwd": str(_path(cwd, access="write")) if cwd else None,
            "elapsed_seconds": elapsed_seconds,
            "stdout_chars": stdout_chars,
            "stderr_chars": stderr_chars,
            "combined_chars": len(combined_output),
            "log_file": str(log_file) if log_file else None,
            "resume_instructions": None,
            "error": str(error),
            "traceback": traceback.format_exc()
        }


def QUERY_AAVSO_STAR_CHART(
    star=None,
    ra=None,
    dec=None,
    fov=30,
    maglimit=14.5,
    resolution=150,
    north="up",
    east="left",
    dss=True,
    chart_id=None,
    timeout_seconds=30.0,
    download_image=True,
    image_output_dir=None,
    max_photometry_rows=200,
):
    """Fetch an AAVSO chart JSON and optionally download the rendered chart image."""
    base_url = "https://app.aavso.org/vsp/api/chart/"
    params = {"format": "json"}

    try:
        if chart_id:
            url = urljoin(base_url, f"{chart_id}/")
        else:
            if not star and not (ra and dec):
                raise ValueError("Provide chart_id, star, or both ra and dec")

            url = base_url
            params.update({
                "fov": fov,
                "maglimit": maglimit,
                "resolution": resolution,
                "north": north,
                "east": east,
                "dss": dss
            })

            if star:
                params["star"] = star
            else:
                params["ra"] = ra
                params["dec"] = dec

        response = requests.get(url, params=params, timeout=timeout_seconds)

        if response.status_code != 200:
            response_text, response_text_truncated = _truncate_text(response.text)
            return {
                "ok": False,
                "status": "http_error",
                "url": response.url,
                "params": params,
                "status_code": response.status_code,
                "chart_json": None,
                "image_uri": None,
                "image_path": None,
                "image_content_type": None,
                "image_error": None,
                "error": response_text,
                "traceback": None,
                "response_text": response_text,
                "response_text_truncated": response_text_truncated
            }

        try:
            chart_json = response.json()
        except Exception as error:
            response_text, response_text_truncated = _truncate_text(response.text)
            return {
                "ok": False,
                "status": "json_error",
                "url": response.url,
                "params": params,
                "status_code": response.status_code,
                "chart_json": None,
                "image_uri": None,
                "image_path": None,
                "image_content_type": None,
                "image_error": None,
                "error": str(error),
                "traceback": traceback.format_exc(),
                "response_text": response_text,
                "response_text_truncated": response_text_truncated
            }

        compact_chart_json, photometry_count, photometry_truncated = _compact_aavso_chart_json(
            chart_json,
            max_photometry_rows=max_photometry_rows
        )
        image_uri = chart_json.get("image_uri") if isinstance(chart_json, dict) else None
        image_path = None
        image_content_type = None
        image_error = None
        image_traceback = None
        chart_json_path = None
        status = "success"
        output_dir = None

        if image_output_dir or download_image:
            output_dir = _path(image_output_dir or "aavso_charts", access="write")
            output_dir.mkdir(parents=True, exist_ok=True)

            file_stem = _safe_filename(str(chart_id or star or "aavso_chart"))
            chart_json_path = output_dir / f"{file_stem}.json"
            chart_json_path.write_text(json.dumps(chart_json, indent=2), encoding="utf-8")
            chart_json_path = str(chart_json_path)

        if download_image and image_uri:
            try:
                image_url = urljoin(response.url, image_uri)
                image_response = requests.get(image_url, timeout=timeout_seconds)
                image_response.raise_for_status()
                image_content_type = image_response.headers.get("content-type")
                suffix = ".svg" if "svg" in (image_content_type or "").lower() else ".png"
                saved_path = output_dir / f"{file_stem}{suffix}"
                saved_path.write_bytes(image_response.content)
                image_path = str(saved_path)
            except Exception as error:
                status = "image_download_error"
                image_error = str(error)
                image_traceback = traceback.format_exc()

        return {
            "ok": True,
            "status": status,
            "url": response.url,
            "params": params,
            "status_code": response.status_code,
            "chart_json": compact_chart_json,
            "chart_json_path": chart_json_path,
            "photometry_count": photometry_count,
            "photometry_truncated": photometry_truncated,
            "max_photometry_rows": max_photometry_rows,
            "image_uri": image_uri,
            "image_path": image_path,
            "image_content_type": image_content_type,
            "image_error": image_error,
            "image_traceback": image_traceback,
            "error": None,
            "traceback": None,
            "response_text": None,
            "response_text_truncated": False
        }
    except Exception as error:
        return {
            "ok": False,
            "status": "error",
            "url": base_url if not chart_id else urljoin(base_url, f"{chart_id}/"),
            "params": params,
            "status_code": None,
            "chart_json": None,
            "chart_json_path": None,
            "photometry_count": None,
            "photometry_truncated": None,
            "max_photometry_rows": max_photometry_rows,
            "image_uri": None,
            "image_path": None,
            "image_content_type": None,
            "image_error": None,
            "error": str(error),
            "traceback": traceback.format_exc(),
            "response_text": None,
            "response_text_truncated": False
        }


def PLOT_XY(
    x,
    y,
    output_path,
    title="Plot",
    xlabel="x",
    ylabel="y",
    kind="scatter",
    invert_yaxis=False,
    connect_points=False,
):
    """Render a simple scatter or line plot to a workspace image file."""
    try:
        if len(x) != len(y):
            raise ValueError("x and y must have the same length")

        if not x:
            raise ValueError("x and y must contain at least one point")

        if kind not in {"scatter", "line"}:
            raise ValueError("kind must be 'scatter' or 'line'")

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        file_path = _path(output_path, access="write")
        file_path.parent.mkdir(parents=True, exist_ok=True)

        fig, ax = plt.subplots()

        if kind == "scatter":
            ax.scatter(x, y)
            if connect_points:
                ax.plot(x, y)
        else:
            ax.plot(x, y)

        if invert_yaxis:
            ax.invert_yaxis()

        ax.set_title(title)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        fig.tight_layout()
        fig.savefig(file_path)
        plt.close(fig)

        return {
            "ok": True,
            "status": "success",
            "output_path": str(file_path),
            "num_points": len(x),
            "title": title,
            "xlabel": xlabel,
            "ylabel": ylabel,
            "kind": kind,
            "invert_yaxis": invert_yaxis,
            "error": None,
            "traceback": None
        }
    except Exception as error:
        return {
            "ok": False,
            "status": "error",
            "output_path": None,
            "num_points": len(x) if isinstance(x, list) else 0,
            "title": title,
            "xlabel": xlabel,
            "ylabel": ylabel,
            "kind": kind,
            "invert_yaxis": invert_yaxis,
            "error": str(error),
            "traceback": traceback.format_exc()
        }


def VIEW_FITS_GCOLAB_FORMAT(fits_file_path, output_path=None, title="FITS Image", hdu_index=0):
    """Create a Google-Colab-style PNG visualization from a FITS image HDU."""
    try:
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from astropy.io import fits
        from astropy.visualization import ImageNormalize, ZScaleInterval

        fits_path = _path(fits_file_path)

        if output_path is None:
            image_path = _path(Path("output") / f"{fits_path.stem}.png", access="write")
        else:
            image_path = _path(output_path, access="write")

        image_path.parent.mkdir(parents=True, exist_ok=True)

        with fits.open(fits_path) as hdul:
            hdu_index_used = hdu_index
            data = hdul[hdu_index].data

            if data is None:
                for index, hdu in enumerate(hdul):
                    if hdu.data is not None:
                        data = hdu.data
                        hdu_index_used = index
                        break

            if data is None:
                raise ValueError("No image data found in FITS file")

            image_data = np.asarray(data, dtype=float)

        image_data = np.squeeze(image_data)

        if image_data.ndim != 2:
            raise ValueError(f"Expected 2D FITS image data, got shape {image_data.shape}")

        finite_mask = np.isfinite(image_data)
        finite_values = image_data[finite_mask]

        if finite_values.size == 0:
            raise ValueError("FITS image contains no finite pixel values")

        fill_value = float(np.nanmedian(finite_values))
        safe_data = np.where(finite_mask, image_data, fill_value)

        interval = ZScaleInterval()
        vmin, vmax = interval.get_limits(safe_data)
        norm = ImageNormalize(vmin=vmin, vmax=vmax)

        fig, ax = plt.subplots()
        image = ax.imshow(safe_data, cmap="viridis", origin="lower", norm=norm)
        ax.set_title(title)
        ax.set_xlabel("X Pixel")
        ax.set_ylabel("Y Pixel")
        colorbar = fig.colorbar(image, ax=ax)
        colorbar.set_label("Pixel Value")
        fig.tight_layout()
        fig.savefig(image_path, dpi=150)
        plt.close(fig)

        return {
            "ok": True,
            "status": "success",
            "fits_file_path": str(fits_path),
            "output_path": str(image_path),
            "title": title,
            "hdu_index": hdu_index,
            "hdu_index_used": hdu_index_used,
            "shape": list(image_data.shape),
            "nan_pixels": int(np.isnan(image_data).sum()),
            "vmin": float(vmin),
            "vmax": float(vmax),
            "colormap": "viridis",
            "origin": "lower",
            "normalization": "ZScale",
            "error": None,
            "traceback": None
        }
    except Exception as error:
        return {
            "ok": False,
            "status": "error",
            "fits_file_path": fits_file_path,
            "output_path": output_path,
            "title": title,
            "hdu_index": hdu_index,
            "error": str(error),
            "traceback": traceback.format_exc()
        }


def _find_fits_files(directory_path):
    return sorted(
        path
        for path in directory_path.iterdir()
        if path.is_file() and path.name.lower().endswith((".fits", ".fits.gz", ".fit"))
    )


def _derive_host_star_name(planet_name):
    if not planet_name:
        return None

    parts = str(planet_name).strip().split()
    if len(parts) > 1 and re.fullmatch(r"[a-zA-Z]", parts[-1]):
        return " ".join(parts[:-1])

    return str(planet_name).strip()


def _read_fits_metadata(first_image):
    """Extract observation metadata needed to build an EXOTIC `inits.json` file."""
    metadata = {
        "observation_date": None,
        "latitude": None,
        "longitude": None,
        "elevation_m": None,
        "filter_name": None,
        "exposure_time_s": None,
        "pixel_binning": None,
        "camera_type": "CCD",
        "telescope": None,
        "observatory": None,
        "origin": None,
        "instrument": None,
        "pixel_scale": None,
    }

    try:
        from astropy.io import fits
        from astropy.wcs import WCS
        import math

        with fits.open(first_image) as hdul:
            header = hdul[0].header

        date_obs = header.get("DATE-OBS") or header.get("DATEOBS") or header.get("DATE")
        if date_obs:
            metadata["observation_date"] = str(date_obs).split("T")[0].strip()

        for key in ("LATITUDE", "SITELAT", "OBS-LAT", "OBSGEO-B"):
            if header.get(key) is not None:
                metadata["latitude"] = str(header.get(key))
                break

        for key in ("LONGITUDE", "SITELONG", "OBS-LONG", "OBSGEO-L"):
            if header.get(key) is not None:
                metadata["longitude"] = str(header.get(key))
                break

        for key in ("ALTITUDE", "ELEVATIO", "ELEVATION", "HEIGHT", "OBSGEO-H"):
            if header.get(key) is not None:
                try:
                    metadata["elevation_m"] = float(header.get(key))
                except Exception:
                    metadata["elevation_m"] = header.get(key)
                break

        metadata["filter_name"] = header.get("FILTER") or header.get("FILTNAM") or header.get("BAND")
        metadata["exposure_time_s"] = header.get("EXPTIME") or header.get("EXPOSURE")
        metadata["telescope"] = _clean_fits_header_value(header.get("TELESCOP"))
        metadata["observatory"] = _clean_fits_header_value(header.get("OBSERVAT") or header.get("OBSERVATORY"))
        metadata["origin"] = _clean_fits_header_value(header.get("ORIGIN"))
        metadata["instrument"] = _clean_fits_header_value(header.get("INSTRUME"))

        xbin = header.get("XBINNING") or header.get("CCDXBIN") or header.get("BINX")
        ybin = header.get("YBINNING") or header.get("CCDYBIN") or header.get("BINY")
        if xbin and ybin:
            metadata["pixel_binning"] = f"{xbin}x{ybin}"
        elif header.get("BINNING"):
            metadata["pixel_binning"] = str(header.get("BINNING"))

        instrume = str(header.get("INSTRUME", "")).lower()
        if "dslr" in instrume or "bayer" in instrume:
            metadata["camera_type"] = "DSLR"

        try:
            wcs = WCS(header)
            pixel_scales = wcs.proj_plane_pixel_scales()
            if len(pixel_scales) >= 2:
                scale_deg = sum(abs(float(scale)) for scale in pixel_scales[:2]) / 2.0
                if math.isfinite(scale_deg) and scale_deg > 0:
                    metadata["pixel_scale"] = scale_deg * 3600.0
        except Exception:
            pass
    except Exception:
        pass

    return metadata


def _clean_fits_header_value(value):
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def _normalize_camera_type(camera_type):
    if camera_type is None:
        return None

    normalized = str(camera_type).strip().upper()
    if normalized not in {"CCD", "DSLR"}:
        raise ValueError("camera_type must be either 'CCD' or 'DSLR'")

    return normalized


def _observing_notes_telescope(metadata, notebook_telescope):
    header_telescope = metadata.get("telescope")
    observatory = metadata.get("observatory")
    origin = metadata.get("origin")

    if not header_telescope:
        return notebook_telescope or "Unknown telescope"

    details = []
    if observatory:
        details.append(observatory)
    if origin and origin != observatory:
        details.append(origin)

    if details:
        return f"{header_telescope} ({'; '.join(details)})"

    return header_telescope


def _load_planetary_parameters(planet_name, supplied_planetary_parameters=None):
    if supplied_planetary_parameters:
        return supplied_planetary_parameters, None

    if not planet_name:
        return None, "planet_name is required when no inits.json is present"

    try:
        from exotic.exotic import NASAExoplanetArchive

        target = NASAExoplanetArchive(planet=planet_name)
        resolved_name = target.planet_info()[0]

        if not target.resolve_name():
            return None, f"NASA Exoplanet Archive could not resolve {planet_name!r}"

        planet_info = target.planet_info(fancy=True)
        planetary_parameters = json.loads(planet_info)

        if isinstance(planetary_parameters, dict):
            planetary_parameters.setdefault("Planet Name", resolved_name)
            planetary_parameters.setdefault("Host Star Name", _derive_host_star_name(resolved_name))

        return planetary_parameters, None
    except Exception as error:
        return None, f"Could not load planetary parameters from EXOTIC/NASA archive: {error}"


def _coerce_xy_pair(value, field_name):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{field_name} must be [x, y]")

    return [float(value[0]), float(value[1])]


def _coerce_xy_pairs(value, field_name):
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field_name} must be a non-empty array of [x, y] pairs")

    return [_coerce_xy_pair(pair, field_name) for pair in value]


def _write_exotic_standard_inits(
    inits_path,
    fits_directory,
    output_directory,
    planetary_parameters,
    metadata,
    target_star_xy,
    comparison_stars_xy,
    primary_observer_code=None,
    secondary_observer_code=None,
    notes=None,
    add_comparison_stars_from_aavso="n",
):
    """Write the standard EXOTIC notebook `inits.json` from metadata and star picks."""
    user_info = {
        "Directory with FITS files": str(fits_directory),
        "Directory to Save Plots": str(output_directory),
        "Directory of Flats": None,
        "Directory of Darks": None,
        "Directory of Biases": None,
        "AAVSO Observer Code (N/A if none)": primary_observer_code or "N/A",
        "Secondary Observer Codes (N/A if none)": secondary_observer_code or "N/A",
        "Observation date": metadata.get("observation_date"),
        "Obs. Latitude": metadata.get("latitude"),
        "Obs. Longitude": metadata.get("longitude"),
        "Obs. Elevation (meters)": metadata.get("elevation_m"),
        "Camera Type (CCD or DSLR)": metadata.get("camera_type") or "CCD",
        "Pixel Binning": metadata.get("pixel_binning") or "1x1",
        "Filter Name (aavso.org/filters)": metadata.get("filter_name") or "Open",
        "Observing Notes": notes or "Generated by the EXOTIC Standard notebook pipeline tool.",
        "Plate Solution? (y/n)": "y" if metadata.get("pixel_scale") is not None else "n",
        "Add Comparison Stars from AAVSO? (y/n)": add_comparison_stars_from_aavso,
        "Target Star X & Y Pixel": target_star_xy,
        "Comparison Star(s) X & Y Pixel": comparison_stars_xy,
        "Demosaic Format": None,
        "Demosaic Output": None,
    }

    optional_info = {
        "Pixel Scale (Ex: 5.21 arcsecs/pixel)": metadata.get("pixel_scale"),
        "Filter Minimum Wavelength (nm)": None,
        "Filter Maximum Wavelength (nm)": None,
    }

    inits_data = {
        "planetary_parameters": planetary_parameters,
        "user_info": user_info,
        "optional_info": optional_info,
    }

    inits_path.parent.mkdir(parents=True, exist_ok=True)
    inits_path.write_text(json.dumps(inits_data, indent=2), encoding="utf-8")
    return inits_data


def EXOTIC_STANDARD_NOTEBOOK_PIPELINE(
    fits_directory,
    planet_name=None,
    host_star_name=None,
    telescope="MicroObservatory",
    camera_type=None,
    primary_observer_code=None,
    secondary_observer_code=None,
    output_directory=None,
    vlm_answers=None,
    planetary_parameters=None,
    run_exotic=True,
    idle_timeout_seconds=None,
    hard_timeout_seconds=None,
    tail_chars=6000,
    exotic_input=None,
    exotic_session_id=None,
):
    """
    Local, resumable implementation of the attached EXOTIC Standard notebook.

    The Colab notebook has interactive VLM/human prompts for target and comparison
    coordinates. This tool runs until that point, returns the notebook prompts and
    image artifacts, and resumes when vlm_answers are supplied.
    """
    log_file = None
    try:
        requested_idle_timeout_seconds = idle_timeout_seconds
        idle_timeout_seconds = _resolve_exotic_idle_timeout(idle_timeout_seconds)
        dataset_dir = _path(fits_directory)

        if not dataset_dir.is_dir():
            raise FileNotFoundError(f"FITS directory does not exist: {dataset_dir}")

        output_dir = _path(output_directory, access="write") if output_directory else WORKSPACE_ROOT / "output" / "EXOTIC_output"
        output_dir.mkdir(parents=True, exist_ok=True)
        log_file = _agent_log_file()
        _log_tool_progress(log_file, "pipeline_start", {
            "fits_directory": str(dataset_dir),
            "planet_name": planet_name,
            "host_star_name": host_star_name,
            "telescope": telescope,
            "camera_type": camera_type,
            "output_directory": str(output_dir),
            "has_vlm_answers": bool(vlm_answers),
            "has_planetary_parameters": bool(planetary_parameters),
            "run_exotic": run_exotic,
            "has_exotic_input": exotic_input is not None,
            "exotic_session_id": exotic_session_id,
            "requested_idle_timeout_seconds": requested_idle_timeout_seconds,
            "idle_timeout_seconds": idle_timeout_seconds,
            "max_idle_timeout_seconds": EXOTIC_IDLE_TIMEOUT_CAP_SECONDS,
            "hard_timeout_seconds": hard_timeout_seconds
        })

        fits_files = _find_fits_files(dataset_dir)
        json_files = sorted(path for path in dataset_dir.iterdir() if path.is_file() and path.suffix.lower() == ".json")
        _log_tool_progress(log_file, "dataset_scanned", {
            "fits_count": len(fits_files),
            "json_count": len(json_files),
            "first_fits_image": str(fits_files[0]) if fits_files else None,
            "json_files": [str(path) for path in json_files]
        })

        if not fits_files:
            _log_tool_progress(log_file, "pipeline_stopped_no_fits_files", {
                "fits_directory": str(dataset_dir)
            })
            return {
                "ok": False,
                "status": "no_fits_files",
                "notebook_step": "Step 2: Load Telescope Images",
                "fits_directory": str(dataset_dir),
                "output_directory": str(output_dir),
                "log_file": str(log_file),
                "fits_count": 0,
                "error": "No .fits, .fits.gz, or .fit files were found.",
                "traceback": None,
            }

        first_image = fits_files[0]
        _log_tool_progress(log_file, "fits_metadata_reading", {
            "first_fits_image": str(first_image)
        })
        metadata = _read_fits_metadata(first_image)
        supplied_camera_type = _normalize_camera_type(camera_type)
        if supplied_camera_type:
            metadata["camera_type"] = supplied_camera_type
        _log_tool_progress(log_file, "fits_metadata_read", {
            "metadata": metadata
        })
        inits_file_path = None
        inits_data = None

        if len(json_files) == 1:
            inits_file_path = json_files[0]
            _log_tool_progress(log_file, "existing_inits_detected", {
                "inits_json": str(inits_file_path)
            })
            try:
                inits_data = json.loads(inits_file_path.read_text(encoding="utf-8"))
                inits_output_dir = inits_data.get("user_info", {}).get("Directory to Save Plots")
                if inits_output_dir:
                    try:
                        output_dir = _path(inits_output_dir, access="write")
                        _log_tool_progress(log_file, "existing_inits_output_directory_loaded", {
                            "inits_json": str(inits_file_path),
                            "output_directory": str(output_dir)
                        })
                    except PermissionError as error:
                        _log_tool_progress(log_file, "existing_inits_output_directory_ignored", {
                            "inits_json": str(inits_file_path),
                            "requested_output_directory": str(inits_output_dir),
                            "fallback_output_directory": str(output_dir),
                            "error": str(error)
                        })

                output_dir.mkdir(parents=True, exist_ok=True)

                if isinstance(inits_data, dict):
                    user_info = inits_data.setdefault("user_info", {})
                    user_info["Directory with FITS files"] = str(dataset_dir)
                    user_info["Directory to Save Plots"] = str(output_dir)

                source_inits_path = inits_file_path
                inits_file_path = output_dir / "inits.json"
                inits_file_path.write_text(json.dumps(inits_data, indent=2), encoding="utf-8")
                _log_tool_progress(log_file, "existing_inits_copied_to_workspace", {
                    "source_inits_json": str(source_inits_path),
                    "workspace_inits_json": str(inits_file_path),
                    "fits_directory": str(dataset_dir),
                    "output_directory": str(output_dir)
                })
            except Exception as error:
                _log_tool_progress(log_file, "existing_inits_invalid", {
                    "inits_json": str(inits_file_path),
                    "error": str(error)
                })
                return {
                    "ok": False,
                    "status": "invalid_existing_inits",
                    "notebook_step": "Step 2: Load Telescope Images",
                    "inits_json": str(inits_file_path),
                    "output_directory": str(output_dir),
                    "log_file": str(log_file),
                    "error": str(error),
                    "traceback": traceback.format_exc(),
                }

        if inits_file_path is None:
            host_star = host_star_name or _derive_host_star_name(planet_name)
            rendered_dir = output_dir / "notebook_prompts"
            rendered_dir.mkdir(parents=True, exist_ok=True)
            _log_tool_progress(log_file, "observation_image_rendering", {
                "first_fits_image": str(first_image),
                "rendered_dir": str(rendered_dir)
            })
            observation_image = VIEW_FITS_GCOLAB_FORMAT(
                fits_file_path=str(first_image),
                output_path=str(rendered_dir / f"{first_image.stem}_vlm.png"),
                title="First Telescope Image",
            )
            _log_tool_progress(log_file, "observation_image_rendered", {
                "status": observation_image.get("status"),
                "ok": observation_image.get("ok"),
                "output_path": observation_image.get("output_path"),
                "shape": observation_image.get("shape"),
                "error": observation_image.get("error")
            })

            fov = 56.44 if telescope == "MicroObservatory" else 38.42
            _log_tool_progress(log_file, "aavso_chart_query_starting", {
                "host_star": host_star,
                "fov": fov,
                "maglimit": 15,
                "north": "down",
                "east": "left"
            })
            chart_result = QUERY_AAVSO_STAR_CHART(
                star=host_star,
                fov=fov,
                maglimit=15,
                resolution=150,
                north="down",
                east="left",
                dss=True,
                download_image=True,
                image_output_dir=str(rendered_dir),
            ) if host_star else {
                "ok": False,
                "status": "missing_host_star",
                "error": "host_star_name or planet_name is required to generate an AAVSO chart",
            }
            _log_tool_progress(log_file, "aavso_chart_query_finished", {
                "status": chart_result.get("status"),
                "ok": chart_result.get("ok"),
                "url": chart_result.get("url"),
                "image_path": chart_result.get("image_path"),
                "chart_json_path": chart_result.get("chart_json_path"),
                "error": chart_result.get("error")
            })

            if not vlm_answers:
                _log_tool_progress(log_file, "pipeline_waiting_for_vlm_answers", {
                    "observation_image_path": observation_image.get("output_path"),
                    "aavso_chart_image_path": chart_result.get("image_path"),
                    "aavso_chart_status": chart_result.get("status")
                })
                return {
                    "ok": False,
                    "status": "needs_vlm_answers",
                    "notebook_step": "Step 3: Identify target star and comparison stars",
                    "resume_tool": "EXOTIC_STANDARD_NOTEBOOK_PIPELINE",
                    "resume_instructions": (
                        "Use the observation image and AAVSO chart to answer the notebook prompts, "
                        "then call this same tool again with top-level camera_type, "
                        "vlm_answers.target_star_xy, and vlm_answers.comparison_stars_xy."
                    ),
                    "fits_directory": str(dataset_dir),
                    "output_directory": str(output_dir),
                    "log_file": str(log_file),
                    "fits_count": len(fits_files),
                    "first_fits_image": str(first_image),
                    "camera_type": metadata.get("camera_type"),
                    "observation_image_path": observation_image.get("output_path"),
                    "aavso_chart_image_path": chart_result.get("image_path"),
                    "aavso_chart_json_path": chart_result.get("chart_json_path"),
                    "aavso_chart_status": chart_result.get("status"),
                    "aavso_chart_error": chart_result.get("error"),
                    "notebook_prompts": [
                        {
                            "name": "camera_type",
                            "prompt": (
                                "Data Entry 1 of 3: Enter Camera Type (CCD or DSLR). "
                                "Prefer FITS header or instrument evidence when available. "
                                "Return one of: CCD, DSLR. Pass this value as top-level camera_type when resuming."
                            ),
                            "answer_schema": {"type": "string", "enum": ["CCD", "DSLR"]},
                        },
                        {
                            "name": "target_star_xy",
                            "prompt": (
                                "Data Entry 2 of 3: Enter coordinates for the target star. "
                                "In the right image, find the crosshairs in the center; that represents your target star. "
                                "On the left image, find this target star and note the X and Y coordinates. "
                                "Return coordinates in the format [x, y]."
                            ),
                            "answer_schema": {"type": "array", "items": "number", "length": 2},
                        },
                        {
                            "name": "comparison_stars_xy",
                            "prompt": (
                                "Data Entry 3 of 3: Enter coordinates for at least two comparison stars. "
                                "In the AAVSO chart, find the stars with numbers that represent suggested comparison stars. "
                                "On the telescope image, find each comparison star and note the coordinates. "
                                "Return coordinates in the format [[x1, y1], [x2, y2]]. If no labelled comparison stars "
                                "are visible, select about 3-5 stars close to the target and as bright as or brighter than the target."
                            ),
                            "answer_schema": {"type": "array", "items": {"type": "array", "items": "number", "length": 2}},
                        },
                    ],
                    "error": "Visual target/comparison-star coordinates are required before inits.json can be generated.",
                    "traceback": None,
                }

            _log_tool_progress(log_file, "vlm_answers_validating", {
                "vlm_answer_keys": sorted(vlm_answers.keys()) if isinstance(vlm_answers, dict) else None
            })
            target_star_xy = _coerce_xy_pair(vlm_answers.get("target_star_xy"), "vlm_answers.target_star_xy")
            comparison_stars_xy = _coerce_xy_pairs(
                vlm_answers.get("comparison_stars_xy"),
                "vlm_answers.comparison_stars_xy",
            )
            _log_tool_progress(log_file, "vlm_answers_validated", {
                "target_star_xy": target_star_xy,
                "comparison_star_count": len(comparison_stars_xy),
                "comparison_stars_xy": comparison_stars_xy
            })
            _log_tool_progress(log_file, "planetary_parameters_loading", {
                "planet_name": planet_name,
                "supplied_planetary_parameters": bool(planetary_parameters)
            })
            planetary, planetary_error = _load_planetary_parameters(planet_name, planetary_parameters)

            if planetary is None:
                _log_tool_progress(log_file, "planetary_parameters_required", {
                    "planet_name": planet_name,
                    "host_star_name": host_star,
                    "error": planetary_error
                })
                return {
                    "ok": False,
                    "status": "needs_planetary_parameters",
                    "notebook_step": "Step 2: Load Telescope Images",
                    "planet_name": planet_name,
                    "host_star_name": host_star,
                    "output_directory": str(output_dir),
                    "log_file": str(log_file),
                    "error": planetary_error,
                    "traceback": None,
                }
            _log_tool_progress(log_file, "planetary_parameters_loaded", {
                "planet_name": planetary.get("Planet Name") if isinstance(planetary, dict) else planet_name,
                "host_star_name": planetary.get("Host Star Name") if isinstance(planetary, dict) else host_star,
                "parameter_count": len(planetary) if isinstance(planetary, dict) else None
            })

            inits_file_path = output_dir / "inits.json"
            observed_telescope = _observing_notes_telescope(metadata, telescope)
            notes = (
                f"Generated from EXOTIC Standard notebook flow; telescope={observed_telescope}; "
                f"camera type={metadata.get('camera_type') or 'CCD'}; "
                f"first image={first_image.name}; VLM supplied target and comparison-star coordinates."
            )
            _log_tool_progress(log_file, "inits_json_writing", {
                "inits_json": str(inits_file_path)
            })
            inits_data = _write_exotic_standard_inits(
                inits_path=inits_file_path,
                fits_directory=dataset_dir,
                output_directory=output_dir,
                planetary_parameters=planetary,
                metadata=metadata,
                target_star_xy=target_star_xy,
                comparison_stars_xy=comparison_stars_xy,
                primary_observer_code=primary_observer_code,
                secondary_observer_code=secondary_observer_code,
                notes=notes,
                add_comparison_stars_from_aavso="n",
            )
            _log_tool_progress(log_file, "inits_json_written", {
                "inits_json": str(inits_file_path),
                "inits_json_exists": inits_file_path.exists(),
                "user_info_keys": sorted(inits_data.get("user_info", {}).keys()) if isinstance(inits_data, dict) else None
            })

        exotic_result = None
        expected_outputs = {}

        if run_exotic:
            _log_tool_progress(log_file, "exotic_run_starting", {
                "inits_json": str(inits_file_path),
                "requested_idle_timeout_seconds": requested_idle_timeout_seconds,
                "idle_timeout_seconds": idle_timeout_seconds,
                "max_idle_timeout_seconds": EXOTIC_IDLE_TIMEOUT_CAP_SECONDS,
                "hard_timeout_seconds": hard_timeout_seconds,
                "tail_chars": tail_chars,
                "has_exotic_input": exotic_input is not None,
                "exotic_session_id": exotic_session_id
            })
            exotic_result = RUN_EXOTIC_UNTIL_IDLE(
                inits_json=str(inits_file_path),
                cwd=str(WORKSPACE_ROOT),
                idle_timeout_seconds=idle_timeout_seconds,
                hard_timeout_seconds=hard_timeout_seconds,
                tail_chars=tail_chars,
                log_file=str(log_file),
                input_text=exotic_input,
                session_id=exotic_session_id,
            )
            _log_tool_progress(log_file, "exotic_run_finished", {
                "ok": exotic_result.get("ok"),
                "status": exotic_result.get("status"),
                "returncode": exotic_result.get("returncode"),
                "elapsed_seconds": exotic_result.get("elapsed_seconds"),
                "stdout_chars": exotic_result.get("stdout_chars"),
                "stderr_chars": exotic_result.get("stderr_chars"),
                "combined_chars": exotic_result.get("combined_chars"),
                "awaiting_input": exotic_result.get("awaiting_input"),
                "session_id": exotic_result.get("session_id"),
                "error": exotic_result.get("error")
            })

            date_obs = None
            planet = None

            if isinstance(inits_data, dict):
                date_obs = inits_data.get("user_info", {}).get("Observation date")
                planet = inits_data.get("planetary_parameters", {}).get("Planet Name")

            if date_obs and planet:
                expected_outputs = {
                    "aavso_submission": str(output_dir / f"AAVSO_{planet}_{date_obs}.txt"),
                    "lightcurve": str(output_dir / f"FinalLightCurve_{planet}_{date_obs}.png"),
                    "fov": str(output_dir / "temp" / f"FOV_{planet}_{date_obs}_LinearStretch.png"),
                    "triangle": str(output_dir / "temp" / f"Triangle_{planet}_{date_obs}.png"),
                }
                _log_tool_progress(log_file, "expected_outputs_recorded", expected_outputs)

        if not run_exotic:
            pipeline_status = "completed"
        elif exotic_result and exotic_result.get("status") == "awaiting_input":
            pipeline_status = "awaiting_exotic_input"
        elif exotic_result and exotic_result.get("ok"):
            pipeline_status = "completed"
        else:
            pipeline_status = "exotic_failed"

        if run_exotic and exotic_result and exotic_result.get("status") == "awaiting_input":
            pipeline_ok = None
        elif run_exotic and exotic_result:
            pipeline_ok = bool(exotic_result.get("ok"))
        else:
            pipeline_ok = True

        result = {
            "ok": pipeline_ok,
            "status": pipeline_status,
            "notebook_steps_completed": [
                "Step 1: Load EXOTIC libraries",
                "Step 2: Load Telescope Images",
                "Step 3: Identify target star and comparison stars",
                "Step 4: Run EXOTIC to generate a lightcurve",
            ],
            "fits_directory": str(dataset_dir),
            "output_directory": str(output_dir),
            "log_file": str(log_file),
            "fits_count": len(fits_files),
            "first_fits_image": str(first_image),
            "inits_json": str(inits_file_path),
            "inits_data": inits_data,
            "exotic_result": exotic_result,
            "expected_outputs": expected_outputs,
            "error": None if not exotic_result else exotic_result.get("error"),
            "traceback": None if not exotic_result else exotic_result.get("traceback"),
        }
        _log_tool_progress(log_file, "pipeline_finished", {
            "ok": result.get("ok"),
            "status": result.get("status"),
            "inits_json": result.get("inits_json"),
            "output_directory": result.get("output_directory")
        })
        return result
    except Exception as error:
        _log_tool_progress(log_file, "pipeline_error", {
            "error": str(error),
            "traceback": traceback.format_exc()
        })
        result = _error_dict(error)
        result["log_file"] = str(log_file) if log_file else None
        return result


TOOLS_LIST = [
    {
        "type": "function",
        "function": {
            "name": "ReadFileTool",
            "description": "Read files with character encoding detection, line ranges, and syntax highlighting metadata for code and structured files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "max_chars": {"type": "integer"},
                    "highlight": {"type": "boolean"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "WriteFileTool",
            "description": "Write content to files with automatic backup and directory creation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "encoding": {"type": "string"},
                    "backup": {"type": "boolean"},
                    "create_dirs": {"type": "boolean"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "EditFileTool",
            "description": "Edit existing files by applying targeted old_text to new_text replacements.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
                                "count": {"type": "integer"}
                            },
                            "required": ["old_text", "new_text"]
                        }
                    },
                    "encoding": {"type": "string"},
                    "backup": {"type": "boolean"}
                },
                "required": ["path", "edits"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ListDirectoryTool",
            "description": "Recursively list directory contents with optional filtering.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "recursive": {"type": "boolean"},
                    "pattern": {"type": "string"},
                    "include_dirs": {"type": "boolean"},
                    "include_files": {"type": "boolean"},
                    "max_results": {"type": "integer"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "FileSearchTool",
            "description": "Execute regular-expression searches across files and return matching lines with surrounding context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "file_pattern": {"type": "string"},
                    "context_lines": {"type": "integer"},
                    "max_results": {"type": "integer"}
                },
                "required": ["pattern", "path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "FindFilesTool",
            "description": "Locate files using glob-style patterns such as **/*.py or *.md.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "max_results": {"type": "integer"}
                },
                "required": ["pattern", "path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "WorkspaceInfoTool",
            "description": "Return the active sandbox workspace paths for this run.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "RunCommandTool",
            "description": "Run shell commands in a persistent bash session rooted in the active run workspace with a scrubbed environment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                    "cwd": {"type": "string"},
                    "env": {"type": "object"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "RunPythonTool",
            "description": "Execute Python code directly with common libraries pre-loaded for analysis, scripting, or validation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "timeout": {"type": "integer"},
                    "cwd": {"type": "string"},
                    "env": {"type": "object"}
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "WebSearchTool",
            "description": "Perform web searches and return structured search results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                    "timeout_seconds": {"type": "number"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "TodoRead",
            "description": "Read the current task tracking list.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "TodoWrite",
            "description": "Replace the current task tracking list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {"type": "object"}
                    }
                },
                "required": ["todos"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "DisplayImageTool",
            "description": "Render images in supported interfaces for inline visual analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "RUN_EXOTIC_UNTIL_IDLE",
            "description": "Run EXOTIC against an inits.json file until it exits, becomes idle, or reaches a hard timeout. idle_timeout_seconds defaults to EXOAGENT_EXOTIC_IDLE_TIMEOUT_SECONDS, initially 300 seconds, and is capped at 900 seconds.",
            "parameters": {
                "type": "object",
                "properties": {
                    "inits_json": {"type": "string"},
                    "cwd": {"type": "string"},
                    "idle_timeout_seconds": {
                        "type": "number",
                        "description": "Seconds of silence before terminating EXOTIC. Defaults to 300 via EXOAGENT_EXOTIC_IDLE_TIMEOUT_SECONDS and is capped at 900."
                    },
                    "tail_chars": {"type": "integer"},
                    "hard_timeout_seconds": {"type": "number"},
                    "env": {"type": "object"},
                    "input_text": {
                        "type": "string",
                        "description": "Text to send to a live EXOTIC process that previously returned awaiting_input."
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session identifier returned by a previous awaiting_input result."
                    },
                    "prompt_quiet_seconds": {"type": "number"}
                },
                "required": ["inits_json"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "EXOTIC_STANDARD_NOTEBOOK_PIPELINE",
            "description": (
                "Run the attached EXOTIC Standard notebook flow as one resumable tool: "
                "load/check FITS images, reuse or create inits.json, return notebook-style "
                "VLM prompts for target/comparison coordinates when needed, generate a complete "
                "non-interactive inits.json when possible, then run or resume EXOTIC."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fits_directory": {"type": "string"},
                    "planet_name": {"type": "string"},
                    "host_star_name": {"type": "string"},
                    "telescope": {
                        "type": "string",
                        "enum": ["MicroObservatory", "Exoplanet Watch .4 Meter"]
                    },
                    "camera_type": {
                        "type": "string",
                        "enum": ["CCD", "DSLR"],
                        "description": "Camera Type (CCD or DSLR)."
                    },
                    "primary_observer_code": {"type": "string"},
                    "secondary_observer_code": {"type": "string"},
                    "output_directory": {"type": "string"},
                    "vlm_answers": {
                        "type": "object",
                        "properties": {
                            "target_star_xy": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2,
                                "maxItems": 2
                            },
                            "comparison_stars_xy": {
                                "type": "array",
                                "items": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                    "minItems": 2,
                                    "maxItems": 2
                                }
                            }
                        }
                    },
                    "planetary_parameters": {"type": "object"},
                    "run_exotic": {"type": "boolean"},
                    "idle_timeout_seconds": {
                        "type": "number",
                        "description": "Seconds of EXOTIC silence before terminating. Defaults to 300 via EXOAGENT_EXOTIC_IDLE_TIMEOUT_SECONDS and is capped at 900."
                    },
                    "hard_timeout_seconds": {"type": "number"},
                    "tail_chars": {"type": "integer"},
                    "exotic_input": {
                        "type": "string",
                        "description": "Text to send when resuming an EXOTIC process awaiting interactive input."
                    },
                    "exotic_session_id": {
                        "type": "string",
                        "description": "Session identifier returned by a previous awaiting_exotic_input result."
                    }
                },
                "required": ["fits_directory"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "QUERY_AAVSO_STAR_CHART",
            "description": "Query the AAVSO VSP API for a star chart and optionally download the chart image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "star": {"type": "string"},
                    "ra": {"type": "string"},
                    "dec": {"type": "string"},
                    "fov": {"type": "number"},
                    "maglimit": {"type": "number"},
                    "resolution": {"type": "integer"},
                    "north": {"type": "string"},
                    "east": {"type": "string"},
                    "dss": {"type": "boolean"},
                    "chart_id": {"type": "string"},
                    "timeout_seconds": {"type": "number"},
                    "download_image": {"type": "boolean"},
                    "image_output_dir": {"type": "string"},
                    "max_photometry_rows": {"type": "integer"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "PLOT_XY",
            "description": "Create a simple scientific scatter or line plot and save it to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {
                        "type": "array",
                        "items": {"type": "number"}
                    },
                    "y": {
                        "type": "array",
                        "items": {"type": "number"}
                    },
                    "output_path": {"type": "string"},
                    "title": {"type": "string"},
                    "xlabel": {"type": "string"},
                    "ylabel": {"type": "string"},
                    "kind": {"type": "string", "enum": ["scatter", "line"]},
                    "invert_yaxis": {"type": "boolean"},
                    "connect_points": {"type": "boolean"}
                },
                "required": ["x", "y", "output_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "VIEW_FITS_GCOLAB_FORMAT",
            "description": "Create a PNG visualization of a FITS image using matplotlib, viridis, origin lower, ZScale normalization, labeled axes, and a colorbar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fits_file_path": {"type": "string"},
                    "output_path": {"type": "string"},
                    "title": {"type": "string"},
                    "hdu_index": {"type": "integer"}
                },
                "required": ["fits_file_path"]
            }
        }
    }
]


TOOL_MAPPING = {
    "ReadFileTool": ReadFileTool,
    "WriteFileTool": WriteFileTool,
    "EditFileTool": EditFileTool,
    "ListDirectoryTool": ListDirectoryTool,
    "FileSearchTool": FileSearchTool,
    "FindFilesTool": FindFilesTool,
    "WorkspaceInfoTool": WorkspaceInfoTool,
    "RunCommandTool": RunCommandTool,
    "RunPythonTool": RunPythonTool,
    "WebSearchTool": WebSearchTool,
    "TodoRead": TodoRead,
    "TodoWrite": TodoWrite,
    "DisplayImageTool": DisplayImageTool,
    "RUN_EXOTIC_UNTIL_IDLE": RUN_EXOTIC_UNTIL_IDLE,
    "EXOTIC_STANDARD_NOTEBOOK_PIPELINE": EXOTIC_STANDARD_NOTEBOOK_PIPELINE,
    "QUERY_AAVSO_STAR_CHART": QUERY_AAVSO_STAR_CHART,
    "PLOT_XY": PLOT_XY,
    "VIEW_FITS_GCOLAB_FORMAT": VIEW_FITS_GCOLAB_FORMAT
}


if __name__ == "__main__":
    smoke_file = "smoke/test.txt"
    smoke_write = WriteFileTool(smoke_file, "smoke test\n", backup=False)
    smoke_result = {
        "tool_count": len(TOOLS_LIST),
        "mapping_count": len(TOOL_MAPPING),
        "workspace_root": str(WORKSPACE_ROOT),
        "write_workspace_ok": smoke_write.get("ok"),
        "read_workspace_ok": ReadFileTool(smoke_file, start_line=1, end_line=3).get("ok"),
        "todo_ok": TodoWrite([{"content": "smoke test", "status": "done"}]).get("ok")
    }
    print(json.dumps(smoke_result, indent=2))
