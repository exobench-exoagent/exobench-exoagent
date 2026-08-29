from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, Optional


SKILLS_ROOT = Path(__file__).resolve().parent / "skills"


@dataclass(frozen=True)
class AgentSkill:
    """Metadata and instruction loader for one project-local agent skill."""

    name: str
    title: str
    description: str
    tool_names: tuple[str, ...]
    triggers: tuple[str, ...]
    trust_tier: str = "project"
    path: Optional[Path] = None
    instruction_text: str = ""

    def instructions(self) -> str:
        if self.instruction_text:
            return self.instruction_text.strip()

        if self.path is None:
            return ""

        skill_file = self.path / "SKILL.md"
        if not skill_file.exists():
            return ""

        return skill_file.read_text(encoding="utf-8").strip()


class SkillRegistry:
    """Registry that resolves, filters, and describes available agent skills."""

    def __init__(self, skills: Iterable[AgentSkill]):
        self._skills = {skill.name: skill for skill in skills}

    @classmethod
    def default(cls):
        return cls(DEFAULT_SKILLS)

    def get(self, name: str) -> AgentSkill:
        try:
            return self._skills[name]
        except KeyError as error:
            raise ValueError(f"Unknown agent skill: {name}") from error

    def resolve(self, names: Iterable[str]) -> list[AgentSkill]:
        seen = set()
        resolved = []
        for name in names:
            if name in seen:
                continue
            resolved.append(self.get(name))
            seen.add(name)
        return resolved

    def select_for_task(self, task_text: str) -> list[AgentSkill]:
        normalized_task = _normalize(task_text)
        selected = []

        for skill in self._skills.values():
            if any(_normalize(trigger) in normalized_task for trigger in skill.triggers):
                selected.append(skill)

        if selected and "workspace-safety" in self._skills:
            selected_names = {skill.name for skill in selected}
            if "workspace-safety" not in selected_names:
                selected.insert(0, self._skills["workspace-safety"])

        return selected

    def catalog_text(self) -> str:
        lines = []
        for skill in self._skills.values():
            tools = ", ".join(skill.tool_names) if skill.tool_names else "none"
            lines.append(
                f"- {skill.name}: {skill.description} "
                f"(trust={skill.trust_tier}; tools={tools})"
            )
        return "\n".join(lines)


