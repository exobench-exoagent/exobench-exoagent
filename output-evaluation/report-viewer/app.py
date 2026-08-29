#!/usr/bin/env python3
from pathlib import Path

from flask import Flask, abort, Response, send_file


VIEWER_ROOT = Path(__file__).resolve().parent

app = Flask(__name__, static_folder=None)
ALLOWED_ARTIFACT_EXTENSIONS = {".csv", ".json", ".pdf", ".png", ".txt"}


def report_path(report_id):
    return VIEWER_ROOT / report_id / "index.html"


def is_report_id(value):
    return bool(value) and "/" not in value and value.replace("-", "").isalnum()


@app.route("/<report_id>/files/<path:filename>")
def report_file(report_id, filename):
    if not is_report_id(report_id):
        abort(404)
    path = (VIEWER_ROOT / report_id / "files" / filename).resolve()
    files_dir = (VIEWER_ROOT / report_id / "files").resolve()
    if files_dir not in path.parents:
        abort(404)
    if path.suffix.lower() not in ALLOWED_ARTIFACT_EXTENSIONS:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_file(path)


@app.route("/<report_id>")
@app.route("/<report_id>/")
def report(report_id):
    if not is_report_id(report_id):
        abort(404)

    path = report_path(report_id)
    if not path.is_file():
        abort(404)

    return Response(path.read_text(encoding="utf-8"), mimetype="text/html")


@app.route("/")
@app.route("/<path:unused>")
def not_found(unused=None):
    abort(404)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765)
