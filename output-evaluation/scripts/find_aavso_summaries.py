#!/usr/bin/env python3
import argparse
from pathlib import Path

from exoagent_eval_common import CSV_ROOT, iter_observation_dirs, parse_aavso_headers, relpath, write_csv


def select_aavso(paths):
    if not paths:
        return None
    def score(path):
        name_score = 1 if "EXOTIC_output" not in str(path) else 0
        return (name_score, path.stat().st_mtime, str(path))
    return sorted(paths, key=score)[-1]


def main():
    parser = argparse.ArgumentParser(description="Find the AAVSO summary for every ExoAgent observation/log folder.")
    parser.add_argument("--output", type=Path, default=CSV_ROOT / "aavso_summaries.csv")
    args = parser.parse_args()

    rows = []
    for item in iter_observation_dirs():
        paths = sorted(item["observation_dir"].glob("output/**/AAVSO_*.txt"))
        selected = select_aavso(paths)
        headers = parse_aavso_headers(selected) if selected else {}
        rows.append({
            "observation_path": item["observation_path"],
            "observation": item["observation"],
            "model": item["model"],
            "source_class": item["source_class"],
            "agent_framework": item["agent_framework"],
            "aavso_summary_path": relpath(selected) if selected else "",
            "aavso_summary_count": len(paths),
            "all_aavso_summary_paths": "|".join(relpath(path) for path in paths),
            "has_aavso_summary": "yes" if selected else "no",
            "aavso_exoplanet_name": headers.get("EXOPLANET_NAME", "") if headers else "",
            "aavso_star_name": headers.get("STAR_NAME", "") if headers else "",
        })

    write_csv(args.output, rows, [
        "observation_path",
        "observation",
        "model",
        "source_class",
        "agent_framework",
        "aavso_summary_path",
        "aavso_summary_count",
        "all_aavso_summary_paths",
        "has_aavso_summary",
        "aavso_exoplanet_name",
        "aavso_star_name",
    ])
    print(f"Wrote {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
