"""Thin wrapper around Nebius Token Factory (OpenAI-API-compatible) plus a
tool-calling agent loop.

Nebius Token Factory speaks the same wire protocol as the OpenAI SDK, so we
just point the client at Nebius's base_url with our Token Factory API key.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

from openai import OpenAI

from .config import settings

_client: OpenAI | None = None
# Active counters, outermost first: (count box, limit or None).
_call_counters: ContextVar[tuple[tuple[list[int], int | None], ...]] = ContextVar("copilot_model_calls", default=())


class ModelBudgetExceeded(RuntimeError):
    """The model-call budget for this request is used up."""


@contextmanager
def count_model_calls(limit: int | None = None) -> Iterator[list[int]]:
    """Counts every model call (agent turns, json_completion, edit calls)
    made inside the block, including inside nested blocks: `with
    count_model_calls() as n: ...`, then n[0]. Calls that fail still count.
    With a limit, the call that would exceed it raises ModelBudgetExceeded
    instead of being made."""
    box = [0]
    token = _call_counters.set(_call_counters.get() + ((box, limit),))
    try:
        yield box
    finally:
        _call_counters.reset(token)


def note_model_call() -> None:
    """Call right before every model request."""
    counters = _call_counters.get()
    for box, limit in counters:
        if limit is not None and box[0] >= limit:
            raise ModelBudgetExceeded(f"model-call budget of {limit} for this request is used up")
    for box, _ in counters:
        box[0] += 1


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.nebius_api_key, base_url=settings.nebius_base_url)
    return _client


def json_completion(client, messages: list[dict], max_tokens: int = 4000, thinking: bool = False) -> str:
    """One JSON-object completion for a structured check (a tool's own model
    call, not the agent loop). Reasoning is off by default: Nemotron can
    otherwise spend the whole output budget thinking and return no content
    (finish_reason=length). thinking=True keeps it on for genuine reasoning
    tasks; give it a large max_tokens. Raises if the output was cut off."""
    note_model_call()
    response = client.chat.completions.create(
        model=settings.nebius_model,
        messages=messages,
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        extra_body={"chat_template_kwargs": {"enable_thinking": thinking}},
    )
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise RuntimeError("model output was cut off (finish_reason=length)")
    return choice.message.content or ""


SYSTEM_PROMPT = """\
You are a hardware design copilot. You help with the following active \
projects: RISC-V core (riscv-core), SIMT GPU core (simt-gpu-core), \
micro-NPU (micro-npu), and MXINT8 GEMM accelerator (mxint8-gemm). Use the \
tools you're given to check \
project memory before answering questions about status, and to actually \
carry out tasks (running testbenches, checking lint, drafting spec text, \
etc.) rather than just describing what you'd do.

Never call apply_diff unless the user has explicitly approved a diff you \
already showed them via modify_module. If unsure whether you have approval, \
ask first.
"""


def run_agent_loop(
    user_message: str,
    history: list[dict],
    tool_schemas: list[dict],
    tool_impls: dict[str, Callable[..., dict]],
    confirm_tool_call: Callable[[str, dict], bool] | None = None,
    max_turns: int = 8,
    extra_system: str = "",
    max_model_calls: int | None = None,
) -> tuple[str, list[dict]]:
    """Runs one user turn through the agent, executing tool calls as needed.

    confirm_tool_call(name, args) -> bool lets the caller (e.g. the CLI)
    gate sensitive tools like apply_diff behind an explicit yes/no before
    they actually run. If it returns False, the tool is skipped and the
    model is told the call was declined. If it is None, gated tools are
    always declined (fail closed) — they never run without a human yes.

    extra_system is appended to the system prompt (e.g. the web demo's
    workspace rules); it can add constraints but never removes the gate.

    max_model_calls caps every model call made for this turn, the agent's own
    and those inside tools (the web demo's spend limit); None = no cap.

    Returns (final_text_response, updated_history).
    """
    client = get_client()
    # Only arguments a tool's schema declares reach it: internal parameters
    # (vcd_out, compile_check, data_dirs, ...) are for the code, never the model.
    declared = {
        s["function"]["name"]: set(s["function"].get("parameters", {}).get("properties", {})) for s in tool_schemas
    }
    system = SYSTEM_PROMPT + ("\n" + extra_system if extra_system else "")
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": user_message}]

    with count_model_calls(max_model_calls):
        for _ in range(max_turns):
            try:
                note_model_call()
            except ModelBudgetExceeded:
                return ("I've used this request's model-call budget before finishing; ask again with a "
                        "narrower request.", messages[1:])
            response = client.chat.completions.create(
                model=settings.nebius_model,
                messages=messages,
                tools=tool_schemas,
                tool_choice="auto",
                max_tokens=16000,
            )
            choice = response.choices[0]
            msg = choice.message
            messages.append(msg.model_dump(exclude_none=True))

            if not msg.tool_calls:
                return msg.content or "", messages[1:]  # drop system prompt from returned history

            for call in msg.tool_calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                dropped = sorted(set(args) - declared.get(name, set()))
                args = {k: v for k, v in args.items() if k not in dropped}

                if name in _CONFIRM_REQUIRED and confirm_tool_call is None:
                    # Fail closed: a caller with no human in the loop (cron job,
                    # endpoint, script) can never run a gated tool.
                    result = {
                        "status": "declined",
                        "message": f"'{name}' needs explicit human approval and no approval prompt is available here.",
                    }
                elif name in _CONFIRM_REQUIRED and not _confirmed(confirm_tool_call, name, args):
                    result = {"status": "declined", "message": "User did not approve this action."}
                elif name not in tool_impls:
                    result = {"status": "error", "message": f"Unknown tool '{name}'"}
                else:
                    try:
                        result = tool_impls[name](**args)
                    except Exception as exc:  # noqa: BLE001 - surface to the model, don't crash the loop
                        result = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
                if dropped and isinstance(result, dict):
                    result = {**result, "ignored_arguments": dropped}

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, default=str),
                    }
                )

        return (
            "I've hit the tool-call limit for this turn without reaching a final answer — "
            "try breaking the request into smaller steps.",
            messages[1:],
        )


def _confirmed(confirm_tool_call: Callable[[str, dict], bool], name: str, args: dict) -> bool:
    """A confirm handler that crashes counts as a "no", never a "yes"."""
    try:
        return bool(confirm_tool_call(name, args))
    except Exception:  # noqa: BLE001 - fail closed on any handler error
        return False


# Tools that must go through the CLI's confirmation prompt, regardless of
# what confirm_tool_call's caller passes in for everything else.
_CONFIRM_REQUIRED = {"apply_diff"}
