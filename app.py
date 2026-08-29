import hashlib
import re
from collections import defaultdict
from pathlib import Path

from flask import Flask, abort, jsonify, render_template_string


app = Flask(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_ROOT = (SCRIPT_DIR / "exoagent-workspaces").resolve()


# =========================================================
# Log discovery
# =========================================================

def _is_inside_log_root(path):
    try:
        path.resolve().relative_to(LOG_ROOT)
    except (ValueError, OSError):
        return False

    return True


def _log_directories():
    """
    Recursively find every directory named exactly 'logs'
    underneath the ExoAgent workspaces directory.
    """
    return sorted(
        path.resolve()
        for path in LOG_ROOT.rglob("logs")
        if path.is_dir() and _is_inside_log_root(path)
    )


def _log_files():
    """
    Find every .txt file inside every discovered logs directory.

    Files nested inside subdirectories of a logs directory are
    included as well.
    """
    paths = set()

    for log_directory in _log_directories():
        for path in log_directory.rglob("*.txt"):
            if path.is_file():
                paths.add(path.resolve())

    return sorted(
        paths,
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _containing_log_directory(path):
    """
    Return the nearest ancestor directory named 'logs'.
    """
    path = path.resolve()

    for parent in path.parents:
        if parent.name == "logs":
            return parent

    return None


def _collection_key(log_directory):
    """
    Return the top-level workspace directory containing a logs directory.

    Examples:
        LOG_ROOT/runs/wasp43-a/logs
            -> runs

        LOG_ROOT/logs
            -> Main
    """
    relative_parts = log_directory.relative_to(LOG_ROOT).parts

    if not relative_parts or relative_parts[0] == "logs":
        return "Main"

    return relative_parts[0]


def _collection_id(collection_key):
    return collection_key


def _collection_label(collection_key):
    return collection_key


def _log_id(path):
    """
    Return a unique ID based on the file path relative to SCRIPT_DIR.
    """
    return path.resolve().relative_to(SCRIPT_DIR).as_posix()


# =========================================================
# File reading and signatures
# =========================================================

def _read_log(path):
    return path.read_text(
        encoding="utf-8",
        errors="replace",
    )


def _content_signature(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _logs_signature(paths):
    """
    Generate a signature that changes whenever a discovered log is
    added, removed, renamed, moved, or modified.
    """
    digest = hashlib.sha256()

    for path in sorted(paths, key=lambda item: _log_id(item)):
        digest.update(_log_id(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(_content_signature(path).encode("ascii"))
        digest.update(b"\0")

    return digest.hexdigest()


# =========================================================
# Log parsing
# =========================================================


def _parse_log(path):
    text = _read_log(path)
    lines = text.splitlines()

    prompt = path.stem
    started = ""

    if lines and lines[0].startswith("# "):
        prompt = lines[0][2:].strip()

    if len(lines) > 1 and lines[1].startswith("Started:"):
        started = lines[1].replace(
            "Started:",
            "",
            1,
        ).strip()

    parts = re.split(r"\n\n(?=## )", text)
    sections = []

    for part in parts:
        if not part.startswith("## "):
            continue

        section_lines = part.splitlines()

        title = section_lines[0].replace(
            "##",
            "",
            1,
        ).strip()

        body = "\n".join(section_lines[1:]).strip()

        sections.append(
            {
                "title": title,
                "body": body,
            }
        )

    log_directory = _containing_log_directory(path)

    if log_directory is None:
        abort(404)

    relative_to_logs = path.relative_to(log_directory).as_posix()

    return {
        "log_id": _log_id(path),
        "filename": path.name,
        "relative_path": relative_to_logs,
        "source": path.parent.relative_to(SCRIPT_DIR).as_posix(),
        "prompt": prompt,
        "started": started,
        "sections": list(reversed(sections)),
        "raw": text,
    }


def _build_collections(paths):
    """
    Group logs by the first directory under SCRIPT_DIR.

    Examples:
        exo/agent/revised/v/3/logs -> exo
        logs -> Main

    Collections are sorted alphabetically by label.
    Logs within each collection retain the newest-first order
    produced by _log_files().
    """
    grouped = defaultdict(list)

    for path in paths:
        log_directory = _containing_log_directory(path)

        if log_directory is None:
            continue

        grouped[_collection_key(log_directory)].append(path)

    collections = []

    sorted_keys = sorted(
        grouped,
        key=lambda collection_key: (
            _collection_label(collection_key).lower(),
            _collection_id(collection_key).lower(),
        ),
    )

    for collection_key in sorted_keys:
        collection_paths = sorted(
            grouped[collection_key],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

        collections.append(
            {
                "id": _collection_id(collection_key),
                "label": _collection_label(collection_key),
                "path": _collection_id(collection_key),
                "logs": [
                    _parse_log(path)
                    for path in collection_paths
                ],
            }
        )

    return collections


# =========================================================
# Safe path resolution
# =========================================================

def _safe_log_path(log_id):
    """
    Resolve a log ID while preventing directory traversal and access
    to files outside LOG_ROOT.
    """
    try:
        path = (SCRIPT_DIR / log_id).resolve()
        path.relative_to(LOG_ROOT)
    except (ValueError, OSError):
        abort(404)

    if not path.is_file():
        abort(404)

    if path.suffix.lower() != ".txt":
        abort(404)

    if _containing_log_directory(path) is None:
        abort(404)

    if path not in set(_log_files()):
        abort(404)

    return path


# =========================================================
# Templates
# =========================================================

INDEX_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">

    <meta
        name="viewport"
        content="width=device-width, initial-scale=1"
    >

    <title>LLM Agent Logs</title>

    <link
        rel="preconnect"
        href="https://fonts.googleapis.com"
    >

    <link
        rel="preconnect"
        href="https://fonts.gstatic.com"
        crossorigin
    >

    <link
        href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"
        rel="stylesheet"
    >

    <style>
        :root {
            --bg: #fbfaf4;
            --ink: #20201c;
            --muted: #6f6b61;
            --line: #ded9cb;
            --card: rgba(255, 254, 249, 0.92);
            --accent: #335c67;
            --accent-soft: rgba(51, 92, 103, 0.08);
        }

        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            min-height: 100vh;
            color: var(--ink);

            font-family:
                Inter,
                ui-sans-serif,
                system-ui,
                -apple-system,
                BlinkMacSystemFont,
                "Segoe UI",
                sans-serif;

            background-color: var(--bg);

            background-image:
                linear-gradient(
                    rgba(32, 32, 28, 0.06) 1px,
                    transparent 1px
                ),
                linear-gradient(
                    90deg,
                    rgba(32, 32, 28, 0.06) 1px,
                    transparent 1px
                );

            background-size: 28px 28px;
        }

        main {
            width: min(1200px, calc(100% - 40px));
            margin: 0 auto;
            padding: 40px 0;
        }

        .page-header {
            display: flex;
            align-items: end;
            justify-content: space-between;
            gap: 24px;
            margin-bottom: 28px;
        }

        h1 {
            margin: 0;
            font-size: clamp(30px, 5vw, 56px);
            letter-spacing: 0;
        }

        .count {
            color: var(--muted);
            font-weight: 600;
        }

        .collection-list {
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        .collection {
            overflow: hidden;
            border: 1px solid var(--line);
            border-radius: 10px;
            background: rgba(255, 254, 249, 0.78);
            box-shadow: 0 10px 30px rgba(32, 32, 28, 0.06);
        }

        .collection summary {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 20px;

            padding: 18px 20px;

            cursor: pointer;
            user-select: none;
            list-style: none;

            background: var(--accent-soft);
        }

        .collection summary::-webkit-details-marker {
            display: none;
        }

        .collection-title-area {
            min-width: 0;
        }

        .collection-title {
            margin: 0 0 4px;
            font-size: 18px;
            font-weight: 700;
            overflow-wrap: anywhere;
        }

        .collection-path {
            color: var(--muted);

            font-family:
                ui-monospace,
                SFMono-Regular,
                Menlo,
                Consolas,
                monospace;

            font-size: 11px;
            overflow-wrap: anywhere;
        }

        .collection-summary-right {
            display: flex;
            align-items: center;
            gap: 12px;
            flex-shrink: 0;
        }

        .collection-count {
            color: var(--muted);
            font-size: 13px;
            font-weight: 600;
            white-space: nowrap;
        }

        .chevron {
            display: inline-block;
            width: 10px;
            height: 10px;

            border-right: 2px solid var(--accent);
            border-bottom: 2px solid var(--accent);

            transform: rotate(45deg);
            transition: transform 160ms ease;
        }

        .collection[open] .chevron {
            transform: rotate(225deg);
        }

        .collection-content {
            padding: 16px;
            border-top: 1px solid var(--line);
        }

        .log-list {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        .log-card {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            align-items: center;
            gap: 18px;

            padding: 16px 18px;

            border: 1px solid var(--line);
            border-radius: 8px;

            background: var(--card);
        }

        .log-main {
            min-width: 0;
        }

        .log-card h2 {
            margin: 0 0 7px;
            font-size: 16px;
            line-height: 1.3;
        }

        .meta {
            margin-bottom: 4px;
            color: var(--muted);
            font-size: 13px;
        }

        .source {
            color: var(--muted);

            font-family:
                ui-monospace,
                SFMono-Regular,
                Menlo,
                Consolas,
                monospace;

            font-size: 11px;
            overflow-wrap: anywhere;
        }

        .preview {
            display: -webkit-box;
            margin-top: 10px;
            overflow: hidden;

            color: #2f2d28;
            font-size: 13px;
            line-height: 1.45;

            -webkit-box-orient: vertical;
            -webkit-line-clamp: 3;
        }

        .view-more {
            display: inline-flex;
            align-items: center;
            justify-content: center;

            padding: 9px 12px;

            border: 1px solid var(--accent);
            border-radius: 7px;

            color: var(--accent);
            font-size: 13px;
            font-weight: 700;
            text-decoration: none;
            white-space: nowrap;
        }

        .view-more:hover {
            color: white;
            background: var(--accent);
        }

        .empty {
            padding: 28px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--card);
        }

        @media (max-width: 650px) {
            .page-header {
                align-items: start;
                flex-direction: column;
                gap: 8px;
            }

            .collection summary {
                align-items: start;
            }

            .collection-summary-right {
                padding-top: 3px;
            }

            .collection-path {
                display: none;
            }

            .log-card {
                grid-template-columns: 1fr;
            }

            .view-more {
                width: 100%;
            }
        }
    </style>
</head>

<body>
    <main>
        <header class="page-header">
            <h1>LLM Agent Logs</h1>

            <div class="count">
                {{ total_logs }}
                {% if total_logs == 1 %}
                    log
                {% else %}
                    logs
                {% endif %}
                across
                {{ collections|length }}
                {% if collections|length == 1 %}
                    collection
                {% else %}
                    collections
                {% endif %}
            </div>
        </header>

        {% if collections %}
            <section class="collection-list">
                {% for collection in collections %}
                    <details
                        class="collection"
                        {% if loop.first %}open{% endif %}
                    >
                        <summary>
                            <div class="collection-title-area">
                                <div class="collection-title">
                                    {{ collection.label }}
                                </div>

                                <div class="collection-path">
                                    {{ collection.path }}
                                </div>
                            </div>

                            <div class="collection-summary-right">
                                <span class="collection-count">
                                    {{ collection.logs|length }}
                                    {% if collection.logs|length == 1 %}
                                        log
                                    {% else %}
                                        logs
                                    {% endif %}
                                </span>

                                <span class="chevron"></span>
                            </div>
                        </summary>

                        <div class="collection-content">
                            <div class="log-list">
                                {% for log in collection.logs %}
                                    <article class="log-card">
                                        <div class="log-main">
                                            <h2>{{ log.prompt }}</h2>

                                            <div class="meta">
                                                {{ log.started or log.filename }}
                                            </div>

                                            <div class="source">
                                                {{ log.relative_path }}
                                            </div>

                                            {% if log.sections %}
                                                <div class="preview">
                                                    {{ log.sections[0].body }}
                                                </div>
                                            {% endif %}
                                        </div>

                                        <a
                                            class="view-more"
                                            href="{{ url_for(
                                                'view_log',
                                                log_id=log.log_id
                                            ) }}"
                                            target="_blank"
                                            rel="noopener"
                                        >
                                            View Log
                                        </a>
                                    </article>
                                {% endfor %}
                            </div>
                        </div>
                    </details>
                {% endfor %}
            </section>
        {% else %}
            <div class="empty">
                No <code>.txt</code> files were found inside folders
                named <code>logs</code> beneath
                <code>exoagent-workspaces</code>.
            </div>
        {% endif %}
    </main>

    <script>
        const initialSignature = {{ signature|tojson }};

        async function refreshWhenLogsChange() {
            try {
                const response = await fetch(
                    "{{ url_for('index_status') }}",
                    {
                        cache: "no-store"
                    }
                );

                if (!response.ok) {
                    return;
                }

                const status = await response.json();

                if (status.signature !== initialSignature) {
                    window.location.reload();
                }
            } catch (error) {
                // Leave the current page visible if the server
                // temporarily becomes unavailable.
            }
        }

        window.setInterval(
            refreshWhenLogsChange,
            5000
        );
    </script>
</body>
</html>
"""


DETAIL_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">

    <meta
        name="viewport"
        content="width=device-width, initial-scale=1"
    >

    <title>{{ log.prompt }}</title>

    <link
        rel="preconnect"
        href="https://fonts.googleapis.com"
    >

    <link
        rel="preconnect"
        href="https://fonts.gstatic.com"
        crossorigin
    >

    <link
        href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"
        rel="stylesheet"
    >

    <style>
        :root {
            --bg: #fbfaf4;
            --ink: #20201c;
            --muted: #6f6b61;
            --line: #ded9cb;
            --card: rgba(255, 254, 249, 0.92);
            --accent: #335c67;
        }

        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            min-height: 100vh;
            color: var(--ink);

            font-family:
                Inter,
                ui-sans-serif,
                system-ui,
                -apple-system,
                BlinkMacSystemFont,
                "Segoe UI",
                sans-serif;

            background-color: var(--bg);

            background-image:
                linear-gradient(
                    rgba(32, 32, 28, 0.06) 1px,
                    transparent 1px
                ),
                linear-gradient(
                    90deg,
                    rgba(32, 32, 28, 0.06) 1px,
                    transparent 1px
                );

            background-size: 28px 28px;
        }

        main {
            width: min(980px, calc(100% - 40px));
            margin: 0 auto;
            padding: 40px 0;
        }

        a {
            color: var(--accent);
            font-weight: 700;
            text-decoration: none;
        }

        h1 {
            margin: 18px 0 8px;
            font-size: clamp(28px, 5vw, 52px);
            line-height: 1.05;
        }

        .meta {
            margin-bottom: 6px;
            color: var(--muted);
            font-weight: 600;
        }

        .source {
            margin-bottom: 24px;
            overflow-wrap: anywhere;
            color: var(--muted);

            font-family:
                ui-monospace,
                SFMono-Regular,
                Menlo,
                Consolas,
                monospace;

            font-size: 12px;
        }

        .section {
            margin-bottom: 14px;
            padding: 18px;

            border: 1px solid var(--line);
            border-radius: 8px;

            background: var(--card);
            box-shadow: 0 10px 30px rgba(32, 32, 28, 0.06);
        }

        .section-title {
            margin-bottom: 10px;
            color: var(--accent);
            font-size: 12px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }

        pre {
            margin: 0;
            white-space: pre-wrap;
            word-break: break-word;

            font:
                13px/1.5
                ui-monospace,
                SFMono-Regular,
                Menlo,
                Consolas,
                monospace;
        }
    </style>
</head>

<body>
    <main>
        <a href="{{ url_for('index') }}">
            Back to collections
        </a>

        <h1>{{ log.prompt }}</h1>

        <div class="meta">
            {{ log.started or log.filename }}
        </div>

        <div class="source">
            {{ log.source }}/{{ log.filename }}
        </div>

        {% for section in log.sections %}
            <section class="section">
                <div class="section-title">
                    {{ section.title }}
                </div>

                <pre>{{ section.body }}</pre>
            </section>
        {% endfor %}
    </main>

    <script>
        const initialSignature = {{ signature|tojson }};

        async function refreshWhenLogChanges() {
            try {
                const response = await fetch(
                    "{{ url_for(
                        'log_status',
                        log_id=log.log_id
                    ) }}",
                    {
                        cache: "no-store"
                    }
                );

                if (response.status === 404) {
                    window.location.reload();
                    return;
                }

                if (!response.ok) {
                    return;
                }

                const status = await response.json();

                if (status.signature !== initialSignature) {
                    window.location.reload();
                }
            } catch (error) {
                // Leave the current page visible if the server
                // temporarily becomes unavailable.
            }
        }

        window.setInterval(
            refreshWhenLogChanges,
            3000
        );
    </script>
</body>
</html>
"""


# =========================================================
# Flask routes
# =========================================================

@app.route("/")
def index():
    paths = _log_files()
    collections = _build_collections(paths)

    return render_template_string(
        INDEX_TEMPLATE,
        collections=collections,
        total_logs=len(paths),
        signature=_logs_signature(paths),
    )


@app.route("/status")
def index_status():
    paths = _log_files()

    return jsonify(
        {
            "signature": _logs_signature(paths),
        }
    )


@app.route("/log/<path:log_id>")
def view_log(log_id):
    path = _safe_log_path(log_id)

    return render_template_string(
        DETAIL_TEMPLATE,
        log=_parse_log(path),
        signature=_content_signature(path),
    )


@app.route("/log/<path:log_id>/status")
def log_status(log_id):
    path = _safe_log_path(log_id)

    return jsonify(
        {
            "signature": _content_signature(path),
        }
    )


if __name__ == "__main__":
    app.run(
        debug=True,
        port=8080,
    )
