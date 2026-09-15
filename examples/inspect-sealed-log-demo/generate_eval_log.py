#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate the small, offline Inspect eval log this demo seals.

Not a lab eval, not anyone's held-out benchmark — four self-authored
arithmetic questions, run with Inspect's built-in ``mockllm/model`` provider
(no API key, no network call to any model, fully deterministic). It exists
only to produce a real ``.eval`` log with the shape capsule-emit's adapter
reads: one sample per question, TWO model calls per sample (a reasoning step
and a final-answer step), because a single-call sample would not exercise
"one sealed record per model call inside the sample."

Usage:
    pip install inspect_ai
    python generate_eval_log.py [output_dir]   # defaults to ./eval_logs
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai import eval as inspect_eval
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatMessageUser, ModelOutput
from inspect_ai.scorer import match
from inspect_ai.solver import Generate, TaskState, solver

# (a, b) pairs -- self-authored, not drawn from any published benchmark.
_PAIRS = [(12, 7), (45, 38), (100, 250), (9, 9)]

_FOLLOWUP = "Now give ONLY the final numeric answer, digits only, no words."


def mock_generate(input, tools, tool_choice, config) -> ModelOutput:
    """Deterministic two-turn mock model: reasoning, then a bare digit answer.

    Content is derived from the arithmetic in the sample's own first user
    message, not scripted per-call-index — so the same task run twice
    produces byte-identical model output every time.
    """
    first_user = next(m for m in input if m.role == "user")
    match_nums = re.search(r"(\d+)\s*\+\s*(\d+)", str(first_user.content))
    a, b = int(match_nums.group(1)), int(match_nums.group(2))
    total = a + b

    last = input[-1]
    if last.role == "user" and "final numeric answer" in str(last.content):
        content = str(total)
    else:
        content = f"Let me add {a} and {b} step by step. {a} + {b} = {total}."
    return ModelOutput.from_content(model="mockllm/model", content=content)


@solver
def reason_then_answer():
    """Two explicit generate() calls per sample -- two ModelEvents to seal."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        state = await generate(state)  # model call 1: reasoning
        state.messages.append(ChatMessageUser(content=_FOLLOWUP))
        state = await generate(state)  # model call 2: final answer
        return state

    return solve


@task
def tiny_arithmetic() -> Task:
    dataset = [
        Sample(input=f"What is {a} + {b}?", target=str(a + b), id=f"add-{a}-{b}")
        for a, b in _PAIRS
    ]
    return Task(dataset=dataset, solver=reason_then_answer(), scorer=match())


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("eval_logs")
    out_dir.mkdir(parents=True, exist_ok=True)

    logs = inspect_eval(
        tiny_arithmetic(),
        model="mockllm/model",
        model_args={"custom_outputs": mock_generate},
        log_dir=str(out_dir),
        log_format="eval",
        display="plain",
    )
    log = logs[0]
    print(f"status: {log.status}")
    print(f"log file: {log.location}")
    print(f"samples: {len(log.samples or [])}")
    return 0 if log.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
