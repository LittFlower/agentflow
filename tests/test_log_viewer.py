from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

import agentflow.cli
from agentflow.cli import app as cli_app
from agentflow.log_viewer import create_log_viewer_app
from agentflow.log_viewer.analyzer import LogAnalyzer, extract_template_references
from agentflow.log_viewer.codex_rollout import CodexRolloutAnalyzer
from agentflow.specs import NodeAttempt, NodeResult, NodeStatus, PipelineSpec, RunRecord, RunStatus


def _write_debug_run(tmp_path: Path) -> tuple[Path, str]:
    runs_dir = tmp_path / "runs"
    run_id = "debug-run"
    run_dir = runs_dir / run_id
    source_dir = run_dir / "artifacts" / "source"
    target_dir = run_dir / "artifacts" / "target"
    source_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "single-line.js").write_text("x" * 1_100_000, encoding="utf-8")

    pipeline = PipelineSpec.model_validate(
        {
            "name": "debug-pipeline",
            "working_dir": str(workspace),
            "nodes": [
                {"id": "source", "agent": "codex", "prompt": "Produce evidence"},
                {
                    "id": "target",
                    "agent": "codex",
                    "depends_on": ["source"],
                    "prompt": "Analyze {{ nodes.source.output }} in {{ pipeline.working_dir }}",
                    "timeout_seconds": 30,
                    "skills": ["pwn-audit"],
                    "mcps": [{"name": "files", "command": "mcp-files"}],
                },
            ],
        }
    )
    run = RunRecord(
        id=run_id,
        status=RunStatus.FAILED,
        pipeline=pipeline,
        created_at="2026-07-14T08:00:00+00:00",
        started_at="2026-07-14T08:00:01+00:00",
        finished_at="2026-07-14T08:00:32+00:00",
        nodes={
            "source": NodeResult(
                node_id="source",
                status=NodeStatus.COMPLETED,
                output="upstream evidence",
                final_response="upstream evidence",
                started_at="2026-07-14T08:00:01+00:00",
                finished_at="2026-07-14T08:00:02+00:00",
            ),
            "target": NodeResult(
                node_id="target",
                status=NodeStatus.FAILED,
                exit_code=124,
                started_at="2026-07-14T08:00:02+00:00",
                finished_at="2026-07-14T08:00:32+00:00",
                current_attempt=1,
                attempts=[
                    NodeAttempt(
                        number=1,
                        status=NodeStatus.FAILED,
                        exit_code=124,
                        started_at="2026-07-14T08:00:02+00:00",
                        finished_at="2026-07-14T08:00:32+00:00",
                    )
                ],
            ),
        },
    )
    (run_dir / "run.json").write_text(run.model_dump_json(indent=2), encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-07-14T08:00:02+00:00",
                "run_id": run_id,
                "type": "node_started",
                "node_id": "target",
                "data": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    launch = {
        "attempt": 1,
        "kind": "process",
        "command": ["codex", "exec", "--json", "Analyze upstream evidence in " + str(workspace)],
        "cwd": str(workspace),
        "stdin": None,
        "env": {},
        "runtime_files": [],
        "payload": None,
    }
    (target_dir / "launch.json").write_text(json.dumps(launch), encoding="utf-8")
    (target_dir / "launch-attempt-1.json").write_text(json.dumps(launch), encoding="utf-8")
    (target_dir / "prompt.json").write_text(
        json.dumps(
            {
                "attempt": 1,
                "prompt_template": "Analyze {{ nodes.source.output }} in {{ pipeline.working_dir }}",
                "rendered_pipeline_prompt": "Analyze upstream evidence in " + str(workspace),
                "agent_input": "Wrapper instructions\n\n---\n\nAnalyze upstream evidence in " + str(workspace),
                "prepared_command": launch["command"],
                "stdin": None,
            }
        ),
        encoding="utf-8",
    )
    trace_events = [
        {
            "timestamp": "2026-07-14T08:00:02+00:00",
            "node_id": "target",
            "agent": "codex",
            "attempt": 1,
            "source": "stdout",
            "kind": "event",
            "title": "thread.started",
            "content": "",
            "raw": {"type": "thread.started", "thread_id": "019f-test-session"},
        },
        {
            "timestamp": "2026-07-14T08:00:03+00:00",
            "node_id": "target",
            "agent": "codex",
            "attempt": 1,
            "source": "stdout",
            "kind": "item_started",
            "title": "Item started: command_execution",
            "content": "",
            "raw": {
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": "/bin/zsh -lc \"sed -n '1,220p' single-line.js\"",
                    "status": "in_progress",
                },
            },
        },
        {
            "timestamp": "2026-07-14T08:00:04+00:00",
            "node_id": "target",
            "agent": "codex",
            "attempt": 1,
            "source": "stdout",
            "kind": "tool_call",
            "title": "Tool call: mcp__files__read",
            "content": '{"path":"single-line.js"}',
            "raw": {"type": "function_call", "name": "mcp__files__read"},
        },
        {
            "timestamp": "2026-07-14T08:00:31+00:00",
            "node_id": "target",
            "agent": "codex",
            "attempt": 1,
            "source": "stdout",
            "kind": "completed",
            "title": "Turn completed",
            "content": "",
            "raw": {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1200,
                    "cached_input_tokens": 800,
                    "output_tokens": 90,
                    "reasoning_output_tokens": 40,
                },
            },
        },
    ]
    (target_dir / "trace.jsonl").write_text(
        "\n".join(json.dumps(event) for event in trace_events) + "\n{broken\n",
        encoding="utf-8",
    )
    (target_dir / "stderr.log").write_text("Timed out after 30s\n", encoding="utf-8")
    (target_dir / "output.txt").write_text("", encoding="utf-8")
    return runs_dir, run_id


