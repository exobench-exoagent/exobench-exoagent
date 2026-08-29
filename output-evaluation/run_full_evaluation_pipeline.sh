#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  output-evaluation/run_full_evaluation_pipeline.sh --max-per-reviewer K --num-reviewers N
  output-evaluation/run_full_evaluation_pipeline.sh --max-per-reviewer K --reviewers "Reviewer 01,Reviewer 02,Reviewer 03"
  output-evaluation/run_full_evaluation_pipeline.sh --max-per-reviewer K --reviewers-csv reviewers.csv

Runs the full ExoAgent evaluation pipeline:
  1. Extract AAVSO summaries from the configured results root
  2. Extract reports from .txt logs using exact FINAL_REPORT tags only
  3. Extract NASA ephemerides from NASA Exoplanet Archive
  4. Build run/model/framework statistics
  5. Build submitted-run report viewer CSV
  6. Generate submitted-run static HTML pages
  7. Assign submitted reports to reviewers

Assignment constraints are enforced by assign_reviewers.py:
  - each report gets two distinct reviewers by default
  - each reviewer gets at most K reports
  - capacity must satisfy N * K >= 2 * submitted_report_count

Optional:
  --reviews-per-report R   Default: 2
  --seed S                 Default: 20260812
  --assignment-output P    Default: output-evaluation/report-viewer/review_assignments.csv
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
export PYTHONDONTWRITEBYTECODE=1

if [[ $# -eq 0 ]]; then
  usage
  exit 2
fi

ASSIGN_ARGS=()
ASSIGN_OUTPUT="${SCRIPT_DIR}/report-viewer/review_assignments.csv"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --assignment-output)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --assignment-output" >&2
        exit 2
      fi
      ASSIGN_OUTPUT="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      ASSIGN_ARGS+=("$1")
      shift
      ;;
  esac
done

cd "${PROJECT_ROOT}"

run_step() {
  local label="$1"
  shift
  echo
  echo "==> ${label}"
  "$@"
}

run_step "Extract AAVSO summaries" \
  "${PYTHON_BIN}" "${SCRIPT_DIR}/scripts/find_aavso_summaries.py"

run_step "Extract reports from .txt FINAL_REPORT blocks" \
  "${PYTHON_BIN}" "${SCRIPT_DIR}/scripts/extract_reports.py"

run_step "Extract NASA ephemerides" \
  "${PYTHON_BIN}" "${SCRIPT_DIR}/scripts/extract_nasa_ephemerides.py"

run_step "Build evaluation statistics and markdown artifacts" \
  "${PYTHON_BIN}" "${SCRIPT_DIR}/scripts/build_evaluation_artifacts.py"

run_step "Build submitted-run report viewer CSV" \
  "${PYTHON_BIN}" "${SCRIPT_DIR}/report-viewer/build_report_viewer_csv.py"

run_step "Generate submitted-run static HTML pages" \
  "${PYTHON_BIN}" "${SCRIPT_DIR}/report-viewer/build_static_site.py"

run_step "Assign submitted reports to reviewers" \
  "${PYTHON_BIN}" "${SCRIPT_DIR}/report-viewer/assign_reviewers.py" \
    "${ASSIGN_ARGS[@]}" \
    --output "${ASSIGN_OUTPUT}"

echo
echo "Pipeline complete."
echo "CSV outputs: ${SCRIPT_DIR}/csv"
echo "Markdown outputs: ${SCRIPT_DIR}/markdown"
echo "Report viewer: ${SCRIPT_DIR}/report-viewer"
echo "Assignments: ${ASSIGN_OUTPUT}"
