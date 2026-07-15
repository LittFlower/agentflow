const state = {
  source: "agentflow",
  runs: [],
  run: null,
  runId: null,
  nodeId: null,
  node: null,
  events: [],
  eventsTotal: 0,
  eventCategories: {},
  eventParseErrors: [],
  category: "all",
  attempt: "",
  query: "",
  order: "asc",
  activeTab: "overview",
  artifactName: null,
  artifact: null,
  requestVersion: 0,
};

const elements = Object.fromEntries([
  "notice", "run-count", "run-search", "run-list", "node-count", "node-search", "node-list",
  "run-header", "flow-map", "event-search", "attempt-filter", "sort-button", "category-filters",
  "timeline-summary", "timeline", "load-more", "inspector-title", "inspector-status", "inspector-tabs",
  "inspector-content", "live-toggle", "refresh-button", "source-switch", "source-directory",
  "source-path-label", "run-list-label", "node-list-label", "inspector-eyebrow", "brand-subtitle",
].map((id) => [id.replaceAll("-", "_"), document.getElementById(id)]));

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch {}
    throw new Error(message);
  }
  return response.json();
}

function showNotice(message) {
  elements.notice.textContent = message;
  elements.notice.hidden = !message;
}

function formatDate(value, includeDate = true) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    ...(includeDate ? { month: "short", day: "2-digit" } : {}),
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(date);
}

function formatDuration(start, end) {
  if (!start) return "-";
  const seconds = Math.max(0, ((end ? new Date(end) : new Date()) - new Date(start)) / 1000);
  if (!Number.isFinite(seconds)) return "-";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

function formatNumber(value) {
  return value === null || value === undefined ? "未上报" : Number(value).toLocaleString("zh-CN");
}

const statusLabels = {
  pending: "等待中", queued: "已排队", ready: "就绪", running: "运行中", retrying: "重试中",
  completed: "已完成", failed: "失败", skipped: "已跳过", cancelled: "已取消", cancelling: "取消中",
  idle: "空闲", completed_with_aborts: "完成（有中止）",
};

function statusLabel(status) {
  const raw = String(status || "pending").toLowerCase();
  return statusLabels[raw] || raw;
}

function attemptLabel(node) {
  const attempts = Number(node.attempts || 0);
  if (attempts > 0) return `${attempts} 次尝试`;
  if (node.status === "skipped") return "未执行 · 已跳过";
  if (["pending", "queued", "ready"].includes(node.status)) return "等待执行";
  return "尚未启动";
}

function statusClass(status) {
  return `status-${String(status || "pending").toLowerCase()}`;
}

function stringify(value) {
  if (typeof value === "string") return value;
  if (value === undefined) return "";
  return JSON.stringify(value, null, 2);
}

function jsonTree(value, key = null, depth = 0) {
  const keyMarkup = key === null ? "" : `<span class="json-key">${escapeHtml(key)}</span>: `;
  if (value === null) return `<div>${keyMarkup}<span class="json-null">null</span></div>`;
  if (typeof value === "string") return `<div>${keyMarkup}<span class="json-string">${escapeHtml(JSON.stringify(value))}</span></div>`;
  if (typeof value === "number") return `<div>${keyMarkup}<span class="json-number">${escapeHtml(value)}</span></div>`;
  if (typeof value === "boolean") return `<div>${keyMarkup}<span class="json-boolean">${value}</span></div>`;
  if (Array.isArray(value)) {
    const children = value.map((item, index) => jsonTree(item, String(index), depth + 1)).join("");
    return `<details${depth < 1 ? " open" : ""}><summary>${keyMarkup}数组（${value.length}）</summary>${children}</details>`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value);
    const children = entries.map(([childKey, item]) => jsonTree(item, childKey, depth + 1)).join("");
    return `<details${depth < 1 ? " open" : ""}><summary>${keyMarkup}对象（${entries.length}）</summary>${children}</details>`;
  }
  return `<div>${keyMarkup}${escapeHtml(value)}</div>`;
}

function empty(message) {
  return `<div class="empty-state">${escapeHtml(message)}</div>`;
}

function formatPercent(value) {
  return value === null || value === undefined ? "未上报" : `${(Number(value) * 100).toFixed(1)}%`;
}

function configureSourceUi() {
  const codex = state.source === "codex";
  elements.source_switch.querySelectorAll("[data-source]").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.source === state.source));
  });
  elements.source_directory.textContent = elements.source_directory.dataset[state.source] || "";
  elements.source_path_label.textContent = codex ? "Rollout 目录" : "运行目录";
  elements.run_list_label.textContent = codex ? "Codex Sessions" : "运行记录";
  elements.node_list_label.textContent = codex ? "Turns" : "Agent 节点";
  elements.inspector_eyebrow.textContent = codex ? "Session 分析" : "当前节点";
  elements.brand_subtitle.textContent = codex ? "Codex rollout 分析台" : "AgentFlow 运行调试台";
  elements.run_search.placeholder = codex ? "筛选 session、目录、模型" : "筛选运行记录";
  elements.node_search.placeholder = codex ? "筛选 turn" : "筛选节点";
  elements.attempt_filter.hidden = codex;
  const labels = codex
    ? ["分析", "上下文", "消息", "工具", "Turns", "原始"]
    : ["概览", "输入", "输出", "工具", "传递", "文件"];
  elements.inspector_tabs.querySelectorAll("[data-tab]").forEach((button, index) => {
    button.textContent = labels[index];
  });
}

function resetSelection() {
  state.runs = [];
  state.run = null;
  state.runId = null;
  state.nodeId = null;
  state.node = null;
  state.events = [];
  state.eventsTotal = 0;
  state.eventCategories = {};
  state.eventParseErrors = [];
  state.category = "all";
  state.attempt = "";
  state.query = "";
  state.activeTab = "overview";
  state.artifactName = null;
  state.artifact = null;
  elements.event_search.value = "";
}

function renderRuns() {
  const query = elements.run_search.value.trim().toLowerCase();
  const runs = state.runs.filter((run) => [run.name, run.id, run.status, run.cwd, run.model, run.originator].some((value) => String(value || "").toLowerCase().includes(query)));
  elements.run_count.textContent = state.runs.length;
  elements.run_list.innerHTML = runs.map((run) => `
    <button class="run-item ${run.id === state.runId ? "active" : ""}" type="button" data-run-id="${escapeHtml(run.id)}" role="option" aria-selected="${run.id === state.runId}">
      <span class="run-row">
        <span class="run-name">${escapeHtml(run.name)}</span>
        <span class="status-dot ${statusClass(run.status)}" title="${escapeHtml(statusLabel(run.status))}"></span>
      </span>
      <span class="run-meta">
        <span class="run-id">${escapeHtml(run.id.slice(0, 10))}</span>
        <span>${escapeHtml(formatDate(run.created_at))}</span>
        <span class="trace-count">${Number(run.trace_count || 0).toLocaleString("zh-CN")} 条事件</span>
      </span>
    </button>
  `).join("") || empty(state.source === "codex" ? "没有匹配的 Codex session。" : "没有匹配的运行记录。");
  elements.run_list.querySelectorAll("[data-run-id]").forEach((button) => {
    button.onclick = () => selectRun(button.dataset.runId);
  });
}

