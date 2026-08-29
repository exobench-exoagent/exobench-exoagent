import csv
import json
import math
import os
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = Path(os.getenv("EXOBENCH_RESULTS_ROOT", PROJECT_ROOT / "ANONYMIZED_RESULTS"))
OUTPUT_ROOT = PROJECT_ROOT / "output-evaluation"
CSV_ROOT = OUTPUT_ROOT / "csv"
MARKDOWN_ROOT = OUTPUT_ROOT / "markdown"

MODEL_RUNS = [
    ("gpt-5.6-luna", "gpt-5.6-luna", "OpenAI-compatible"),
    (
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "OpenRouter",
    ),
    ("qwen/qwen3.6-35b-a3b", "qwen/qwen3.6-35b-a3b", "OpenRouter"),
]
MODELS = [(model, source_class) for _, model, source_class in MODEL_RUNS]

AGENT_FRAMEWORK_RUNS = [
    ("ReAct", "ReAct"),
    ("ReAct+Skills", "ReAct+Skills"),
]
AGENT_FRAMEWORKS = [agent_framework for _, agent_framework in AGENT_FRAMEWORK_RUNS]

EPHEMERIS_FIELDS = [
    "mid_transit_time",
    "orbital_period",
    "transit_depth",
    "transit_duration",
]


