#!/usr/bin/env python3
import csv
import json
import math
import re
from pathlib import Path

from exoagent_eval_common import (
    AGENT_FRAMEWORKS,
    CSV_ROOT,
    EPHEMERIS_FIELDS,
    MARKDOWN_ROOT,
    MODELS,
    PROJECT_ROOT,
    aavso_ephemerides,
    last_emitted_block,
    last_emitted_json,
    mean,
    median,
    norm_key,
    percent_difference,
    read_csv,
    read_json_with_optional_tags,
    relpath,
    to_float,
    write_csv,
)


def load_truth():
    by_planet = {}
    by_filename = {}
    for path in (PROJECT_ROOT / "exoagent_lco_debug").glob("*/frame_coordinates.csv"):
        planet = path.parent.name.split("_20", 1)[0].replace("_", " ")
        with open(path, newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            for row in reader:
                truth = {
                    "target_x": to_float(row.get("target_x_e91")),
                    "target_y": to_float(row.get("target_y_e91")),
                    "truth_source": relpath(path),
                }
                by_planet.setdefault(norm_key(planet), truth)
                for key in ("science_filename", "e91_filename"):
                    if row.get(key):
                        by_filename[Path(row[key]).name] = truth
                break

    mo_path = PROJECT_ROOT / "exoagent_mo_debug" / "target_pixel_coordinates.csv"
    if mo_path.exists():
        with open(mo_path, newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            for row in reader:
                truth = {
                    "target_x": to_float(row.get("target_x_px_fits_1_indexed")),
                    "target_y": to_float(row.get("target_y_px_fits_1_indexed")),
                    "truth_source": relpath(mo_path),
                }
                by_planet[norm_key(row.get("target_folder"))] = truth
                if row.get("fits_file"):
                    by_filename[Path(row["fits_file"]).name] = truth
    return by_planet, by_filename


def parse_first_image(notes):
    match = re.search(r"first image=([^;]+)", notes or "")
    return Path(match.group(1).strip()).name if match else ""


def selected_inits(observation_dir, aavso_path):
    if aavso_path:
        candidate = Path(aavso_path).parent / "inits.json"
        if candidate.exists():
            return candidate
    candidates = sorted(Path(observation_dir).glob("output/**/inits.json"))
    return candidates[0] if candidates else None


def load_inits(inits_path):
    if not inits_path:
        return {}
    try:
        return json.loads(Path(inits_path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def xy_from_target_mapping(target):
    if not isinstance(target, dict):
        return None, None
    x = to_float(target.get("pixel_x", target.get("x")))
    y = to_float(target.get("pixel_y", target.get("y")))
    return x, y


def target_xy_from_emitted_text(text):
    patterns = [
        r"pixel coordinates used by EXOTIC[^[(]*(?:\*\*)?\[\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*\]",
        r"target(?:\s+star)?(?:\s+pixel)?\s+coordinates[^[(]*(?:\*\*)?[\[(]\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*[\])]",
        r"target(?:\s+star)?[^.\n]*?\bpixel\s*[\[(]\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*[\])]",
        r"target(?:\s+star)?[^.\n]*?\bat\s*[\[(]\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*[\])]",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "", re.IGNORECASE)
        if match:
            return to_float(match.group(1)), to_float(match.group(2))
    return None, None


def target_xy_from_emitted_block(observation_dir):
    block_names = ["TARGET_COMPARISON_STAR", "STAR_IDENTIFICATION"]
    data = last_emitted_json(observation_dir, block_names)
    target = data.get("target_star") if isinstance(data, dict) else None
    if target is None and isinstance(data, dict):
        target = data.get("target")
    x, y = xy_from_target_mapping(target)
    if x is not None and y is not None:
        return x, y
    return target_xy_from_emitted_text(last_emitted_block(observation_dir, block_names))


def target_xy_from_star_json(observation_dir):
    for pattern in ("output/**/final_star_id.json", "output/**/star_identification.json", "scratch/**/final_star_id.json"):
        for path in sorted(Path(observation_dir).glob(pattern)):
            try:
                data = read_json_with_optional_tags(path)
            except Exception:
                continue
            target = data.get("target_star") if isinstance(data, dict) else None
            x, y = xy_from_target_mapping(target)
            if x is not None and y is not None:
                return x, y
    return None, None


def observed_target_xy(observation_dir, inits):
    x, y = target_xy_from_emitted_block(observation_dir)
    if x is not None and y is not None:
        return x, y

    user_info = inits.get("user_info") or {}
    target = user_info.get("Target Star X & Y Pixel")
    if isinstance(target, list) and len(target) >= 2:
        x = to_float(target[0])
        y = to_float(target[1])
        if x is not None and y is not None:
            return x, y
    return target_xy_from_star_json(observation_dir)


def target_error(observation, first_image, observed_x, observed_y, truth_by_planet, truth_by_filename):
    truth = truth_by_filename.get(first_image) if first_image else None
    truth = truth or truth_by_planet.get(norm_key(observation)) or {}
    truth_x = to_float(truth.get("target_x"))
    truth_y = to_float(truth.get("target_y"))
    if None in (observed_x, observed_y, truth_x, truth_y):
        return None, truth
    return math.hypot(observed_x - truth_x, observed_y - truth_y), truth


def block_json_from_text(text, block_name):
    pattern = re.compile(
        rf"<<<{block_name}_BEGIN>>>\s*(.*?)\s*<<<{block_name}_END>>>",
        re.DOTALL,
    )
    matches = pattern.findall(text)
    if not matches:
        return None
    body = matches[-1].strip()
    try:
        return json.loads(body)
    except Exception:
        return None


def parse_number(value):
    if value is None:
        return None
    text = str(value).translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+"))
    sci = re.search(
        r"(?P<base>[-+]?\d+(?:\.\d+)?)\s*(?:x|×)\s*10(?P<exp>[-+]?\d+)",
        text,
        re.IGNORECASE,
    )
    if sci:
        return float(sci.group("base")) * (10 ** int(sci.group("exp")))
    match = re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text)
    return to_float(match.group(0)) if match else None


def parse_labeled_value(text, labels, scale=1.0):
    for label in labels:
        match = re.search(
            rf"{label}[^0-9+\-]*(?P<value>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?:\s*(?:x|×)\s*10[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺+\-0-9]+)?)",
            text,
            re.IGNORECASE,
        )
        if match:
            value = parse_number(match.group("value"))
            return value * scale if value is not None else None
    return None


def parse_ephemerides_from_prose(text):
    return {
        "mid_transit_time": {"value": parse_labeled_value(text, [
            r"Fitted mid-transit time",
            r"Final EXOTIC fitted mid-transit time",
            r"Final fitted mid-transit time",
            r"fitted Tc",
        ])},
        "orbital_period": {"value": parse_labeled_value(text, [r"Published orbital period prior", r"orbital period", r"Period"])},
        "transit_depth": {"value": parse_labeled_value(text, [r"Fitted transit depth", r"transit depth"], scale=0.01)},
        "transit_duration": {"value": parse_labeled_value(text, [r"Fitted duration", r"transit duration", r"Duration"])},
    }


def load_model_ephemerides(observation_dir):
    block_names = ["EPHEMERIDES"]
    emitted = last_emitted_json(observation_dir, block_names)
    if isinstance(emitted, dict) and any(field in emitted for field in EPHEMERIS_FIELDS):
        return emitted
    emitted_text = last_emitted_block(observation_dir, block_names)
    emitted_from_prose = parse_ephemerides_from_prose(emitted_text)
    if any(ephem_value(emitted_from_prose, field) is not None for field in EPHEMERIS_FIELDS):
        return emitted_from_prose

    for pattern in ("output/**/final_ephemerides.json", "output/**/ephemerides.json"):
        for path in sorted(Path(observation_dir).glob(pattern)):
            try:
                data = read_json_with_optional_tags(path)
            except Exception:
                continue
            if isinstance(data, dict) and any(field in data for field in EPHEMERIS_FIELDS):
                return data

    best = None
    for log_path in sorted((Path(observation_dir) / "logs").glob("*.txt")):
        text = log_path.read_text(encoding="utf-8", errors="replace")
        data = block_json_from_text(text, "EPHEMERIDES")
        if data:
            best = data
    return best or {}


def ephem_value(model_ephemerides, field):
    value = model_ephemerides.get(field)
    if isinstance(value, dict):
        return to_float(value.get("value"))
    return to_float(value)


def extraction_metrics(model_ephemerides, observed):
    tolerances = {
        "mid_transit_time": ("absolute", 0.01),
        "orbital_period": ("absolute", 0.0001),
        "transit_depth": ("percent", 1.0),
        "transit_duration": ("percent", 1.0),
    }
    diffs = []
    matches = []
    for field in EPHEMERIS_FIELDS:
        model_value = ephem_value(model_ephemerides, field)
        observed_value = to_float(observed.get(field))
        diff = percent_difference(model_value, observed_value)
        if diff is not None:
            diffs.append(diff)
        if model_value is None or observed_value is None:
            continue
        mode, tolerance = tolerances[field]
        if mode == "absolute":
            matches.append(1.0 if abs(model_value - observed_value) <= tolerance else 0.0)
        else:
            matches.append(1.0 if diff is not None and diff <= tolerance else 0.0)
    return mean(matches), mean(diffs)


def propagated_midpoint(nasa, observed_tc):
    ref = to_float(nasa.get("mid_transit_time_bjd"))
    period = to_float(nasa.get("orbital_period_days"))
    observed_tc = to_float(observed_tc)
    if None in (ref, period, observed_tc) or period == 0:
        return None
    epoch = round((observed_tc - ref) / period)
    return ref + epoch * period


def nasa_accuracy_metrics(observed, nasa):
    expected = {
        "mid_transit_time": propagated_midpoint(nasa, observed.get("mid_transit_time")),
        "orbital_period": to_float(nasa.get("orbital_period_days")),
        "transit_depth": to_float(nasa.get("transit_depth_fraction")),
        "transit_duration": to_float(nasa.get("transit_duration_days")),
    }
    diffs = [
        percent_difference(observed.get(field), expected_value)
        for field, expected_value in expected.items()
    ]
    return mean(diffs)


def exotic_generation_metric(observed, nasa):
    diffs = [
        percent_difference(observed.get("rp_over_rstar"), nasa.get("radius_ratio")),
        percent_difference(observed.get("a_over_rstar"), nasa.get("a_over_rstar")),
    ]
    return mean(diffs)


def word_count(report):
    return len(re.findall(r"\b\w+\b", report or ""))


def stats_for(rows):
    submitted = [row for row in rows if row["has_aavso_summary"] == "yes"]
    return {
        "attempted_tasks": len(rows),
        "submitted_reports": len(submitted),
        "workflow_completion_rate_percent": len(submitted) / len(rows) * 100 if rows else None,
        "mean_target_star_error_px": mean([to_float(row["target_star_error_px"]) for row in submitted]),
        "median_target_star_error_px": median([to_float(row["target_star_error_px"]) for row in submitted]),
        "ephemerides_exact_match_percent": (mean([to_float(row["ephemerides_exact_match_fraction"]) for row in submitted]) or 0) * 100,
        "mean_ephemerides_extraction_difference_percent": mean([to_float(row["ephemerides_extraction_difference_percent"]) for row in submitted]),
        "mean_nasa_ephemerides_difference_percent": mean([to_float(row["nasa_ephemerides_difference_percent"]) for row in submitted]),
        "mean_exotic_generation_difference_percent": mean([to_float(row["exotic_generation_difference_percent"]) for row in submitted]),
        "mean_time_per_submitted_report_min": mean([to_float(row["time_taken_minutes"]) for row in submitted]),
        "mean_report_length_words": mean([to_float(row["report_word_count"]) for row in submitted]),
    }


def fmt(value, digits=3):
    value = to_float(value)
    return "" if value is None else f"{value:.{digits}f}"


def fmt1(value):
    return fmt(value, 1)


def markdown_table(rows, headers):
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] + ["---:"] * (len(headers) - 1)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(header, "")) for header in headers) + " |")
    return "\n".join(lines)


