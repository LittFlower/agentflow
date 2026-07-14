from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from agentflow.utils import path_within


_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9-]+$")
_STRING_PREVIEW_LIMIT = 64_000
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
_CALL_TYPES = {"function_call", "custom_tool_call", "web_search_call"}
_CALL_OUTPUT_TYPES = {"function_call_output", "custom_tool_call_output"}


def _timestamp_key(value: str | None) -> datetime:
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.min


def _duration_seconds(start: str | None, end: str | None) -> float | None:
    started = _timestamp_key(start)
    finished = _timestamp_key(end)
    if started == datetime.min or finished == datetime.min:
        return None
    return max(0.0, (finished - started).total_seconds())


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("payload")
    return value if isinstance(value, dict) else {}


def _inner_type(record: dict[str, Any]) -> str:
    payload = _payload(record)
    return str(payload.get("type") or record.get("type") or "unknown")


def _metadata_turn_id(payload: dict[str, Any]) -> str | None:
    direct = payload.get("turn_id")
    if isinstance(direct, str) and direct:
        return direct
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(metadata, dict):
        value = metadata.get("turn_id")
        if isinstance(value, str) and value:
            return value
    return None


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_flatten_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "message", "output_text", "input_text"):
            if key in value:
                text = _flatten_text(value[key])
                if text:
                    return text
        parts = [_flatten_text(item) for item in value.values()]
        return "\n".join(part for part in parts if part)
    return ""


def _message_text(payload: dict[str, Any]) -> str:
    for key in ("message", "content", "last_agent_message"):
        if key in payload:
            text = _flatten_text(payload.get(key))
            if text:
                return text
    return ""


def _preview(value: Any, limit: int = 240) -> str:
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            value = str(value)
    compact = " ".join(value.strip().split())
    return compact[:limit] + ("..." if len(compact) > limit else "")


def _bounded_text(value: str, limit: int = _STRING_PREVIEW_LIMIT) -> tuple[str, bool, int]:
    characters = len(value)
    if characters <= limit:
        return value, False, characters
    return value[:limit], True, characters


def _is_user_title_candidate(value: str) -> bool:
    text = value.lstrip().casefold()
    injected_prefixes = (
        "<environment_context",
        "<permissions instructions",
        "<collaboration_mode",
        "<skills_instructions",
        "<multi_agent_mode",
    )
    return bool(text) and not text.startswith(injected_prefixes)


