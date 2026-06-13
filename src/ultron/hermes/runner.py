"""Live Hermes runner seam.

The subprocess runner imports hermes-agent lazily so the default Ultron test and fake
paths never require Hermes to be installed.
"""

from __future__ import annotations

import importlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ultron.hermes.adapter import HermesInvocationPlan, LiveHermesUnavailable


class RunnerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trajectory_id: str
    trajectory_path: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    tool_calls: int = 0
    measured_guardrails: dict[str, Any] = Field(default_factory=dict)
    model_provider: str
    model_name: str
    model_snapshot: dict[str, Any] = Field(default_factory=dict)


class HermesRunner(Protocol):
    def run_plan(self, plan: HermesInvocationPlan, isolated_root: str) -> RunnerResult: ...


class SubprocessHermesRunner:
    """Real Hermes runner using the pinned hermes-agent integration seams."""

    def run_plan(self, plan: HermesInvocationPlan, isolated_root: str) -> RunnerResult:
        root = Path(isolated_root).resolve()
        home = root / "home"
        workspace = root / "workspace"
        home.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(parents=True, exist_ok=True)
        self._materialize_plan(plan, home, workspace)

        old_home = os.environ.get("HOME")
        old_cwd = Path.cwd()
        os.environ["HOME"] = str(home)
        os.chdir(workspace)
        modules: dict[str, Any] | None = None
        started = time.monotonic()
        trajectory_id = uuid.uuid4().hex
        try:
            modules = self._import_hermes()
            budget = self._build_budget(modules["budget"], plan.iteration_budget)
            toolset = self._build_toolset(modules["toolsets"], plan.hermes_tool_allowlist, workspace)
            result = modules["conversation"].run_conversation(
                plan.request_text,
                toolset=toolset,
                iteration_budget=budget,
                environment_hints=modules["prompt_builder"].build_environment_hints(str(workspace)),
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            trajectory_path = self._save_trajectory(modules["trajectory"], result, trajectory_id, workspace, plan.trajectory_tags)
            output = self._parse_output(result)
            tool_calls = self._count_tool_calls(result)
            provider, name, snapshot = self._model_identity(result)
            guardrails = {"cost": self._read_attr(result, "cost", 0), "latency_ms": latency_ms, "tool_calls": tool_calls}
            return RunnerResult(
                trajectory_id=trajectory_id,
                trajectory_path=str(trajectory_path) if trajectory_path else None,
                output=output,
                tool_calls=tool_calls,
                measured_guardrails=guardrails,
                model_provider=provider,
                model_name=name,
                model_snapshot=snapshot,
            )
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
            os.chdir(old_cwd)

    def _import_hermes(self) -> dict[str, Any]:
        try:
            return {
                "toolsets": importlib.import_module("toolsets"),
                "conversation": importlib.import_module("agent.conversation_loop"),
                "prompt_builder": importlib.import_module("agent.prompt_builder"),
                "budget": importlib.import_module("agent.iteration_budget"),
                "trajectory": importlib.import_module("agent.trajectory"),
                "state": importlib.import_module("hermes_state"),
            }
        except ImportError as exc:
            raise LiveHermesUnavailable("hermes-agent not installed") from exc

    def _materialize_plan(self, plan: HermesInvocationPlan, home: Path, workspace: Path) -> None:
        for relative, lines in plan.prompt_slot_injections.items():
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(str(line) for line in lines) + "\n", encoding="utf-8")
        (workspace / "ultron-plan.json").write_text(
            json.dumps(
                {
                    "request_text": plan.request_text,
                    "tool_allowlist": plan.hermes_tool_allowlist,
                    "iteration_budget": plan.iteration_budget,
                    "skill_refs": plan.skill_refs,
                    "trajectory_tags": plan.trajectory_tags,
                    "home": str(home),
                },
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _build_budget(self, budget_module: Any, policy: dict[str, Any]) -> Any:
        max_tool_calls = int(policy.get("max_tool_calls") or policy.get("policy", {}).get("max_tool_calls") or 1)
        budget_cls = getattr(budget_module, "IterationBudget")
        try:
            return budget_cls(max_tool_calls=max_tool_calls)
        except TypeError:
            return budget_cls(max_iterations=max_tool_calls)

    def _build_toolset(self, toolsets_module: Any, allowlist: list[str], workspace: Path) -> Any:
        create = getattr(toolsets_module, "create_custom_toolset")
        try:
            return create(allowed_tools=allowlist, workspace=str(workspace))
        except TypeError:
            try:
                return create(allowlist, str(workspace))
            except TypeError:
                return create(allowlist)

    def _save_trajectory(self, trajectory_module: Any, result: Any, trajectory_id: str, workspace: Path, tags: dict[str, str | None]) -> Path | None:
        save = getattr(trajectory_module, "save_trajectory")
        path = workspace / "trajectories" / f"{trajectory_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            saved = save(result, path=str(path), tags=tags)
        except TypeError:
            saved = save(result, str(path))
        return Path(saved) if saved else path

    def _parse_output(self, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            text = result.get("output") or result.get("content") or result.get("text") or json.dumps(result)
        else:
            text = self._read_attr(result, "output", None) or self._read_attr(result, "content", None) or self._read_attr(result, "text", str(result))
        if isinstance(text, dict):
            payload = text
        else:
            try:
                payload = json.loads(str(text))
            except json.JSONDecodeError:
                payload = {"plan": [str(text).strip()], "risk": [], "tests": []}
        return {"plan": payload.get("plan", []), "risk": payload.get("risk", payload.get("risks", [])), "tests": payload.get("tests", [])}

    def _count_tool_calls(self, result: Any) -> int:
        calls = self._read_attr(result, "tool_calls", None)
        if isinstance(calls, list):
            return len(calls)
        if isinstance(calls, int):
            return calls
        messages = self._read_attr(result, "messages", [])
        if isinstance(messages, list):
            return sum(1 for item in messages if isinstance(item, dict) and item.get("tool_call"))
        return 0

    def _model_identity(self, result: Any) -> tuple[str, str, dict[str, Any]]:
        snapshot = self._read_attr(result, "model_snapshot", {}) or {}
        if not isinstance(snapshot, dict):
            snapshot = {"raw": str(snapshot)}
        provider = str(snapshot.get("provider") or self._read_attr(result, "model_provider", "hermes-agent"))
        name = str(snapshot.get("name") or self._read_attr(result, "model_name", "hermes-live-model"))
        snapshot.setdefault("provider", provider)
        snapshot.setdefault("name", name)
        return provider, name, snapshot

    def _read_attr(self, value: Any, name: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)
