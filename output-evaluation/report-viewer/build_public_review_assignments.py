#!/usr/bin/env python3
import argparse
import csv
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_CSV = SCRIPT_DIR / "review_assignments.csv"
DEFAULT_OUTPUT_CSV = SCRIPT_DIR / "review_assignments_public_urls.csv"
BASE_URL = "https://exoagent-report-evaluation.vercel.app/"


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["reviewer", "exoplanet", "report_url"])
        writer.writeheader()
        writer.writerows(rows)


def public_report_url(report_url):
    return BASE_URL + report_url.lstrip("/")


def build_public_assignments(rows):
    assignments = [
        {
            "reviewer": row["reviewer"],
            "exoplanet": row.get("exoplanet") or row.get("observation", ""),
            "report_url": public_report_url(row["report_url"]),
        }
        for row in rows
    ]
    return sorted(assignments, key=lambda row: (row["reviewer"], row["report_url"]))


def main():
    parser = argparse.ArgumentParser(
        description="Create a reviewer-sorted assignment CSV with public report URLs only."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_CSV)
    args = parser.parse_args()

    rows = read_csv(args.input)
    assignments = build_public_assignments(rows)
    write_csv(args.output, assignments)
    print(f"Wrote {len(assignments)} assignments to {args.output}")


if __name__ == "__main__":
    main()