def timing_minutes(observation_dir):
    best = None
    for log_path in sorted((Path(observation_dir) / "logs").glob("*.txt")):
        text = log_path.read_text(encoding="utf-8", errors="replace")
        start = re.search(r"^Started:\s*(.+)$", text, re.MULTILINE)
        logged = re.findall(r"^Logged:\s*(.+)$", text, re.MULTILINE)
        if not start or not logged:
            continue
        from datetime import datetime
        try:
            seconds = (datetime.fromisoformat(logged[-1].strip()) - datetime.fromisoformat(start.group(1).strip())).total_seconds()
        except ValueError:
            continue
        best = seconds / 60
    return best


def csv_path(value):
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def main():
    aavso_rows = read_csv(CSV_ROOT / "aavso_summaries.csv")
    report_rows = read_csv(CSV_ROOT / "reports.csv")
    nasa_rows = read_csv(CSV_ROOT / "nasa_ephemerides.csv")
    reports_by_path = {row["observation_path"]: row["report"] for row in report_rows}
    nasa_by_observation = {row["observation"]: row for row in nasa_rows}
    truth_by_planet, truth_by_filename = load_truth()

    run_rows = []
    for row in aavso_rows:
        observation_dir = csv_path(row["observation_path"])
        aavso_path = csv_path(row["aavso_summary_path"])
        inits_path = selected_inits(observation_dir, aavso_path)
        inits = load_inits(inits_path)
        user_info = inits.get("user_info") or {}
        first_image = parse_first_image(user_info.get("Observing Notes"))
        obs_x, obs_y = observed_target_xy(observation_dir, inits)
        target_dist, truth = target_error(row["observation"], first_image, obs_x, obs_y, truth_by_planet, truth_by_filename)

        observed_ephem = aavso_ephemerides(aavso_path) if aavso_path and aavso_path.exists() else {}
        report = reports_by_path.get(row["observation_path"], "")
        model_ephem = load_model_ephemerides(observation_dir) if aavso_path else {}
        extraction_fraction, extraction_diff = extraction_metrics(model_ephem, observed_ephem)
        nasa = nasa_by_observation.get(row["observation"], {})
        nasa_diff = nasa_accuracy_metrics(observed_ephem, nasa) if observed_ephem else None
        exotic_diff = exotic_generation_metric(observed_ephem, nasa) if observed_ephem else None

        run_rows.append({
            "observation_path": row["observation_path"],
            "observation": row["observation"],
            "model": row["model"],
            "source_class": row["source_class"],
            "agent_framework": row["agent_framework"],
            "has_aavso_summary": row["has_aavso_summary"],
            "aavso_summary_path": row["aavso_summary_path"],
            "target_star_error_px": fmt(target_dist),
            "target_x_observed": fmt(obs_x),
            "target_y_observed": fmt(obs_y),
            "target_x_truth": fmt(truth.get("target_x")),
            "target_y_truth": fmt(truth.get("target_y")),
            "ephemerides_exact_match_fraction": fmt(extraction_fraction),
            "ephemerides_extraction_difference_percent": fmt(extraction_diff),
            "nasa_ephemerides_difference_percent": fmt(nasa_diff),
            "exotic_generation_difference_percent": fmt(exotic_diff),
            "time_taken_minutes": fmt(timing_minutes(observation_dir), 3),
            "report_word_count": word_count(report),
        })

    run_fields = list(run_rows[0].keys())
    write_csv(CSV_ROOT / "evaluation_runs.csv", run_rows, run_fields)
    write_csv(CSV_ROOT / "run_statistics.csv", run_rows, run_fields)

    model_rows = []
    for model, source_class in MODELS:
        stats = stats_for([row for row in run_rows if row["model"] == model])
        model_rows.append({
            "Model": model,
            "Source Class": source_class,
            "Attempted Tasks": stats["attempted_tasks"],
            "Submitted Reports": stats["submitted_reports"],
            "Workflow Completion Rate (%)": fmt1(stats["workflow_completion_rate_percent"]),
            "Mean Target-Star Error (px)": fmt(stats["mean_target_star_error_px"]),
            "Median Target-Star Error (px)": fmt(stats["median_target_star_error_px"]),
            "Ephemerides Exact-Match (%)": fmt1(stats["ephemerides_exact_match_percent"]),
            "Mean Ephemerides Extraction Difference (%)": fmt(stats["mean_ephemerides_extraction_difference_percent"]),
            "Mean NASA Ephemerides Difference (%)": fmt(stats["mean_nasa_ephemerides_difference_percent"]),
            "Mean EXOTIC Generation Difference (%)": fmt(stats["mean_exotic_generation_difference_percent"]),
            "Mean Time Per Submitted Report (min)": fmt(stats["mean_time_per_submitted_report_min"], 2),
            "Mean Report Length (words)": fmt1(stats["mean_report_length_words"]),
        })

    framework_rows = []
    for agent_framework in AGENT_FRAMEWORKS:
        stats = stats_for([row for row in run_rows if row["agent_framework"] == agent_framework])
        framework_rows.append({
            "Agent Framework": agent_framework,
            "Attempted Tasks": stats["attempted_tasks"],
            "Submitted Reports": stats["submitted_reports"],
            "Workflow Completion Rate (%)": fmt1(stats["workflow_completion_rate_percent"]),
            "Mean Target-Star Error (px)": fmt(stats["mean_target_star_error_px"]),
            "Median Target-Star Error (px)": fmt(stats["median_target_star_error_px"]),
            "Ephemerides Exact-Match (%)": fmt1(stats["ephemerides_exact_match_percent"]),
            "Mean Ephemerides Extraction Difference (%)": fmt(stats["mean_ephemerides_extraction_difference_percent"]),
            "Mean NASA Ephemerides Difference (%)": fmt(stats["mean_nasa_ephemerides_difference_percent"]),
            "Mean EXOTIC Generation Difference (%)": fmt(stats["mean_exotic_generation_difference_percent"]),
            "Mean Time Per Submitted Report (min)": fmt(stats["mean_time_per_submitted_report_min"], 2),
            "Mean Report Length (words)": fmt1(stats["mean_report_length_words"]),
        })

    combo_rows = []
    for model, source_class in MODELS:
        for agent_framework in AGENT_FRAMEWORKS:
            stats = stats_for([
                row for row in run_rows
                if row["model"] == model and row["agent_framework"] == agent_framework
            ])
            combo_rows.append({
                "Model": model,
                "Source Class": source_class,
                "Agent Framework": agent_framework,
                "Attempted Tasks": stats["attempted_tasks"],
                "Submitted Reports": stats["submitted_reports"],
                "Workflow Completion Rate (%)": fmt1(stats["workflow_completion_rate_percent"]),
                "Mean Target-Star Error (px)": fmt(stats["mean_target_star_error_px"]),
                "Median Target-Star Error (px)": fmt(stats["median_target_star_error_px"]),
                "Ephemerides Exact-Match (%)": fmt1(stats["ephemerides_exact_match_percent"]),
                "Mean Ephemerides Extraction Difference (%)": fmt(stats["mean_ephemerides_extraction_difference_percent"]),
                "Mean NASA Ephemerides Difference (%)": fmt(stats["mean_nasa_ephemerides_difference_percent"]),
                "Mean EXOTIC Generation Difference (%)": fmt(stats["mean_exotic_generation_difference_percent"]),
                "Mean Time Per Submitted Report (min)": fmt(stats["mean_time_per_submitted_report_min"], 2),
                "Mean Report Length (words)": fmt1(stats["mean_report_length_words"]),
            })

    write_csv(CSV_ROOT / "model_comparison.csv", model_rows, list(model_rows[0].keys()))
    write_csv(CSV_ROOT / "agent_framework_comparison.csv", framework_rows, list(framework_rows[0].keys()))
    write_csv(CSV_ROOT / "model_agent_framework_comparison.csv", combo_rows, list(combo_rows[0].keys()))

    model_table = markdown_table(model_rows, list(model_rows[0].keys()))
    framework_table = markdown_table(framework_rows, list(framework_rows[0].keys()))
    combo_table = markdown_table(combo_rows, list(combo_rows[0].keys()))

    luna = next(row for row in model_rows if row["Model"] == "gpt-5.6-luna")
    nemotron = next(row for row in model_rows if row["Model"] == "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free")
    qwen = next(row for row in model_rows if row["Model"] == "qwen/qwen3.6-35b-a3b")
    react = next(row for row in framework_rows if row["Agent Framework"] == "ReAct")
    react_skills = next(row for row in framework_rows if row["Agent Framework"] == "ReAct+Skills")

if __name__ == "__main__":
    main()