function renderNodes() {
  if (state.source === "codex") {
    const turns = state.run?.turns || [];
    const query = elements.node_search.value.trim().toLowerCase();
    const filtered = turns.filter((turn) => [turn.id, turn.status, turn.user_message, turn.model].some((value) => String(value || "").toLowerCase().includes(query)));
    elements.node_count.textContent = turns.length;
    const allItem = state.run ? `
      <button class="node-item ${state.nodeId === "__all__" ? "active" : ""}" type="button" data-node-id="__all__" role="option" aria-selected="${state.nodeId === "__all__"}">
        <span class="node-row"><span class="node-name">全部 session 事件</span><span class="status-dot ${statusClass(state.run.status)}"></span></span>
        <span class="node-meta"><span>${turns.length} turns</span><span class="trace-count">${state.run.event_count || 0} 步</span></span>
      </button>` : "";
    elements.node_list.innerHTML = allItem + filtered.map((turn) => `
      <button class="node-item ${turn.id === state.nodeId ? "active" : ""}" type="button" data-node-id="${escapeHtml(turn.id)}" role="option" aria-selected="${turn.id === state.nodeId}">
        <span class="node-row">
          <span class="node-name">Turn ${turn.index}</span>
          <span class="status-dot ${statusClass(turn.status)}" title="${escapeHtml(statusLabel(turn.status))}"></span>
        </span>
        <span class="node-meta">
          <span class="run-id">${escapeHtml(turn.id.slice(0, 12))}</span>
          <span>${turn.tool_call_count || 0} 工具</span>
          <span class="trace-count">${turn.event_count || 0} 步</span>
        </span>
      </button>
    `).join("") || empty(state.run ? "没有匹配的 turn。" : "请先选择一个 Codex session。");
    elements.node_list.querySelectorAll("[data-node-id]").forEach((button) => {
      button.onclick = () => selectNode(button.dataset.nodeId);
    });
    return;
  }
  const nodes = state.run?.nodes || [];
  const query = elements.node_search.value.trim().toLowerCase();
  const filtered = nodes.filter((node) => [node.id, node.agent, node.status, node.model].some((value) => String(value || "").toLowerCase().includes(query)));
  elements.node_count.textContent = nodes.length;
  elements.node_list.innerHTML = filtered.map((node) => {
    const rollouts = (node.codex_rollouts || []).filter((rollout) => rollout.available);
    const latestRollout = rollouts.at(-1);
    return `
      <div class="node-item ${node.id === state.nodeId ? "active" : ""}" role="group">
        <button class="node-select" type="button" data-node-id="${escapeHtml(node.id)}" role="option" aria-selected="${node.id === state.nodeId}">
          <span class="node-row">
            <span class="node-name">${escapeHtml(node.id)}</span>
            <span class="status-dot ${statusClass(node.status)}" title="${escapeHtml(statusLabel(node.status))}"></span>
          </span>
          <span class="node-meta">
            <span>${escapeHtml(node.agent)}</span>
            <span>${escapeHtml(attemptLabel(node))}</span>
            <span class="trace-count">${node.trace_count} 步</span>
          </span>
        </button>
        ${latestRollout ? `
          <button
            class="node-rollout"
            type="button"
            data-node-rollout="${escapeHtml(latestRollout.session_id)}"
            title="${escapeHtml(latestRollout.relative_source_file || `查看第 ${latestRollout.attempt || node.attempts || "-"} 次尝试对应的 Codex rollout`)}"
          >Rollout${rollouts.length > 1 ? ` (${rollouts.length})` : ""}</button>
        ` : ""}
      </div>
    `;
  }).join("") || empty(state.run ? "没有匹配的节点。" : "请先选择一条运行记录。");
  elements.node_list.querySelectorAll("[data-node-id]").forEach((button) => {
    button.onclick = () => selectNode(button.dataset.nodeId);
  });
  elements.node_list.querySelectorAll("[data-node-rollout]").forEach((button) => {
    button.onclick = () => switchSource("codex", button.dataset.nodeRollout);
  });
}

function topoLevels(nodes) {
  const map = Object.fromEntries(nodes.map((node) => [node.id, node]));
  const memo = {};
  const visiting = new Set();
  function level(id) {
    if (memo[id] !== undefined) return memo[id];
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const deps = (map[id]?.depends_on || []).filter((dep) => map[dep]);
    const result = deps.length ? Math.max(...deps.map(level)) + 1 : 0;
    visiting.delete(id);
    memo[id] = result;
    return result;
  }
  nodes.forEach((node) => level(node.id));
  const groups = [];
  nodes.forEach((node) => (groups[memo[node.id]] ||= []).push(node));
  return groups.filter(Boolean);
}

function renderFlowMap() {
  if (state.source === "codex") {
    const turns = state.run?.turns || [];
    elements.flow_map.innerHTML = turns.map((turn) => `
      <div class="flow-level">
        <button class="flow-node ${statusClass(turn.status)} ${turn.id === state.nodeId ? "active" : ""}" type="button" data-node-id="${escapeHtml(turn.id)}" title="${escapeHtml(`Turn ${turn.index} · ${statusLabel(turn.status)}`)}">Turn ${turn.index}</button>
      </div>
    `).join("");
    elements.flow_map.querySelectorAll("[data-node-id]").forEach((button) => {
      button.onclick = () => selectNode(button.dataset.nodeId);
    });
    return;
  }
  const nodes = state.run?.nodes || [];
  elements.flow_map.innerHTML = topoLevels(nodes).map((level) => `
    <div class="flow-level">
      ${level.map((node) => `<button class="flow-node ${statusClass(node.status)} ${node.id === state.nodeId ? "active" : ""}" type="button" data-node-id="${escapeHtml(node.id)}" title="${escapeHtml(`${node.id} · ${node.agent} · ${statusLabel(node.status)}`)}">${escapeHtml(node.id)}</button>`).join("")}
    </div>
  `).join("");
  elements.flow_map.querySelectorAll("[data-node-id]").forEach((button) => {
    button.onclick = () => selectNode(button.dataset.nodeId);
  });
}

