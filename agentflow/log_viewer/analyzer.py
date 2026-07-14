from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from jinja2 import Environment, nodes
from jinja2.visitor import NodeVisitor

from agentflow.context import build_render_context
from agentflow.specs import NodeSpec, RunRecord
from agentflow.utils import path_within


_JSON_ARTIFACTS = {"launch.json", "result.json"}
_TEXT_ARTIFACTS = {"output.txt", "stdout.log", "stderr.log", "trace.jsonl", "diff.patch"}
_SAFE_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_JINJA_ENV = Environment()
_EVENT_STRING_PREVIEW_LIMIT = 64_000


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _parse_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.min


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _file_meta(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
        "format": "jsonl" if path.suffix == ".jsonl" else "json" if path.suffix == ".json" else "text",
    }


class _ReferenceVisitor(NodeVisitor):
    def __init__(self) -> None:
        self.references: set[tuple[str, ...]] = set()

    def _chain(self, node: nodes.Node) -> tuple[str, ...] | None:
        if isinstance(node, nodes.Name):
            return (node.name,)
        if isinstance(node, nodes.Getattr):
            parent = self._chain(node.node)
            return (*parent, node.attr) if parent else None
        if isinstance(node, nodes.Getitem) and isinstance(node.arg, nodes.Const):
            parent = self._chain(node.node)
            return (*parent, str(node.arg.value)) if parent else None
        return None

    def visit_Getattr(self, node: nodes.Getattr, *args: Any, **kwargs: Any) -> None:
        chain = self._chain(node)
        if chain:
            self.references.add(chain)
        self.generic_visit(node, *args, **kwargs)

    def visit_Getitem(self, node: nodes.Getitem, *args: Any, **kwargs: Any) -> None:
        chain = self._chain(node)
        if chain:
            self.references.add(chain)
        self.generic_visit(node, *args, **kwargs)


def extract_template_references(template: str) -> list[tuple[str, ...]]:
    """Return maximal dotted references used by a Jinja template."""

    try:
        tree = _JINJA_ENV.parse(template)
    except Exception:
        return []
    visitor = _ReferenceVisitor()
    visitor.visit(tree)
    references = sorted(visitor.references)
    return [
        reference
        for reference in references
        if not any(len(other) > len(reference) and other[: len(reference)] == reference for other in references)
    ]


def _resolve_reference(context: dict[str, Any], reference: Iterable[str]) -> tuple[bool, Any]:
    current: Any = context
    for part in reference:
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, (list, tuple)) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return False, None
    return True, current


def _transfer_kind(reference: tuple[str, ...]) -> tuple[str, str | None]:
    if len(reference) >= 2 and reference[0] == "nodes":
        return "node", reference[1]
    if len(reference) >= 2 and reference[0] == "fanouts":
        return "fanout", reference[1]
    if reference and reference[0] == "item":
        return "item", None
    if reference and reference[0] == "pipeline":
        return "pipeline", None
    return "context", None


def _extract_prompt(node: NodeSpec, launch: dict[str, Any] | None) -> str | None:
    if not launch:
        return None
    stdin = launch.get("stdin")
    if isinstance(stdin, str) and stdin:
        return stdin
    command = launch.get("command")
    if not isinstance(command, list):
        return None
    command = [str(part) for part in command]
    agent = _status_value(node.agent)
    if agent == "codex" and command:
        return command[-1]
    if agent in {"claude", "kimi"} and "-p" in command:
        index = command.index("-p")
        return command[index + 1] if index + 1 < len(command) else None
    if agent in {"python", "shell"} and "-c" in command:
        index = command.index("-c")
        return command[index + 1] if index + 1 < len(command) else None
    return None


