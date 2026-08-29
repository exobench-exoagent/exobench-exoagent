import os

from llm_agent import LLMAgent
from prompts import RUN_AGENT_PROMPT
from tools import TOOLS_LIST, get_default_input_path, prepare_run_workspace


def prompt_input_path():
    default_input_path = get_default_input_path()
    if default_input_path:
        value = input(f"Input the file path to the FITS files that require processing [{default_input_path}]: ").strip()
        return value or default_input_path

    value = input("Input the file path to the FITS files that require processing: ").strip()
    if not value:
        raise ValueError("An input FITS file or directory is required.")
    return value


def main():
    file_path = prompt_input_path()
    exoplanet_name = input("Input the exoplanet being tracked in the FITS files: ")
    workspace = prepare_run_workspace(input_path=file_path, copy_input=False)

    agent = LLMAgent(
        model_name=os.environ.get("EXOAGENT_MODEL"),
        system_prompt=RUN_AGENT_PROMPT,
        tools=TOOLS_LIST,
        skills=[
            "workspace-safety",
            "exotic-transit-pipeline",
            "visual-star-identification",
            "exotic-output-reporting",
        ],
    )

    print(f"Run ID: {workspace['run_id']}")
    print(f"Run workspace: {workspace['workspace_root']}")
    print(f"Read-only input: {workspace['input_path']}")

    response = agent.call(
        (
            f"Process the read-only FITS dataset at {workspace['input_path']}, "
            f"with exoplanet name \"{exoplanet_name}\", and produce the final scientific report. "
            f"Do not write into the FITS input directory. "
            f"Keep all generated files under {workspace['output_dir']} or {workspace['scratch_dir']}. "
            "Generate a complete non-interactive EXOTIC inits.json before running EXOTIC. "
            "If EXOTIC still asks for CLI input, debug the prompt from the tool output and resume "
            "the same live EXOTIC session instead of finalizing early."
        ),
        max_steps=100,
    )

    if response["error"]:
        print("Error:", response["error"])
    else:
        print(response["output"])


if __name__ == "__main__":
    main()