def _usage(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {field: 0 for field in _TOKEN_FIELDS}
    result: dict[str, int] = {}
    for field in _TOKEN_FIELDS:
        value = payload.get(field)
        result[field] = int(value) if isinstance(value, (int, float)) else 0
    return result


def _usage_delta(current: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    return {field: max(0, current.get(field, 0) - baseline.get(field, 0)) for field in _TOKEN_FIELDS}


def _compact(value: Any, *, limit: int = _STRING_PREVIEW_LIMIT) -> tuple[Any, bool]:
    if isinstance(value, str):
        if len(value) <= limit:
            return value, False
        return {"_truncated": True, "characters": len(value), "preview": value[:limit]}, True
    if isinstance(value, list):
        result: list[Any] = []
        truncated = False
        for item in value:
            compacted, item_truncated = _compact(item, limit=limit)
            result.append(compacted)
            truncated = truncated or item_truncated
        return result, truncated
    if isinstance(value, dict):
        result_dict: dict[str, Any] = {}
        truncated = False
        for key, item in value.items():
            compacted, item_truncated = _compact(item, limit=limit)
            result_dict[str(key)] = compacted
            truncated = truncated or item_truncated
        return result_dict, truncated
    return value, False


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    compacted, truncated = _compact(event)
    if not isinstance(compacted, dict):
        return event
    if truncated:
        compacted["raw_truncated"] = True
    return compacted


def _parse_json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _call_failure(output: Any) -> tuple[bool, int | None]:
    if isinstance(output, dict):
        for key in ("exit_code", "exitCode"):
            value = output.get(key)
            if isinstance(value, (int, float)):
                return int(value) != 0, int(value)
        for key in ("output", "content", "result"):
            if key in output:
                failed, code = _call_failure(output[key])
                if failed:
                    return failed, code
        return bool(output.get("error")), None
    if isinstance(output, list):
        for item in output:
            failed, code = _call_failure(item)
            if failed:
                return failed, code
        return False, None
    text = str(output or "")
    match = re.search(r"(?:exited with code|Process exited with code|exit_code[\"']?\s*[:=])\s*(-?\d+)", text)
    if match:
        code = int(match.group(1))
        return code != 0, code
    lowered = text.casefold()
    return ("is_error" in lowered and "true" in lowered) or lowered.startswith("error:"), None


def _event_category(outer_type: str, inner_type: str, payload: dict[str, Any]) -> str:
    lowered = inner_type.casefold()
    if lowered in {"turn_aborted", "error", "stream_error"} or "error" in lowered:
        return "error"
    if outer_type == "response_item" and inner_type == "reasoning":
        return "reasoning"
    if inner_type == "token_count":
        return "usage"
    if inner_type in _CALL_TYPES | _CALL_OUTPUT_TYPES or "tool" in lowered or "web_search" in lowered:
        return "tool"
    if inner_type in {"message", "user_message", "agent_message"}:
        return "message"
    if outer_type in {"turn_context", "world_state", "compacted"} or inner_type in {
        "context_compacted",
        "thread_rolled_back",
        "thread_settings_applied",
    }:
        return "context"
    if outer_type == "session_meta" or inner_type in {"task_started", "task_complete", "patch_apply_end"}:
        return "lifecycle"
    if "output" in lowered:
        return "output"
    return "lifecycle"


def _event_content(outer_type: str, inner_type: str, payload: dict[str, Any]) -> Any:
    if inner_type in {"message", "user_message", "agent_message"}:
        return _message_text(payload)
    if inner_type == "reasoning":
        return _flatten_text(payload.get("summary"))
    if inner_type in _CALL_TYPES:
        return _parse_json_value(payload.get("arguments", payload.get("input", payload.get("query"))))
    if inner_type in _CALL_OUTPUT_TYPES:
        return _parse_json_value(payload.get("output"))
    if inner_type == "token_count":
        return payload.get("info")
    if inner_type in {"task_complete", "turn_aborted"}:
        return {key: value for key, value in payload.items() if key != "type"}
    if outer_type == "turn_context":
        return {
            key: payload.get(key)
            for key in ("turn_id", "cwd", "model", "effort", "approval_policy", "sandbox_policy")
            if payload.get(key) is not None
        }
    return None


def _event_title(outer_type: str, inner_type: str, payload: dict[str, Any]) -> str:
    if outer_type == "session_meta":
        return "Codex session metadata"
    if outer_type == "turn_context":
        return "Turn context"
    titles = {
        "task_started": "Turn started",
        "task_complete": "Turn completed",
        "turn_aborted": "Turn aborted",
        "token_count": "Token usage snapshot",
        "context_compacted": "Context compacted",
        "thread_rolled_back": "Thread rolled back",
        "thread_settings_applied": "Thread settings applied",
        "reasoning": "Model reasoning",
        "function_call_output": "Function result",
        "custom_tool_call_output": "Tool result",
        "web_search_call": "Web search",
    }
    if inner_type == "message":
        return f"{str(payload.get('role') or 'unknown').title()} message"
    if inner_type in {"user_message", "agent_message"}:
        return "User message" if inner_type == "user_message" else "Assistant message"
    if inner_type in _CALL_TYPES:
        return f"Tool call: {payload.get('name') or payload.get('query') or inner_type}"
    return titles.get(inner_type, inner_type.replace("_", " ").title())


def _event_summary(inner_type: str, payload: dict[str, Any], content: Any) -> str:
    if inner_type == "token_count":
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        last = _usage(info.get("last_token_usage"))
        total = _usage(info.get("total_token_usage"))
        window = info.get("model_context_window")
        return f"context {last['input_tokens']:,} / {window or '?'}; cumulative {total['total_tokens']:,} tokens"
    if inner_type in _CALL_OUTPUT_TYPES:
        failed, exit_code = _call_failure(content)
        prefix = f"exit {exit_code}" if exit_code is not None else "failed" if failed else "completed"
        return f"{prefix}: {_preview(content)}"
    if content not in (None, "", [], {}):
        return _preview(content)
    if inner_type == "reasoning" and payload.get("encrypted_content"):
        return "Encrypted reasoning content; no plaintext summary was recorded"
    return _preview({key: value for key, value in payload.items() if key != "type"})


def _normalized_event(
    record: dict[str, Any], line_number: int, current_turn_id: str | None
) -> dict[str, Any]:
    payload = _payload(record)
    outer_type = str(record.get("type") or "unknown")
    inner_type = _inner_type(record)
    turn_id = _metadata_turn_id(payload) or current_turn_id
    content = _event_content(outer_type, inner_type, payload)
    return {
        "timestamp": record.get("timestamp"),
        "event_type": "codex_rollout",
        "line_number": line_number,
        "turn_id": turn_id,
        "attempt": None,
        "source": "codex",
        "kind": f"{outer_type}:{inner_type}" if outer_type != inner_type else outer_type,
        "title": _event_title(outer_type, inner_type, payload),
        "content": content,
        "raw": record,
        "category": _event_category(outer_type, inner_type, payload),
        "summary": _event_summary(inner_type, payload, content),
    }


def _iter_records(path: Path) -> Iterable[tuple[int, dict[str, Any] | None, dict[str, Any] | None]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    yield line_number, None, {
                        "line_number": line_number,
                        "error": str(exc),
                        "preview": line[:240],
                    }
                    continue
                if not isinstance(record, dict):
                    yield line_number, None, {
                        "line_number": line_number,
                        "error": "JSONL value is not an object",
                        "preview": line[:240],
                    }
                    continue
                yield line_number, record, None
    except OSError:
        return


def _session_status(started: int, completed: int, aborted: int) -> str:
    if started > completed + aborted:
        return "running"
    if aborted and not completed:
        return "cancelled"
    if aborted:
        return "completed_with_aborts"
    if completed:
        return "completed"
    return "idle"


@lru_cache(maxsize=1024)
def _scan_rollout_cached(path_string: str, mtime_ns: int, size: int) -> dict[str, Any]:
    del mtime_ns
    path = Path(path_string)
    metadata: dict[str, Any] = {}
    created_at: str | None = None
    updated_at: str | None = None
    user_title = ""
    started = completed = aborted = event_count = parse_error_count = 0
    tool_calls = message_count = context_compactions = 0
    latest_usage = _usage(None)
    model: str | None = None
    for _, record, error in _iter_records(path):
        if error:
            parse_error_count += 1
            continue
        assert record is not None
        event_count += 1
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str):
            created_at = created_at or timestamp
            updated_at = timestamp
        outer_type = str(record.get("type") or "")
        payload = _payload(record)
        inner_type = _inner_type(record)
        if outer_type == "session_meta" and not metadata:
            metadata = payload
            created_at = str(payload.get("timestamp") or created_at or "") or None
        if outer_type == "turn_context" and isinstance(payload.get("model"), str):
            model = payload["model"]
        if inner_type == "task_started":
            started += 1
        elif inner_type == "task_complete":
            completed += 1
        elif inner_type == "turn_aborted":
            aborted += 1
        elif inner_type == "context_compacted":
            context_compactions += 1
        if inner_type in _CALL_TYPES:
            tool_calls += 1
        if inner_type == "message" and payload.get("role") in {"user", "assistant"}:
            message_count += 1
            if payload.get("role") == "user":
                candidate = _message_text(payload)
                if _is_user_title_candidate(candidate):
                    user_title = candidate
        elif inner_type == "user_message":
            candidate = _message_text(payload)
            if _is_user_title_candidate(candidate):
                user_title = candidate
        if inner_type == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            latest_usage = _usage(info.get("total_token_usage"))
    session_id = str(metadata.get("session_id") or metadata.get("id") or path.stem.rsplit("-", 1)[-1])
    title = _preview(user_title, 100) or f"Codex session {session_id[:8]}"
    return {
        "id": session_id,
        "name": title,
        "title": title,
        "status": _session_status(started, completed, aborted),
        "created_at": created_at,
        "updated_at": updated_at,
        "cwd": metadata.get("cwd"),
        "originator": metadata.get("originator"),
        "source": metadata.get("source"),
        "thread_source": metadata.get("thread_source"),
        "model_provider": metadata.get("model_provider"),
        "model": model,
        "cli_version": metadata.get("cli_version"),
        "event_count": event_count,
        "trace_count": event_count,
        "turn_count": started,
        "completed_turns": completed,
        "aborted_turns": aborted,
        "tool_calls": tool_calls,
        "message_count": message_count,
        "context_compactions": context_compactions,
        "parse_error_count": parse_error_count,
        "total_tokens": latest_usage["total_tokens"],
        "size": size,
    }


def _new_turn(turn_id: str, timestamp: str | None, index: int, baseline: dict[str, int]) -> dict[str, Any]:
    return {
        "id": turn_id,
        "index": index,
        "status": "running",
        "started_at": timestamp,
        "finished_at": None,
        "duration_ms": None,
        "time_to_first_token_ms": None,
        "user_message": "",
        "final_response": "",
        "event_count": 0,
        "message_count": 0,
        "tool_call_count": 0,
        "reasoning_count": 0,
        "model": None,
        "cwd": None,
        "usage": _usage(None),
        "_baseline_usage": dict(baseline),
    }


@lru_cache(maxsize=2)
def _load_rollout_cached(path_string: str, mtime_ns: int, size: int) -> dict[str, Any]:
    del mtime_ns, size
    path = Path(path_string)
    metadata: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    calls_by_id: dict[str, dict[str, Any]] = {}
    turns: dict[str, dict[str, Any]] = {}
    turn_order: list[str] = []
    current_turn_id: str | None = None
    latest_total_usage = _usage(None)
    latest_last_usage = _usage(None)
    latest_rate_limits: dict[str, Any] | None = None
    context_window: int | None = None
    peak_context_tokens = 0
    model_calls = 0
    context_compactions = 0
    rollbacks = 0
    reasoning_items = 0
    reasoning_summaries = 0
    encrypted_reasoning_items = 0
    output_bytes = 0

    for line_number, record, parse_error in _iter_records(path):
        if parse_error:
            parse_errors.append(parse_error)
            continue
        assert record is not None
        outer_type = str(record.get("type") or "")
        payload = _payload(record)
        inner_type = _inner_type(record)
        explicit_turn_id = _metadata_turn_id(payload)
        if inner_type == "task_started":
            current_turn_id = explicit_turn_id or f"turn-{len(turn_order) + 1}"
            if current_turn_id not in turns:
                turns[current_turn_id] = _new_turn(
                    current_turn_id,
                    record.get("timestamp"),
                    len(turn_order) + 1,
                    latest_total_usage,
                )
                turn_order.append(current_turn_id)
        event = _normalized_event(record, line_number, current_turn_id)
        events.append(event)
        turn_id = event.get("turn_id")
        turn = turns.get(turn_id) if isinstance(turn_id, str) else None
        if turn:
            turn["event_count"] += 1

        if outer_type == "session_meta" and not metadata:
            metadata = payload
        if outer_type == "turn_context":
            context_turn_id = explicit_turn_id or current_turn_id
            context_turn = turns.get(context_turn_id) if context_turn_id else None
            if context_turn:
                context_turn["model"] = payload.get("model")
                context_turn["cwd"] = payload.get("cwd")
        if inner_type == "message":
            role = str(payload.get("role") or "unknown")
            text = _message_text(payload)
            bounded_text, text_truncated, text_characters = _bounded_text(text)
            message = {
                "line_number": line_number,
                "timestamp": record.get("timestamp"),
                "turn_id": turn_id,
                "role": role,
                "phase": payload.get("phase"),
                "text": bounded_text,
                "text_truncated": text_truncated,
                "text_characters": text_characters,
            }
            messages.append(message)
            if turn and role in {"user", "assistant"}:
                turn["message_count"] += 1
                if role == "user" and text and not turn["user_message"]:
                    turn["user_message"] = bounded_text
                if role == "assistant" and text:
                    turn["final_response"] = bounded_text
        elif inner_type == "task_complete" and turn:
            turn["status"] = "completed"
            turn["finished_at"] = record.get("timestamp")
            turn["duration_ms"] = payload.get("duration_ms")
            turn["time_to_first_token_ms"] = payload.get("time_to_first_token_ms")
            if isinstance(payload.get("last_agent_message"), str):
                turn["final_response"] = _bounded_text(payload["last_agent_message"])[0]
            current_turn_id = None
        elif inner_type == "turn_aborted" and turn:
            turn["status"] = "cancelled"
            turn["finished_at"] = record.get("timestamp")
            turn["duration_ms"] = payload.get("duration_ms")
            turn["abort_reason"] = payload.get("reason")
            current_turn_id = None
        elif inner_type == "token_count":
            model_calls += 1
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            latest_total_usage = _usage(info.get("total_token_usage"))
            latest_last_usage = _usage(info.get("last_token_usage"))
            if isinstance(info.get("model_context_window"), (int, float)):
                context_window = int(info["model_context_window"])
            peak_context_tokens = max(peak_context_tokens, latest_last_usage["input_tokens"])
            latest_rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else None
            if turn:
                turn["usage"] = _usage_delta(latest_total_usage, turn["_baseline_usage"])
        elif inner_type == "context_compacted":
            context_compactions += 1
        elif inner_type == "thread_rolled_back":
            rollbacks += 1
        elif inner_type == "reasoning":
            reasoning_items += 1
            summary = _flatten_text(payload.get("summary"))
            if summary:
                reasoning_summaries += 1
            if payload.get("encrypted_content"):
                encrypted_reasoning_items += 1
            if turn:
                turn["reasoning_count"] += 1

        if inner_type in _CALL_TYPES:
            call_id = str(payload.get("call_id") or payload.get("id") or f"line-{line_number}")
            call_input = _parse_json_value(payload.get("arguments", payload.get("input", payload.get("query"))))
            compacted_input, input_truncated = _compact(call_input)
            call = {
                "id": call_id,
                "type": inner_type,
                "name": str(payload.get("name") or ("web_search" if inner_type == "web_search_call" else inner_type)),
                "turn_id": turn_id,
                "started_at": record.get("timestamp"),
                "finished_at": None,
                "duration_seconds": None,
                "status": "running",
                "line_number": line_number,
                "result_line_number": None,
                "input": compacted_input,
                "input_truncated": input_truncated,
                "input_preview": _preview(call_input),
                "output_preview": "",
                "output_bytes": 0,
                "exit_code": None,
            }
            calls.append(call)
            calls_by_id[call_id] = call
            if turn:
                turn["tool_call_count"] += 1
        elif inner_type in _CALL_OUTPUT_TYPES:
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            call = calls_by_id.get(call_id)
            if call is None:
                call = {
                    "id": call_id or f"result-line-{line_number}",
                    "type": inner_type,
                    "name": "unmatched_result",
                    "turn_id": turn_id,
                    "started_at": None,
                    "finished_at": record.get("timestamp"),
                    "duration_seconds": None,
                    "status": "unmatched_result",
                    "line_number": None,
                    "result_line_number": line_number,
                    "input": None,
                    "input_preview": "",
                    "output_preview": "",
                    "output_bytes": 0,
                    "exit_code": None,
                }
                calls.append(call)
            output = _parse_json_value(payload.get("output"))
            serialized = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
            failed, exit_code = _call_failure(output)
            call.update(
                {
                    "finished_at": record.get("timestamp"),
                    "duration_seconds": _duration_seconds(call.get("started_at"), record.get("timestamp")),
                    "status": "failed" if failed else "completed",
                    "result_line_number": line_number,
                    "output_preview": _preview(output, 1000),
                    "output_bytes": len(serialized.encode("utf-8")),
                    "exit_code": exit_code,
                }
            )
            output_bytes += call["output_bytes"]

    for turn_id in turn_order:
        turn = turns[turn_id]
        turn.pop("_baseline_usage", None)
        if turn["finished_at"] is None and events:
            turn["duration_seconds"] = _duration_seconds(turn["started_at"], events[-1].get("timestamp"))
        else:
            turn["duration_seconds"] = (
                float(turn["duration_ms"]) / 1000
                if isinstance(turn.get("duration_ms"), (int, float))
                else _duration_seconds(turn["started_at"], turn["finished_at"])
            )

    parsed_turns = [turns[turn_id] for turn_id in turn_order]
    call_counts = Counter(call["name"] for call in calls if call["name"] != "unmatched_result")
    category_counts = Counter(str(event["category"]) for event in events)
    role_counts = Counter(message["role"] for message in messages)
    failed_calls = [call for call in calls if call["status"] == "failed"]
    pending_calls = [call for call in calls if call["status"] == "running"]
    unmatched_results = [call for call in calls if call["status"] == "unmatched_result"]
    diagnostics: list[dict[str, Any]] = []

    def add(severity: str, code: str, title: str, evidence: str, suggestion: str | None = None) -> None:
        diagnostics.append(
            {"severity": severity, "code": code, "title": title, "evidence": evidence, "suggestion": suggestion}
        )

    if parse_errors:
        add(
            "error",
            "invalid_jsonl",
            f"{len(parse_errors)} 行 rollout 无法解析",
            f"首个错误位于第 {parse_errors[0]['line_number']} 行：{parse_errors[0]['error']}",
            "原始文件可能仍在写入；刷新后若错误仍在，再检查文件完整性。",
        )
    cancelled_turns = [turn for turn in parsed_turns if turn["status"] == "cancelled"]
    if cancelled_turns:
        add(
            "warning",
            "aborted_turns",
            f"{len(cancelled_turns)} 个 turn 被中止",
            "；".join(
                f"#{turn['index']} {turn.get('abort_reason') or '未记录原因'}" for turn in cancelled_turns[:5]
            ),
            "结合中止前最后一条工具事件判断是用户取消、超时还是执行卡住。",
        )
    running_turns = [turn for turn in parsed_turns if turn["status"] == "running"]
    if running_turns:
        add(
            "info",
            "active_turn",
            "会话仍有未结束 turn",
            f"Turn #{running_turns[-1]['index']} 尚未出现 task_complete 或 turn_aborted。",
            "实时刷新时这是正常状态；静态文件长期不再更新时才表示异常退出。",
        )
    if failed_calls:
        add(
            "warning",
            "tool_failures",
            f"{len(failed_calls)} 次工具调用失败",
            "；".join(f"{call['name']} ({call.get('exit_code', 'error')})" for call in failed_calls[:8]),
            "在时间线中打开对应 result 行查看完整失败输出。",
        )
    if pending_calls:
        add(
            "warning",
            "unpaired_tool_calls",
            f"{len(pending_calls)} 次工具调用没有结果事件",
            "；".join(call["name"] for call in pending_calls[:8]),
            "这通常表示会话在工具执行期间被中止，或 rollout 仍在写入。",
        )
    if unmatched_results:
        add(
            "warning",
            "unmatched_tool_results",
            f"{len(unmatched_results)} 个工具结果找不到调用事件",
            "、".join(call["id"] for call in unmatched_results[:5]),
            "可能是日志截断、回滚或旧版 Codex 事件格式导致。",
        )
    context_utilization = (
        peak_context_tokens / context_window if context_window and context_window > 0 else None
    )
    if context_utilization is not None and context_utilization >= 0.85:
        add(
            "warning",
            "context_pressure",
            "上下文窗口接近上限",
            f"观测峰值 {peak_context_tokens:,} / {context_window:,} tokens（{context_utilization:.1%}）。",
            "检查大工具输出和重复上下文；必要时拆分任务或减少整文件读取。",
        )
    if context_compactions:
        add(
            "info",
            "context_compaction",
            f"发生 {context_compactions} 次上下文压缩",
            "压缩会保留摘要，但早期逐字上下文不再完整进入后续模型调用。",
        )
    if rollbacks:
        add(
            "info",
            "thread_rollback",
            f"发生 {rollbacks} 次线程回滚",
            "回滚后的事件仍保留在 rollout 中，分析总量可能高于当前可见对话分支。",
        )
    if reasoning_items and reasoning_summaries == 0 and encrypted_reasoning_items:
        add(
            "info",
            "encrypted_reasoning",
            "推理正文不可直接查看",
            f"{encrypted_reasoning_items} 条 reasoning 事件只包含加密内容，没有明文摘要。",
            "查看器会分析推理次数和时间位置，但不会尝试解密模型内部推理。",
        )
    if output_bytes > 10_000_000:
        add(
            "warning",
            "large_tool_output",
            "工具输出体积较大",
            f"已配对工具结果合计约 {output_bytes / 1_000_000:.1f} MB。",
            "大输出会抬高上下文消耗；优先缩小命令范围或先聚合后读取。",
        )
    if latest_rate_limits and latest_rate_limits.get("rate_limit_reached_type"):
        add(
            "error",
            "rate_limit_reached",
            "Codex 报告达到速率限制",
            str(latest_rate_limits.get("rate_limit_reached_type")),
            "等待限制恢复，或检查当前账户与模型的额度设置。",
        )

    usage = {
        **latest_total_usage,
        "last_model_call": latest_last_usage,
        "model_calls": model_calls,
        "context_window": context_window,
        "peak_context_tokens": peak_context_tokens,
        "context_utilization": context_utilization,
        "cache_hit_ratio": (
            latest_total_usage["cached_input_tokens"] / latest_total_usage["input_tokens"]
            if latest_total_usage["input_tokens"]
            else None
        ),
        "turns": [
            {"turn_id": turn["id"], "index": turn["index"], **turn["usage"]} for turn in parsed_turns
        ],
        "rate_limits": latest_rate_limits,
    }
    analysis = {
        "event_count": len(events),
        "categories": dict(category_counts),
        "message_count": len(messages),
        "messages_by_role": dict(role_counts),
        "tool_call_count": len([call for call in calls if call["type"] in _CALL_TYPES]),
        "failed_tool_call_count": len(failed_calls),
        "pending_tool_call_count": len(pending_calls),
        "unmatched_result_count": len(unmatched_results),
        "tool_output_bytes": output_bytes,
        "tools": [{"name": name, "calls": count} for name, count in call_counts.most_common()],
        "reasoning_items": reasoning_items,
        "reasoning_summaries": reasoning_summaries,
        "encrypted_reasoning_items": encrypted_reasoning_items,
        "context_compactions": context_compactions,
        "rollbacks": rollbacks,
    }
    return {
        "metadata": metadata,
        "events": events,
        "parse_errors": parse_errors,
        "turns": parsed_turns,
        "messages": messages,
        "calls": calls,
        "usage": usage,
        "analysis": analysis,
        "diagnostics": diagnostics,
    }


@dataclass(slots=True)
class CodexRolloutAnalyzer:
    sessions_dir: Path

    def __init__(self, sessions_dir: str | Path) -> None:
        self.sessions_dir = Path(sessions_dir).expanduser().resolve()

    def _paths(self) -> list[Path]:
        if not self.sessions_dir.is_dir():
            return []
        paths: list[Path] = []
        for path in self.sessions_dir.glob("**/rollout-*.jsonl"):
            try:
                resolved = path.resolve()
                if path.is_file() and path_within(self.sessions_dir, resolved):
                    paths.append(resolved)
            except OSError:
                continue
        return paths

    def _signature(self, path: Path) -> tuple[int, int]:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size

    def _summary(self, path: Path) -> dict[str, Any]:
        mtime_ns, size = self._signature(path)
        return _scan_rollout_cached(str(path), mtime_ns, size)

    def _session_path(self, session_id: str) -> Path:
        if not _SAFE_SESSION_ID.fullmatch(session_id):
            raise KeyError(session_id)
        for path in self._paths():
            try:
                if self._summary(path)["id"] == session_id:
                    if not path_within(self.sessions_dir, path):
                        break
                    return path
            except OSError:
                continue
        raise KeyError(session_id)

    def _document(self, session_id: str) -> tuple[Path, dict[str, Any]]:
        path = self._session_path(session_id)
        mtime_ns, size = self._signature(path)
        return path, _load_rollout_cached(str(path), mtime_ns, size)

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        for path in self._paths():
            try:
                sessions.append(self._summary(path))
            except OSError:
                continue
        return sorted(sessions, key=lambda item: _timestamp_key(item.get("updated_at")), reverse=True)

    def session_detail(self, session_id: str) -> dict[str, Any]:
        path, document = self._document(session_id)
        summary = self._summary(path)
        metadata = document["metadata"]
        return {
            **summary,
            "started_at": summary.get("created_at"),
            "finished_at": summary.get("updated_at") if summary.get("status") != "running" else None,
            "source_file": str(path),
            "relative_source_file": str(path.relative_to(self.sessions_dir)),
            "metadata": {
                key: value
                for key, value in metadata.items()
                if key not in {"base_instructions"}
            },
            "base_instructions": _flatten_text(metadata.get("base_instructions")),
            "turns": document["turns"],
            "messages": document["messages"],
            "calls": document["calls"],
            "usage": document["usage"],
            "analysis": document["analysis"],
            "diagnostics": document["diagnostics"],
            "parse_errors": document["parse_errors"],
        }

    def session_events(
        self,
        session_id: str,
        *,
        offset: int = 0,
        limit: int = 200,
        turn_id: str | None = None,
        category: str | None = None,
        query: str | None = None,
        order: str = "asc",
    ) -> dict[str, Any]:
        _, document = self._document(session_id)
        events = list(document["events"])
        if turn_id:
            events = [event for event in events if event.get("turn_id") == turn_id]
        if query:
            needle = query.casefold()
            events = [
                event
                for event in events
                if needle in json.dumps(event, ensure_ascii=False, default=str).casefold()
            ]
        categories = Counter(str(event["category"]) for event in events)
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
            "categories": dict(categories),
            "parse_errors": document["parse_errors"],
        }

    def event_detail(self, session_id: str, line_number: int) -> dict[str, Any]:
        if line_number < 1:
            raise KeyError(line_number)
        _, document = self._document(session_id)
        for event in document["events"]:
            if event["line_number"] == line_number:
                return event
        raise KeyError(line_number)

    def raw_chunk(
        self,
        session_id: str,
        *,
        offset: int = 0,
        limit: int = 200_000,
    ) -> dict[str, Any]:
        path = self._session_path(session_id)
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(min(offset, size))
            raw = handle.read(limit)
        return {
            "name": path.name,
            "offset": offset,
            "next_offset": offset + len(raw),
            "size": size,
            "has_more": offset + len(raw) < size,
            "content": raw.decode("utf-8", errors="replace"),
            "parsed": None,
        }