function renderRunHeader() {
  const run = state.run;
  if (!run) {
    elements.run_header.innerHTML = '<div class="empty-inline">选择一条运行记录以查看执行过程。</div>';
    return;
  }
  if (state.source === "codex") {
    const analysis = run.analysis || {};
    const turns = run.turns || [];
    const active = turns.filter((turn) => turn.status === "running").length;
    const abnormal = turns.filter((turn) => ["failed", "cancelled"].includes(turn.status)).length;
    elements.run_header.innerHTML = `
      <div class="run-title-row">
        <div>
          <h1>${escapeHtml(run.name)} <span class="status-badge ${statusClass(run.status)}">${escapeHtml(statusLabel(run.status))}</span></h1>
        <div class="run-subtitle"><code>${escapeHtml(run.relative_source_file || run.id)}</code> · ${escapeHtml(run.cwd || "未知目录")} · ${escapeHtml(run.model || run.model_provider || "未知模型")}</div>
        </div>
        <div class="run-metrics">
          <div class="metric"><strong>${turns.length}</strong><span>Turns</span></div>
          <div class="metric"><strong>${Number(run.event_count || 0).toLocaleString("zh-CN")}</strong><span>事件</span></div>
          <div class="metric"><strong>${analysis.tool_call_count || 0}</strong><span>工具调用</span></div>
          <div class="metric"><strong>${active}</strong><span>运行中</span></div>
          <div class="metric"><strong>${abnormal}</strong><span>中止</span></div>
          <div class="metric"><strong>${escapeHtml(formatDuration(run.started_at, run.finished_at))}</strong><span>跨度</span></div>
        </div>
      </div>
    `;
    return;
  }
  const failed = run.nodes.filter((node) => ["failed", "cancelled"].includes(node.status)).length;
  const running = run.nodes.filter((node) => ["running", "retrying"].includes(node.status)).length;
  const steps = run.nodes.reduce((total, node) => total + node.trace_count, 0);
  elements.run_header.innerHTML = `
    <div class="run-title-row">
      <div>
        <h1>${escapeHtml(run.name)} <span class="status-badge ${statusClass(run.status)}">${escapeHtml(statusLabel(run.status))}</span></h1>
        <div class="run-subtitle"><code>${escapeHtml(run.id)}</code> · ${escapeHtml(run.working_dir)} · ${escapeHtml(formatDate(run.created_at))}</div>
      </div>
      <div class="run-metrics">
        <div class="metric"><strong>${run.nodes.length}</strong><span>节点</span></div>
        <div class="metric"><strong>${steps.toLocaleString("zh-CN")}</strong><span>步骤</span></div>
        <div class="metric"><strong>${running}</strong><span>运行中</span></div>
        <div class="metric"><strong>${failed}</strong><span>异常</span></div>
        <div class="metric"><strong>${escapeHtml(formatDuration(run.started_at, run.finished_at))}</strong><span>耗时</span></div>
      </div>
    </div>
  `;
}

function rawItem(event) {
  return event?.raw?.item || event?.raw?.params?.item || {};
}

function eventTitleLabel(title) {
  const text = String(title || "");
  const exact = {
    "Assistant message": "助手消息",
    "Assistant delta": "助手消息片段",
    "Turn completed": "本轮完成",
    "Turn started": "本轮开始",
    "Turn aborted": "本轮已中止",
    "Turn context": "Turn 上下文",
    "Token usage snapshot": "Token 用量快照",
    "Context compacted": "上下文已压缩",
    "Thread rolled back": "线程已回滚",
    "Thread settings applied": "线程设置已应用",
    "Model reasoning": "模型推理",
    "Function result": "函数结果",
    "Tool result": "工具结果",
    "Codex session metadata": "Codex session 元数据",
    "User message": "用户消息",
    "Developer message": "开发者消息",
    "Thread started": "线程开始",
    "Command output": "命令输出",
    "stderr": "标准错误",
    "stdout": "标准输出",
    "Node Started": "节点开始",
    "Node Completed": "节点完成",
    "Node Failed": "节点失败",
    "Node Cancelled": "节点已取消",
  };
  if (exact[text]) return exact[text];
  const itemMatch = text.match(/^Item (started|completed): (.+)$/i);
  if (itemMatch) {
    const action = itemMatch[1].toLowerCase() === "started" ? "开始" : "完成";
    const type = itemMatch[2] === "command_execution" ? "命令执行" : itemMatch[2] === "agent_message" ? "助手消息" : itemMatch[2];
    return `步骤${action}：${type}`;
  }
  const toolMatch = text.match(/^Tool call:\s*(.+)$/i);
  if (toolMatch) return `工具调用：${toolMatch[1]}`;
  return text || "追踪事件";
}

function eventBody(event) {
  const item = rawItem(event);
  const isCommand = event.category === "command" && item.command;
  const output = item.aggregated_output ?? item.output ?? null;
  return `
    ${isCommand ? `<div class="detail-label">执行命令</div><pre class="code-block command">$ ${escapeHtml(item.command)}</pre>` : ""}
    ${event.content !== null && event.content !== undefined && event.content !== "" ? `<div class="detail-label">结构化内容</div><pre class="code-block output">${escapeHtml(stringify(event.content))}</pre>` : ""}
    ${output !== null && output !== "" ? `<div class="detail-label">工具输出</div><pre class="code-block output">${escapeHtml(stringify(output))}</pre>` : ""}
    <div class="detail-label">原始 JSONL 事件 · 第 ${event.line_number} 行</div>
    <div class="json-tree">${jsonTree(event.raw ?? event)}</div>
    ${event.raw_truncated ? `<button class="button compact load-full-event" type="button" data-line-number="${event.line_number}">加载完整原始事件</button>` : ""}
  `;
}

function renderTimeline(append = false) {
  if (!state.nodeId) {
    elements.timeline.innerHTML = empty("选择一个节点以查看内部执行步骤。");
    elements.timeline_summary.innerHTML = "";
    return;
  }
  const markup = state.events.map((event, index) => `
    <article class="event-card category-${escapeHtml(event.category)}" data-event-index="${index}" id="event-${event.line_number}">
      <button class="event-head" type="button" aria-expanded="false">
        <span class="event-kind">${escapeHtml(categoryLabels[event.category] || event.category)}</span>
        <span class="event-copy">
          <span class="event-title">${escapeHtml(eventTitleLabel(event.title || event.kind))}</span>
          <span class="event-summary">${escapeHtml(event.summary || "")}</span>
        </span>
        <span class="event-time">${escapeHtml(formatDate(event.timestamp, false))}<span>${state.source === "codex" ? escapeHtml(event.turn_id ? `Turn ${String(event.turn_id).slice(0, 8)}` : "Session") : `第 ${escapeHtml(event.attempt || "-")} 次`} · #${event.line_number}</span></span>
      </button>
      <div class="event-detail" hidden></div>
    </article>
  `).join("");
  elements.timeline.innerHTML = markup || empty("没有匹配当前筛选条件的事件。");
  elements.timeline.querySelectorAll(".event-card").forEach((card) => {
    const button = card.querySelector(".event-head");
    const detail = card.querySelector(".event-detail");
    button.onclick = () => {
      const open = button.getAttribute("aria-expanded") === "true";
      button.setAttribute("aria-expanded", String(!open));
      detail.hidden = open;
      if (!open && !detail.dataset.rendered) {
        detail.innerHTML = eventBody(state.events[Number(card.dataset.eventIndex)]);
        detail.dataset.rendered = "true";
        detail.querySelector(".load-full-event")?.addEventListener("click", () => loadFullEvent(Number(card.dataset.eventIndex)));
      }
    };
  });
  const shown = state.events.length;
  elements.timeline_summary.innerHTML = `
    <span>已显示 ${shown.toLocaleString("zh-CN")} / ${state.eventsTotal.toLocaleString("zh-CN")} 条匹配事件</span>
    <span>${state.eventParseErrors.length ? `<span class="error-text">${state.eventParseErrors.length} 行 JSONL 格式异常</span> · ` : ""}${escapeHtml(state.source === "codex" ? (state.run?.analysis?.event_count ?? shown) : (state.node?.activity?.event_count ?? shown))} 条追踪记录</span>
  `;
  elements.load_more.hidden = shown >= state.eventsTotal;
}

