"""Plan mode helpers: ask the model for a plan, then parse it robustly."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

NUMBERED_RE = re.compile(r"^\s*(?:\d+[.)、]|[-*·])\s+(?P<title>.+?)\s*$")


@dataclass
class PlanStep:
    """One step of an execution plan."""

    title: str
    detail: str = ""
    status: str = "pending"  # pending | running | done | failed | skipped

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "detail": self.detail, "status": self.status}


def extract_json(text: str) -> Any | None:
    """Pull the first JSON object/array out of a model reply."""

    if not text:
        return None
    candidates: list[str] = []
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence (and any language tag) plus the closing fence.
        body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        body = body.rsplit("```", 1)[0]
        candidates.append(body.strip())
    candidates.append(stripped)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        for opener, closer in (("{", "}"), ("[", "]")):
            start = candidate.find(opener)
            if start < 0:
                continue
            depth = 0
            in_string = False
            escaped = False
            for index in range(start, len(candidate)):
                char = candidate[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(candidate[start : index + 1])
                        except json.JSONDecodeError:
                            break
    return None


def parse_plan(text: str, max_steps: int = 12) -> list[PlanStep]:
    """Turn a model reply into a list of steps, tolerating loose formatting."""

    data = extract_json(text)
    steps: list[PlanStep] = []

    if isinstance(data, dict):
        raw_steps = data.get("steps") or data.get("plan") or []
        if isinstance(raw_steps, list):
            for item in raw_steps:
                if isinstance(item, str):
                    steps.append(PlanStep(title=item.strip()))
                elif isinstance(item, dict):
                    title = str(
                        item.get("title") or item.get("step") or item.get("name") or ""
                    ).strip()
                    detail = str(item.get("detail") or item.get("how") or "").strip()
                    if title:
                        steps.append(PlanStep(title=title, detail=detail))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                steps.append(PlanStep(title=item.strip()))
            elif isinstance(item, dict):
                title = str(item.get("title") or item.get("step") or "").strip()
                if title:
                    steps.append(PlanStep(title=title, detail=str(item.get("detail") or "")))

    if not steps:
        # Fallback: numbered or bulleted lines from a plain-text answer.
        for line in text.splitlines():
            match = NUMBERED_RE.match(line)
            if match:
                title = match.group("title").strip()
                if title and len(title) < 200:
                    steps.append(PlanStep(title=title))

    return [step for step in steps if step.title][:max_steps]


def step_instruction(index: int, total: int, step: PlanStep) -> str:
    detail = f"\n补充说明：{step.detail}" if step.detail else ""
    return (
        f"【计划执行 · 第 {index + 1}/{total} 步】{step.title}{detail}\n"
        "只完成这一步，完成后用一两句话说明结果；不要提前做后续步骤。"
    )
