# ExoBench: Multi-Expert Evaluation of Agentic End-to-End Exoplanet Transit Analysis and Scientific Report Generation

ExoBench is a local benchmark and evaluation workspace for autonomous exoplanet transit analysis. It contains ExoAgent runtimes that process FITS observations with EXOTIC, generate scientific reports, preserve run artifacts, scrape or prepare observation datasets, and publish blinded report pages for multi-expert review.

The workspace is organized around one questions: can ExoAgent run an end-to-end exoplanet transit reduction from raw FITS images to a scientific report?

## Repository Structure

```text
exoagent/
  README.md
  app.py
  exoagent-react/
  exoagent-react-skills/
  exoagent-workspaces/
  observation-scraping/
  output-evaluation/
```

`exoagent-react/` is the baseline ReAct implementation. It exposes the complete tool set to the model and loops through reason, tool call, observation, and final answer steps.

`exoagent-react-skills/` is the ReAct + Skills implementation. It uses the same scientific tools, but adds a local skill registry that injects task-specific procedures and restricts the visible tool set to tools allowed by the active skills.

`exoagent-workspaces/` stores generated run outputs. Each run gets its own isolated workspace under `runs/<run_id>/`.

`observation-scraping/` contains dataset acquisition and preparation workflows, including MicroObservatory scraping and target plate solving.

`output-evaluation/report-viewer/` contains the report viewer, static-site builder, artifact linking logic, and reviewer assignment scripts.

## Setup

Each agent variant has its own virtual environment and setup script. Both load a shared `.env` file from the repository root.

Example `.env`:

```text
EXOAGENT_LLM_PROVIDER=openrouter
EXOAGENT_MODEL=tencent/hy3:free
EXOAGENT_LLM_TIMEOUT_SECONDS=180
EXOAGENT_EXOTIC_IDLE_TIMEOUT_SECONDS=300

OPENROUTER_API_KEY=...
OPENAI_API_KEY=...
ASTROMETRY_API_KEY=...

EXOAGENT_INPUT_ROOT=/absolute/path/to/fits-input-parent-folder
EXOAGENT_DEFAULT_INPUT_PATH=/absolute/path/to/default-fits-dataset-folder
EXOAGENT_WORKSPACE_ROOT=/absolute/path/to/exoagent-workspaces
EXOAGENT_EVALUATION_ROOT=/absolute/path/to/output-evaluation
```

Use `EXOAGENT_LLM_PROVIDER=openai` with an OpenAI model ID to use OpenAI's Responses API. Use `openrouter` to call OpenRouter through the OpenAI-compatible client.

Install the baseline ReAct environment:

```bash
cd exoagent-react
sh setup_environment.sh
source .venv/bin/activate
```

Install the ReAct + Skills environment:

```bash
cd exoagent-react-skills
PYTHON_BIN=python3.10 sh setup_environment.sh
source .venv/bin/activate
```

Python 3.10 is preferred for the skills environment because EXOTIC and astronomy dependencies are sensitive to Python/package compatibility.

## Running ExoAgent

Run baseline ReAct:

```bash
cd exoagent-react
.venv/bin/python main.py
```

Run ReAct + Skills:

```bash
cd exoagent-react-skills
.venv/bin/python main.py
```

The entry point prompts for:

1. The FITS directory to process. Press Enter to use `EXOAGENT_DEFAULT_INPUT_PATH`.
2. The exoplanet name, such as `WASP-43 b`.

After startup, the program prints the active run identifiers:

```text
Run ID: <run_id>
Run workspace: <EXOAGENT_WORKSPACE_ROOT>/runs/<run_id>
Read-only input: <fits_input_path>
```

If FITS files are stored in Google Drive, mount Drive first and pass the mounted filesystem path. Do not pass a `drive.google.com` URL; EXOTIC requires ordinary local paths.

For multiple simultaneous runs, give each process a unique `EXOAGENT_RUN_ID`, or omit `EXOAGENT_RUN_ID` and let the runtime generate unique IDs:

```bash
EXOAGENT_RUN_ID=wasp43-react .venv/bin/python main.py
EXOAGENT_RUN_ID=wasp43-skills .venv/bin/python main.py
```

Two active processes must not share the same `EXOAGENT_RUN_ID`, because that intentionally points them at the same output workspace.

## ReAct Flow

The baseline runtime starts in `exoagent-react/main.py`.

The flow is:

1. Read the FITS path and exoplanet name from the terminal.
2. Call `prepare_run_workspace()` from `tools.py`.
3. Build an `LLMAgent` with `RUN_AGENT_PROMPT`, `REACT_PROMPT`, and `TOOLS_LIST`.
4. Send the model a task to process the read-only FITS dataset and produce the final scientific report.
5. Let the model call tools through `TOOL_MAPPING`.
6. Log prompts, responses, tool calls, tool observations, usage metadata, and logprob sidecars when available.
7. Continue until the model emits a final response or the step budget is exhausted.

The ReAct loop is implemented in `llm_agent.py`. It supports two provider paths:

- OpenAI Responses API when `EXOAGENT_LLM_PROVIDER=openai`.
- OpenAI-compatible chat completions when `EXOAGENT_LLM_PROVIDER=openrouter`.

Tool outputs are fed back into the next model call as observations. If a workflow tool returns a nonterminal status such as `needs_vlm_answers`, `needs_planetary_parameters`, `awaiting_input`, or `awaiting_exotic_input`, the agent is instructed to continue rather than finalize early.

## ReAct + Skills Flow

The skills runtime starts in `exoagent-react-skills/main.py`.

It activates these project skills:

- `workspace-safety`
- `exotic-transit-pipeline`
- `visual-star-identification`
- `exotic-output-reporting`

The skill system is implemented in `agent_skills.py`. Each skill declares:

- Name and description.
- Trigger terms.
- Allowed tool names.
- Embedded procedural instructions.

The per-skill markdown files were removed during repository cleanup. Their runtime-critical instructions are now embedded directly in `agent_skills.py`, so ReAct + Skills still receives the same operational guidance without relying on separate markdown files.

The skills runtime adds two controls on top of baseline ReAct:

1. It appends active skill instructions to the system context.
2. It filters available tools to the union allowed by the active skills.

For chat-completions providers, the skills runtime uses a LangGraph reason/observe graph:

```text
START
  -> reason: call the model
  -> observe: execute tool calls
  -> reason: continue with observations
  -> END: final response or step limit
```

For OpenAI Responses, it uses a direct loop with equivalent continuation and tool-observation behavior.

## Scientific Pipeline

The core scientific workflow is `EXOTIC_STANDARD_NOTEBOOK_PIPELINE` in `tools.py`. It is a local, resumable implementation of the EXOTIC Standard notebook flow.

The pipeline performs these stages:

1. Validate the selected FITS directory.
2. Find science files with `.fits`, `.fit`, or `.fits.gz` extensions.
3. Read metadata from the first FITS header, including date, location, elevation, filter, exposure, binning, telescope, observatory, instrument, and WCS-derived pixel scale when available.
4. Detect an existing JSON `inits.json` if exactly one JSON file is present in the input directory.
5. If an existing `inits.json` is found, copy it into the run workspace and rewrite input/output paths to safe workspace paths.
6. If no usable `inits.json` exists, render the first FITS image using `VIEW_FITS_GCOLAB_FORMAT`.
7. Query AAVSO with `QUERY_AAVSO_STAR_CHART` to obtain finder-chart data and an optional chart image.
8. Return `needs_vlm_answers` when target and comparison-star pixel coordinates are needed.
9. Resume with `vlm_answers.target_star_xy` and `vlm_answers.comparison_stars_xy`.
10. Load planetary parameters through EXOTIC/NASA Exoplanet Archive unless parameters are supplied explicitly.
11. Write a workspace-local `inits.json`.
12. Run or resume EXOTIC with `RUN_EXOTIC_UNTIL_IDLE`.
13. Parse generated outputs and produce final scientific report blocks.

The final answer is expected to include these delimited blocks:

```text
<<<STAR_IDENTIFICATION_BEGIN>>>
...
<<<STAR_IDENTIFICATION_END>>>

<<<EPHEMERIDES_BEGIN>>>
...
<<<EPHEMERIDES_END>>>

<<<FINAL_REPORT_BEGIN>>>
...
<<<FINAL_REPORT_END>>>
```

When logprobs are available from the model API, token-level records for these delimited blocks are written beside the run log as `.logprobs.jsonl`.

## EXOTIC Continuation

EXOTIC is expected to run non-interactively from a complete `inits.json`, but it may still ask for CLI input.

`RUN_EXOTIC_UNTIL_IDLE` handles that by starting EXOTIC as a subprocess and reading stdout/stderr until one of these states occurs:

- `completed`: process exited.
- `awaiting_input`: output appears to be waiting at an interactive prompt.
- `idle_timeout`: no output appeared for the configured idle timeout.
- `hard_timeout`: the optional hard runtime limit was reached.
- `error`: startup or runtime wrapper failure.

If the process is waiting for input, the tool keeps the process alive and returns a session ID. The agent should inspect the prompt, recent output tail, `inits.json`, FITS metadata, and generated files, then resume the same process with targeted input.

For example, if EXOTIC asks whether darks, flats, or biases exist and the dataset does not contain those calibration frames, the agent should answer `n` rather than pointing EXOTIC at the science FITS directory.

## Workspaces

ExoAgent separates read-only observations from generated artifacts.

`EXOAGENT_INPUT_ROOT` is the allowed read-only parent for FITS datasets. The selected FITS directory must be under this root.

`EXOAGENT_WORKSPACE_ROOT` is the generated-output parent. Each run writes to:

```text
<EXOAGENT_WORKSPACE_ROOT>/runs/<run_id>/
  output/
  scratch/
  logs/
  tmp/
```

Common run files:

- `output/inits.json`: generated or copied EXOTIC configuration.
- `output/FinalLightCurve_*.png`: final light curve image.
- `output/FinalLightCurve_*.pdf`: final light curve PDF.
- `output/AAVSO_*.txt`: AAVSO submission text.
- `scratch/*.png`: intermediate FITS renders, chart views, or diagnostics.
- `logs/*.txt`: agent run logs.
- `logs/*.logprobs.jsonl`: token-level sidecars for delimited final outputs.
- `exotic.log`: EXOTIC runtime log.

The tool layer blocks write and execute access outside the active run workspace. Read access outside the workspace is allowed only under `EXOAGENT_INPUT_ROOT`.

## Observation Scraping

Observation scraping tools live under `observation-scraping/`.

### MicroObservatory

`observation-scraping/microobservatory_exoplanet_observations/main.py` downloads public MicroObservatory archive data.

It:

1. Requests the MicroObservatory image directory with `SearchFor=ExoPlanets`.
2. Parses the returned HTML table with BeautifulSoup.
3. Groups consecutive rows by observed object.
4. Creates local `planet_<index>/` folders.
5. Downloads each science FITS file into the planet folder.
6. Places calibration rows under that planet folder's `darks/` directory.

Run it with:

```bash
cd observation-scraping/microobservatory_exoplanet_observations
python main.py
```

### Plate Solving

`plate_solve_targets.py` computes target pixel coordinates for MicroObservatory folders using Astrometry.net.

It:

1. Selects a reference science FITS frame per target folder.
2. Builds a median dark when matching dark frames exist.
3. Extracts source lists with `sep`.
4. Submits source lists to Astrometry.net.
5. Converts FITS header target RA/Dec into image pixel coordinates from the solved WCS.
6. Writes CSV and JSON summaries under `_plate_solve_results/`.

Dry run:

```bash
python plate_solve_targets.py --dry-run
```

Full run requires one of:

```text
ASTROMETRY_NET_API_KEY
ASTROMETRY_API_KEY
AN_API_KEY
```

### LCO

`observation-scraping/lco_exoplanet_observations/reduced_LCO_scraper.ipynb` contains the LCO-oriented observation preparation workflow.

## Report Viewer

The report viewer is in `output-evaluation/report-viewer/`.

It supports two modes:

- Flask serving for local inspection.
- Static page generation for public deployment.

Run the Flask app:

```bash
cd output-evaluation/report-viewer
python app.py
```

The app serves:

```text
/<report_id>/
/<report_id>/files/<artifact>
```

Artifact serving is restricted to:

```text
.csv
.json
.pdf
.png
.txt
```

The root route intentionally returns 404. Reviewers should receive direct report URLs.

## Static Report Generation

`build_report_viewer_csv.py` joins report text with run statistics.

Inputs:

```text
output-evaluation/csv/reports.csv
output-evaluation/csv/run_statistics.csv
```

Output:

```text
output-evaluation/csv/report_viewer_reports_with_stats.csv
```

Rows are included only when:

- The run has an AAVSO summary.
- The report text is non-empty.
- The observation path still exists.

Each report receives a stable 12-character ID derived from observation path, model, and agent framework.

`build_static_site.py` converts the report-viewer CSV into static HTML pages. It:

1. Removes previously generated report page folders.
2. Sanitizes model-generated markdown.
3. Removes line-number prefixes when reports came from numbered file reads.
4. Preserves model IDs and agent-framework names in rendered evaluation pages.
5. Normalizes local workspace paths.
6. Finds referenced artifacts in the source run folder.
7. Copies referenced artifacts into `<report_id>/files/`.
8. Rewrites artifact paths into clickable links.
9. Renders MathJax-enabled HTML at `<report_id>/index.html`.
10. Writes `404.html`.
11. Writes `output-evaluation/csv/report_viewer_artifact_manifest.csv`.

Build the static site:

```bash
cd output-evaluation/report-viewer
python build_report_viewer_csv.py
python build_static_site.py
```

## Reviewer Assignment

`assign_reviewers.py` assigns generated reports to reviewers for multi-expert evaluation.

It reads:

```text
output-evaluation/csv/report_viewer_reports_with_stats.csv
```

It writes:

```text
output-evaluation/report-viewer/review_assignments.csv
```

The assignment algorithm:

1. Validates that every candidate report is non-empty and points to an existing run folder.
2. Requires enough reviewer capacity for the requested number of reviews per report.
3. Randomizes report order with a reproducible seed.
4. Assigns each report to distinct reviewers.
5. Balances reviewer load, preferring a load spread of at most one.

Examples:

```bash
cd output-evaluation/report-viewer
python assign_reviewers.py --num-reviewers 12 --max-per-reviewer 15 --reviews-per-report 2
python assign_reviewers.py --reviewers "Reviewer 01,Reviewer 02,Reviewer 03" --max-per-reviewer 20
python assign_reviewers.py --reviewers-csv reviewers.csv --max-per-reviewer 15
```

`build_public_review_assignments.py` converts relative report paths into public Vercel URLs:

```bash
python build_public_review_assignments.py
```

It writes:

```text
output-evaluation/report-viewer/review_assignments_public_urls.csv
```

The public URL base in the script is:

```text
https://exoagent-report-evaluation.vercel.app/
```