async function loadFullEvent(index) {
  const current = state.events[index];
  if (!current || !state.runId || !state.nodeId) return;
  const button = document.querySelector(`#event-${CSS.escape(String(current.line_number))} .load-full-event`);
  if (button) {
    button.disabled = true;
    button.textContent = "正在加载完整事件...";
  }
  try {
    const full = await api(state.source === "codex"
      ? `/api/codex/sessions/${encodeURIComponent(state.runId)}/events/${current.line_number}`
      : `/api/runs/${encodeURIComponent(state.runId)}/nodes/${encodeURIComponent(state.nodeId)}/events/${current.line_number}`);
    state.events[index] = full;
    const card = document.querySelector(`#event-${CSS.escape(String(current.line_number))}`);
    const detail = card?.querySelector(".event-detail");
    if (detail) {
      detail.innerHTML = eventBody(full);
      detail.dataset.rendered = "true";
    }
  } catch (error) {
    if (button) {
      button.disabled = false;
      button.textContent = `加载失败：${error.message}`;
    }
  }
}

const categoryLabels = {
  all: "全部", message: "消息", command: "命令", tool: "工具", error: "错误",
  lifecycle: "生命周期", output: "输出", reasoning: "推理", usage: "Token", context: "上下文",
};

function renderCategoryFilters() {
  const keys = state.source === "codex"
    ? ["all", "message", "reasoning", "tool", "usage", "context", "error", "lifecycle", "output"]
    : ["all", "message", "command", "tool", "error", "lifecycle", "output"];
  elements.category_filters.innerHTML = keys.map((key) => {
    const count = key === "all" ? Object.values(state.eventCategories).reduce((sum, value) => sum + value, 0) : (state.eventCategories[key] || 0);
    return `<button class="filter-chip ${state.category === key ? "active" : ""}" type="button" data-category="${key}">${categoryLabels[key]}<span>${count}</span></button>`;
  }).join("");
  elements.category_filters.querySelectorAll("[data-category]").forEach((button) => {
    button.onclick = async () => {
      state.category = button.dataset.category;
      await loadEvents();
    };
  });
}

function renderAttemptFilter() {
  const attempts = state.node?.result?.attempts || [];
  elements.attempt_filter.innerHTML = '<option value="">全部尝试</option>' + attempts.map((attempt) => `<option value="${attempt.number}">第 ${attempt.number} 次 · ${escapeHtml(statusLabel(attempt.status))}</option>`).join("");
  elements.attempt_filter.value = state.attempt;
}

function tile(value, label) {
  return `<div class="summary-tile"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`;
}

function renderDiagnostics() {
  const diagnostics = state.node?.diagnostics || [];
  return diagnostics.map((item) => `
    <div class="diagnostic severity-${escapeHtml(item.severity)}">
      <strong>${escapeHtml(item.title)}</strong>
      <p>${escapeHtml(item.evidence)}</p>
      ${item.suggestion ? `<p class="suggestion">${escapeHtml(item.suggestion)}</p>` : ""}
    </div>
  `).join("") || empty("暂未发现诊断信息。");
}

function renderOverview() {
  const node = state.node;
  const result = node.result || {};
  const usage = node.usage || {};
  const activity = node.activity || {};
  return `
    <section class="inspector-section">
      <h3>执行健康度</h3>
      <div class="summary-grid">
        ${tile(statusLabel(result.status), "状态")}
        ${tile(result.exit_code ?? "-", "退出码")}
        ${tile(formatDuration(result.started_at, result.finished_at), "耗时")}
        ${tile((result.attempts || []).length, "尝试次数")}
        ${tile(activity.command_count || 0, "执行命令")}
        ${tile(activity.pending_command_count || 0, "未结束")}
      </div>
    </section>
    <section class="inspector-section">
      <h3>停止或卡住原因</h3>
      ${renderDiagnostics()}
    </section>
    <section class="inspector-section">
      <h3>Token 用量</h3>
      <div class="summary-grid">
        ${tile(formatNumber(usage.input_tokens), "输入")}
        ${tile(formatNumber(usage.cached_input_tokens), "缓存输入")}
        ${tile(formatNumber(usage.output_tokens), "输出")}
        ${tile(formatNumber(usage.reasoning_tokens), "推理")}
        ${tile(formatNumber(usage.total_tokens), "总计")}
        ${tile(usage.attempts?.length || 0, "已上报尝试")}
      </div>
      ${usage.note ? `<div class="empty-inline" style="margin-top:7px">${escapeHtml(usage.note)}</div>` : ""}
    </section>
    <section class="inspector-section">
      <h3>按尝试统计的 Provider 报告</h3>
      ${(usage.attempts || []).map((attempt) => `
        <details class="data-card">
          <summary>第 ${attempt.attempt} 次 · 共 ${formatNumber(attempt.total_tokens)} Token</summary>
          <div class="data-card-body"><div class="json-tree">${jsonTree(attempt)}</div></div>
        </details>
      `).join("") || empty("事件流中未找到 Token 用量报告。")}
    </section>
    <section class="inspector-section">
      <h3>Codex 原生 Rollout</h3>
      ${(node.codex_rollouts || []).map((rollout) => `
        <div class="transfer-card">
          <div class="prompt-meta"><span>第 ${rollout.attempt || "-"} 次尝试</span><span>${escapeHtml(formatDate(rollout.timestamp))}</span></div>
          <div class="source-file" style="margin:6px 0">${escapeHtml(rollout.relative_source_file || rollout.session_id)}</div>
          <button class="button compact" type="button" data-codex-session="${escapeHtml(rollout.session_id)}" ${rollout.available ? "" : "disabled"}>${rollout.available ? "查看 rollout 详细分析" : "当前目录中未找到 rollout"}</button>
        </div>
      `).join("") || empty("该节点的 trace 中没有 Codex thread.started 事件。")}
    </section>
  `;
}

