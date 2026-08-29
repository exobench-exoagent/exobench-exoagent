#!/usr/bin/env python3
import argparse
import re
from pathlib import Path

from exoagent_eval_common import CSV_ROOT, clean_report_text, iter_observation_dirs, write_csv


FINAL_REPORT_BLOCK_RE = re.compile(
    r"<<<FINAL_REPORT_BEGIN>>>\s*(.*?)\s*<<<FINAL_REPORT_END>>>",
    re.DOTALL,
)


def tagged_report_from_text(text):
    matches = FINAL_REPORT_BLOCK_RE.findall(text)
    if matches:
        return clean_report_text(matches[-1])
    return ""


def report_from_logs(observation_dir):
    best_report = ""
    for log_path in sorted((observation_dir / "logs").glob("*.txt")):
        text = log_path.read_text(encoding="utf-8", errors="replace")
        report = tagged_report_from_text(text)
        if report:
            best_report = report
    return best_report


def main():
    parser = argparse.ArgumentParser(description="Extract generated reports for every ExoAgent observation/log folder.")
    parser.add_argument("--output", type=Path, default=CSV_ROOT / "reports.csv")
    args = parser.parse_args()

    rows = []
    for item in iter_observation_dirs():
        report = report_from_logs(item["observation_dir"])
        rows.append({
            "observation_path": item["observation_path"],
            "model": item["model"],
            "agent_framework": item["agent_framework"],
            "report": report,
        })

    write_csv(args.output, rows, ["observation_path", "model", "agent_framework", "report"])
    print(f"Wrote {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