def _event_category(event: dict[str, Any]) -> str:
    kind = str(event.get("kind") or "").lower()
    title = str(event.get("title") or "").lower()
    source = str(event.get("source") or "").lower()
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
    item_type = str(item.get("type") or "").lower()
    if source == "stderr" or "error" in kind or "error" in title or item_type == "error":
        return "error"
    if item_type == "command_execution" or "command_execution" in title or kind == "command_output":
        return "command"
    if "tool" in kind or "tool" in title or item_type in {"function_call", "tool_use", "tool_result"}:
        return "tool"
    if "message" in kind or "message" in title or item_type in {"agent_message", "agentmessage", "message"}:
        return "message"
    if kind in {"event", "completed"}:
        return "lifecycle"
    return "output"


def _event_summary(event: dict[str, Any]) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
    command = item.get("command")
    if isinstance(command, str) and command.strip():
        return command.strip()
    content = event.get("content")
    if isinstance(content, str) and content.strip():
        compact = " ".join(content.strip().split())
        return compact[:240] + ("..." if len(compact) > 240 else "")
    return str(event.get("title") or event.get("kind") or "Trace event")


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    started = _parse_timestamp(start)
    finished = _parse_timestamp(end)
    if started == datetime.min or finished == datetime.min:
        return None
    return max(0.0, (finished - started).total_seconds())