function selectedCodexTurn() {
  if (state.source !== "codex" || state.nodeId === "__all__") return null;
  return (state.run?.turns || []).find((turn) => turn.id === state.nodeId) || null;
}

function renderCodexOverview() {
  const session = state.run;
  const analysis = session.analysis || {};
  const usage = session.usage || {};
  const turn = selectedCodexTurn();
  const scope = turn ? `Turn ${turn.index}` : "完整 Session";
  const contextPercent = usage.context_utilization;
  return `
    <section class="inspector-section">
      <h3>${escapeHtml(scope)} 健康度</h3>
      <div class="summary-grid">
        ${tile(statusLabel(turn?.status || session.status), "状态")}
        ${tile(turn ? formatDuration(turn.started_at, turn.finished_at) : formatDuration(session.started_at, session.finished_at), "耗时 / 跨度")}
        ${tile(turn?.event_count ?? analysis.event_count ?? 0, "事件")}
        ${tile(turn?.message_count ?? analysis.message_count ?? 0, "消息")}
        ${tile(turn?.tool_call_count ?? analysis.tool_call_count ?? 0, "工具调用")}
        ${tile(turn?.reasoning_count ?? analysis.reasoning_items ?? 0, "推理事件")}
      </div>
    </section>
    <section class="inspector-section">
      <h3>诊断</h3>
      ${renderDiagnostics()}
    </section>
    <section class="inspector-section">
      <h3>Token 与上下文</h3>
      <div class="summary-grid">
        ${tile(formatNumber(turn?.usage?.input_tokens ?? usage.input_tokens), "输入")}
        ${tile(formatNumber(turn?.usage?.cached_input_tokens ?? usage.cached_input_tokens), "缓存输入")}
        ${tile(formatNumber(turn?.usage?.output_tokens ?? usage.output_tokens), "输出")}
        ${tile(formatNumber(turn?.usage?.reasoning_output_tokens ?? usage.reasoning_output_tokens), "推理输出")}
        ${tile(formatNumber(turn?.usage?.total_tokens ?? usage.total_tokens), "总计")}
        ${tile(formatPercent(usage.cache_hit_ratio), "缓存命中率")}
      </div>
      <div class="empty-inline" style="margin:10px 0 5px">观测上下文峰值：${formatNumber(usage.peak_context_tokens)} / ${formatNumber(usage.context_window)} · ${formatPercent(contextPercent)}</div>
      <div class="progress-track" title="上下文窗口占用峰值"><div class="progress-fill" style="width:${Math.min(100, Math.max(0, Number(contextPercent || 0) * 100))}%"></div></div>
    </section>
    <section class="inspector-section">
      <h3>会话结构</h3>
      <div class="summary-grid">
        ${tile((session.turns || []).length, "Turns")}
        ${tile(analysis.context_compactions || 0, "上下文压缩")}
        ${tile(analysis.rollbacks || 0, "线程回滚")}
        ${tile(analysis.failed_tool_call_count || 0, "失败调用")}
        ${tile(analysis.pending_tool_call_count || 0, "未配对调用")}
        ${tile(formatBytes(analysis.tool_output_bytes || 0), "工具输出")}
      </div>
    </section>
    <section class="inspector-section">
      <h3>事件分布</h3>
      <div class="json-tree">${jsonTree(analysis.categories || {})}</div>
    </section>
  `;
}

function renderCodexContext() {
  const session = state.run;
  const turn = selectedCodexTurn();
  return `
    <section class="inspector-section">
      <h3>Session 元数据</h3>
      <div class="json-tree">${jsonTree(session.metadata || {})}</div>
    </section>
    ${turn ? `<section class="inspector-section"><h3>当前 Turn</h3><div class="json-tree">${jsonTree(turn)}</div></section>` : ""}
    <section class="inspector-section">
      <h3>Base Instructions</h3>
      <pre class="code-block prompt-block">${escapeHtml(session.base_instructions || "rollout 未记录 base instructions。")}</pre>
    </section>
    <section class="inspector-section">
      <h3>Rollout 来源</h3>
      <div class="source-file">${escapeHtml(session.source_file)}</div>
    </section>
  `;
}

function renderCodexMessages() {
  const turn = selectedCodexTurn();
  const all = (state.run.messages || []).filter((message) => !turn || message.turn_id === turn.id);
  const messages = all.slice(-200);
  return `
    <section class="inspector-section">
      <h3>${turn ? `Turn ${turn.index}` : "Session"} 消息 · ${all.length}</h3>
      ${all.length > messages.length ? `<div class="empty-inline" style="margin-bottom:8px">消息较多，仅展示最后 ${messages.length} 条；完整内容可在时间线和原始 JSONL 中查看。</div>` : ""}
      ${messages.map((message) => `
        <details class="data-card message-card" ${message.role === "assistant" ? "open" : ""}>
          <summary>
            <span class="role-pill role-${escapeHtml(message.role)}">${escapeHtml(message.role)}</span>
            <span class="message-preview">${escapeHtml(String(message.text || "").replaceAll("\n", " ").slice(0, 120) || "空消息")}</span>
          </summary>
          <div class="data-card-body">
            <div class="prompt-meta"><span>${escapeHtml(formatDate(message.timestamp))}</span><span>#${message.line_number}${message.phase ? ` · ${escapeHtml(message.phase)}` : ""}</span></div>
            <pre class="code-block result-block">${escapeHtml(message.text || "")}</pre>
            ${message.text_truncated ? `<div class="empty-inline">消息共 ${formatNumber(message.text_characters)} 字符；此处显示前 64,000 字符，完整内容请打开时间线中的原始事件。</div>` : ""}
          </div>
        </details>
      `).join("") || empty("当前范围没有 message 事件。")}
    </section>
  `;
}

function renderCodexTools() {
  const turn = selectedCodexTurn();
  const allCalls = (state.run.calls || []).filter((call) => !turn || call.turn_id === turn.id);
  const calls = allCalls.slice(-300);
  const tools = state.run.analysis?.tools || [];
  return `
    <section class="inspector-section">
      <h3>工具分布</h3>
      ${tools.map((tool) => `<span class="tool-pill">${escapeHtml(tool.name)} · ${tool.calls}</span>`).join("") || empty("没有工具调用。")}
    </section>
    <section class="inspector-section">
      <h3>${turn ? `Turn ${turn.index}` : "Session"} 调用明细 · ${allCalls.length}</h3>
      ${allCalls.length > calls.length ? `<div class="empty-inline" style="margin-bottom:8px">仅展示最后 ${calls.length} 次调用。</div>` : ""}
      ${calls.map((call) => `
        <details class="data-card">
          <summary>${escapeHtml(call.name)} · ${escapeHtml(statusLabel(call.status))}${call.exit_code !== null ? ` · exit ${call.exit_code}` : ""}</summary>
          <div class="data-card-body">
            <div class="prompt-meta"><span>${escapeHtml(formatDate(call.started_at))}</span><span>${escapeHtml(call.duration_seconds === null ? "-" : `${Number(call.duration_seconds).toFixed(2)} 秒`)} · ${formatBytes(call.output_bytes)}</span></div>
            <div class="detail-label">输入</div>
            <pre class="code-block command">${escapeHtml(stringify(call.input ?? call.input_preview))}</pre>
            <div class="detail-label">输出预览</div>
            <pre class="code-block output">${escapeHtml(call.output_preview || "尚未记录结果事件。")}</pre>
            <div class="empty-inline">调用行 #${call.line_number ?? "-"} · 结果行 #${call.result_line_number ?? "-"}</div>
          </div>
        </details>
      `).join("") || empty("当前范围没有工具调用。")}
    </section>
  `;
}

