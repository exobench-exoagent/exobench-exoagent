#!/usr/bin/env python3
import csv
import hashlib
import html
import re
import shutil
from pathlib import Path
from urllib.parse import quote

import markdown as markdown_renderer


VIEWER_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = VIEWER_ROOT.parents[1]
OUTPUT_EVALUATION_ROOT = VIEWER_ROOT.parent
CSV_ROOT = OUTPUT_EVALUATION_ROOT / "csv"
DATA_CSV = CSV_ROOT / "report_viewer_reports_with_stats.csv"
ARTIFACT_MANIFEST_CSV = CSV_ROOT / "report_viewer_artifact_manifest.csv"
PATH_ANCHORS = ("output", "logs", "scratch", "tmp")
ARTIFACT_EXTENSIONS = {".csv", ".json", ".pdf", ".png", ".txt"}
LOCAL_PATH_PREFIX_RE = re.compile(
    r"(?:PROJECT_ROOT/(?:exoagent-workspaces/)?)|(?:exoagent-workspaces/)"
)
BLIND_REDACTION_TERMS = [
    r"Model\s+[A-Z]",
    r"Agent\s+Framework\s+[A-Z]",
    r"Source\s+Class\s+[A-Z]",
]
REDACTION_RE = re.compile(
    r"(?:" + "|".join(BLIND_REDACTION_TERMS) + r")",
    re.IGNORECASE,
)


