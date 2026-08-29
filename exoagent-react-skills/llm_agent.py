from prompts import REACT_PROMPT
from tools import TOOL_MAPPING
from agent_logger import log, log_path, log_prompt
from agent_skills import SkillRegistry, build_skill_context, filter_tools_for_skills
from openai import OpenAI

from dotenv import load_dotenv
import os
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import TypedDict

PROJECT_ROOT = Path(__file__).resolve().parent
SHARED_ENV_PATH = PROJECT_ROOT.parent / ".env"
load_dotenv(dotenv_path=SHARED_ENV_PATH)

LLM_PROVIDER = os.getenv("EXOAGENT_LLM_PROVIDER", "openrouter").strip().lower()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RESPONSES_REASONING_SUMMARY = os.getenv("EXOAGENT_RESPONSES_REASONING_SUMMARY", "auto").strip()
TOP_LOGPROBS = max(0, min(20, int(os.getenv("EXOAGENT_TOP_LOGPROBS", "0"))))
REQUIRE_DELIMITED_LOGPROBS = (
    os.getenv("EXOAGENT_REQUIRE_DELIMITED_LOGPROBS", "false").strip().lower()
    not in {"0", "false", "no", "off"}
)
DEFAULT_LLM_API_TIMEOUT_SECONDS = 180.0
LOGPROB_CONTEXT_KEYS = {
    "logprobs",
    "tokens",
    "top_logprobs",
}


def _positive_float_env(name, default):
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default

    try:
        value = float(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive number of seconds") from error

    if value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds")

    return value


LLM_API_TIMEOUT_SECONDS = _positive_float_env(
    "EXOAGENT_LLM_TIMEOUT_SECONDS",
    DEFAULT_LLM_API_TIMEOUT_SECONDS,
)


def _resolve_llm_timeout(timeout):
    return LLM_API_TIMEOUT_SECONDS if timeout is None else timeout


def _is_timeout_error(error):
    error_name = error.__class__.__name__.lower()
    message = str(error).lower()
    return "timeout" in error_name or "timed out" in message or "timeout" in message


def _timeout_error_message(timeout):
    return (
        f"LLM API call timed out after {timeout:g} seconds; terminating run. "
        "Set EXOAGENT_LLM_TIMEOUT_SECONDS in .env to change this limit."
    )


def openai_client_for_provider(provider):
    if provider == "openai":
        return OpenAI(api_key=OPENAI_API_KEY)

    if provider == "openrouter":
        return OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY
        )

    raise ValueError("EXOAGENT_LLM_PROVIDER must be 'openrouter' or 'openai'")

DELIMITED_OUTPUT_RE = re.compile(
    r"<<<(?P<name>[A-Z0-9_]+)_BEGIN>>>(?P<body>.*?)<<<(?P=name)_END>>>",
    re.DOTALL,
)


class AgentGraphState(TypedDict, total=False):
    messages: list
    tool_errors: list
    model_iterations: int
    active_skills: list
    pending_continuation: dict