function renderCodexTurns() {
  const usageByTurn = Object.fromEntries((state.run.usage?.turns || []).map((item) => [item.turn_id, item]));
  return `
    <section class="inspector-section">
      <h3>Turn 生命周期 · ${(state.run.turns || []).length}</h3>
      ${(state.run.turns || []).map((turn) => `
        <details class="data-card" ${turn.id === state.nodeId ? "open" : ""}>
          <summary>Turn ${turn.index} · ${escapeHtml(statusLabel(turn.status))} · ${escapeHtml(formatDuration(turn.started_at, turn.finished_at))}</summary>
          <div class="data-card-body">
            <div class="prompt-meta"><span>${escapeHtml(turn.id)}</span><span>TTFT ${turn.time_to_first_token_ms ?? "-"} ms</span></div>
            <div class="detail-label">用户请求</div>
            <pre class="code-block prompt-block">${escapeHtml(turn.user_message || "未捕获")}</pre>
            <div class="summary-grid" style="margin-top:8px">
              ${tile(turn.event_count || 0, "事件")}
              ${tile(turn.tool_call_count || 0, "工具")}
              ${tile(turn.reasoning_count || 0, "推理")}
              ${tile(formatNumber(usageByTurn[turn.id]?.total_tokens), "Token")}
            </div>
            ${turn.abort_reason ? `<div class="diagnostic severity-warning" style="margin-top:8px"><strong>中止原因</strong><p>${escapeHtml(turn.abort_reason)}</p></div>` : ""}
          </div>
        </details>
      `).join("") || empty("该 rollout 没有 task_started 事件。")}
    </section>
  `;
}

function renderCodexRaw() {
  const artifact = state.artifact;
  return `
    <section class="inspector-section">
      <h3>原始 Rollout JSONL</h3>
      <div class="source-file">${escapeHtml(state.run.source_file)}</div>
      <div style="margin-top:9px">
        <button id="codex-load-raw" class="button" type="button">${artifact ? "重新加载开头" : "加载第一段"}</button>
      </div>
      <div class="artifact-viewer">
        ${artifact ? `<div class="prompt-meta"><span>${escapeHtml(artifact.name)}</span><span>${formatBytes(artifact.size)}</span></div><pre class="code-block output">${escapeHtml(artifact.content)}</pre>${artifact.has_more ? '<button id="artifact-more" class="button" type="button">加载下一段</button>' : ""}` : empty("原始文件按字节分块读取，不会一次加载整个大型 rollout。")}
      </div>
    </section>
    <section class="inspector-section">
      <h3>解析异常</h3>
      <div class="json-tree">${jsonTree(state.run.parse_errors || [])}</div>
    </section>
  `;
}

function renderInput() {
  const context = state.node.context || {};
  const agentInput = context.agent_input || context.rendered_prompt || "";
  return `
    <section class="inspector-section">
      <h3>实际发送给 Agent 的提示词</h3>
      <div class="prompt-meta"><span>${escapeHtml(context.agent_input_source || "未捕获")}</span><span>${formatBytes(new Blob([agentInput]).size)}</span></div>
      <pre class="code-block prompt-block">${escapeHtml(agentInput || "没有可用的 launch artifact，无法恢复实际渲染后的提示词。")}</pre>
    </section>
    <section class="inspector-section">
      <h3>AgentFlow 渲染后的提示词</h3>
      <pre class="code-block prompt-block">${escapeHtml(context.rendered_prompt || "")}</pre>
    </section>
    <section class="inspector-section">
      <h3>Pipeline 提示词模板</h3>
      <pre class="code-block prompt-block">${escapeHtml(context.prompt_template || "")}</pre>
    </section>
    <section class="inspector-section">
      <h3>已解析的上下文引用</h3>
      ${(state.node.inbound || []).map(renderTransfer).join("") || empty("该提示词未显式引用上游节点的值。")}
    </section>
    <section class="inspector-section">
      <h3>启动计划</h3>
      <div class="json-tree">${jsonTree(context.launch || {})}</div>
    </section>
    <section class="inspector-section">
      <h3>节点配置</h3>
      <div class="json-tree">${jsonTree(state.node.spec || {})}</div>
    </section>
  `;
}

function renderOutput() {
  const result = state.node.result || {};
  return `
    <section class="inspector-section">
      <h3>传递给下游的节点输出</h3>
      <pre class="code-block result-block">${escapeHtml(result.output || "未产生输出。")}</pre>
    </section>
    <section class="inspector-section">
      <h3>Agent 最终回复</h3>
      <pre class="code-block result-block">${escapeHtml(result.final_response || "未捕获到最终回复。")}</pre>
    </section>
    <section class="inspector-section">
      <h3>成功条件检查</h3>
      <div class="json-tree">${jsonTree({ success: result.success, details: result.success_details || [] })}</div>
    </section>
  `;
}

function renderTools() {
  const activity = state.node.activity || {};
  const configuredMcps = activity.configured_mcps || [];
  const observedMcps = activity.observed_mcp_calls || [];
  const tools = activity.tools || [];
  const skills = activity.configured_skills || [];
  return `
    <section class="inspector-section">
      <h3>工具策略与实际调用</h3>
      <div class="empty-inline" style="margin-bottom:7px">权限：<strong>${escapeHtml(activity.configured_tool_access || "未知")}</strong></div>
      ${tools.map((tool) => `<span class="tool-pill">${escapeHtml(tool.name)} · ${tool.calls}</span>`).join("") || empty("未观察到具名工具调用；命令执行记录列在下方。")}
    </section>
    <section class="inspector-section">
      <h3>执行命令</h3>
      <table class="list-table">
        <thead><tr><th>命令</th><th>退出码</th><th>输出</th></tr></thead>
        <tbody>${(activity.commands || []).map((command) => `<tr><td><code>${escapeHtml(command.command)}</code></td><td>${escapeHtml(command.exit_code ?? command.status)}</td><td>${escapeHtml(formatBytes(command.output_bytes))}</td></tr>`).join("")}</tbody>
      </table>
      ${activity.commands?.length ? "" : empty("未观察到命令执行。")}
    </section>
    <section class="inspector-section">
      <h3>MCP 服务</h3>
      ${configuredMcps.map((mcp) => `<details class="data-card"><summary>${escapeHtml(mcp.name)} · 已配置</summary><div class="data-card-body"><div class="json-tree">${jsonTree(mcp)}</div></div></details>`).join("") || empty("该节点未配置 MCP 服务。")}
      ${observedMcps.map((mcp) => `<span class="tool-pill">已观察到 ${escapeHtml(mcp.name)} · ${mcp.calls}</span>`).join("")}
    </section>
    <section class="inspector-section">
      <h3>节点选用的 Skill</h3>
      ${skills.map((skill) => `<span class="tool-pill">${escapeHtml(skill)}</span>`).join("") || empty("该节点未选用 AgentFlow Skill。")}
    </section>
  `;
}