def _write_codex_rollout(tmp_path: Path) -> tuple[Path, str]:
    sessions_dir = tmp_path / "codex" / "sessions"
    rollout_dir = sessions_dir / "2026" / "07" / "15"
    rollout_dir.mkdir(parents=True)
    session_id = "019f-test-session"
    path = rollout_dir / f"rollout-2026-07-15T10-00-00-{session_id}.jsonl"
    turn_one = "turn-one"
    turn_two = "turn-two"
    records = [
        {
            "timestamp": "2026-07-15T02:00:00Z",
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "timestamp": "2026-07-15T02:00:00Z",
                "cwd": "/tmp/project",
                "originator": "codex-tui",
                "source": "cli",
                "thread_source": "user",
                "model_provider": "openai",
                "cli_version": "0.144.4",
                "base_instructions": {"text": "You are Codex."},
            },
        },
        {
            "timestamp": "2026-07-15T02:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": turn_one},
        },
        {
            "timestamp": "2026-07-15T02:00:01Z",
            "type": "turn_context",
            "payload": {"turn_id": turn_one, "cwd": "/tmp/project", "model": "gpt-5.5", "effort": "high"},
        },
        {
            "timestamp": "2026-07-15T02:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<environment_context><cwd>/tmp/project</cwd></environment_context>"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_one},
            },
        },
        {
            "timestamp": "2026-07-15T02:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Investigate the failing test"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_one},
            },
        },
        {
            "timestamp": "2026-07-15T02:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"pytest"}',
                "call_id": "call-1",
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_one},
            },
        },
        {
            "timestamp": "2026-07-15T02:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "Process exited with code 2\nOutput:\nfailed",
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_one},
            },
        },
        {
            "timestamp": "2026-07-15T02:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [],
                "encrypted_content": "opaque",
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_one},
            },
        },
        {
            "timestamp": "2026-07-15T02:00:06Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 120,
                    },
                    "last_token_usage": {
                        "input_tokens": 90,
                        "cached_input_tokens": 40,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 110,
                    },
                    "model_context_window": 100,
                },
                "rate_limits": None,
            },
        },
        {
            "timestamp": "2026-07-15T02:00:07Z",
            "type": "event_msg",
            "payload": {"type": "context_compacted"},
        },
        {
            "timestamp": "2026-07-15T02:00:08Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": turn_one,
                "duration_ms": 7000,
                "time_to_first_token_ms": 250,
                "last_agent_message": "The test command failed.",
            },
        },
        {
            "timestamp": "2026-07-15T02:01:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": turn_two},
        },
        {
            "timestamp": "2026-07-15T02:01:01Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": "run more checks",
                "call_id": "call-2",
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_two},
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n{broken\n", encoding="utf-8")
    return sessions_dir, session_id


def test_extract_template_references_returns_maximal_paths():
    references = extract_template_references("{{ nodes.plan.output }} {{ fanouts.work.outputs[0] }}")

    assert ("nodes", "plan", "output") in references
    assert ("fanouts", "work", "outputs", "0") in references
    assert ("nodes", "plan") not in references


def test_log_analyzer_exposes_context_transfers_usage_tools_and_diagnostics(tmp_path):
    runs_dir, run_id = _write_debug_run(tmp_path)
    analyzer = LogAnalyzer(runs_dir)

    detail = analyzer.node_detail(run_id, "target")

    assert detail["context"]["rendered_prompt"].startswith("Analyze upstream evidence")
    assert detail["context"]["agent_input"].startswith("Wrapper instructions")
    assert detail["context"]["agent_input_source"] == "prompt.json"
    assert detail["inbound"][0]["source"] == "source"
    assert detail["inbound"][0]["value"] == "upstream evidence"
    assert detail["usage"]["input_tokens"] == 1200
    assert detail["usage"]["total_tokens"] == 1290
    assert detail["activity"]["pending_command_count"] == 1
    assert detail["activity"]["configured_skills"] == ["pwn-audit"]
    assert detail["activity"]["configured_mcps"][0]["name"] == "files"
    assert detail["activity"]["observed_mcp_calls"] == [{"name": "mcp__files__read", "calls": 1}]
    assert detail["codex_rollouts"] == [
        {
            "session_id": "019f-test-session",
            "attempt": 1,
            "timestamp": "2026-07-14T08:00:02+00:00",
        }
    ]
    diagnostic_codes = {item["code"] for item in detail["diagnostics"]}
    assert {"node_timeout", "unfinished_command", "unbounded_large_file_read"} <= diagnostic_codes
    assert detail["parse_errors"][0]["line_number"] == 5


def test_log_viewer_api_paginates_structured_events_and_artifacts(tmp_path):
    runs_dir, run_id = _write_debug_run(tmp_path)
    client = TestClient(create_log_viewer_app(runs_dir=runs_dir))

    assert client.get("/api/health").json()["runs"] == 1
    assert client.get("/api/runs").json()[0]["id"] == run_id
    run = client.get(f"/api/runs/{run_id}").json()
    assert [node["id"] for node in run["nodes"]] == ["source", "target"]
    assert run["nodes"][1]["codex_rollouts"] == [
        {
            "session_id": "019f-test-session",
            "attempt": 1,
            "timestamp": "2026-07-14T08:00:02+00:00",
            "available": False,
            "relative_source_file": None,
        }
    ]

    events = client.get(f"/api/runs/{run_id}/nodes/target/events", params={"limit": 2, "order": "desc"})
    assert events.status_code == 200
    assert events.json()["total"] == 5  # four trace events plus node_started lifecycle
    assert len(events.json()["items"]) == 2
    assert events.json()["has_more"] is True
    assert events.json()["parse_errors"][0]["line_number"] == 5

    artifact = client.get(f"/api/runs/{run_id}/nodes/target/artifacts/launch.json", params={"limit": 10000})
    assert artifact.status_code == 200
    assert artifact.json()["parsed"]["attempt"] == 1
    assert client.get(f"/api/runs/{run_id}/nodes/target/artifacts/missing.log").status_code == 404


def test_codex_rollout_analyzer_builds_turn_tool_usage_and_diagnostics(tmp_path):
    sessions_dir, session_id = _write_codex_rollout(tmp_path)
    analyzer = CodexRolloutAnalyzer(sessions_dir)

    sessions = analyzer.list_sessions()
    assert sessions[0]["id"] == session_id
    assert sessions[0]["name"] == "Investigate the failing test"
    assert sessions[0]["status"] == "running"
    assert sessions[0]["turn_count"] == 2

    detail = analyzer.session_detail(session_id)
    assert detail["base_instructions"] == "You are Codex."
    assert detail["turns"][0]["status"] == "completed"
    assert detail["turns"][0]["final_response"] == "The test command failed."
    assert detail["turns"][0]["usage"]["total_tokens"] == 120
    assert detail["turns"][1]["status"] == "running"
    assert detail["calls"][0]["name"] == "exec_command"
    assert detail["calls"][0]["status"] == "failed"
    assert detail["calls"][0]["exit_code"] == 2
    assert detail["calls"][1]["status"] == "running"
    assert detail["usage"]["context_utilization"] == 0.9
    diagnostic_codes = {item["code"] for item in detail["diagnostics"]}
    assert {
        "invalid_jsonl",
        "active_turn",
        "tool_failures",
        "unpaired_tool_calls",
        "context_pressure",
        "context_compaction",
        "encrypted_reasoning",
    } <= diagnostic_codes


def test_codex_rollout_api_filters_events_and_streams_raw_file(tmp_path):
    runs_dir, _ = _write_debug_run(tmp_path)
    sessions_dir, session_id = _write_codex_rollout(tmp_path)
    client = TestClient(create_log_viewer_app(runs_dir=runs_dir, codex_sessions_dir=sessions_dir))

    health = client.get("/api/health").json()
    assert health["codex_sessions"] == 1
    assert client.get("/api/codex/sessions").json()[0]["id"] == session_id
    detail = client.get(f"/api/codex/sessions/{session_id}")
    assert detail.status_code == 200
    assert detail.json()["analysis"]["failed_tool_call_count"] == 1
    run = client.get("/api/runs/debug-run").json()
    target = next(node for node in run["nodes"] if node["id"] == "target")
    assert target["codex_rollouts"][0]["available"] is True
    assert target["codex_rollouts"][0]["relative_source_file"].endswith(f"{session_id}.jsonl")
    linked_node = client.get("/api/runs/debug-run/nodes/target").json()
    assert linked_node["codex_rollouts"] == [
        {
            "session_id": session_id,
            "attempt": 1,
            "timestamp": "2026-07-14T08:00:02+00:00",
            "available": True,
            "relative_source_file": target["codex_rollouts"][0]["relative_source_file"],
        }
    ]

    events = client.get(
        f"/api/codex/sessions/{session_id}/events",
        params={"turn_id": "turn-one", "category": "tool", "limit": 1},
    )
    assert events.status_code == 200
    assert events.json()["total"] == 2
    assert events.json()["has_more"] is True
    line_number = events.json()["items"][0]["line_number"]
    assert client.get(f"/api/codex/sessions/{session_id}/events/{line_number}").json()["turn_id"] == "turn-one"

    raw = client.get(f"/api/codex/sessions/{session_id}/raw", params={"limit": 80}).json()
    assert raw["name"].startswith("rollout-")
    assert raw["has_more"] is True
    assert client.get("/api/codex/sessions/../raw").status_code in {404, 405}


def test_logs_cli_starts_independent_viewer(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(agentflow.cli, "_serve_web_app", lambda web_app, host, port: captured.update(app=web_app, host=host, port=port))

    result = CliRunner().invoke(
        cli_app,
        [
            "logs",
            "--host",
            "127.0.0.2",
            "--port",
            "8129",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--codex-sessions-dir",
            str(tmp_path / "codex-sessions"),
        ],
    )

    assert result.exit_code == 0
    assert captured["app"].title == "AgentFlow Log Explorer"
    assert captured["host"] == "127.0.0.2"
    assert captured["port"] == 8129
    assert captured["app"].state.codex_analyzer.sessions_dir == (tmp_path / "codex-sessions").resolve()
