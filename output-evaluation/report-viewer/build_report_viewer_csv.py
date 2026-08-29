#!/usr/bin/env python3
import csv
import hashlib
from pathlib import Path


VIEWER_ROOT = Path(__file__).resolve().parent
OUTPUT_EVALUATION_ROOT = VIEWER_ROOT.parent
CSV_ROOT = OUTPUT_EVALUATION_ROOT / "csv"

REPORTS_CSV = CSV_ROOT / "reports.csv"
RUN_STATS_CSV = CSV_ROOT / "run_statistics.csv"
OUTPUT_CSV = CSV_ROOT / "report_viewer_reports_with_stats.csv"


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def stable_report_id(row, used_ids):
    basis = "|".join([
        row.get("observation_path", ""),
        row.get("model", ""),
        row.get("agent_framework", ""),
    ])
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]
    report_id = digest
    suffix = 2
    while report_id in used_ids:
        report_id = f"{digest}-{suffix}"
        suffix += 1
    used_ids.add(report_id)
    return report_id


def main():
    reports = {
        row["observation_path"]: row.get("report", "")
        for row in read_csv(REPORTS_CSV)
    }
    stats_rows = []
    for row in read_csv(RUN_STATS_CSV):
        report = reports.get(row["observation_path"], "")
        if row.get("has_aavso_summary") != "yes":
            continue
        if not report.strip():
            continue
        if not Path(row["observation_path"]).exists():
            continue
        stats_rows.append(row)

    rows = []
    used_ids = set()
    for stats in stats_rows:
        row = {
            "report_id": stable_report_id(stats, used_ids),
            **stats,
            "report": reports.get(stats["observation_path"], ""),
        }
        rows.append(row)

    fieldnames = ["report_id"] + list(stats_rows[0].keys()) + ["report"]
    write_csv(OUTPUT_CSV, rows, fieldnames)
    print(f"Wrote {len(rows)} rows to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