function renderTransfer(edge) {
  const source = edge.source_type === "fanout" ? `扇出组：${edge.source}` : (edge.source || edge.source_type);
  return `
    <article class="transfer-card">
      <div class="transfer-route">
        ${edge.source_type === "node" && edge.source ? `<button type="button" data-jump-node="${escapeHtml(edge.source)}">${escapeHtml(source)}</button>` : `<span>${escapeHtml(source)}</span>`}
        <span>&gt;</span>
        <button type="button" data-jump-node="${escapeHtml(edge.target)}">${escapeHtml(edge.target)}</button>
      </div>
      ${edge.source_type === "fanout" && edge.source_nodes?.length ? `<div class="dependency-only">成员：${edge.source_nodes.map((node) => escapeHtml(node)).join("、")}</div>` : ""}
      ${edge.explicit ? `<div class="transfer-expression">{{ ${escapeHtml(edge.expression)} }}</div>` : '<div class="dependency-only">仅为调度依赖，未发现直接模板值引用。</div>'}
      ${edge.resolved ? `<details><summary class="dependency-only">已传递的值</summary><pre class="code-block transfer-value">${escapeHtml(stringify(edge.value))}</pre></details>` : ""}
    </article>
  `;
}

function renderTransfers() {
  const inbound = state.node.inbound || [];
  const outbound = state.node.outbound || [];
  return `
    <section class="inspector-section">
      <h3>来自上游的输入 · ${inbound.length}</h3>
      ${inbound.map(renderTransfer).join("") || empty("未发现上游数据传递。")}
    </section>
    <section class="inspector-section">
      <h3>被下游消费的输出 · ${outbound.length}</h3>
      ${outbound.map(renderTransfer).join("") || empty("没有下游提示词直接引用该节点。")}
    </section>
  `;
}

function renderFiles() {
  const artifacts = state.node.artifacts || [];
  return `
    <section class="inspector-section">
      <h3>节点 Artifact · ${artifacts.length}</h3>
      ${artifacts.map((artifact) => `
        <button class="artifact-item ${state.artifactName === artifact.name ? "active" : ""}" type="button" data-artifact="${escapeHtml(artifact.name)}">
          <span class="artifact-name">${escapeHtml(artifact.name)}</span>
          <span class="artifact-size">${escapeHtml(formatBytes(artifact.size))}</span>
        </button>
      `).join("") || empty("该节点没有 Artifact 文件。")}
      <div id="artifact-viewer" class="artifact-viewer">
        ${state.artifact ? `<div class="prompt-meta"><span>${escapeHtml(state.artifact.name)}</span><span>${formatBytes(state.artifact.size)}</span></div>${state.artifact.parsed ? `<div class="json-tree">${jsonTree(state.artifact.parsed)}</div>` : `<pre class="code-block output">${escapeHtml(state.artifact.content)}</pre>`}${state.artifact.has_more ? '<button id="artifact-more" class="button" type="button">加载下一段</button>' : ""}` : empty("选择一个 Artifact，以分块方式查看内容。")}
      </div>
    </section>
  `;
}

function bindInspectorActions() {
  elements.inspector_content.querySelectorAll("[data-jump-node]").forEach((button) => {
    button.onclick = () => selectNode(button.dataset.jumpNode);
  });
  elements.inspector_content.querySelectorAll("[data-artifact]").forEach((button) => {
    button.onclick = () => loadArtifact(button.dataset.artifact, 0);
  });
  elements.inspector_content.querySelectorAll("[data-codex-session]").forEach((button) => {
    button.onclick = () => switchSource("codex", button.dataset.codexSession);
  });
  const raw = document.getElementById("codex-load-raw");
  if (raw) raw.onclick = () => loadArtifact("rollout", 0);
  const more = document.getElementById("artifact-more");
  if (more) more.onclick = () => loadArtifact(state.artifactName, state.artifact.next_offset, true);
}

function renderInspector() {
  if (!state.node) {
    elements.inspector_title.textContent = "未选择";
    elements.inspector_status.textContent = "等待中";
    elements.inspector_status.className = "status-badge status-pending";
    elements.inspector_content.innerHTML = empty(state.source === "codex" ? "选择一个 Codex session 以查看详情。" : "选择一个 Agent 节点以查看详情。");
    return;
  }
  if (state.source === "codex") {
    const turn = selectedCodexTurn();
    const status = turn?.status || state.run.status;
    elements.inspector_title.textContent = turn ? `Turn ${turn.index}` : state.run.id.slice(0, 18);
    elements.inspector_status.textContent = statusLabel(status);
    elements.inspector_status.className = `status-badge ${statusClass(status)}`;
    elements.inspector_tabs.querySelectorAll("[data-tab]").forEach((button) => {
      button.setAttribute("aria-selected", String(button.dataset.tab === state.activeTab));
    });
    const views = {
      overview: renderCodexOverview,
      input: renderCodexContext,
      output: renderCodexMessages,
      tools: renderCodexTools,
      transfers: renderCodexTurns,
      files: renderCodexRaw,
    };
    elements.inspector_content.innerHTML = (views[state.activeTab] || renderCodexOverview)();
    bindInspectorActions();
    return;
  }
  const status = state.node.result?.status || "pending";
  elements.inspector_title.textContent = state.node.id;
  elements.inspector_status.textContent = statusLabel(status);
  elements.inspector_status.className = `status-badge ${statusClass(status)}`;
  elements.inspector_tabs.querySelectorAll("[data-tab]").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.tab === state.activeTab));
  });
  const views = { overview: renderOverview, input: renderInput, output: renderOutput, tools: renderTools, transfers: renderTransfers, files: renderFiles };
  elements.inspector_content.innerHTML = (views[state.activeTab] || renderOverview)();
  bindInspectorActions();
}

