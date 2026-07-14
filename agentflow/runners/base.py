from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

from agentflow.prepared import ExecutionPaths, PreparedExecution
from agentflow.specs import NodeSpec


# Codex and other streaming agents can emit a complete tool result as one JSONL
# record. Keep this comfortably above asyncio's 64 KiB default line limit.
SUBPROCESS_STREAM_LIMIT_BYTES = 8 * 1024 * 1024


class RawExecutionResult(BaseModel):
    exit_code: int
    stdout_lines: list[str] = Field(default_factory=list)
    stderr_lines: list[str] = Field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False


StreamCallback = Callable[[str, str], Awaitable[None]]
CancelCallback = Callable[[], bool]


@dataclass(slots=True)
class LaunchPlan:
    kind: str = "process"
    command: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    stdin: str | None = None
    runtime_files: list[str] = field(default_factory=list)
    payload: dict[str, object] | None = None


class Runner(ABC):
    def plan_execution(
        self,
        node: NodeSpec,
        prepared: PreparedExecution,
        paths: ExecutionPaths,
    ) -> LaunchPlan:
        return LaunchPlan(
            command=list(prepared.command),
            env=dict(prepared.env),
            cwd=prepared.cwd,
            stdin=prepared.stdin,
            runtime_files=sorted(prepared.runtime_files),
        )

    @abstractmethod
    async def execute(
        self,
        node: NodeSpec,
        prepared: PreparedExecution,
        paths: ExecutionPaths,
        on_output: StreamCallback,
        should_cancel: CancelCallback,
    ) -> RawExecutionResult:
        raise NotImplementedError

    def materialize_runtime_files(self, base_dir: Path, runtime_files: dict[str, str]) -> None:
        for relative_path, content in runtime_files.items():
            target = base_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def materialize_runtime_symlinks(self, base_dir: Path, runtime_symlinks: dict[str, str]) -> None:
        for relative_path, source in runtime_symlinks.items():
            target = base_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.exists():
                target.unlink()
            target.symlink_to(source)
