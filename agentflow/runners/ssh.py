"""SSH remote runner for AgentFlow nodes."""

from __future__ import annotations

import asyncio
import shlex

from agentflow.runners.base import (
    SUBPROCESS_STREAM_LIMIT_BYTES,
    CancelCallback,
    ExecutionPaths,
    LaunchPlan,
    PreparedExecution,
    RawExecutionResult,
    Runner,
    StreamCallback,
)
from agentflow.specs import NodeSpec


class SSHRunner(Runner):
    """Execute agent nodes on remote hosts via SSH.

    Uses the system ``ssh`` binary so there is no extra Python dependency.
    The target spec provides host, port, username, and optional identity file.
    """

    def _build_ssh_command(
        self,
        node: NodeSpec,
        prepared: PreparedExecution,
        paths: ExecutionPaths,
    ) -> list[str]:
        target = node.target
        ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
        if getattr(target, "port", 22) != 22:
            ssh_cmd += ["-p", str(target.port)]
        if getattr(target, "identity_file", None):
            ssh_cmd += ["-i", target.identity_file]

        username = getattr(target, "username", None)
        host = target.host
        destination = f"{username}@{host}" if username else host

        env_exports = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in prepared.env.items()
        ) if prepared.env else ""
        remote_workdir = getattr(target, "remote_workdir", None) or str(paths.target_workdir)
        inner_cmd = " ".join(shlex.quote(part) for part in prepared.command)
        remote_script = f"cd {shlex.quote(remote_workdir)} && {env_exports + ' ' if env_exports else ''}{inner_cmd}"

        ssh_cmd += [destination, remote_script]
        return ssh_cmd

    def plan_execution(
        self,
        node: NodeSpec,
        prepared: PreparedExecution,
        paths: ExecutionPaths,
    ) -> LaunchPlan:
        return LaunchPlan(
            kind="ssh",
            command=self._build_ssh_command(node, prepared, paths),
            env={},
            cwd=str(paths.host_workdir),
        )

    async def execute(
        self,
        node: NodeSpec,
        prepared: PreparedExecution,
        paths: ExecutionPaths,
        on_output: StreamCallback,
        should_cancel: CancelCallback,
    ) -> RawExecutionResult:
        ssh_cmd = self._build_ssh_command(node, prepared, paths)

        process = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if prepared.stdin else asyncio.subprocess.DEVNULL,
            cwd=str(paths.host_workdir),
            limit=SUBPROCESS_STREAM_LIMIT_BYTES,
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        timed_out = False
        cancelled = False

        async def _consume(stream: asyncio.StreamReader, name: str, buf: list[str]) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                buf.append(text)
                await on_output(name, text)

        if prepared.stdin and process.stdin:
            process.stdin.write(prepared.stdin.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(_consume(process.stdout, "stdout", stdout_lines))
        stderr_task = asyncio.create_task(_consume(process.stderr, "stderr", stderr_lines))

        timeout = node.timeout_seconds if node.timeout_seconds and node.timeout_seconds > 0 else None
        try:
            if timeout:
                await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task), timeout=timeout)
            else:
                await asyncio.gather(stdout_task, stderr_task)
            await process.wait()
        except asyncio.TimeoutError:
            timed_out = True
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        if not timed_out and should_cancel():
            cancelled = True
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

        exit_code = process.returncode if process.returncode is not None else (124 if timed_out else 130)
        return RawExecutionResult(
            exit_code=exit_code,
            stdout_lines=stdout_lines,
            stderr_lines=stderr_lines,
            timed_out=timed_out,
            cancelled=cancelled,
        )