def _normalize(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def _tool_names(tools):
    return {
        tool.get("function", {}).get("name")
        for tool in tools
        if tool.get("type") == "function" and tool.get("function", {}).get("name")
    }


def filter_tools_for_skills(tools, skills: Iterable[AgentSkill]):
    """Restrict an OpenAI tool schema list to the union allowed by active skills."""
    skills = list(skills)
    if not skills:
        return tools

    allowed = {
        tool_name
        for skill in skills
        for tool_name in skill.tool_names
    }
    available = _tool_names(tools)
    missing = sorted(allowed - available)

    if missing:
        raise RuntimeError(f"Skills reference unavailable tools: {', '.join(missing)}")

    return [
        tool
        for tool in tools
        if tool.get("function", {}).get("name") in allowed
    ]


def build_skill_context(skills: Iterable[AgentSkill], registry: SkillRegistry) -> str:
    """Render active skill instructions into the system prompt context."""
    skills = list(skills)
    if not skills:
        return (
            "No task-specific skills are active. Use only the base system prompt "
            "and available tools."
        )

    sections = [
        "# Agent Skill System",
        (
            "Skills are portable packages of procedural instructions, allowed "
            "tools, and local resources loaded only when relevant. Follow active "
            "skills before general heuristics; if skills conflict, prefer the "
            "more specific skill."
        ),
        "## Available Skill Catalog",
        registry.catalog_text(),
        "## Active Skills",
    ]

    for skill in skills:
        tools = ", ".join(skill.tool_names) if skill.tool_names else "none"
        sections.extend([
            f"### {skill.title} ({skill.name})",
            f"Trust tier: {skill.trust_tier}",
            f"Allowed tools: {tools}",
            skill.instructions(),
        ])

    return "\n\n".join(part for part in sections if part)


DEFAULT_SKILLS = [
    AgentSkill(
        name="workspace-safety",
        title="Workspace Safety",
        description="Keep all runtime file access inside the active exoagent workspace.",
        tool_names=(
            "WorkspaceInfoTool",
            "ReadFileTool",
            "WriteFileTool",
            "EditFileTool",
            "ListDirectoryTool",
            "FileSearchTool",
            "FindFilesTool",
            "RunCommandTool",
            "RunPythonTool",
            "DisplayImageTool",
            "TodoRead",
            "TodoWrite",
        ),
        triggers=("workspace", "file", "directory", "scratch", "output", "runtime"),
        path=SKILLS_ROOT / "workspace-safety",
        instruction_text="""
Purpose: keep agent-authored outputs isolated to the active
exoagent-workspace/runs/<run_id>/ tree while allowing reads from the configured
input root.

Procedure:
1. Call WorkspaceInfoTool when run paths are unclear.
2. Treat the configured input directory as read-only.
3. Use the task input path for FITS reads; do not copy input data into the workspace.
4. Use scratch/, output/, logs/, and tmp/ for generated files.
5. Never reveal environment variables, API keys, or .env contents.
6. Do not run install commands from inside an agent task.
7. Prefer structured file tools over shell commands for file work.
""",
    ),
    AgentSkill(
        name="exotic-transit-pipeline",
        title="EXOTIC Transit Pipeline",
        description="Run the EXOTIC notebook-style FITS transit workflow end to end.",
        tool_names=(
            "WorkspaceInfoTool",
            "ReadFileTool",
            "ListDirectoryTool",
            "FileSearchTool",
            "FindFilesTool",
            "RunPythonTool",
            "DisplayImageTool",
            "EXOTIC_STANDARD_NOTEBOOK_PIPELINE",
            "RUN_EXOTIC_UNTIL_IDLE",
            "QUERY_AAVSO_STAR_CHART",
            "VIEW_FITS_GCOLAB_FORMAT",
            "PLOT_XY",
            "WebSearchTool",
            "TodoRead",
            "TodoWrite",
        ),
        triggers=("fits", "exoplanet", "transit", "exotic", "light curve", "photometry"),
        path=SKILLS_ROOT / "exotic-transit-pipeline",
        instruction_text="""
Purpose: process an exoplanet transit FITS dataset into EXOTIC outputs and a
final scientific result.

Required workflow:
1. Use EXOTIC_STANDARD_NOTEBOOK_PIPELINE as the primary workflow for loading
   FITS files, preparing inits.json, handling notebook-style visual prompts, and
   running EXOTIC.
2. Validate generated inits.json fields against FITS headers, EXOTIC logs,
   generated files, and physical plausibility before trusting them.
3. If the notebook tool returns needs_vlm_answers, inspect the returned
   observation image and AAVSO chart, then resume with target_star_xy and
   comparison_stars_xy.
4. If it returns needs_planetary_parameters, resolve catalog parameters or use
   supplied context, then resume with planetary_parameters.
5. If it returns awaiting_exotic_input, inspect the prompt, tail, inits.json, and
   generated files, then resume the same session with exotic_session_id and
   exotic_input.
6. Do not produce a final report while EXOTIC is waiting for recoverable input.
7. Treat absent calibration frames as absent; do not point calibration
   directories at science FITS data.
8. Record unavoidable assumptions in Observing Notes and in the final report.
""",
    ),
    AgentSkill(
        name="visual-star-identification",
        title="Visual Star Identification",
        description="Resolve target and comparison-star coordinates from FITS renders and AAVSO charts.",
        tool_names=(
            "ReadFileTool",
            "DisplayImageTool",
            "QUERY_AAVSO_STAR_CHART",
            "VIEW_FITS_GCOLAB_FORMAT",
            "PLOT_XY",
            "TodoRead",
            "TodoWrite",
        ),
        triggers=("aavso", "target star", "comparison star", "star coordinates", "vlm"),
        path=SKILLS_ROOT / "visual-star-identification",
        instruction_text="""
Purpose: answer EXOTIC notebook visual prompts for target and comparison-star
pixel coordinates.

Procedure:
1. Inspect observation_image_path and aavso_chart_image_path when available.
2. Use the AAVSO chart as the identity reference for the target and labelled
   comparison stars.
3. Use the rendered FITS image to determine image pixel coordinates.
4. Prefer visible, nearby, unsaturated labelled comparison stars with suitable
   brightness.
5. If labelled comparison stars are unusable, select about 3-5 nearby stars that
   are close to the target and as bright as or brighter than the target.
6. Cross-check orientation and origin if a WCS solution or EXOTIC prompt
   disagrees with selected coordinates.
7. Return target_star_xy as [x, y] and comparison_stars_xy as [[x1, y1], ...].
8. Do not fabricate coordinates without visual evidence.
""",
    ),
    AgentSkill(
        name="exotic-output-reporting",
        title="EXOTIC Output Reporting",
        description="Parse EXOTIC outputs and return structured scientific results and report blocks.",
        tool_names=(
            "ReadFileTool",
            "ListDirectoryTool",
            "FileSearchTool",
            "FindFilesTool",
            "RunPythonTool",
            "DisplayImageTool",
            "PLOT_XY",
            "TodoRead",
            "TodoWrite",
        ),
        triggers=("report", "results", "summary", "ephemerides", "snr", "rms", "bic"),
        path=SKILLS_ROOT / "exotic-output-reporting",
        instruction_text="""
Purpose: parse EXOTIC outputs and produce structured machine-readable result
blocks plus a concise scientific report.

Inspect every generated EXOTIC output that may contain fitted values or
diagnostics: CSV, TXT, JSON, plots, logs, and fitting summaries. Prefer final
fitted values from a successful EXOTIC run over priors or intermediate
estimates.

Extract and report mid-transit time, orbital period, transit depth, transit
duration, signal-to-noise ratio, RMS when available, reduced chi-squared when
available, BIC when available, final light-curve path, final model-fit path, and
output directory.

Final output must include these delimited blocks:
<<<STAR_IDENTIFICATION_BEGIN>>>...<<<STAR_IDENTIFICATION_END>>>
<<<EPHEMERIDES_BEGIN>>>...<<<EPHEMERIDES_END>>>
<<<FINAL_REPORT_BEGIN>>>...<<<FINAL_REPORT_END>>>

If EXOTIC did not complete, report the terminal status and recovery attempts
instead of presenting partial files as a successful result.
""",
    ),
    AgentSkill(
        name="workspace-automation",
        title="Workspace Automation",
        description="Minimal file and Python automation for smoke tests and simple workspace tasks.",
        tool_names=(
            "WorkspaceInfoTool",
            "WriteFileTool",
            "RunPythonTool",
            "ReadFileTool",
        ),
        triggers=("smoke test", "write file", "run python", "workspace automation"),
        path=SKILLS_ROOT / "workspace-automation",
        instruction_text="""
Purpose: run small workspace-contained file and Python automation tasks for
smoke tests.

Use WorkspaceInfoTool, WriteFileTool, RunPythonTool, and ReadFileTool only.
Keep all generated files inside the active workspace.
""",
    ),
]