def read_rows():
    with open(DATA_CSV, newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def normalize_path(path):
    path = LOCAL_PATH_PREFIX_RE.sub("", path or "")
    path = path.lstrip("/")
    parts = [part for part in path.split("/") if part and part != "."]
    normalized_parts = ["tmp" if part == "temp" else part for part in parts]
    for anchor in PATH_ANCHORS:
        if anchor in normalized_parts:
            return "/".join(normalized_parts[normalized_parts.index(anchor):])
    return path


def normalize_path_if_needed(value):
    if "/" not in (value or ""):
        return value
    normalized = normalize_path(value)
    normalized_first = normalized.split("/", 1)[0]
    if normalized_first in PATH_ANCHORS:
        return normalized
    if normalized_first == "runs":
        leaf = normalized.rsplit("/", 1)[-1]
        if leaf.lower().endswith(".log"):
            return f"logs/{leaf}"
        return f"output/{leaf}"
    if normalized_first in ("EXOTIC_output", "observation"):
        remainder = normalized.split("/", 1)[1] if "/" in normalized else ""
        return "output/" + remainder if remainder else "output"
    if re.search(r"\.(?:csv|fits|json|log|md|pdf|png|txt)(?:/|$)", normalized, re.IGNORECASE):
        if normalized.lower().endswith(".log"):
            return "logs/" + normalized.rsplit("/", 1)[-1]
        return "output/" + normalized.lstrip("/")
    return value


def redact_blind_terms(markdown):
    return REDACTION_RE.sub("[REDACTED]", markdown or "")


def strip_report_line_numbers(markdown):
    lines = (markdown or "").splitlines()
    numbered = [
        line for line in lines
        if re.match(r"^\s*\d+:\s?", line)
    ]
    content_lines = [line for line in lines if line.strip()]
    if not content_lines or len(numbered) / len(content_lines) < 0.6:
        return markdown

    return "\n".join(
        re.sub(r"^\s*\d+:\s?", "", line)
        for line in lines
    )


def normalize_common_latex(markdown):
    markdown = markdown or ""
    markdown = markdown.replace("BJD(_\\mathrm{TDB})", "\\(\\mathrm{BJD}_{\\mathrm{TDB}}\\)")

    def wrap_parenthesized_latex(match):
        inner = match.group(1).strip()
        if inner.startswith("\\(") or inner.endswith("\\)"):
            return match.group(0)
        return f"\\({inner}\\)"

    markdown = re.sub(
        r"(?<!\\)\(([^()\n]*(?:\\mathrm|\\star|\\pm|\\sigma|\\chi|\\alpha|\\beta|\\gamma|\\delta|\\rho|R_\\|a/R)[^()\n]*)\)",
        wrap_parenthesized_latex,
        markdown,
    )
    return markdown


def sanitize_report(markdown):
    markdown = (markdown or "").replace("\\n", "\n").replace("\\t", "\t")
    markdown = strip_report_line_numbers(markdown)
    markdown = redact_blind_terms(markdown)
    markdown = normalize_common_latex(markdown)
    markdown = markdown.replace("Rp/R*", "Rp/R\\*")
    markdown = re.sub(
        r"`([^`]+)`",
        lambda match: "`" + normalize_path_if_needed(match.group(1)) + "`",
        markdown,
    )
    markdown = LOCAL_PATH_PREFIX_RE.sub("", markdown)
    path_pattern = re.compile(
        r"(?P<path>(?:/?(?:runs|output|logs|scratch|tmp|temp|EXOTIC_output|observation)(?:/[^\s`'\"<>)]+)+))"
    )
    markdown = path_pattern.sub(lambda match: normalize_path_if_needed(match.group("path")), markdown)
    markdown = re.sub(r"(?<![A-Za-z0-9_/-])temp/", "tmp/", markdown)
    markdown = re.sub(r"(?<![A-Za-z0-9_/-])/+(output|logs|scratch|tmp)/", r"\1/", markdown)
    return markdown


def candidate_paths(markdown):
    candidates = []
    markdown = (markdown or "").replace("\\n", "\n").replace("\\t", "\t")
    for match in re.finditer(r"`([^`]+)`", markdown):
        candidates.append(match.group(1).strip())

    patterns = [
        r"PROJECT_ROOT/[^\s`'\"<>]+?\.(?:csv|png|pdf|json|txt)",
        r"(?:runs|output|logs|scratch|tmp|temp|EXOTIC_output|observation)/[^\s`'\"<>]+?\.(?:csv|png|pdf|json|txt)",
        r"(?:AAVSO|Final|FinalLightCurve|FinalParams|NormalizedFlux|PlateStatus|Triangle|FOV|Observing_Statistics|inits)[A-Za-z0-9_.%+,:=@~#() -]*?\.(?:csv|png|pdf|json|txt)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, markdown, re.IGNORECASE):
            candidates.append(match.group(0).strip())
    return candidates


def clean_candidate(candidate):
    candidate = (candidate or "").strip().strip("`")
    candidate = candidate.rstrip(".,;:")
    if ":" in candidate:
        suffix_candidate = candidate.split(":", 1)[1].strip()
        if Path(suffix_candidate).suffix.lower() in ARTIFACT_EXTENSIONS:
            candidate = suffix_candidate
    return candidate


def display_path(candidate, source_path):
    normalized = normalize_path_if_needed(candidate)
    first = normalized.lstrip("/").split("/", 1)[0]
    if first in PATH_ANCHORS:
        return normalized

    source_parts = list(source_path.parts)
    normalized_parts = ["tmp" if part == "temp" else part for part in source_parts]
    for anchor in PATH_ANCHORS:
        if anchor in normalized_parts:
            return "/".join(normalized_parts[normalized_parts.index(anchor):])
    if source_path.suffix.lower() == ".log":
        return f"logs/{source_path.name}"
    return f"output/{source_path.name}"


def resolve_candidate(candidate, observation_dir):
    candidate = clean_candidate(candidate)
    if not candidate:
        return None
    suffix = Path(candidate).suffix.lower()
    if suffix not in ARTIFACT_EXTENSIONS:
        return None

    absolute = Path(candidate)
    if absolute.is_absolute() and absolute.is_file():
        return absolute

    local = LOCAL_PATH_PREFIX_RE.sub("", candidate).lstrip("/")
    possible_roots = [observation_dir, PROJECT_ROOT, PROJECT_ROOT / "exoagent-workspaces"]
    variants = [local, local.replace("/tmp/", "/temp/")]
    if local.startswith("tmp/"):
        variants.append("output/temp/" + local.split("/", 1)[1])
    if local.startswith("EXOTIC_output/"):
        variants.append("output/" + local)
    if local.startswith("runs/"):
        variants.append("exoagent-workspaces/" + local)

    for root in possible_roots:
        for variant in variants:
            path = root / variant
            if path.is_file():
                return path

    name = Path(local).name
    if name:
        matches = sorted(observation_dir.glob(f"**/{name}"))
        for match in matches:
            if match.is_file() and match.suffix.lower() in ARTIFACT_EXTENSIONS:
                return match
    return None


def unique_artifact_name(source_path, used_names):
    index = len(used_names) + 1
    name = f"artifact_{index:03d}{source_path.suffix.lower()}"
    while name in used_names:
        index += 1
        name = f"artifact_{index:03d}{source_path.suffix.lower()}"
    used_names.add(name)
    return name


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_artifact(source_path, destination_path):
    if source_path.suffix.lower() in {".csv", ".json", ".txt"}:
        text = source_path.read_text(encoding="utf-8", errors="replace")
        text = redact_blind_terms(text)
        text = LOCAL_PATH_PREFIX_RE.sub("", text)
        destination_path.write_text(text, encoding="utf-8")
        return "redacted_text_copy"
    shutil.copy2(source_path, destination_path)
    return "byte_copy"


def build_artifacts(row, artifact_manifest_rows):
    observation_dir = Path(row.get("observation_path", ""))
    report_id = row["report_id"]
    files_dir = VIEWER_ROOT / report_id / "files"
    mappings = {}
    used_names = set()

    if not observation_dir.is_dir():
        return mappings

    for candidate in candidate_paths(row.get("report", "")):
        source_path = resolve_candidate(candidate, observation_dir)
        if not source_path:
            continue
        display = display_path(candidate, source_path)
        artifact_name = unique_artifact_name(source_path, used_names)
        files_dir.mkdir(parents=True, exist_ok=True)
        destination_path = files_dir / artifact_name
        copy_mode = copy_artifact(source_path, destination_path)
        href = f"/{report_id}/files/{quote(artifact_name)}"
        artifact_manifest_rows.append({
            "report_id": report_id,
            "candidate_text": candidate,
            "display_text": display,
            "source_path": str(source_path.resolve()),
            "artifact_path": str(destination_path.resolve()),
            "artifact_href": href,
            "extension": source_path.suffix.lower(),
            "copy_mode": copy_mode,
            "source_sha256": file_sha256(source_path),
            "artifact_sha256": file_sha256(destination_path),
        })
        mappings[display] = href
        mappings[redact_blind_terms(display)] = href
        mappings[normalize_path_if_needed(candidate)] = href
        mappings[redact_blind_terms(normalize_path_if_needed(candidate))] = href
        mappings[clean_candidate(candidate)] = href
        mappings[redact_blind_terms(clean_candidate(candidate))] = href
    return mappings


def link_artifact_paths(markdown, artifact_links):
    if not artifact_links:
        return markdown

    def link_html(display, href):
        escaped_display = html.escape(display)
        escaped_href = html.escape(href, quote=True)
        return (
            f'<a class="artifact-link" href="{escaped_href}" target="_blank" rel="noopener">'
            f'<code>{escaped_display}</code><span aria-hidden="true">↗</span></a>'
        )

    sorted_links = sorted(artifact_links.items(), key=lambda item: len(item[0]), reverse=True)
    placeholders = {}
    placeholder_index = 0

    def placeholder_for(html_fragment):
        nonlocal placeholder_index
        key = f"@@EXOAGENT_ARTIFACT_LINK_{placeholder_index}@@"
        placeholder_index += 1
        placeholders[key] = html_fragment
        return key

    def replace_code(match):
        value = match.group(1)
        href = artifact_links.get(value) or artifact_links.get(normalize_path_if_needed(value))
        if not href:
            return match.group(0)
        display = normalize_path_if_needed(value)
        return placeholder_for(link_html(display, href))

    markdown = re.sub(r"`([^`]+)`", replace_code, markdown)

    for display, href in sorted_links:
        if not display:
            continue
        pattern = re.compile(rf"(?<![A-Za-z0-9_./-]){re.escape(display)}(?![A-Za-z0-9_./-])")
        markdown = pattern.sub(lambda _: placeholder_for(link_html(display, href)), markdown)
    for key, value in placeholders.items():
        markdown = markdown.replace(key, value)
    return markdown


def normalize_markdown_syntax(markdown):
    lines = (markdown or "").splitlines()
    normalized = []
    list_item_re = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
    for line in lines:
        starts_list = bool(list_item_re.match(line))
        previous = normalized[-1] if normalized else ""
        previous_starts_list = bool(list_item_re.match(previous))
        previous_is_table = previous.strip().startswith("|")
        if starts_list and previous.strip() and not previous_starts_list and not previous_is_table:
            normalized.append("")
        normalized.append(line)
    return "\n".join(normalized)


def protect_math_spans(markdown):
    placeholders = {}

    def stash(match):
        key = f"@@EXOAGENT_MATH_{len(placeholders)}@@"
        placeholders[key] = match.group(0)
        return key

    patterns = [
        r"\$\$.*?\$\$",
        r"\\\[.*?\\\]",
        r"\\\(.*?\\\)",
    ]
    for pattern in patterns:
        markdown = re.sub(pattern, stash, markdown, flags=re.DOTALL)
    return markdown, placeholders


def markdown_to_html(markdown):
    markdown = normalize_markdown_syntax(markdown)
    markdown, math_placeholders = protect_math_spans(markdown)
    rendered = markdown_renderer.markdown(
        markdown or "",
        extensions=["extra", "sane_lists", "nl2br"],
        output_format="html5",
    )
    for key, value in math_placeholders.items():
        rendered = rendered.replace(key, value)
    return rendered


def page(title, body):
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #fffef7;
      --text: #1c1f24;
      --line: #ded6c8;
      --panel: #ffffff;
      --accent: #c85f1e;
      --accent-soft: #f0b35f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background-color: var(--bg);
      background-image:
        linear-gradient(rgba(0, 0, 0, 0.075) 1px, transparent 1px),
        linear-gradient(90deg, rgba(0, 0, 0, 0.075) 1px, transparent 1px);
      background-size: 28px 28px;
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.55;
    }}
    .brand {{
      width: min(1040px, calc(100% - 36px));
      margin: 22px auto 0;
      color: var(--accent);
      font-size: 0.86rem;
      font-weight: 800;
    }}
    main {{ max-width: 1040px; margin: 0 auto; padding: 18px 18px 40px; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    h1, h2, h3 {{ line-height: 1.2; margin: 1.3em 0 0.5em; }}
    h1 {{ font-size: 2rem; margin-top: 0; }}
    p {{ margin: 0.75em 0; }}
    code, pre {{
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 0.92em;
    }}
    code {{
      color: #9a4415;
      overflow-wrap: anywhere;
    }}
    .artifact-link {{
      color: #c85f1e;
      font-weight: 700;
      text-decoration: none;
    }}
    .artifact-link:hover {{
      text-decoration: underline;
    }}
    .artifact-link code {{
      color: inherit;
    }}
    .artifact-link span {{
      display: inline-block;
      margin-left: 0.28em;
      font-weight: 800;
    }}
    pre {{
      overflow: auto;
      padding: 14px;
      border: 1px solid var(--line);
      background: #f0f2f2;
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      margin: 18px 0;
      background: var(--panel);
    }}
    th, td {{
      border: 1px solid var(--line);
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
    }}
    th {{ background: #fff1df; }}
    .report {{
      border: 1px solid var(--line);
      background: var(--panel);
      padding: 26px;
      border-radius: 6px;
      box-shadow: 0 18px 48px rgba(66, 45, 24, 0.09);
    }}
  </style>
  <script>
    window.MathJax = {{
      tex: {{
        inlineMath: [["\\\\(", "\\\\)"], ["$", "$"]],
        displayMath: [["\\\\[", "\\\\]"], ["$$", "$$"]],
        processEscapes: true
      }},
      options: {{
        skipHtmlTags: ["script", "noscript", "style", "textarea", "pre", "code"]
      }}
    }};
  </script>
  <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"></script>
</head>
<body>
  <div class="brand">ExoAgent Reports for Expert-Based Evaluation</div>
  <main>{body}</main>
</body>
</html>"""


def write_html(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def render_report(row, artifact_manifest_rows):
    artifact_links = build_artifacts(row, artifact_manifest_rows)
    report = sanitize_report(row.get("report", ""))
    report = link_artifact_paths(report, artifact_links)
    body = "<section class=\"report\">" + markdown_to_html(report) + "</section>"
    return page("ExoAgent", body)


def render_404():
    body = (
        "<h1>404</h1>"
        "<p>No report exists for this ID.</p>"
    )
    return page("404 Not Found", body)


def remove_existing_report_pages():
    for path in VIEWER_ROOT.iterdir():
        if path.is_dir() and (path / "index.html").exists():
            shutil.rmtree(path)


def write_artifact_manifest(rows):
    fieldnames = [
        "report_id",
        "candidate_text",
        "display_text",
        "source_path",
        "artifact_path",
        "artifact_href",
        "extension",
        "copy_mode",
        "source_sha256",
        "artifact_sha256",
    ]
    ARTIFACT_MANIFEST_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(ARTIFACT_MANIFEST_CSV, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    rows = read_rows()
    artifact_manifest_rows = []
    remove_existing_report_pages()
    write_html(VIEWER_ROOT / "404.html", render_404())
    for row in rows:
        write_html(VIEWER_ROOT / row["report_id"] / "index.html", render_report(row, artifact_manifest_rows))
    write_artifact_manifest(artifact_manifest_rows)
    index_path = VIEWER_ROOT / "index.html"
    if index_path.exists():
        index_path.unlink()
    print(f"Wrote 404.html and {len(rows)} static report pages in {VIEWER_ROOT}")


if __name__ == "__main__":
    main()