def relpath(path):
    path = Path(path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


def norm_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def observation_name_from_folder(folder_name):
    name = str(folder_name)
    framework_suffixes = [
        "ReAct",
        "ReAct+Skills",
        "ReAct_Skills",
    ]
    suffix_pattern = "|".join(re.escape(suffix) for suffix in framework_suffixes)
    name = re.sub(rf"\s*\((?:{suffix_pattern})\)\s*$", "", name)
    name = re.sub(rf"_(?:{suffix_pattern})\s*$", "", name)
    name = name.replace("_", " ")
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^TRES-", "TrES-", name)
    name = re.sub(r"\bTRES-", "TrES-", name)
    name = re.sub(r"\bTRES\b", "TrES", name)
    name = re.sub(r"\bTrES-(\d+)b\b", r"TrES-\1 b", name)
    return name


def iter_observation_dirs():
    for model_folder, model, source_class in MODEL_RUNS:
        for framework_folder, agent_framework in AGENT_FRAMEWORK_RUNS:
            framework_root = RESULTS_ROOT / model_folder / framework_folder
            if not framework_root.exists():
                continue
            for observation_dir in sorted(path for path in framework_root.iterdir() if path.is_dir()):
                yield {
                    "observation_path": relpath(observation_dir),
                    "observation_dir": observation_dir,
                    "observation": observation_name_from_folder(observation_dir.name),
                    "model": model,
                    "source_class": source_class,
                    "agent_framework": agent_framework,
                }


def read_json_with_optional_tags(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    match = re.search(r"<<<[A-Z_]+_BEGIN>>>\s*(.*?)\s*<<<[A-Z_]+_END>>>", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    return json.loads(text)


def tagged_blocks_from_text(text, block_names):
    blocks = []
    for block_name in block_names:
        patterns = [
            rf"<<<{block_name}_BEGIN>>>\s*(.*?)\s*<<<{block_name}_END>>>",
            rf"<<<BEGIN_{block_name}>>>\s*(.*?)\s*<<<END_{block_name}>>>",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.DOTALL):
                blocks.append((match.start(), block_name, match.group(1).strip()))
    return sorted(blocks, key=lambda item: item[0])


def emitted_blocks(observation_dir, block_names):
    blocks = []
    log_dir = Path(observation_dir) / "logs"
    for log_path in sorted(log_dir.glob("*.txt")):
        text = log_path.read_text(encoding="utf-8", errors="replace")
        blocks.extend((log_path, name, body) for _, name, body in tagged_blocks_from_text(text, block_names))
    for log_path in sorted(log_dir.glob("*.jsonl")):
        with open(log_path, encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = record.get("text") if isinstance(record, dict) else None
                if not isinstance(text, str):
                    continue
                blocks.extend((log_path, name, body) for _, name, body in tagged_blocks_from_text(text, block_names))
    return blocks


def last_emitted_block(observation_dir, block_names):
    blocks = emitted_blocks(observation_dir, block_names)
    return blocks[-1][2] if blocks else ""


def last_emitted_json(observation_dir, block_names):
    for _, _, body in reversed(emitted_blocks(observation_dir, block_names)):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            continue
    return {}


def write_csv(path, rows, fieldnames):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def to_float(value):
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def mean(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def median(values):
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def percent_difference(observed, expected):
    observed = to_float(observed)
    expected = to_float(expected)
    if observed is None or expected in (None, 0):
        return None
    return abs(observed - expected) / abs(expected) * 100


def parse_aavso_headers(path):
    headers = {}
    with open(path, encoding="utf-8", errors="replace") as file:
        for line in file:
            if not line.startswith("#"):
                break
            line = line.rstrip("\n")
            if "=" not in line:
                continue
            key, value = line[1:].split("=", 1)
            headers[key] = value
    for key in ("PRIORS-XC", "RESULTS-XC", "FILTER-XC", "COMP_STAR-XC"):
        if key in headers:
            try:
                headers[key] = json.loads(headers[key])
            except json.JSONDecodeError:
                headers[key] = {}
    return headers


def xc_float(mapping, key, subkey="value"):
    entry = mapping.get(key) if isinstance(mapping, dict) else None
    if isinstance(entry, dict):
        return to_float(entry.get(subkey))
    return None


def parse_number_pair(raw, scale=1.0):
    if raw is None:
        return None, None
    numbers = re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", str(raw))
    value = to_float(numbers[0]) * scale if numbers else None
    uncertainty = to_float(numbers[1]) * scale if len(numbers) > 1 else None
    return value, uncertainty


def final_params_for(output_dir):
    candidates = sorted(Path(output_dir).glob("temp/FinalParams_*.json"))
    if not candidates:
        return {}
    try:
        data = json.loads(candidates[0].read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return data.get("FINAL PLANETARY PARAMETERS") or {}


def aavso_ephemerides(aavso_path):
    headers = parse_aavso_headers(aavso_path)
    priors = headers.get("PRIORS-XC") or {}
    results = headers.get("RESULTS-XC") or {}
    final_params = final_params_for(Path(aavso_path).parent)

    depth, depth_unc = parse_number_pair(final_params.get("Transit depth (Rp/Rs)^2"), scale=0.01)
    if depth is None:
        rp = xc_float(results, "Rp/R*")
        rp_unc = xc_float(results, "Rp/R*", "uncertainty")
        if rp is not None:
            depth = rp * rp
            depth_unc = abs(2 * rp * rp_unc) if rp_unc is not None else None

    duration = xc_float(results, "Duration")
    duration_unc = xc_float(results, "Duration", "uncertainty")
    if duration is None:
        duration, duration_unc = parse_number_pair(final_params.get("Transit Duration (day)"))

    rms, _ = parse_number_pair(
        final_params.get("Scatter in the residuals of the lightcurve fit is"),
        scale=0.01,
    )
    return {
        "mid_transit_time": xc_float(results, "Tc"),
        "mid_transit_time_uncertainty": xc_float(results, "Tc", "uncertainty"),
        "orbital_period": xc_float(priors, "Period"),
        "orbital_period_uncertainty": xc_float(priors, "Period", "uncertainty"),
        "transit_depth": depth,
        "transit_depth_uncertainty": depth_unc,
        "transit_duration": duration,
        "transit_duration_uncertainty": duration_unc,
        "rms": rms,
        "rp_over_rstar": xc_float(results, "Rp/R*"),
        "a_over_rstar": xc_float(priors, "a/R*"),
        "headers": headers,
    }


def clean_report_text(text):
    text = str(text or "")
    if "\\n" in text or "\\\"" in text or "\\t" in text:
        try:
            text = json.loads(f'"{text}"')
        except json.JSONDecodeError:
            text = (
                text
                .replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\/", "/")
            )
    text = re.sub(r"<<<(?:FINAL_)?REPORT_BEGIN>>>\s*", "", text)
    text = re.sub(r"\s*<<<(?:FINAL_)?REPORT_END>>>", "", text)
    text = re.sub(r"<<<BEGIN_REPORT>>>\s*", "", text)
    text = re.sub(r"\s*<<<END_REPORT>>>", "", text)
    return text.strip()
