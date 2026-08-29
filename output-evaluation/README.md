# ExoBench Output Evaluation

This directory contains the evaluation artifacts for ExoBench's multi-expert review workflow. It collects per-run metrics, aggregate benchmark tables, report-viewer inputs, rendered report pages, and reviewer assignment data.

Model identities are represented by their run model IDs: `gpt-5.6-luna`, `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`, and `qwen/qwen3.6-35b-a3b`. Agent configurations are represented as `ReAct` and `ReAct+Skills`. Source categories are represented as `OpenAI-compatible` and `OpenRouter`.

## Layout

```text
output-evaluation/
  README.md
  anonymized_responses_with_reports.csv
  csv/
  report-viewer/
  run_full_evaluation_pipeline.sh
  scripts/
```

`csv/` contains the structured evaluation tables.

`report-viewer/` contains the static report pages, copied report artifacts, Flask app, assignment tooling, and deployment configuration.

`scripts/` contains the metric extraction and aggregation code.

## CSV Files

`csv/aavso_summaries.csv` records whether each attempted run produced an AAVSO summary and where that summary was found in the source tree.

`csv/reports.csv` contains extracted final report text keyed by observation path, model ID, and agent-framework label.

`csv/evaluation_runs.csv` is the per-run metric table. It includes workflow completion, target-star error, ephemerides extraction metrics, NASA comparison metrics, EXOTIC-generation metrics, runtime, and report length.

`csv/model_comparison.csv` aggregates performance by model ID.

`csv/agent_framework_comparison.csv` aggregates performance by agent-framework label.

`csv/model_agent_framework_comparison.csv` aggregates performance by model/framework pair.

`csv/nasa_ephemerides.csv` contains reference ephemerides for the observation targets.

`csv/report_viewer_reports_with_stats.csv` is the report-viewer source table. It joins submitted reports with per-run statistics and assigns stable report IDs.

`csv/report_viewer_artifact_manifest.csv` maps report-page artifact links to copied artifacts. It is compacted so deleted duplicate artifact files are not referenced.

`anonymized_responses_with_reports.csv` contains expert evaluation responses joined with the corresponding report text.

## Deduplication Policy

This directory has been cleaned so there are no exact duplicate files outside [vcs-removed] metadata.

The retained files follow these rules:

- Keep the complete source table when an older export is a subset of the same assignment data.
- Keep generated report pages because distinct report IDs represent distinct benchmark submissions, even when they share the same observation target.
- Deduplicate copied artifacts by content hash.
- When identical artifacts appear inside one report page, keep the first artifact and rewrite links to it.
- When identical artifacts appear across report pages, keep the copy attached to the more complete report page and rewrite links to the retained artifact.
- Keep `review_assignments.csv` as the complete reviewer-assignment source of truth.

Stale partial/public assignment exports were removed. Public URL exports can be regenerated from `review_assignments.csv` when needed.

## Privacy Policy

The shareable files retain raw model names and framework names, but avoid local user paths, private user identifiers, emails, and local project paths.

Current model and framework labels:

```text
gpt-5.6-luna
nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free
qwen/qwen3.6-35b-a3b
ReAct
ReAct+Skills
OpenAI-compatible
OpenRouter
```

Private source folders can be supplied to the pipeline with:

```bash
EXOBENCH_RESULTS_ROOT=/path/to/private/results output-evaluation/run_full_evaluation_pipeline.sh ...
```

Generated outputs should be inspected before sharing:

```bash
rg -n "$PRIVATE_DENYLIST_REGEX" output-evaluation -g '!report-viewer/[metadata-dir-removed]/**'
```

No matches should appear in shareable files. [vcs-removed] metadata under `report-viewer/[metadata-dir-removed]/` is not part of the shareable evaluation artifact set.

## Full Pipeline

Run the full pipeline from the repository root:

```bash
output-evaluation/run_full_evaluation_pipeline.sh --max-per-reviewer 15 --num-reviewers 12
```

The pipeline runs:

1. `scripts/find_aavso_summaries.py`
2. `scripts/extract_reports.py`
3. `scripts/extract_nasa_ephemerides.py`
4. `scripts/build_evaluation_artifacts.py`
5. `report-viewer/build_report_viewer_csv.py`
6. `report-viewer/build_static_site.py`
7. `report-viewer/assign_reviewers.py`

Reviewer sources can be generated or supplied:

```bash
output-evaluation/run_full_evaluation_pipeline.sh --max-per-reviewer 15 --num-reviewers 12
output-evaluation/run_full_evaluation_pipeline.sh --max-per-reviewer 20 --reviewers "Reviewer 01,Reviewer 02,Reviewer 03"
output-evaluation/run_full_evaluation_pipeline.sh --max-per-reviewer 15 --reviewers-csv reviewers.csv
```

## Report Viewer

Run the local Flask viewer:

```bash
cd output-evaluation/report-viewer
python app.py
```

Report pages are served at:

```text
/<report_id>/
```

Linked artifacts are served at:

```text
/<report_id>/files/<artifact_name>
```

The viewer allows only these artifact extensions:

```text
.csv
.json
.pdf
.png
.txt
```

The root route intentionally returns 404 because reviewers receive direct report URLs.

## Static Site Build

Build report-viewer inputs and pages manually:

```bash
cd output-evaluation/report-viewer
python build_report_viewer_csv.py
python build_static_site.py
```

`build_static_site.py` sanitizes model-generated report text, preserves model and framework labels in rendered report pages, normalizes local paths, links copied artifacts, writes `404.html`, and refreshes `csv/report_viewer_artifact_manifest.csv`.

## Reviewer Assignment

Assign reports to reviewers:

```bash
cd output-evaluation/report-viewer
python assign_reviewers.py --num-reviewers 12 --max-per-reviewer 15 --reviews-per-report 2
```

The assignment script validates report rows, enforces reviewer capacity, assigns distinct reviewers per report, balances load, and writes:

```text
report-viewer/review_assignments.csv
```

Generate a public URL export only when needed:

```bash
cd output-evaluation/report-viewer
python build_public_review_assignments.py
```

That derived export is intentionally not kept as the canonical assignment file.