class LLMAgent:
    """LangGraph-backed ReAct agent with optional skill-scoped tool exposure."""

    def __init__(self, model_name, system_prompt, tools, skills=None, skill_registry=None):
        self.model_name = model_name
        self.system_prompt = system_prompt
        self.tools = tools
        self.skills = skills
        self.skill_registry = skill_registry or SkillRegistry.default()
        self.llm_provider = LLM_PROVIDER
        self.openai_client = openai_client_for_provider(self.llm_provider)

    def _prompt_text_for_skill_selection(self, prompt):
        """Flatten supported prompt shapes into text for trigger-based skill selection."""
        if isinstance(prompt, str):
            return prompt

        if isinstance(prompt, dict):
            return prompt.get("content", "")

        if isinstance(prompt, list):
            return "\n".join(
                str(message.get("content", ""))
                if isinstance(message, dict)
                else str(message)
                for message in prompt
            )

        return str(prompt)

    def _active_skills_for_prompt(self, prompt):
        """Resolve configured skills or auto-select skills from prompt text."""
        if not self.skills:
            return []

        if self.skills == "auto":
            return self.skill_registry.select_for_task(
                f"{self.system_prompt}\n{self._prompt_text_for_skill_selection(prompt)}"
            )

        if isinstance(self.skills, str):
            return self.skill_registry.resolve([self.skills])

        return self.skill_registry.resolve(self.skills)

    def _context_for_prompt(self, prompt, active_skills):
        """Build the system context with the base prompt, ReAct rules, and active skills."""
        skill_context = build_skill_context(active_skills, self.skill_registry)
        return [
            {
                "role": "system",
                "content": (
                    f"{self.system_prompt}\n\n"
                    f"{REACT_PROMPT}\n\n"
                    f"{skill_context}"
                )
            }
        ]

    def _format_messages(self, prompt):

        """
        Format messages as OpenAI-style messages
        """

        if isinstance(prompt, str):
            return [{"role": "user", "content": prompt}]

        if isinstance(prompt, dict):
            return [prompt]

        return prompt

    def _format_assistant_message(self, message):

        """
        Format tool calls and response of the agent as context via `assistant` role
        """

        assistant_message = {
            "role": "assistant",
            "content": message.content
        }

        if message.tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": tool_call.type,
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments
                    }
                }
                for tool_call in message.tool_calls
            ]

        if getattr(message, "reasoning", None):
            assistant_message["reasoning"] = message.reasoning

        if getattr(message, "reasoning_details", None):
            assistant_message["reasoning_details"] = message.reasoning_details

        return assistant_message

    def _tool_call_payload(self, tool_call):
        function = tool_call.function if hasattr(tool_call, "function") else tool_call.get("function", {})
        name = function.name if hasattr(function, "name") else function.get("name")
        arguments = function.arguments if hasattr(function, "arguments") else function.get("arguments", "{}")
        tool_call_id = tool_call.id if hasattr(tool_call, "id") else tool_call.get("id")

        return {
            "id": tool_call_id,
            "name": name,
            "arguments": arguments,
        }

    def _tool_calls_from_message(self, message):
        if isinstance(message, dict):
            return message.get("tool_calls") or []

        return message.tool_calls or []

    def _tool_message_to_openai(self, message):
        if isinstance(message, dict):
            return message

        if getattr(message, "type", None) == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }

        return message

    def _openai_messages(self, messages):
        return [
            self._tool_message_to_openai(message)
            for message in messages
        ]

    def _tool_message(self, tool_call_id, name, content):
        try:
            from langchain_core.messages import ToolMessage
        except ImportError:
            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
            }

        return ToolMessage(
            content=content,
            tool_call_id=tool_call_id,
            name=name,
        )

    def _parse_tool_arguments(self, arguments):
        try:
            return json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return None

    def _run_tool_payload(self, payload):

        """
        Run a tool call
        """

        tool_name = payload["name"]

        if tool_name not in TOOL_MAPPING:
            return {
                "error": f"Unknown tool: {tool_name}"
            }

        tool_args = self._parse_tool_arguments(payload.get("arguments"))
        if tool_args is None:
            return {
                "error": f"Invalid JSON arguments for {tool_name}: {payload.get('arguments')}"
            }

        try:
            return TOOL_MAPPING[tool_name](**tool_args)
        except Exception as error:
            return {
                "error": f"{tool_name} failed: {error}"
            }

    def _run_tool(self, tool_call):
        return self._run_tool_payload(self._tool_call_payload(tool_call))

    def _responses_tools(self, tools):
        """Convert Chat Completions function tools to Responses API function tools."""
        responses_tools = []
        for tool in tools:
            if tool.get("type") == "function" and "function" in tool:
                function = tool["function"]
                converted = {
                    "type": "function",
                    "name": function["name"],
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {}),
                }
                if "strict" in function:
                    converted["strict"] = function["strict"]
                responses_tools.append(converted)
            else:
                responses_tools.append(tool)

        return responses_tools

    def _responses_initial_input(self, prompt, active_skills):
        return [
            message
            for message in self._context_for_prompt(prompt, active_skills) + self._format_messages(prompt)
            if message.get("role") != "system"
        ]

    def _responses_instructions(self, prompt, active_skills):
        return "\n\n".join(
            message.get("content", "")
            for message in self._context_for_prompt(prompt, active_skills)
            if message.get("role") == "system"
        )

    def _response_items(self, response):
        if isinstance(response, dict):
            return response.get("output", [])
        return getattr(response, "output", None) or []

    def _response_text(self, response):
        output_text = response.get("output_text") if isinstance(response, dict) else getattr(response, "output_text", None)
        if output_text:
            return output_text

        parts = []
        for item in self._response_items(response):
            item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
            if item_type != "message":
                continue

            content_items = item.get("content", []) if isinstance(item, dict) else getattr(item, "content", [])
            for content in content_items or []:
                content_type = content.get("type") if isinstance(content, dict) else getattr(content, "type", None)
                if content_type == "output_text":
                    parts.append(content.get("text", "") if isinstance(content, dict) else getattr(content, "text", ""))

        return "".join(parts)

    def _response_function_calls(self, response):
        return [
            item
            for item in self._response_items(response)
            if (item.get("type") if isinstance(item, dict) else getattr(item, "type", None)) == "function_call"
        ]

    def _jsonable_response_value(self, value):
        if hasattr(value, "model_dump"):
            value = value.model_dump()

        if isinstance(value, dict):
            return {
                key: self._jsonable_response_value(item)
                for key, item in value.items()
                if key != "encrypted_content"
            }

        if isinstance(value, list):
            return [self._jsonable_response_value(item) for item in value]

        return value

    def _response_usage(self, response):
        usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        if usage is None:
            return None

        return self._jsonable_response_value(usage)

    def _strip_logprobs_for_context(self, value):
        value = self._jsonable_response_value(value)

        if isinstance(value, dict):
            sanitized = {}
            for key, item in value.items():
                if key in LOGPROB_CONTEXT_KEYS:
                    sanitized[key] = "[omitted logprob data from model context; full value is in the run log]"
                else:
                    sanitized[key] = self._strip_logprobs_for_context(item)
            return sanitized

        if isinstance(value, list):
            return [self._strip_logprobs_for_context(item) for item in value]

        return value

    def _json_for_model_context(self, value):
        sanitized = self._strip_logprobs_for_context(value)
        return json.dumps(sanitized, default=str)

    def _response_items_for_context(self, response):
        return [
            self._strip_logprobs_for_context(item)
            for item in self._response_items(response)
        ]

    def _response_reasoning_items(self, response):
        reasoning_items = []
        for item in self._response_items(response):
            item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
            if item_type == "reasoning":
                reasoning_items.append(self._jsonable_response_value(item))
        return reasoning_items

    def _response_output_text_parts(self, response):
        parts = []
        for item in self._response_items(response):
            item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
            if item_type != "message":
                continue

            content_items = item.get("content", []) if isinstance(item, dict) else getattr(item, "content", [])
            for content in content_items or []:
                content_type = content.get("type") if isinstance(content, dict) else getattr(content, "type", None)
                if content_type != "output_text":
                    continue

                text = content.get("text", "") if isinstance(content, dict) else getattr(content, "text", "")
                logprobs = content.get("logprobs") if isinstance(content, dict) else getattr(content, "logprobs", None)
                parts.append({
                    "text": text or "",
                    "logprobs": logprobs,
                })

        if not parts:
            text = response.get("output_text") if isinstance(response, dict) else getattr(response, "output_text", None)
            if text:
                parts.append({"text": text, "logprobs": None})

        return parts

    def _response_tool_call_payload(self, tool_call):
        return {
            "id": (
                tool_call.get("call_id") or tool_call.get("id")
                if isinstance(tool_call, dict)
                else getattr(tool_call, "call_id", None) or getattr(tool_call, "id", None)
            ),
            "name": tool_call.get("name") if isinstance(tool_call, dict) else getattr(tool_call, "name", None),
            "arguments": tool_call.get("arguments", "{}") if isinstance(tool_call, dict) else getattr(tool_call, "arguments", "{}"),
        }

    def _responses_create_kwargs(self, input_items, instructions, timeout, tools):
        kwargs = {
            "model": self.model_name,
            "instructions": instructions,
            "tools": self._responses_tools(tools),
            "input": input_items,
            "timeout": timeout,
        }

        if RESPONSES_REASONING_SUMMARY and RESPONSES_REASONING_SUMMARY.lower() != "none":
            kwargs["reasoning"] = {
                "summary": RESPONSES_REASONING_SUMMARY,
            }

        kwargs["top_logprobs"] = TOP_LOGPROBS

        return kwargs

    def _normalize_logprob_tokens(self, logprob_items, start_index=0):
        tokens = []
        for offset, item in enumerate(logprob_items or []):
            item = self._jsonable_response_value(item)
            if not isinstance(item, dict):
                item = {}

            token = item.get("token")
            bytes_value = item.get("bytes")
            text_piece = token or ""
            if isinstance(bytes_value, list):
                try:
                    text_piece = bytes(bytes_value).decode("utf-8")
                except Exception:
                    text_piece = token or ""

            tokens.append({
                "index": start_index + offset,
                "token": token,
                "text": text_piece,
                "logprob": item.get("logprob"),
                "probability": math.exp(item["logprob"]) if isinstance(item.get("logprob"), (int, float)) and math.isfinite(float(item["logprob"])) else None,
                "bytes": bytes_value,
            })

        return tokens

    def _delimited_blocks_with_token_logprobs(self, text, tokens):
        blocks = [
            {
                "name": match.group("name"),
                "begin": match.start(),
                "end": match.end(),
                "body_begin": match.start("body"),
                "body_end": match.end("body"),
            }
            for match in DELIMITED_OUTPUT_RE.finditer(text)
        ]
        if not blocks:
            return []

        token_text = "".join(str(token.get("text") or token.get("token") or "") for token in tokens)
        if not tokens or token_text != text:
            for block in blocks:
                block["token_alignment"] = "unavailable"
            return blocks

        offset = 0
        token_offsets = []
        for token in tokens:
            piece = str(token.get("text") or token.get("token") or "")
            token_offsets.append((offset, offset + len(piece), token))
            offset += len(piece)

        for block in blocks:
            block_tokens = [
                token
                for token_begin, token_end, token in token_offsets
                if token_end > block["begin"] and token_begin < block["end"]
            ]
            block["token_alignment"] = "exact"
            block["token_begin"] = block_tokens[0]["index"] if block_tokens else None
            block["token_end"] = block_tokens[-1]["index"] + 1 if block_tokens else None

        return blocks

    def _store_response_delimited_outputs(self, log_file, step_index, response):
        parts = self._response_output_text_parts(response)
        text = "".join(part["text"] for part in parts) or self._response_text(response)
        if not DELIMITED_OUTPUT_RE.search(text or ""):
            return

        tokens = []
        for part in parts:
            tokens.extend(self._normalize_logprob_tokens(part.get("logprobs"), len(tokens)))

        logprob_path = f"{os.path.splitext(log_file)[0]}.logprobs.jsonl"
        blocks = self._delimited_blocks_with_token_logprobs(text, tokens)
        with open(logprob_path, "a") as file:
            file.write(json.dumps({
                "log_name": os.path.splitext(os.path.basename(log_file))[0],
                "step_index": step_index,
                "source": "responses.output_text",
                "tool_call_id": None,
                "model": self.model_name,
                "text": text,
                "blocks": blocks,
                "tokens": tokens,
                "token_logprobs_available": bool(tokens),
            }, default=str) + "\n")

        if REQUIRE_DELIMITED_LOGPROBS and not tokens:
            log(log_file, "Logprobs Unavailable", (
                "A delimited output block was generated, but the model/API response did not include "
                "token logprobs."
            ))

    def _workflow_continuation_status(self, tool_response):
        if not isinstance(tool_response, dict):
            return None

        status = tool_response.get("status")
        if status in {
            "awaiting_input",
            "awaiting_exotic_input",
            "needs_vlm_answers",
            "needs_planetary_parameters",
        }:
            return status

        exotic_result = tool_response.get("exotic_result")
        if isinstance(exotic_result, dict):
            exotic_status = exotic_result.get("status")
            if exotic_status in {"awaiting_input", "awaiting_exotic_input"}:
                return exotic_status

        return None

    def _workflow_continuation_message(self, tool_response):
        status = self._workflow_continuation_status(tool_response)
        prompt = tool_response.get("interactive_prompt") if isinstance(tool_response, dict) else None
        resume_instructions = tool_response.get("resume_instructions") if isinstance(tool_response, dict) else None
        exotic_result = tool_response.get("exotic_result") if isinstance(tool_response, dict) else None

        if isinstance(exotic_result, dict):
            prompt = prompt or exotic_result.get("interactive_prompt")
            resume_instructions = resume_instructions or exotic_result.get("resume_instructions")

        return (
            "The previous EXOTIC workflow result is not terminal. "
            f"Current status: {status}. "
            f"Interactive prompt: {prompt or 'not provided'}. "
            f"Resume instructions: {resume_instructions or 'use the appropriate resume tool with the live session id'}. "
            "Do not produce the final report yet. Inspect the tool result, inits.json, FITS metadata, "
            "and generated files as needed, then call the active skill's resume tool with the next targeted input. "
            "Only final-answer after EXOTIC completes or after focused recovery is genuinely exhausted."
        )

    def _call_with_responses(self, prompt, timeout, restrictions, max_steps, log_file, active_skills, tools):
        input_items = self._responses_initial_input(prompt, active_skills)
        instructions = self._responses_instructions(prompt, active_skills)
        tool_errors = []
        pending_continuation = None

        for step_index in range(max_steps):
            response = self.openai_client.responses.create(**self._responses_create_kwargs(
                input_items=input_items,
                instructions=instructions,
                timeout=timeout,
                tools=tools,
            ))
            log(log_file, "Usage", self._response_usage(response))
            input_items.extend(self._response_items_for_context(response))
            output = self._response_text(response)
            reasoning_items = self._response_reasoning_items(response)
            if reasoning_items:
                log(log_file, "Reasoning", reasoning_items)
            log(log_file, "Response", output)
            self._store_response_delimited_outputs(log_file, step_index + 1, response)

            tool_calls = self._response_function_calls(response)
            if not tool_calls:
                if pending_continuation is not None:
                    continuation_message = self._workflow_continuation_message(pending_continuation)
                    log(log_file, "Continuation Required", continuation_message)
                    input_items.append({
                        "role": "user",
                        "content": continuation_message,
                    })
                    continue

                if restrictions(output):
                    return {
                        "output": output,
                        "error": tool_errors[-1] if tool_errors else None,
                        "log_file": log_file,
                        "log_name": os.path.splitext(os.path.basename(log_file))[0],
                    }

                return {"error": "Output did not satisfy restrictions"}

            for tool_call in tool_calls:
                payload = self._response_tool_call_payload(tool_call)
                tool_response = self._run_tool_payload(payload)

                if isinstance(tool_response, dict) and "error" in tool_response:
                    tool_errors.append(tool_response["error"])

                log(log_file, "Tool Call", {
                    "name": payload["name"],
                    "arguments": payload["arguments"],
                    "payload": {
                        "name": payload["name"],
                        "arguments": self._parse_tool_arguments(payload.get("arguments")),
                    },
                })
                log(log_file, "Tool Response", tool_response)
                if self._workflow_continuation_status(tool_response):
                    pending_continuation = tool_response
                elif payload["name"] in {"RUN_EXOTIC_UNTIL_IDLE", "EXOTIC_STANDARD_NOTEBOOK_PIPELINE"}:
                    pending_continuation = None

                input_items.append({
                    "type": "function_call_output",
                    "call_id": tool_call.get("call_id") if isinstance(tool_call, dict) else getattr(tool_call, "call_id", None),
                    "output": self._json_for_model_context(tool_response),
                })

        return {"error": f"Agent stopped after reaching max_steps={max_steps}"}

    def _completion_with_logprobs(self, request):
        """Request a chat completion, falling back when the selected model rejects logprobs."""
        request_with_logprobs = {
            **request,
            "logprobs": True,
        }
        request_with_logprobs["top_logprobs"] = TOP_LOGPROBS

        try:
            return self.openai_client.chat.completions.create(**request_with_logprobs)
        except Exception as error:
            message = str(error).lower()
            if "logprob" not in message and "unsupported" not in message and "400" not in message:
                raise
            return self.openai_client.chat.completions.create(**request)

    def _json_dump(self, value):
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")

        return value

    def _extract_logprob_tokens(self, choice):
        """Normalize token-level logprob data from OpenAI-style response choices."""
        logprobs = getattr(choice, "logprobs", None)
        if not logprobs:
            return []

        content_logprobs = getattr(logprobs, "content", None)
        if not content_logprobs and isinstance(logprobs, dict):
            content_logprobs = logprobs.get("content")

        tokens = []
        for index, item in enumerate(content_logprobs or []):
            token = getattr(item, "token", None)
            logprob = getattr(item, "logprob", None)
            bytes_value = getattr(item, "bytes", None)

            if isinstance(item, dict):
                token = item.get("token", token)
                logprob = item.get("logprob", logprob)
                bytes_value = item.get("bytes", bytes_value)

            text_piece = token or ""
            if isinstance(bytes_value, list):
                try:
                    text_piece = bytes(bytes_value).decode("utf-8")
                except Exception:
                    text_piece = token or ""

            tokens.append({
                "index": index,
                "token": token,
                "text": text_piece,
                "logprob": logprob,
                "probability": math.exp(logprob) if isinstance(logprob, (int, float)) and math.isfinite(float(logprob)) else None,
                "bytes": bytes_value,
            })

        return tokens

    def _message_delimited_sources(self, message):
        """Return assistant content/tool-call arguments containing final output blocks."""
        sources = []
        content = message.content or ""
        if DELIMITED_OUTPUT_RE.search(content):
            sources.append({
                "source": "content",
                "text": content,
            })

        for tool_call in message.tool_calls or []:
            arguments = tool_call.function.arguments or ""
            if DELIMITED_OUTPUT_RE.search(arguments):
                sources.append({
                    "source": f"tool_call:{tool_call.function.name}",
                    "tool_call_id": tool_call.id,
                    "text": arguments,
                })

        return sources

    def _store_delimited_output_logprobs(self, log_file, step_index, choice, message):
        """Persist logprob sidecars for delimited scientific output blocks."""
        sources = self._message_delimited_sources(message)
        if not sources:
            return

        logprob_path = f"{os.path.splitext(log_file)[0]}.logprobs.jsonl"
        tokens = self._extract_logprob_tokens(choice)

        with open(logprob_path, "a") as file:
            for source in sources:
                text = source["text"]
                source_tokens = tokens if source.get("source") == "content" else []
                record = {
                    "log_name": os.path.splitext(os.path.basename(log_file))[0],
                    "step_index": step_index,
                    "source": source.get("source"),
                    "tool_call_id": source.get("tool_call_id"),
                    "model": self.model_name,
                    "text": text,
                    "blocks": self._delimited_blocks_with_token_logprobs(text, source_tokens),
                    "tokens": source_tokens,
                    "token_logprobs_available": bool(source_tokens),
                }
                file.write(json.dumps(record, default=str) + "\n")

                if REQUIRE_DELIMITED_LOGPROBS and not source_tokens:
                    log(log_file, "Logprobs Unavailable", (
                        "A delimited output block was generated, but the model/API response did not include "
                        "token logprobs."
                    ))

    def _build_graph(self, log_file, timeout, max_steps, tools):
        """Compile the reason-observe LangGraph used for a single agent call."""
        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError as error:
            raise RuntimeError(
                "LangGraph is required for LLMAgent. "
                "Run `sh setup_environment.sh`, then run the agent with "
                f"`.venv/bin/python main.py` or activate the venv first. "
                f"Current Python executable: {sys.executable}"
            ) from error

        def reason_node(state: AgentGraphState):
            messages = state.get("messages", [])
            current_request = {
                "model": self.model_name,
                "tools": tools,
                "messages": self._openai_messages(messages),
                "timeout": timeout
            }
            if self.llm_provider == "openrouter":
                current_request["extra_body"] = {
                    "reasoning": {
                        "effort": "medium",
                        "exclude": False
                    }
                }

            response = self._completion_with_logprobs(current_request)
            log(log_file, "Usage", self._response_usage(response))
            choice = response.choices[0]
            message = choice.message
            assistant_message = self._format_assistant_message(message)

            log(log_file, "Response", message.content or "")
            self._store_delimited_output_logprobs(
                log_file,
                len(messages) + 1,
                choice,
                message,
            )
            tool_calls = self._tool_calls_from_message(assistant_message)
            continuation_messages = []
            if not tool_calls and state.get("pending_continuation") is not None:
                continuation_message = self._workflow_continuation_message(state["pending_continuation"])
                log(log_file, "Continuation Required", continuation_message)
                continuation_messages.append({
                    "role": "user",
                    "content": continuation_message,
                })

            return {
                **state,
                "messages": [*messages, assistant_message, *continuation_messages],
                "model_iterations": state.get("model_iterations", 0) + 1,
            }

        def observe_node(state: AgentGraphState):
            messages = state.get("messages", [])
            last_message = messages[-1] if messages else {}
            tool_errors = list(state.get("tool_errors", []))
            observations = []
            pending_continuation = state.get("pending_continuation")

            for tool_call in self._tool_calls_from_message(last_message):
                payload = self._tool_call_payload(tool_call)
                tool_response = self._run_tool_payload(payload)

                if isinstance(tool_response, dict) and "error" in tool_response:
                    tool_errors.append(tool_response["error"])

                log(log_file, "Tool Call", {
                    "name": payload["name"],
                    "arguments": payload["arguments"],
                    "payload": {
                        "name": payload["name"],
                        "arguments": self._parse_tool_arguments(payload.get("arguments"))
                        if isinstance(payload.get("arguments"), str)
                        else payload.get("arguments", {}),
                    },
                })
                log(log_file, "Tool Response", tool_response)
                if self._workflow_continuation_status(tool_response):
                    pending_continuation = tool_response
                elif payload["name"] in {"RUN_EXOTIC_UNTIL_IDLE", "EXOTIC_STANDARD_NOTEBOOK_PIPELINE"}:
                    pending_continuation = None

                observations.append(
                    self._tool_message(
                        tool_call_id=payload["id"],
                        name=payload["name"],
                        content=self._json_for_model_context(tool_response),
                    )
                )

            return {
                **state,
                "messages": [*messages, *observations],
                "tool_errors": tool_errors,
                "pending_continuation": pending_continuation,
            }

        def route_after_reason(state: AgentGraphState):
            messages = state.get("messages", [])
            last_message = messages[-1] if messages else {}
            has_tool_calls = bool(self._tool_calls_from_message(last_message))

            if not has_tool_calls:
                if (
                    state.get("pending_continuation") is not None
                    and state.get("model_iterations", 0) < max_steps
                ):
                    return "reason"
                return END

            if state.get("model_iterations", 0) >= max_steps:
                return END

            return "observe"

        graph = StateGraph(AgentGraphState)
        graph.add_node("reason", reason_node)
        graph.add_node("observe", observe_node)
        graph.add_edge(START, "reason")
        graph.add_conditional_edges("reason", route_after_reason, ["observe", "reason", END])
        graph.add_edge("observe", "reason")
        return graph.compile()

    def call(self, prompt, num_attempts=1, cooldown=5, timeout=None, restrictions=None, max_steps=100):
        
        """Run the skill-aware ReAct graph until a final answer passes restrictions."""

        timeout = _resolve_llm_timeout(timeout)

        if restrictions is None:
            restrictions = lambda x: True

        last_error = None
        log_file = log_path()
        log_prompt(log_file, prompt)
        active_skills = self._active_skills_for_prompt(prompt)
        request_tools = filter_tools_for_skills(self.tools, active_skills)
        log(log_file, "Active Skills", [
            {
                "name": skill.name,
                "title": skill.title,
                "trust_tier": skill.trust_tier,
                "tools": list(skill.tool_names),
            }
            for skill in active_skills
        ])

        if self.llm_provider == "openai":
            try:
                result = self._call_with_responses(
                    prompt=prompt,
                    timeout=timeout,
                    restrictions=restrictions,
                    max_steps=max_steps,
                    log_file=log_file,
                    active_skills=active_skills,
                    tools=request_tools,
                )
                if not result.get("error") or result.get("output") is not None:
                    return result
                last_error = result["error"]
                log(log_file, "Error", last_error)
            except Exception as error:
                last_error = _timeout_error_message(timeout) if _is_timeout_error(error) else str(error)
                log(log_file, "Error", last_error)

            return {
                "output": None,
                "error": last_error,
                "log_file": log_file,
                "log_name": os.path.splitext(os.path.basename(log_file))[0],
            }

        for attempt in range(num_attempts):

            initial_messages = (
                self._context_for_prompt(prompt, active_skills)
                + self._format_messages(prompt)
            )

            try:
                graph = self._build_graph(
                    log_file,
                    timeout=timeout,
                    max_steps=max_steps,
                    tools=request_tools,
                )
                state = graph.invoke(
                    {
                        "messages": initial_messages,
                        "tool_errors": [],
                        "model_iterations": 0,
                        "active_skills": [skill.name for skill in active_skills],
                    },
                    config={
                        "recursion_limit": max((max_steps * 2) + 5, 10),
                    },
                )

                messages = state.get("messages", [])
                tool_errors = state.get("tool_errors", [])
                final_message = messages[-1] if messages else {}

                if (
                    isinstance(final_message, dict)
                    and final_message.get("role") == "assistant"
                    and not final_message.get("tool_calls")
                ):
                    output = final_message.get("content") or ""

                    if restrictions(output):
                        return {
                            "output": output,
                            "error": tool_errors[-1] if tool_errors else None,
                            "log_file": log_file,
                            "log_name": os.path.splitext(os.path.basename(log_file))[0],
                        }

                    last_error = "Output did not satisfy restrictions"
                else:
                    last_error = f"Agent stopped after reaching max_steps={max_steps}"

            except Exception as error:
                last_error = _timeout_error_message(timeout) if _is_timeout_error(error) else str(error)
                log(log_file, "Error", last_error)
                if _is_timeout_error(error):
                    return {
                        "output": None,
                        "error": last_error,
                        "log_file": log_file,
                        "log_name": os.path.splitext(os.path.basename(log_file))[0],
                    }

            if attempt < num_attempts - 1:
                time.sleep(cooldown)

        return {
            "output": None,
            "error": last_error,
            "log_file": log_file,
            "log_name": os.path.splitext(os.path.basename(log_file))[0],
        }
