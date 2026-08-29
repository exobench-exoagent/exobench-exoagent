#!/usr/bin/env python3
import argparse
import csv
import math
import random
from pathlib import Path


VIEWER_ROOT = Path(__file__).resolve().parent
OUTPUT_EVALUATION_ROOT = VIEWER_ROOT.parent
CSV_ROOT = OUTPUT_EVALUATION_ROOT / "csv"
INPUT_CSV = CSV_ROOT / "report_viewer_reports_with_stats.csv"
DEFAULT_OUTPUT_CSV = VIEWER_ROOT / "review_assignments.csv"


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def reviewers_from_csv(path):
    rows = read_csv(path)
    if not rows:
        raise SystemExit(f"No reviewers found in {path}")
    field = "reviewer" if "reviewer" in rows[0] else ("name" if "name" in rows[0] else list(rows[0].keys())[0])
    reviewers = [row[field].strip() for row in rows if row.get(field, "").strip()]
    return dedupe_reviewers(reviewers)


def dedupe_reviewers(reviewers):
    seen = set()
    result = []
    for reviewer in reviewers:
        if reviewer in seen:
            continue
        seen.add(reviewer)
        result.append(reviewer)
    return result


def build_reviewers(args):
    sources = sum(bool(value) for value in (args.num_reviewers, args.reviewers, args.reviewers_csv))
    if sources != 1:
        raise SystemExit("Provide exactly one reviewer source: --num-reviewers, --reviewers, or --reviewers-csv.")
    if args.num_reviewers:
        return [f"Reviewer {index:02d}" for index in range(1, args.num_reviewers + 1)]
    if args.reviewers:
        return dedupe_reviewers([name.strip() for name in args.reviewers.split(",") if name.strip()])
    return reviewers_from_csv(args.reviewers_csv)


def assign_once(reports, reviewers, max_per_reviewer, reviews_per_report, rng):
    total_slots = len(reports) * reviews_per_report
    target_max = math.ceil(total_slots / len(reviewers))
    loads = {reviewer: 0 for reviewer in reviewers}
    assignments = []
    shuffled_reports = list(reports)
    rng.shuffle(shuffled_reports)

    for report in shuffled_reports:
        assigned_reviewers = set()
        for _ in range(reviews_per_report):
            candidates = [
                reviewer for reviewer in reviewers
                if reviewer not in assigned_reviewers
                and loads[reviewer] < max_per_reviewer
            ]
            soft_candidates = [reviewer for reviewer in candidates if loads[reviewer] < target_max]
            candidates = soft_candidates or candidates
            if not candidates:
                return None
            min_load = min(loads[reviewer] for reviewer in candidates)
            tied = [reviewer for reviewer in candidates if loads[reviewer] == min_load]
            reviewer = rng.choice(tied)
            assigned_reviewers.add(reviewer)
            loads[reviewer] += 1
            assignments.append({
                "reviewer": reviewer,
                "report_id": report["report_id"],
                "report_url": f"{report['report_id']}/",
                "observation": report.get("observation", ""),
            })

    return assignments, loads


def assign_reports(reports, reviewers, max_per_reviewer, reviews_per_report, seed, attempts=1000):
    invalid = [
        report for report in reports
        if not report.get("report", "").strip()
        or not Path(report.get("observation_path", "")).exists()
    ]
    if invalid:
        raise SystemExit(
            f"Input contains {len(invalid)} invalid report rows. "
            "Regenerate the report-viewer source CSV so every assigned report is non-empty and has an existing run folder."
        )

    required_slots = len(reports) * reviews_per_report
    capacity = len(reviewers) * max_per_reviewer
    if len(reviewers) < reviews_per_report:
        raise SystemExit(f"Need at least {reviews_per_report} reviewers so each report can receive distinct reviewers.")
    if capacity < required_slots:
        raise SystemExit(
            f"Insufficient capacity: {len(reviewers)} reviewers * {max_per_reviewer} max = {capacity}, "
            f"but {required_slots} review slots are required."
        )

    best = None
    for attempt in range(attempts):
        rng = random.Random(seed + attempt)
        result = assign_once(reports, reviewers, max_per_reviewer, reviews_per_report, rng)
        if result is None:
            continue
        assignments, loads = result
        spread = max(loads.values()) - min(loads.values())
        if spread <= 1:
            return assignments, loads
        if best is None or spread < best[0]:
            best = (spread, assignments, loads)

    if best:
        return best[1], best[2]
    raise SystemExit("Could not build a valid assignment with the provided constraints.")


def main():
    parser = argparse.ArgumentParser(description="Randomly and evenly assign submitted ExoAgent reports for review.")
    parser.add_argument("--num-reviewers", type=int, help="Generate generic reviewer names: Reviewer 01, Reviewer 02, etc.")
    parser.add_argument("--reviewers", help="Comma-separated reviewer names.")
    parser.add_argument("--reviewers-csv", type=Path, help="CSV with reviewer names in a reviewer, name, or first column.")
    parser.add_argument("--max-per-reviewer", type=int, required=True, help="Maximum reports each reviewer can review.")
    parser.add_argument("--reviews-per-report", type=int, default=2, help="Minimum distinct reviewers per report.")
    parser.add_argument("--seed", type=int, default=20260812, help="Random seed for reproducible assignments.")
    parser.add_argument("--input", type=Path, default=INPUT_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_CSV)
    args = parser.parse_args()

    reviewers = build_reviewers(args)
    reports = read_csv(args.input)
    assignments, loads = assign_reports(
        reports=reports,
        reviewers=reviewers,
        max_per_reviewer=args.max_per_reviewer,
        reviews_per_report=args.reviews_per_report,
        seed=args.seed,
    )

    write_csv(args.output, assignments, [
        "reviewer",
        "report_id",
        "report_url",
        "observation",
    ])
    print(f"Wrote {len(assignments)} assignments for {len(reports)} reports to {args.output}")
    print("Reviewer loads:")
    for reviewer in sorted(loads):
        print(f"{reviewer}: {loads[reviewer]}")


if __name__ == "__main__":
    main()