def _raw_item(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    item = raw.get("item") or (raw.get("params") or {}).get("item") or {}
    return item if isinstance(item, dict) else {}


def _usage_value(payload: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _find_usage_payloads(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        usage = value.get("usage")
        if isinstance(usage, dict):
            found.append(usage)
        for key, item in value.items():
            if key != "usage":
                found.extend(_find_usage_payloads(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_usage_payloads(item))
    return found


def _normalize_usage(payload: dict[str, Any]) -> dict[str, int | None]:
    input_tokens = _usage_value(payload, "input_tokens", "inputTokens", "prompt_tokens", "promptTokens")
    cached_tokens = _usage_value(
        payload,
        "cached_input_tokens",
        "cachedInputTokens",
        "cache_read_input_tokens",
        "cacheReadInputTokens",
    )
    cache_write_tokens = _usage_value(
        payload,
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
        "cache_write_input_tokens",
    )
    output_tokens = _usage_value(payload, "output_tokens", "outputTokens", "completion_tokens", "completionTokens")
    reasoning_tokens = _usage_value(payload, "reasoning_output_tokens", "reasoningTokens", "reasoning_tokens")
    total_tokens = _usage_value(payload, "total_tokens", "totalTokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "cache_write_input_tokens": cache_write_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def _tool_name(event: dict[str, Any]) -> str | None:
    item = _raw_item(event)
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    name = item.get("name") or item.get("tool_name") or raw.get("name") or (raw.get("function") or {}).get("name")
    if isinstance(name, str) and name:
        return name
    server = item.get("server") or item.get("server_name")
    tool = item.get("tool") or item.get("tool_name")
    if isinstance(server, str) and isinstance(tool, str):
        return f"mcp__{server}__{tool}"
    title = str(event.get("title") or "")
    match = re.search(r"(?:tool\s*(?:call|use)|ToolCall):\s*([^\s]+)", title, re.IGNORECASE)
    return match.group(1) if match else None


def _command_info(event: dict[str, Any]) -> tuple[str | None, str | None, int | None, str | None]:
    item = _raw_item(event)
    if str(item.get("type") or "").lower() != "command_execution":
        return None, None, None, None
    command = item.get("command")
    command_id = item.get("id")
    exit_code = item.get("exit_code")
    output = item.get("aggregated_output") or item.get("output")
    return (
        str(command) if command is not None else None,
        str(command_id) if command_id is not None else None,
        int(exit_code) if isinstance(exit_code, (int, float)) else None,
        str(output) if output is not None else None,
    )


def _possible_command_files(command: str, cwd: str | None) -> list[Path]:
    if not cwd:
        return []
    try:
        parts = shlex.split(command)
        if "-lc" in parts and parts.index("-lc") + 1 < len(parts):
            parts = shlex.split(parts[parts.index("-lc") + 1])
    except ValueError:
        return []
    files: list[Path] = []
    for part in parts:
        if part.startswith("-") or any(marker in part for marker in ("*", "?", "$", "|", ";")):
            continue
        candidate = Path(part).expanduser()
        if not candidate.is_absolute():
            candidate = Path(cwd) / candidate
        try:
            if candidate.is_file():
                files.append(candidate.resolve())
        except OSError:
            continue
    return files


def _compact_event_value(value: Any, *, limit: int = _EVENT_STRING_PREVIEW_LIMIT) -> tuple[Any, bool]:
    if isinstance(value, str):
        if len(value) <= limit:
            return value, False
        return {"_truncated": True, "characters": len(value), "preview": value[:limit]}, True
    if isinstance(value, list):
        compacted: list[Any] = []
        truncated = False
        for item in value:
            normalized, item_truncated = _compact_event_value(item, limit=limit)
            compacted.append(normalized)
            truncated = truncated or item_truncated
        return compacted, truncated
    if isinstance(value, dict):
        compacted_dict: dict[str, Any] = {}
        truncated = False
        for key, item in value.items():
            normalized, item_truncated = _compact_event_value(item, limit=limit)
            compacted_dict[str(key)] = normalized
            truncated = truncated or item_truncated
        return compacted_dict, truncated
    return value, False


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    compacted, truncated = _compact_event_value(event)
    if not isinstance(compacted, dict):  # pragma: no cover - events are always objects
        return event
    if truncated:
        compacted["raw_truncated"] = True
    return compacted


@dataclass(slots=True)
class LogAnalyzer:
    runs_dir: Path

    def __init__(self, runs_dir: str | Path) -> None:
        self.runs_dir = Path(runs_dir).expanduser().resolve()

    def _run_dir(self, run_id: str) -> Path:
        candidate = (self.runs_dir / run_id).resolve()
        if not path_within(self.runs_dir, candidate) or candidate.parent != self.runs_dir:
            raise KeyError(run_id)
        return candidate

    def load_run(self, run_id: str) -> RunRecord:
        path = self._run_dir(run_id) / "run.json"
        try:
            return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise KeyError(run_id) from exc

    def list_runs(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.runs_dir.exists():
            return records
        for run_file in self.runs_dir.glob("*/run.json"):
            try:
                run = RunRecord.model_validate_json(run_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            statuses: dict[str, int] = {}
            trace_count = 0
            for node_id, result in run.nodes.items():
                status = _status_value(result.status)
                statuses[status] = statuses.get(status, 0) + 1
                trace_path = run_file.parent / "artifacts" / node_id / "trace.jsonl"
                if trace_path.exists():
                    try:
                        with trace_path.open("rb") as handle:
                            trace_count += sum(1 for line in handle if line.strip())
                    except OSError:
                        pass
            records.append(
                {
                    "id": run.id,
                    "name": run.pipeline.name,
                    "description": run.pipeline.description,
                    "status": _status_value(run.status),
                    "created_at": run.created_at,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "node_count": len(run.nodes),
                    "node_statuses": statuses,
                    "trace_count": trace_count,
                    "updated_at": datetime.fromtimestamp(run_file.stat().st_mtime).astimezone().isoformat(),
                }
            )
        return sorted(records, key=lambda record: _parse_timestamp(record["created_at"]), reverse=True)

    def _context(self, run: RunRecord, node: NodeSpec) -> dict[str, Any]:
        return build_render_context(
            run.pipeline,
            run.nodes,
            current_node=node,
            run_id=run.id,
            artifacts_base_dir=self.runs_dir,
        )

    def transfers(self, run: RunRecord) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for target in run.pipeline.nodes:
            context = self._context(run, target)
            referenced_sources: set[str] = set()
            for reference in extract_template_references(target.prompt):
                kind, source = _transfer_kind(reference)
                if kind == "node" and source:
                    referenced_sources.add(source)
                if kind == "fanout" and source:
                    referenced_sources.update(run.pipeline.fanouts.get(source, []))
                resolved, value = _resolve_reference(context, reference)
                key = (source or kind, target.id, ".".join(reference))
                if key in seen:
                    continue
                seen.add(key)
                edges.append(
                    {
                        "id": f"{target.id}:{len(edges)}",
                        "source_type": kind,
                        "source": source,
                        "source_nodes": (
                            [source]
                            if kind == "node" and source
                            else list(run.pipeline.fanouts.get(source, []))
                            if kind == "fanout" and source
                            else []
                        ),
                        "target": target.id,
                        "expression": ".".join(reference),
                        "field": ".".join(reference[2:]) if kind in {"node", "fanout"} else ".".join(reference[1:]),
                        "resolved": resolved,
                        "value": value,
                        "explicit": True,
                    }
                )
            for source in target.depends_on:
                if source in referenced_sources:
                    continue
                key = (source, target.id, "dependency")
                if key in seen:
                    continue
                seen.add(key)
                edges.append(
                    {
                        "id": f"{target.id}:{len(edges)}",
                        "source_type": "node",
                        "source": source,
                        "source_nodes": [source],
                        "target": target.id,
                        "expression": None,
                        "field": None,
                        "resolved": False,
                        "value": None,
                        "explicit": False,
                    }
                )
        return edges

    def run_detail(self, run_id: str) -> dict[str, Any]:
        run = self.load_run(run_id)
        node_specs = run.pipeline.node_map
        nodes_payload: list[dict[str, Any]] = []
        for node in run.pipeline.nodes:
            result = run.nodes.get(node.id)
            trace_path = self._run_dir(run_id) / "artifacts" / node.id / "trace.jsonl"
            trace_count = 0
            if trace_path.exists():
                try:
                    with trace_path.open("rb") as handle:
                        trace_count = sum(1 for line in handle if line.strip())
                except OSError:
                    pass
            nodes_payload.append(
                {
                    "id": node.id,
                    "agent": _status_value(node.agent),
                    "model": node.model,
                    "description": node.description,
                    "depends_on": node.depends_on,
                    "status": _status_value(result.status) if result else "pending",
                    "attempts": len(result.attempts) if result else 0,
                    "current_attempt": result.current_attempt if result else 0,
                    "started_at": result.started_at if result else None,
                    "finished_at": result.finished_at if result else None,
                    "exit_code": result.exit_code if result else None,
                    "trace_count": trace_count,
                    "output_preview": (result.output or "")[:180] if result else "",
                }
            )
        return {
            "id": run.id,
            "name": run.pipeline.name,
            "description": run.pipeline.description,
            "status": _status_value(run.status),
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "working_dir": run.pipeline.working_dir,
            "concurrency": run.pipeline.concurrency,
            "nodes": nodes_payload,
            "fanouts": run.pipeline.fanouts,
            "transfers": [
                {key: value for key, value in edge.items() if key != "value"}
                for edge in self.transfers(run)
            ],
        }

    def _launches(self, run_id: str, node_id: str) -> list[dict[str, Any]]:
        artifact_dir = self._run_dir(run_id) / "artifacts" / node_id
        launches: list[dict[str, Any]] = []
        for path in sorted(artifact_dir.glob("launch-attempt-*.json")):
            payload = _read_json(path)
            if isinstance(payload, dict):
                launches.append(payload)
        if not launches:
            payload = _read_json(artifact_dir / "launch.json")
            if isinstance(payload, dict):
                launches.append(payload)
        return launches

    def _prompt_captures(self, run_id: str, node_id: str) -> list[dict[str, Any]]:
        artifact_dir = self._run_dir(run_id) / "artifacts" / node_id
        captures: list[dict[str, Any]] = []
        for path in sorted(artifact_dir.glob("prompt-attempt-*.json")):
            payload = _read_json(path)
            if isinstance(payload, dict):
                captures.append(payload)
        if not captures:
            payload = _read_json(artifact_dir / "prompt.json")
            if isinstance(payload, dict):
                captures.append(payload)
        return captures

    def _trace_events(self, run_id: str, node_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        trace_path = self._run_dir(run_id) / "artifacts" / node_id / "trace.jsonl"
        events: list[dict[str, Any]] = []
        parse_errors: list[dict[str, Any]] = []
        if not trace_path.exists():
            return events, parse_errors
        try:
            with trace_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        parse_errors.append({"line_number": line_number, "error": str(exc), "preview": line[:240]})
                        continue
                    if not isinstance(event, dict):
                        parse_errors.append({"line_number": line_number, "error": "JSONL value is not an object", "preview": line[:240]})
                        continue
                    events.append(
                        {
                            **event,
                            "event_type": "trace",
                            "line_number": line_number,
                            "category": _event_category(event),
                            "summary": _event_summary(event),
                        }
                    )
        except OSError:
            pass
        return events, parse_errors

    def _usage_summary(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        latest_by_attempt: dict[int, dict[str, Any]] = {}
        for event in events:
            payloads = _find_usage_payloads(event.get("raw"))
            if not payloads:
                continue
            attempt = int(event.get("attempt") or 1)
            normalized = _normalize_usage(payloads[-1])
            if not any(value is not None for value in normalized.values()):
                continue
            latest_by_attempt[attempt] = {
                "attempt": attempt,
                "timestamp": event.get("timestamp"),
                "reported_by": event.get("title") or event.get("kind"),
                **normalized,
            }
        attempts = [latest_by_attempt[key] for key in sorted(latest_by_attempt)]
        totals: dict[str, int | None] = {}
        token_fields = (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
        )
        for field in token_fields:
            values = [attempt[field] for attempt in attempts if attempt.get(field) is not None]
            totals[field] = sum(values) if values else None
        return {
            "reported": bool(attempts),
            "note": None if attempts else "该 Agent/Provider 没有在 JSONL 事件流中上报 Token 用量。",
            "attempts": attempts,
            **totals,
        }

    def _activity_summary(self, events: list[dict[str, Any]], node: NodeSpec) -> dict[str, Any]:
        categories: dict[str, int] = {}
        commands: list[dict[str, Any]] = []
        tools: dict[str, int] = {}
        pending_commands: dict[str, dict[str, Any]] = {}
        mcp_calls: dict[str, int] = {}
        for event in events:
            category = str(event.get("category") or "output")
            categories[category] = categories.get(category, 0) + 1
            command, command_id, exit_code, output = _command_info(event)
            if command:
                status = str(_raw_item(event).get("status") or "")
                entry = {
                    "id": command_id,
                    "command": command,
                    "status": status or ("completed" if exit_code is not None else "started"),
                    "exit_code": exit_code,
                    "output_bytes": len(output.encode("utf-8")) if output else 0,
                    "timestamp": event.get("timestamp"),
                    "attempt": event.get("attempt"),
                    "line_number": event.get("line_number"),
                }
                if str(event.get("kind")) == "item_started" and command_id:
                    pending_commands[command_id] = entry
                elif str(event.get("kind")) == "item_completed":
                    if command_id:
                        pending_commands.pop(command_id, None)
                    commands.append(entry)
            name = _tool_name(event)
            if name:
                tools[name] = tools.get(name, 0) + 1
                lowered = name.lower()
                if lowered.startswith("mcp") or "mcp__" in lowered:
                    mcp_calls[name] = mcp_calls.get(name, 0) + 1
        commands.extend({**entry, "status": "in_progress"} for entry in pending_commands.values())
        return {
            "event_count": len(events),
            "categories": categories,
            "commands": commands,
            "command_count": len(commands),
            "failed_command_count": sum(1 for command in commands if command.get("exit_code") not in {None, 0}),
            "pending_command_count": len(pending_commands),
            "tools": [{"name": name, "calls": count} for name, count in sorted(tools.items())],
            "configured_tool_access": _status_value(node.tools),
            "configured_mcps": [mcp.model_dump(mode="json") for mcp in node.mcps],
            "observed_mcp_calls": [{"name": name, "calls": count} for name, count in sorted(mcp_calls.items())],
            "configured_skills": list(node.skills),
        }

    def _diagnostics(
        self,
        run: RunRecord,
        node: NodeSpec,
        events: list[dict[str, Any]],
        activity: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result = run.nodes.get(node.id)
        artifact_dir = self._run_dir(run.id) / "artifacts" / node.id
        stderr = ""
        stderr_path = artifact_dir / "stderr.log"
        if stderr_path.exists():
            try:
                stderr = stderr_path.read_text(encoding="utf-8", errors="replace")[-200_000:]
            except OSError:
                pass
        diagnostics: list[dict[str, Any]] = []

        def add(severity: str, code: str, title: str, evidence: str, suggestion: str | None = None) -> None:
            diagnostics.append(
                {
                    "severity": severity,
                    "code": code,
                    "title": title,
                    "evidence": evidence,
                    "suggestion": suggestion,
                }
            )

        if result and (result.exit_code == 124 or "timed out after" in stderr.lower()):
            timeout_match = re.search(r"Timed out after\s+([^\n]+)", stderr, re.IGNORECASE)
            timeout_text = timeout_match.group(0) if timeout_match else f"退出码 {result.exit_code}"
            add(
                "error",
                "node_timeout",
                "节点超过执行超时限制",
                f"{timeout_text}；配置的超时时间为 {node.timeout_seconds} 秒。",
                "检查最后一个未结束步骤，并为可能输出大量内容的命令设置边界。",
            )
        pending = [command for command in activity["commands"] if command.get("status") == "in_progress"]
        for command in pending:
            add(
                "error" if result and _status_value(result.status) == "failed" else "warning",
                "unfinished_command",
                "命令已启动但未正常结束",
                command["command"],
                "这是 Agent 卡住或被终止位置的最强候选证据。",
            )
        for command in activity["commands"]:
            if command.get("exit_code") not in {None, 0}:
                add(
                    "error",
                    "command_failed",
                    f"命令以退出码 {command['exit_code']} 结束",
                    command["command"],
                    "展开对应的追踪事件以查看完整输出。",
                )
            if command.get("output_bytes", 0) >= 1_000_000:
                add(
                    "warning",
                    "large_tool_output",
                    "命令向 Agent 上下文注入了大量输出",
                    f"{command['output_bytes']:,} 字节：{command['command']}",
                    "建议使用有字节限制的提取方式，例如 `head -c`、定向 `rg` 或只输出必要字段的解析器。",
                )
        candidate_commands = pending or activity["commands"][-3:]
        for command in candidate_commands:
            text = command["command"]
            if not re.search(r"\b(cat|sed|head\s+-n)\b", text):
                continue
            command_cwd = getattr(node.target, "cwd", None) or run.pipeline.working_dir
            for path in _possible_command_files(text, command_cwd):
                try:
                    size = path.stat().st_size
                    if size < 1_000_000:
                        continue
                    with path.open("rb") as handle:
                        sample = handle.read(min(size, 256_000))
                    newline_count = sample.count(b"\n")
                except OSError:
                    continue
                add(
                    "error" if command in pending else "warning",
                    "unbounded_large_file_read",
                    "按行读取命令访问了体积大且换行很少的文件",
                    f"{path} 大小为 {size:,} 字节；前 {len(sample):,} 字节仅含 {newline_count} 个换行符。命令：{text}",
                    "请改用限制字节数的读取（`head -c`），或只提取匹配 token，避免输出整行内容。",
                )
        if result and _status_value(result.status) in {"running", "retrying"} and events:
            last_timestamp = events[-1].get("timestamp")
            idle_seconds = _seconds_between(last_timestamp, datetime.now(timezone.utc).isoformat())
            if idle_seconds is not None and idle_seconds >= 60:
                add(
                    "warning",
                    "no_recent_activity",
                    "最近没有上报结构化活动",
                    f"最后一条追踪事件位于 {last_timestamp}，距今约 {idle_seconds:.0f} 秒。",
                    "模型可能在静默推理、阻塞于工具调用，或等待 Provider 响应；取消前请先检查进程日志。",
                )
        error_events = [event for event in events if event.get("category") == "error"]
        seen_errors: set[str] = set()
        for event in error_events[-5:]:
            evidence = str(event.get("summary") or event.get("title") or "Agent 错误")
            if evidence in seen_errors:
                continue
            seen_errors.add(evidence)
            add(
                "error",
                "agent_or_tool_error",
                str(event.get("title") or "Agent 或工具报告了错误"),
                evidence,
                "展开时间线中的对应错误事件，检查 Provider 的原始 payload。",
            )
        if not diagnostics and result and _status_value(result.status) == "completed":
            add("info", "healthy_completion", "未发现明显执行问题", "节点已完成，且未发现失败或未结束的命令。")
        return diagnostics

    def node_detail(self, run_id: str, node_id: str) -> dict[str, Any]:
        run = self.load_run(run_id)
        node = run.pipeline.node_map.get(node_id)
        if node is None:
            raise KeyError(node_id)
        result = run.nodes.get(node_id)
        trace_events, parse_errors = self._trace_events(run_id, node_id)
        activity = self._activity_summary(trace_events, node)
        usage = self._usage_summary(trace_events)
        launches = self._launches(run_id, node_id)
        latest_launch = launches[-1] if launches else None
        prompt_captures = self._prompt_captures(run_id, node_id)
        latest_prompt_capture = prompt_captures[-1] if prompt_captures else None
        transfers = self.transfers(run)
        artifact_dir = self._run_dir(run_id) / "artifacts" / node_id
        artifacts = []
        if artifact_dir.exists():
            for path in sorted(artifact_dir.iterdir()):
                if path.is_file() and (_SAFE_ARTIFACT_NAME.fullmatch(path.name)):
                    artifacts.append(_file_meta(path))
        result_payload = result.model_dump(mode="json") if result else None
        if result_payload:
            result_payload.pop("trace_events", None)
            result_payload.pop("stdout_lines", None)
            result_payload.pop("stderr_lines", None)
        codex_rollouts: list[dict[str, Any]] = []
        seen_session_ids: set[str] = set()
        for event in trace_events:
            raw = event.get("raw")
            if not isinstance(raw, dict) or raw.get("type") != "thread.started":
                continue
            session_id = raw.get("thread_id")
            if not isinstance(session_id, str) or not session_id or session_id in seen_session_ids:
                continue
            seen_session_ids.add(session_id)
            codex_rollouts.append(
                {
                    "session_id": session_id,
                    "attempt": event.get("attempt"),
                    "timestamp": event.get("timestamp"),
                }
            )
        return {
            "id": node_id,
            "spec": node.model_dump(mode="json"),
            "result": result_payload,
            "context": {
                "prompt_template": node.prompt,
                "rendered_prompt": (
                    latest_prompt_capture.get("rendered_pipeline_prompt")
                    if latest_prompt_capture
                    else _extract_prompt(node, latest_launch)
                ),
                "rendered_prompt_source": "prompt.json" if latest_prompt_capture else "launch.json" if latest_launch else None,
                "agent_input": latest_prompt_capture.get("agent_input") if latest_prompt_capture else _extract_prompt(node, latest_launch),
                "agent_input_source": "prompt.json" if latest_prompt_capture else "launch.json" if latest_launch else None,
                "references": [".".join(reference) for reference in extract_template_references(node.prompt)],
                "launch": latest_launch,
                "launches": launches,
                "prompt_capture": latest_prompt_capture,
                "prompt_captures": prompt_captures,
            },
            "inbound": [edge for edge in transfers if edge["target"] == node_id],
            "outbound": [edge for edge in transfers if node_id in edge.get("source_nodes", [])],
            "artifacts": artifacts,
            "activity": activity,
            "usage": usage,
            "codex_rollouts": codex_rollouts,
            "diagnostics": self._diagnostics(run, node, trace_events, activity),
            "parse_errors": parse_errors,
        }

    def node_events(
        self,
        run_id: str,
        node_id: str,
        *,
        offset: int = 0,
        limit: int = 200,
        attempt: int | None = None,
        category: str | None = None,
        query: str | None = None,
        order: str = "asc",
    ) -> dict[str, Any]:
        run = self.load_run(run_id)
        if node_id not in run.pipeline.node_map:
            raise KeyError(node_id)
        events, parse_errors = self._trace_events(run_id, node_id)
        for index, lifecycle in enumerate(self._read_lifecycle_events(run_id, node_id), 1):
            events.append(
                {
                    **lifecycle,
                    "event_type": "lifecycle",
                    "line_number": index,
                    "attempt": lifecycle.get("data", {}).get("attempt"),
                    "source": "system",
                    "kind": lifecycle.get("type"),
                    "title": str(lifecycle.get("type") or "Lifecycle event").replace("_", " ").title(),
                    "content": lifecycle.get("data"),
                    "raw": lifecycle,
                    "category": "error" if lifecycle.get("type") == "node_failed" else "lifecycle",
                    "summary": _event_summary({"title": lifecycle.get("type"), "content": lifecycle.get("data")}),
                }
            )
        events.sort(key=lambda event: (_parse_timestamp(event.get("timestamp")), event.get("line_number", 0)))
        if attempt is not None:
            events = [event for event in events if event.get("attempt") == attempt]
        if query:
            needle = query.casefold()
            events = [event for event in events if needle in json.dumps(event, ensure_ascii=False, default=str).casefold()]
        categories: dict[str, int] = {}
        for event in events:
            event_category = event["category"]
            categories[event_category] = categories.get(event_category, 0) + 1
        if category and category != "all":
            events = [event for event in events if event.get("category") == category]
        if order == "desc":
            events.reverse()
        total = len(events)
        page = [_compact_event(event) for event in events[offset : offset + limit]]
        return {
            "items": page,
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + len(page) < total,
            "categories": categories,
            "parse_errors": parse_errors,
        }

    def event_detail(self, run_id: str, node_id: str, line_number: int) -> dict[str, Any]:
        run = self.load_run(run_id)
        if node_id not in run.pipeline.node_map or line_number < 1:
            raise KeyError(node_id)
        path = self._run_dir(run_id) / "artifacts" / node_id / "trace.jsonl"
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for current_line, line in enumerate(handle, 1):
                    if current_line != line_number:
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise KeyError(line_number)
                    return {
                        **event,
                        "event_type": "trace",
                        "line_number": line_number,
                        "category": _event_category(event),
                        "summary": _event_summary(event),
                    }
        except (OSError, json.JSONDecodeError) as exc:
            raise KeyError(line_number) from exc
        raise KeyError(line_number)

    def _read_lifecycle_events(self, run_id: str, node_id: str) -> list[dict[str, Any]]:
        path = self._run_dir(run_id) / "events.jsonl"
        events: list[dict[str, Any]] = []
        if not path.exists():
            return events
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and event.get("node_id") == node_id and event.get("type") != "node_trace":
                        events.append(event)
        except OSError:
            pass
        return events

    def artifact_chunk(
        self,
        run_id: str,
        node_id: str,
        name: str,
        *,
        offset: int = 0,
        limit: int = 200_000,
    ) -> dict[str, Any]:
        run = self.load_run(run_id)
        if node_id not in run.pipeline.node_map or not _SAFE_ARTIFACT_NAME.fullmatch(name):
            raise KeyError(name)
        path = (self._run_dir(run_id) / "artifacts" / node_id / name).resolve()
        artifact_dir = (self._run_dir(run_id) / "artifacts" / node_id).resolve()
        if not path_within(artifact_dir, path) or not path.is_file():
            raise KeyError(name)
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(min(offset, size))
            raw = handle.read(limit)
        text = raw.decode("utf-8", errors="replace")
        parsed: Any | None = None
        if offset == 0 and size <= limit and name in _JSON_ARTIFACTS:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
        return {
            "name": name,
            "offset": offset,
            "next_offset": offset + len(raw),
            "size": size,
            "has_more": offset + len(raw) < size,
            "content": text,
            "parsed": parsed,
        }