async function loadRuns({ autoSelect = true } = {}) {
  state.runs = await api(state.source === "codex" ? "/api/codex/sessions" : "/api/runs");
  renderRuns();
  if (autoSelect && !state.runId && state.runs.length) await selectRun(state.runs[0].id);
}

async function selectRun(runId, { preserveNode = false } = {}) {
  const version = ++state.requestVersion;
  showNotice("");
  const run = await api(state.source === "codex"
    ? `/api/codex/sessions/${encodeURIComponent(runId)}`
    : `/api/runs/${encodeURIComponent(runId)}`);
  if (version !== state.requestVersion) return;
  state.runId = runId;
  state.run = run;
  const preferredNode = state.source === "codex"
    ? (preserveNode && (state.nodeId === "__all__" || run.turns.some((turn) => turn.id === state.nodeId)) ? state.nodeId : "__all__")
    : (preserveNode && run.nodes.some((node) => node.id === state.nodeId) ? state.nodeId : run.nodes[0]?.id);
  state.nodeId = null;
  state.node = null;
  state.events = [];
  renderRuns();
  renderRunHeader();
  renderNodes();
  renderFlowMap();
  if (preferredNode) await selectNode(preferredNode);
}

async function selectNode(nodeId, { preserveTab = true } = {}) {
  if (!state.runId) return;
  const version = ++state.requestVersion;
  const changed = state.nodeId !== nodeId;
  state.nodeId = nodeId;
  state.node = null;
  state.events = [];
  state.attempt = changed ? "" : state.attempt;
  state.artifact = null;
  state.artifactName = null;
  if (!preserveTab) state.activeTab = "overview";
  elements.inspector_title.textContent = nodeId;
  elements.inspector_content.innerHTML = '<div class="loading">正在加载节点上下文...</div>';
  elements.timeline.innerHTML = '<div class="loading">正在解析 JSONL 时间线...</div>';
  renderNodes();
  renderFlowMap();
  if (state.source === "codex") {
    const events = await fetchEventsPage(0);
    if (version !== state.requestVersion) return;
    state.node = state.run;
    applyEventsPage(events, false);
    renderNodes();
    renderFlowMap();
    renderInspector();
    return;
  }
  const [node, events] = await Promise.all([
    api(`/api/runs/${encodeURIComponent(state.runId)}/nodes/${encodeURIComponent(nodeId)}`),
    fetchEventsPage(0),
  ]);
  if (version !== state.requestVersion) return;
  state.node = node;
  applyEventsPage(events, false);
  renderNodes();
  renderFlowMap();
  renderAttemptFilter();
  renderInspector();
}

function eventsUrl(offset = 0) {
  const params = new URLSearchParams({ offset: String(offset), limit: "300", order: state.order });
  if (state.category !== "all") params.set("category", state.category);
  if (state.source === "codex" && state.nodeId !== "__all__") params.set("turn_id", state.nodeId);
  if (state.source !== "codex" && state.attempt) params.set("attempt", state.attempt);
  if (state.query) params.set("q", state.query);
  return state.source === "codex"
    ? `/api/codex/sessions/${encodeURIComponent(state.runId)}/events?${params}`
    : `/api/runs/${encodeURIComponent(state.runId)}/nodes/${encodeURIComponent(state.nodeId)}/events?${params}`;
}

function fetchEventsPage(offset = 0) {
  return api(eventsUrl(offset));
}

function applyEventsPage(page, append) {
  state.events = append ? [...state.events, ...page.items] : page.items;
  state.eventsTotal = page.total;
  state.eventCategories = page.categories || {};
  state.eventParseErrors = page.parse_errors || [];
  renderTimeline();
  renderCategoryFilters();
}

async function loadEvents({ append = false } = {}) {
  if (!state.runId || !state.nodeId) return;
  const page = await fetchEventsPage(append ? state.events.length : 0);
  applyEventsPage(page, append);
}

async function loadArtifact(name, offset = 0, append = false) {
  if (!state.runId || !state.nodeId || !name) return;
  const artifact = await api(state.source === "codex"
    ? `/api/codex/sessions/${encodeURIComponent(state.runId)}/raw?offset=${offset}&limit=250000`
    : `/api/runs/${encodeURIComponent(state.runId)}/nodes/${encodeURIComponent(state.nodeId)}/artifacts/${encodeURIComponent(name)}?offset=${offset}&limit=250000`);
  if (append && (state.source === "codex" || state.artifact?.name === name)) artifact.content = state.artifact.content + artifact.content;
  state.artifactName = name;
  state.artifact = artifact;
  renderInspector();
}

async function refreshCurrent() {
  try {
    await loadRuns({ autoSelect: false });
    if (state.runId) await selectRun(state.runId, { preserveNode: true });
  } catch (error) {
    showNotice(`刷新失败：${error.message}`);
  }
}

let searchTimer = null;
elements.run_search.oninput = renderRuns;
elements.node_search.oninput = renderNodes;
elements.event_search.oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    state.query = elements.event_search.value.trim();
    await loadEvents();
  }, 250);
};
elements.attempt_filter.onchange = async () => {
  state.attempt = elements.attempt_filter.value;
  await loadEvents();
};
elements.sort_button.onclick = async () => {
  state.order = state.order === "asc" ? "desc" : "asc";
  elements.sort_button.dataset.order = state.order;
  elements.sort_button.textContent = state.order === "asc" ? "最早在前" : "最新在前";
  await loadEvents();
};
elements.load_more.onclick = () => loadEvents({ append: true });
elements.refresh_button.onclick = refreshCurrent;
elements.inspector_tabs.querySelectorAll("[data-tab]").forEach((button) => {
  button.onclick = () => {
    state.activeTab = button.dataset.tab;
    renderInspector();
  };
});
async function switchSource(source, targetRunId = null) {
  if (source === state.source && !targetRunId) return;
  state.requestVersion += 1;
  state.source = source;
  resetSelection();
  configureSourceUi();
  renderRuns();
  renderNodes();
  renderRunHeader();
  renderFlowMap();
  renderTimeline();
  renderInspector();
  try {
    await loadRuns({ autoSelect: !targetRunId });
    if (targetRunId) {
      if (state.runs.some((run) => run.id === targetRunId)) await selectRun(targetRunId);
      else showNotice(`当前 Codex rollout 目录中未找到 session ${targetRunId}`);
    }
  } catch (error) {
    showNotice(`无法加载日志数据源：${error.message}`);
  }
}

elements.source_switch.querySelectorAll("[data-source]").forEach((button) => {
  button.onclick = () => switchSource(button.dataset.source);
});

setInterval(() => {
  if (elements.live_toggle.checked && state.runId && !document.hidden) refreshCurrent();
}, 5000);

configureSourceUi();
loadRuns().catch((error) => {
  showNotice(`无法加载运行记录：${error.message}`);
  elements.run_list.innerHTML = empty("无法读取运行记录目录。");
});
