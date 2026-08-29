#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import urlopen

from exoagent_eval_common import CSV_ROOT, iter_observation_dirs, write_csv


NASA_TAP_URL = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"
NASA_COLUMNS = [
    "pl_name",
    "hostname",
    "pl_orbper",
    "pl_orbpererr1",
    "pl_orbpererr2",
    "pl_tranmid",
    "pl_tranmiderr1",
    "pl_tranmiderr2",
    "pl_trandur",
    "pl_trandurerr1",
    "pl_trandurerr2",
    "pl_trandep",
    "pl_trandeperr1",
    "pl_trandeperr2",
    "pl_ratror",
    "pl_ratrorerr1",
    "pl_ratrorerr2",
    "pl_ratdor",
    "pl_ratdorerr1",
    "pl_ratdorerr2",
]


def adql_string(value):
    return str(value).replace("'", "''")


def uncertainty(record, err1_key, err2_key):
    values = []
    for key in (err1_key, err2_key):
        value = record.get(key)
        if value not in (None, ""):
            values.append(abs(float(value)))
    return max(values) if values else None


def unique_observations():
    by_name = {}
    for row in iter_observation_dirs():
        by_name[row["observation"]] = row["observation"]
    return sorted(by_name)


def fetch_rows(observations, timeout):
    quoted = ", ".join(f"'{adql_string(name)}'" for name in observations)
    query = (
        f"select {','.join(NASA_COLUMNS)} from pscomppars "
        f"where pl_name in ({quoted})"
    )
    url = f"{NASA_TAP_URL}?query={quote_plus(query)}&format=json"
    with urlopen(url, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    records = {row["pl_name"]: row for row in data}
    return records, url


def build_rows(observations, records, source_url):
    rows = []
    for observation in observations:
        record = records.get(observation) or {}
        duration_hours = record.get("pl_trandur")
        duration_days = float(duration_hours) / 24 if duration_hours not in (None, "") else None
        duration_unc_hours = uncertainty(record, "pl_trandurerr1", "pl_trandurerr2")
        depth_percent = record.get("pl_trandep")
        depth_fraction = float(depth_percent) / 100 if depth_percent not in (None, "") else None
        rows.append({
            "observation": observation,
            "nasa_planet_name": record.get("pl_name", ""),
            "hostname": record.get("hostname", ""),
            "orbital_period_days": record.get("pl_orbper", ""),
            "orbital_period_uncertainty_days": uncertainty(record, "pl_orbpererr1", "pl_orbpererr2") or "",
            "mid_transit_time_bjd": record.get("pl_tranmid", ""),
            "mid_transit_time_uncertainty_days": uncertainty(record, "pl_tranmiderr1", "pl_tranmiderr2") or "",
            "transit_duration_hours": duration_hours or "",
            "transit_duration_days": duration_days or "",
            "transit_duration_uncertainty_days": (duration_unc_hours / 24 if duration_unc_hours is not None else ""),
            "transit_depth_percent": depth_percent or "",
            "transit_depth_fraction": depth_fraction or "",
            "radius_ratio": record.get("pl_ratror", ""),
            "a_over_rstar": record.get("pl_ratdor", ""),
            "source_url": source_url,
            "found": "yes" if record else "no",
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description="Extract NASA Exoplanet Archive ephemerides for the 20 ExoAgent observations.")
    parser.add_argument("--output", type=Path, default=CSV_ROOT / "nasa_ephemerides.csv")
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args()

    observations = unique_observations()
    records, source_url = fetch_rows(observations, args.timeout)
    rows = build_rows(observations, records, source_url)
    write_csv(args.output, rows, [
        "observation",
        "nasa_planet_name",
        "hostname",
        "orbital_period_days",
        "orbital_period_uncertainty_days",
        "mid_transit_time_bjd",
        "mid_transit_time_uncertainty_days",
        "transit_duration_hours",
        "transit_duration_days",
        "transit_duration_uncertainty_days",
        "transit_depth_percent",
        "transit_depth_fraction",
        "radius_ratio",
        "a_over_rstar",
        "source_url",
        "found",
    ])
    print(f"Wrote {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
