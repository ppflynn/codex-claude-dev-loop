const state = {
  projects: [],
  selectedProjectId: localStorage.getItem("selectedProjectId") || null,
  tasks: [],
  selectedTaskId: localStorage.getItem("selectedTaskId") || null,
  taskView: localStorage.getItem("taskView") || "active",
  artifacts: {},
  activeArtifact: null,
};

const taskStatusLabels = {
  CREATED: "已创建",
  WAITING_FOR_CLAUDE: "等待 Claude",
  CLAUDE_WINDOW_STARTED: "Claude 运行中",
  WAITING_FOR_CODEX: "等待 Codex",
  CODEX_WINDOW_STARTED: "Codex 运行中",
  NEEDS_FIX: "需要修复",
  PASS: "通过",
  BLOCKED: "阻塞",
  FAILED: "失败",
  CANCELLED: "已取消",
};

const clientLabels = {
  claude: "Claude",
  codex: "Codex",
};

const stageLabels = {
  created: "已创建",
  claude_running: "Claude 实现中",
  waiting_for_codex: "等待 Codex 审查",
  codex_running: "Codex 审查中",
  no_changes: "无改动",
  review_complete: "审查完成",
  review_invalid: "审查无效",
  max_rounds_exhausted: "已达最大轮次",
  cancelled: "已取消",
  git_collection_failed: "Git 收集失败",
};

const terminalStatuses = new Set(["PASS", "BLOCKED", "FAILED", "CANCELLED"]);
const runningTaskStatuses = new Set(["CLAUDE_WINDOW_STARTED", "CODEX_WINDOW_STARTED"]);

const terminalConnections = {
  claude: { source: null, done: false, subKey: null, reconnectTimer: null, reconnectDelay: 1000 },
  codex: { source: null, done: false, subKey: null, reconnectTimer: null, reconnectDelay: 1000 },
};

// xterm.js terminal instances
const VSCodeTerminalTheme = {
  background: "#1e1e1e",
  foreground: "#cccccc",
  cursor: "#ffffff",
  selectionBackground: "#264f78",
  black: "#000000",
  red: "#cd3131",
  green: "#0dbc79",
  yellow: "#e5e510",
  blue: "#2472c8",
  magenta: "#bc3fbc",
  cyan: "#11a8cd",
  white: "#e5e5e5",
  brightBlack: "#666666",
  brightRed: "#f14c4c",
  brightGreen: "#23d18b",
  brightYellow: "#f5f543",
  brightBlue: "#3b8eea",
  brightMagenta: "#d670d6",
  brightCyan: "#29b8db",
  brightWhite: "#ffffff",
};

const terminalInstances = {
  claude: { term: null, fitAddon: null, observer: null, hasOutput: false },
  codex: { term: null, fitAddon: null, observer: null, hasOutput: false },
};

function createTerminal(client) {
  destroyTerminal(client);
  const container = document.getElementById(`${client}-output`);
  if (!container) return null;

  container.innerHTML = "";

  const term = new Terminal({
    theme: VSCodeTerminalTheme,
    fontSize: 13,
    fontFamily: 'Consolas, "Cascadia Mono", "Microsoft YaHei UI", monospace',
    allowProposedApi: true,
    cursorBlink: false,
    disableStdin: true,
    scrollback: 10000,
    convertEol: true,
  });

  const FitAddonCtor = window.FitAddon?.FitAddon || window.FitAddon;
  const fitAddon = new FitAddonCtor();
  term.loadAddon(fitAddon);
  term.open(container);

  const observer = new ResizeObserver(() => {
    try { fitAddon.fit(); } catch (_e) { /* ignore */ }
  });
  observer.observe(container);

  terminalInstances[client] = { term, fitAddon, observer, hasOutput: false };
  return term;
}

function destroyTerminal(client) {
  const inst = terminalInstances[client];
  if (inst.observer) {
    inst.observer.disconnect();
    inst.observer = null;
  }
  if (inst.term) {
    inst.term.dispose();
    inst.term = null;
  }
  inst.fitAddon = null;
  inst.hasOutput = false;
}

function destroyAllTerminals() {
  destroyTerminal("claude");
  destroyTerminal("codex");
}

function writeToTerminal(client, text) {
  const term = terminalInstances[client].term;
  if (term) {
    term.write(text);
  }
}

function clearAndWriteTerminal(client, text) {
  const term = terminalInstances[client].term;
  if (term) {
    term.reset();
    if (text) term.write(text);
  }
}

function writeTerminalPlaceholder(client, text) {
  const term = terminalInstances[client].term;
  if (term) {
    term.reset();
    term.write("\x1b[2m" + text + "\x1b[0m\r\n");
  }
}

function connectTerminal(client) {
  disconnectTerminal(client);
  const task = selectedTask();
  if (!task) return;

  const taskId = task.id;
  const taskRound = task.round;

  const term = createTerminal(client);
  if (!term) return;
  term.write("\x1b[2m正在连接...\x1b[0m\r\n");
  terminalConnections[client].done = false;
  terminalInstances[client].hasOutput = false;
  updateTerminalBadges();

  const es = new EventSource(`/api/tasks/${taskId}/terminal/${client}/stream`);
  terminalConnections[client].source = es;

  es.onmessage = (event) => {
    const current = selectedTask();
    if (!current || current.id !== taskId || current.round !== taskRound) {
      es.close();
      if (terminalConnections[client].source === es) terminalConnections[client].source = null;
      return;
    }
    try {
      const data = JSON.parse(event.data);
      if (data.waiting) {
        if (!terminalInstances[client].hasOutput) {
          term.reset();
          term.write("\x1b[2m等待 CLI 启动并创建日志文件...\x1b[0m\r\n");
        }
      }
      if (data.chunk) {
        terminalConnections[client].reconnectDelay = 1000;
        if (!terminalInstances[client].hasOutput) {
          term.reset();
          terminalInstances[client].hasOutput = true;
        }
        term.write(data.chunk);
      }
      if (data.done) {
        if (terminalConnections[client].source === es) {
          terminalConnections[client].done = true;
          es.close();
          terminalConnections[client].source = null;
        }
        updateTerminalBadges();
      }
    } catch (_e) {
      // ignore parse errors
    }
  };

  es.onerror = () => {
    es.close();
    const wasCurrent = terminalConnections[client].source === es;
    if (wasCurrent) {
      terminalConnections[client].source = null;
      terminalConnections[client].subKey = null;
    }
    updateTerminalBadges();

    if (!wasCurrent) return;

    const current = selectedTask();
    const activeForClient =
      (client === "claude" && (current?.activeClient === "claude" || current?.status === "CLAUDE_WINDOW_STARTED")) ||
      (client === "codex" && (current?.activeClient === "codex" || current?.status === "CODEX_WINDOW_STARTED"));
    if (current && current.id === taskId && current.round === taskRound
        && runningTaskStatuses.has(current.status) && activeForClient) {
      const delay = terminalConnections[client].reconnectDelay;
      terminalConnections[client].reconnectTimer = setTimeout(() => {
        terminalConnections[client].reconnectTimer = null;
        if (selectedTask()?.id === taskId && selectedTask()?.round === taskRound) {
          terminalConnections[client].reconnectDelay = Math.min(delay * 2, 30000);
          connectTerminal(client);
        }
      }, delay);
    }
  };
}

function disconnectTerminal(client) {
  const conn = terminalConnections[client];
  if (conn.reconnectTimer) {
    clearTimeout(conn.reconnectTimer);
    conn.reconnectTimer = null;
  }
  if (conn.source) {
    conn.source.close();
    conn.source = null;
  }
  conn.done = false;
}

function disconnectAllTerminals() {
  disconnectTerminal("claude");
  disconnectTerminal("codex");
  updateTerminalBadges();
}

function updateTerminalBadges() {
  const task = selectedTask();
  const state = document.getElementById("terminal-state");
  if (!state) return;

  if (!task) {
    state.textContent = "待选择";
    state.className = "status-pill";
    return;
  }

  updateClientTitleBadge("claude", task);
  updateClientTitleBadge("codex", task);

  const hasClaude = terminalConnections.claude.source !== null;
  const hasCodex = terminalConnections.codex.source !== null;
  if (hasClaude || hasCodex) {
    const labels = [];
    if (hasClaude) labels.push("Claude 连接中");
    if (hasCodex) labels.push("Codex 连接中");
    state.textContent = labels.join(" / ");
    state.className = "status-pill running";
  } else if (terminalConnections.claude.done || terminalConnections.codex.done) {
    state.textContent = "已完成";
    state.className = "status-pill pass";
  } else if (task.activeClient && runningTaskStatuses.has(task.status)) {
    state.textContent = "等待输出";
    state.className = "status-pill running";
  } else {
    state.textContent = "待命中";
    state.className = "status-pill";
  }
}

function updateClientTitleBadge(client, task) {
  const box = document.getElementById(`${client}-terminal-box`);
  if (!box) return;
  const title = box.querySelector(".terminal-title");
  if (!title) return;

  let badge = title.querySelector(".terminal-client-badge");
  if (!badge) {
    badge = document.createElement("span");
    badge.className = "terminal-client-badge";
    title.appendChild(badge);
  }

  const isActive = task.activeClient === client && runningTaskStatuses.has(task.status);
  const isConnected = terminalConnections[client].source !== null;
  const isDone = terminalConnections[client].done;

  if (isConnected) {
    badge.textContent = "实时";
    badge.className = "terminal-client-badge active";
  } else if (isDone) {
    badge.textContent = "完成";
    badge.className = "terminal-client-badge";
  } else if (isActive) {
    badge.textContent = "运行中";
    badge.className = "terminal-client-badge active";
  } else {
    badge.textContent = "";
    badge.className = "terminal-client-badge";
  }
}

async function loadTerminalContent(client, taskId, taskRound) {
  const term = createTerminal(client);
  if (!term) return;

  try {
    const meta = await api(`/api/tasks/${taskId}/terminal/${client}`);
    const current = selectedTask();
    if (!current || current.id !== taskId || current.round !== taskRound) return;

    if (!meta.exists) {
      term.write("\x1b[2m（" + (client === "claude" ? "Claude" : "Codex") + " CLI 尚未启动或日志文件不存在。）\x1b[0m\r\n");
      return;
    }
    const artifacts = await api(`/api/tasks/${taskId}/artifacts`);
    const current2 = selectedTask();
    if (!current2 || current2.id !== taskId || current2.round !== taskRound) return;

    const logName = meta.logName;
    if (artifacts.artifacts && artifacts.artifacts[logName] && artifacts.artifacts[logName].exists) {
      term.write(artifacts.artifacts[logName].content);
    }
    terminalInstances[client].hasOutput = true;
  } catch (_e) {
    const current = selectedTask();
    if (!current || current.id !== taskId || current.round !== taskRound) return;
    term.write("\x1b[2m（无法读取 " + (client === "claude" ? "Claude" : "Codex") + " 终端输出。）\x1b[0m\r\n");
  }
}

function refreshTerminalsForTask() {
  const task = selectedTask();
  const taskId = task?.id ?? null;
  const taskRound = task?.round ?? null;

  const claudeKey = task ? `${task.id}|${task.round}|claude|${task.status}|${task.activeClient ?? ""}` : null;
  const codexKey = task ? `${task.id}|${task.round}|codex|${task.status}|${task.activeClient ?? ""}` : null;
  if (claudeKey === terminalConnections.claude.subKey && codexKey === terminalConnections.codex.subKey) {
    return;
  }
  terminalConnections.claude.subKey = claudeKey;
  terminalConnections.codex.subKey = codexKey;

  disconnectAllTerminals();
  destroyAllTerminals();

  // Create fresh terminals with placeholder
  const placeholder = task
    ? `任务 ${taskId} 轮次 ${taskRound} — 等待终端输出...`
    : "选择运行中的任务以查看终端输出。";

  const claudeTerm = createTerminal("claude");
  const codexTerm = createTerminal("codex");
  if (claudeTerm) writeTerminalPlaceholder("claude", placeholder);
  if (codexTerm) writeTerminalPlaceholder("codex", placeholder);

  if (!task) {
    updateTerminalBadges();
    return;
  }

  if (runningTaskStatuses.has(task.status)) {
    const claudeActive = task.activeClient === "claude" || task.status === "CLAUDE_WINDOW_STARTED";
    const codexActive = task.activeClient === "codex" || task.status === "CODEX_WINDOW_STARTED";
    if (claudeActive) connectTerminal("claude"); else loadTerminalContent("claude", taskId, taskRound);
    if (codexActive) connectTerminal("codex"); else loadTerminalContent("codex", taskId, taskRound);
  } else {
    loadTerminalContent("claude", taskId, taskRound);
    loadTerminalContent("codex", taskId, taskRound);
    updateTerminalBadges();
  }
}

const $ = (id) => document.getElementById(id);

function selectedProject() {
  return state.projects.find((project) => project.id === state.selectedProjectId) || null;
}

function selectedTask() {
  return state.tasks.find((task) => task.id === state.selectedTaskId) || null;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.classList.add("show");
  window.clearTimeout(box._timer);
  box._timer = window.setTimeout(() => box.classList.remove("show"), 2800);
}

async function loadProjects() {
  const data = await api("/api/projects");
  state.projects = data.projects || [];
  if (!state.selectedProjectId && state.projects.length) {
    state.selectedProjectId = state.projects[0].id;
  }
  if (state.selectedProjectId && !state.projects.some((p) => p.id === state.selectedProjectId)) {
    state.selectedProjectId = state.projects[0]?.id || null;
  }
  renderProjects();
  await loadSelectedProject();
}

function renderProjects() {
  const list = $("project-list");
  list.innerHTML = "";
  if (!state.projects.length) {
    const empty = document.createElement("div");
    empty.className = "empty-projects";
    empty.innerHTML = "<strong>还没有项目</strong><span>导入一个 Git 仓库后开始创建任务。</span>";
    list.appendChild(empty);
    return;
  }

  state.projects.forEach((project) => {
    const button = document.createElement("button");
    button.type = "button";
    const isAvailable = project.available !== false;
    const cls = ["project-item"];
    if (project.id === state.selectedProjectId) cls.push("active");
    if (!isAvailable) cls.push("project-unavailable");
    button.className = cls.join(" ");
    button.onclick = () => selectProject(project.id);

    const title = document.createElement("div");
    title.className = "project-title";
    let badges = `<span class="kind-pill">${kindLabel(project.kind)}</span>`;
    const wtLabel = worktreeTypeLabel(project.worktreeType);
    if (wtLabel) {
      badges += `<span class="kind-pill ${worktreeTypeClass(project.worktreeType)}">${wtLabel}</span>`;
    }
    if (!isAvailable) {
      badges += `<span class="kind-pill kind-pill-warn">不可用</span>`;
    }
    title.innerHTML = `<span>${escapeHtml(project.name)}</span><span class="pill-group">${badges}</span>`;

    const path = document.createElement("div");
    path.className = "project-path";
    path.textContent = project.path;

    const meta = document.createElement("div");
    meta.className = "project-meta";
    const parts = [];
    if (project.branch) parts.push(`分支：${project.branch}`);
    if (project.worktreeType === "worktree" && project.mainWorktreePath) {
      parts.push(`父项目：${project.mainWorktreePath}`);
    }
    if (project.lastResult) parts.push(`上次：${project.lastResult}`);
    meta.textContent = parts.length ? parts.join("  ·  ") : "可创建本地任务";

    button.append(title, path, meta);
    list.appendChild(button);
  });
}

async function selectProject(id) {
  state.selectedProjectId = id;
  state.selectedTaskId = null;
  localStorage.setItem("selectedProjectId", id);
  localStorage.removeItem("selectedTaskId");
  renderProjects();
  await loadSelectedProject();
}

async function loadSelectedProject() {
  const project = selectedProject();
  $("project-name").textContent = project ? project.name : "未选择项目";

  let pathLabel = "先导入或选择一个项目。";
  if (project) {
    const parts = [`${project.path}`];
    const wtLabel = worktreeTypeLabel(project.worktreeType);
    if (wtLabel) parts.push(wtLabel);
    parts.push(kindLabel(project.kind));
    if (project.branch) parts.push(`分支：${project.branch}`);
    if (project.worktreeType === "worktree" && project.mainWorktreePath) {
      parts.push(`父：${project.mainWorktreePath}`);
    }
    if (project.available === false) {
      parts.push("⚠ 路径不存在");
    }
    pathLabel = parts.join("  ·  ");
  }
  $("project-path-label").textContent = pathLabel;

  if (project && project.available === false) {
    $("project-path-label").classList.add("path-unavailable");
  } else {
    $("project-path-label").classList.remove("path-unavailable");
  }

  $("plan-editor").disabled = !project || project.available === false;
  updateActionStates();

  if (!project) {
    $("plan-editor").value = "";
    $("plan-state").textContent = "待选择";
    state.tasks = [];
    renderTasks();
    return;
  }

  try {
    const plan = await api(`/api/projects/${project.id}/plan`);
    $("plan-editor").value = plan.content || "";
    $("plan-state").textContent = plan.exists ? "已加载" : "新建中";
  } catch (error) {
    $("plan-state").textContent = "读取失败";
    toast(error.message);
  }
  await loadTasks();
}

function taskEndpointForView() {
  if (state.taskView === "archived") return "/api/tasks?archived=1";
  if (state.taskView === "trash") return "/api/trash/tasks";
  return "/api/tasks";
}

async function loadTasks() {
  const project = selectedProject();
  if (!project) return;
  const data = await api(taskEndpointForView());
  state.tasks = (data.tasks || [])
    .filter((task) => task.projectId === project.id)
    .sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt)));

  if (state.selectedTaskId && !state.tasks.some((task) => task.id === state.selectedTaskId)) {
    state.selectedTaskId = state.tasks[0]?.id || null;
  } else if (!state.selectedTaskId && state.tasks.length) {
    state.selectedTaskId = state.tasks[0].id;
  }
  if (state.selectedTaskId) {
    localStorage.setItem("selectedTaskId", state.selectedTaskId);
  } else {
    localStorage.removeItem("selectedTaskId");
  }
  renderTasks();
  await loadArtifacts();
  refreshTerminalsForTask();
}

function renderTasks() {
  renderTaskViewTabs();
  const list = $("task-list");
  list.innerHTML = "";
  if (!state.tasks.length) {
    const empty = document.createElement("div");
    empty.className = "empty-projects";
    empty.innerHTML = `<strong>${emptyTaskTitle()}</strong><span>${emptyTaskMessage()}</span>`;
    list.appendChild(empty);
  } else {
    state.tasks.forEach((task) => {
      const button = document.createElement("button");
      button.type = "button";
      const isRunning = runningTaskStatuses.has(task.status);
      button.className = `project-item ${task.id === state.selectedTaskId ? "active" : ""} ${isRunning ? "task-running" : ""}`;
      button.onclick = () => selectTask(task.id);

      const title = document.createElement("div");
      title.className = "project-title";
      const titleSpan = document.createElement("span");
      titleSpan.textContent = task.title;
      const pillGroup = document.createElement("span");
      pillGroup.className = "pill-group";
      const statusPill = document.createElement("span");
      statusPill.className = `kind-pill ${statusClass(task.status)}`;
      statusPill.textContent = taskBadge(task);
      pillGroup.appendChild(statusPill);
      if (task.activeClient) {
        const clientPill = document.createElement("span");
        clientPill.className = "kind-pill client-pill";
        clientPill.textContent = clientLabels[task.activeClient] || task.activeClient;
        pillGroup.appendChild(clientPill);
      }
      title.append(titleSpan, pillGroup);

      const path = document.createElement("div");
      path.className = "project-path";
      path.textContent = task.id;

      const meta = document.createElement("div");
      meta.className = "project-meta";
      meta.textContent = taskMeta(task);

      button.append(title, path, meta);

      if (task.progress != null && task.progress > 0 && !terminalStatuses.has(task.status)) {
        const bar = document.createElement("div");
        bar.className = "progress-bar";
        const fill = document.createElement("div");
        fill.className = "progress-fill";
        fill.style.width = `${Math.min(100, Math.max(0, task.progress))}%`;
        bar.appendChild(fill);
        button.appendChild(bar);
      }

      list.appendChild(button);
    });
  }
  renderTaskDetails();
}

function renderTaskViewTabs() {
  ["active", "archived", "trash"].forEach((view) => {
    const id = view === "active" ? "active-tasks-tab" : view === "archived" ? "archived-tasks-tab" : "trash-tasks-tab";
    $(id).className = state.taskView === view ? "active" : "";
  });
}

function emptyTaskTitle() {
  if (state.taskView === "archived") return "还没有已归档任务";
  if (state.taskView === "trash") return "回收站为空";
  return "还没有任务";
}

function emptyTaskMessage() {
  if (state.taskView === "archived") return "归档任务后会出现在这里。";
  if (state.taskView === "trash") return "删除任务记录后，任务目录会先移动到工具回收站。";
  return "填写任务表单后会出现在这里。";
}

function taskBadge(task) {
  if (state.taskView === "archived") return "已归档";
  if (state.taskView === "trash") return "回收站";
  return labelStatus(task.status);
}

function taskMeta(task) {
  if (state.taskView === "archived") return `归档：${task.archivedAt || "-"}`;
  if (state.taskView === "trash") return `移入：${task.deletedAt || "-"}`;
  const parts = [];
  parts.push(`轮次 ${task.round}/${task.maxRounds}`);
  if (task.activeClient) {
    parts.push(clientLabels[task.activeClient] || task.activeClient);
  }
  if (task.progress != null) {
    parts.push(`进度 ${task.progress}%`);
  }
  if (task.lastActivityAt) {
    parts.push(formatTime(task.lastActivityAt));
  }
  return parts.join("  ·  ");
}

function formatTime(iso) {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return iso;
  }
}

async function selectTask(id) {
  state.selectedTaskId = id;
  localStorage.setItem("selectedTaskId", id);
  renderTasks();
  await loadArtifacts();
  refreshTerminalsForTask();
}

function renderTaskDetails() {
  const task = selectedTask();
  const status = task?.status || "IDLE";
  $("task-state").textContent = task ? labelStatus(status) : "无任务";
  $("task-state").className = `status-pill ${statusClass(status)}`;
  $("task-title-label").textContent = task?.title || "-";
  $("task-id-label").textContent = task?.id || "-";
  $("task-path-label").textContent = task?.projectPath || "-";
  $("task-round").textContent = task ? `${task.round}` : "-";
  $("task-max-rounds").textContent = task ? `${task.maxRounds}` : "-";
  $("task-created").textContent = task?.createdAt || "-";
  $("task-updated").textContent = task?.lastActivityAt || task?.updatedAt || "-";

  const stageLabel = task?.stage ? (stageLabels[task.stage] || task.stage) : "-";
  $("task-stage-label").textContent = stageLabel;

  const progress = task?.progress != null ? `${task.progress}%` : "-";
  $("task-progress-label").textContent = progress;
  $("task-progress-container").style.display = task && task.progress != null ? "block" : "none";
  const fill = $("task-progress-fill");
  if (task && task.progress != null) {
    fill.style.width = `${Math.min(100, Math.max(0, task.progress))}%`;
  }

  const clientText = task?.activeClient ? (clientLabels[task.activeClient] || task.activeClient) : "-";
  $("task-active-client").textContent = clientText;

  if (task && task.activeClient && runningTaskStatuses.has(task.status)) {
    $("task-active-client").className = "task-detail-value client-running";
  } else {
    $("task-active-client").className = "task-detail-value";
  }

  const history = $("task-history");
  history.textContent = task?.history?.length
    ? task.history.map((item) => `[${item.at}] ${item.event}: ${item.message}`).join("\n")
    : "暂无任务历史。";
  updateActionStates();
}

async function loadArtifacts() {
  const task = selectedTask();
  if (!task || state.taskView === "trash") {
    state.artifacts = {};
    state.activeArtifact = null;
    renderArtifacts();
    return;
  }
  try {
    const data = await api(`/api/tasks/${task.id}/artifacts`);
    state.artifacts = data.artifacts || {};
    const keys = Object.keys(state.artifacts);
    if (!state.activeArtifact || !state.artifacts[state.activeArtifact]) {
      state.activeArtifact = keys[0] || null;
    }
    renderArtifacts();
  } catch (error) {
    state.artifacts = {};
    state.activeArtifact = null;
    renderArtifacts();
    toast(error.message);
  }
}

function renderArtifacts() {
  const tabs = $("artifact-tabs");
  tabs.innerHTML = "";
  const keys = Object.keys(state.artifacts).sort();
  keys.forEach((key) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `tab-button ${state.activeArtifact === key ? "active" : ""}`;
    button.textContent = key;
    button.onclick = () => {
      state.activeArtifact = key;
      renderArtifacts();
    };
    tabs.appendChild(button);
  });
  const artifact = state.artifacts[state.activeArtifact];
  $("artifact-content").textContent = artifact?.exists ? artifact.content : "这个任务还没有可显示的产物。";
}

function updateActionStates() {
  const project = selectedProject();
  const task = selectedTask();
  const status = task?.status || "";
  const taskIsRunning = runningTaskStatuses.has(status);
  const isActiveView = state.taskView === "active";
  const isArchiveView = state.taskView === "archived";
  const isTrashView = state.taskView === "trash";

  const projectAvailable = project && project.available !== false;
  $("initialize-button").disabled = !projectAvailable || project.kind !== "git-uninitialized";
  $("save-plan-button").disabled = !projectAvailable;
  $("remove-project-button").disabled = !project;
  $("create-task-button").disabled = !projectAvailable || !isActiveView;
  $("launch-claude-button").disabled = !task || !isActiveView || status !== "WAITING_FOR_CLAUDE";
  $("claude-completed-button").disabled = !task || !isActiveView || status !== "CLAUDE_WINDOW_STARTED";
  $("launch-codex-button").disabled = !task || !isActiveView || status !== "WAITING_FOR_CODEX";
  $("codex-completed-button").disabled = !task || !isActiveView || status !== "CODEX_WINDOW_STARTED";
  $("cancel-task-button").disabled = !task || !isActiveView || terminalStatuses.has(status);
  $("archive-task-button").disabled = !task || !isActiveView || taskIsRunning;
  $("restore-task-button").disabled = !task || !isArchiveView;
  $("delete-task-button").disabled = !task || !isActiveView || taskIsRunning;
  $("restore-trash-task-button").disabled = !task || !isTrashView;
}

async function setTaskView(view) {
  state.taskView = view;
  state.selectedTaskId = null;
  state.activeArtifact = null;
  localStorage.setItem("taskView", view);
  localStorage.removeItem("selectedTaskId");
  disconnectAllTerminals();
  destroyAllTerminals();
  terminalConnections.claude.subKey = null;
  terminalConnections.codex.subKey = null;
  await loadTasks();
}

async function addProject(event) {
  event.preventDefault();
  const input = $("project-path");
  try {
    const data = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({ path: input.value }),
    });
    input.value = "";
    await loadProjects();
    await selectProject(data.project.id);
    toast("项目已导入");
  } catch (error) {
    toast(error.message);
  }
}

async function initializeProject() {
  const project = selectedProject();
  if (!project) return;
  try {
    await api(`/api/projects/${project.id}/initialize`, { method: "POST", body: "{}" });
    toast("项目已初始化");
    await loadProjects();
  } catch (error) {
    toast(error.message);
  }
}

async function removeProject() {
  const project = selectedProject();
  if (!project) return;
  const wtLabel = worktreeTypeLabel(project.worktreeType);
  let message = "只从工具中移除，不删除本地文件。";
  if (wtLabel) {
    message += "\n\n此操作只从工具中移除工作区记录，不会删除本地目录或Git分支。";
  }
  message += `\n\n项目：${project.name}\n路径：${project.path}`;
  if (wtLabel) message += `\n类型：${wtLabel}`;
  if (project.branch) message += `\n分支：${project.branch}`;
  message += "\n\n确认移除这个项目记录吗？";
  if (!window.confirm(message)) return;
  try {
    await api(`/api/projects/${project.id}`, { method: "DELETE" });
    state.selectedProjectId = null;
    state.selectedTaskId = null;
    localStorage.removeItem("selectedProjectId");
    localStorage.removeItem("selectedTaskId");
    await loadProjects();
    toast("项目记录已移除，本地文件未删除");
  } catch (error) {
    toast(error.message);
  }
}

async function savePlan() {
  const project = selectedProject();
  if (!project) return;
  try {
    await api(`/api/projects/${project.id}/plan`, {
      method: "PUT",
      body: JSON.stringify({ content: $("plan-editor").value }),
    });
    $("plan-state").textContent = "已保存";
    toast("PLAN 已保存");
  } catch (error) {
    toast(error.message);
  }
}

async function createTask(event) {
  event.preventDefault();
  const project = selectedProject();
  if (!project) return;
  try {
    const data = await api("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        projectId: project.id,
        title: $("task-title").value,
        description: $("task-description").value,
        acceptance: $("task-acceptance").value,
        testCommand: $("task-test-command").value,
        maxRounds: $("task-max-rounds-input").value || 3,
      }),
    });
    state.taskView = "active";
    state.selectedTaskId = data.task.id;
    localStorage.setItem("taskView", "active");
    localStorage.setItem("selectedTaskId", data.task.id);
    $("task-form").reset();
    $("task-max-rounds-input").value = "3";
    await loadTasks();
    toast("任务已创建");
  } catch (error) {
    toast(error.message);
  }
}

async function taskAction(endpoint, message) {
  const task = selectedTask();
  if (!task) return;
  try {
    const data = await api(`/api/tasks/${task.id}/${endpoint}`, { method: "POST", body: "{}" });
    state.selectedTaskId = data.task.id;
    await loadTasks();
    toast(message);
  } catch (error) {
    toast(error.message);
  }
}

async function archiveSelectedTask() {
  const task = selectedTask();
  if (!task) return;
  if (!window.confirm(`确认归档任务「${task.title}」吗？运行中的任务不能归档。`)) return;
  await taskAction("archive", "任务已归档");
}

async function restoreSelectedTask() {
  const task = selectedTask();
  if (!task) return;
  await taskAction("restore", "任务已恢复");
  state.taskView = "active";
  localStorage.setItem("taskView", "active");
  await loadTasks();
}

async function deleteSelectedTask() {
  const task = selectedTask();
  if (!task) return;
  const message = `删除任务记录会先把任务目录移动到本工具的 .gui/trash/tasks。\n不会永久删除用户代码，也不会删除项目目录。\n\n任务：${task.title}\nID：${task.id}\n\n确认继续吗？`;
  if (!window.confirm(message)) return;
  try {
    await api(`/api/tasks/${task.id}`, { method: "DELETE" });
    state.selectedTaskId = null;
    localStorage.removeItem("selectedTaskId");
    await loadTasks();
    toast("任务记录已移入工具回收站");
  } catch (error) {
    toast(error.message);
  }
}

async function restoreTrashTask() {
  const task = selectedTask();
  if (!task) return;
  try {
    const data = await api(`/api/trash/tasks/${task.id}/restore`, { method: "POST", body: "{}" });
    state.taskView = "active";
    state.selectedTaskId = data.task.id;
    localStorage.setItem("taskView", "active");
    localStorage.setItem("selectedTaskId", data.task.id);
    await loadTasks();
    toast("任务已从回收站恢复");
  } catch (error) {
    toast(error.message);
  }
}

function labelStatus(status) {
  return taskStatusLabels[status] || status || "-";
}

function statusClass(status) {
  if (status === "PASS") return "pass";
  if (["WAITING_FOR_CLAUDE", "CLAUDE_WINDOW_STARTED", "WAITING_FOR_CODEX", "CODEX_WINDOW_STARTED", "NEEDS_FIX"].includes(status)) return "running";
  if (status === "CANCELLED" || status === "BLOCKED") return "warn";
  if (status === "FAILED") return "fail";
  return "";
}

function kindLabel(kind) {
  if (kind === "orchestrator") return "协同项目";
  if (kind === "git-uninitialized") return "Git 仓库";
  return kind || "未知";
}

function worktreeTypeLabel(worktreeType) {
  if (worktreeType === "primary") return "主工作区";
  if (worktreeType === "worktree") return "Worktree";
  return null;
}

function worktreeTypeClass(worktreeType) {
  if (worktreeType === "primary") return "wt-primary";
  if (worktreeType === "worktree") return "wt-worktree";
  return "";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

document.addEventListener("DOMContentLoaded", async () => {
  $("add-project-form").addEventListener("submit", addProject);
  $("initialize-button").addEventListener("click", initializeProject);
  $("remove-project-button").addEventListener("click", removeProject);
  $("save-plan-button").addEventListener("click", savePlan);
  $("task-form").addEventListener("submit", createTask);
  $("launch-claude-button").addEventListener("click", () => taskAction("launch-claude", "Claude CLI 窗口已启动"));
  $("claude-completed-button").addEventListener("click", () => taskAction("claude-completed", "Claude 结果已收集"));
  $("launch-codex-button").addEventListener("click", () => taskAction("launch-codex", "Codex CLI 窗口已启动"));
  $("codex-completed-button").addEventListener("click", () => taskAction("codex-completed", "Codex 审查已处理"));
  $("cancel-task-button").addEventListener("click", () => taskAction("cancel", "任务已取消"));
  $("archive-task-button").addEventListener("click", archiveSelectedTask);
  $("restore-task-button").addEventListener("click", restoreSelectedTask);
  $("delete-task-button").addEventListener("click", deleteSelectedTask);
  $("restore-trash-task-button").addEventListener("click", restoreTrashTask);
  $("active-tasks-tab").addEventListener("click", () => setTaskView("active"));
  $("archived-tasks-tab").addEventListener("click", () => setTaskView("archived"));
  $("trash-tasks-tab").addEventListener("click", () => setTaskView("trash"));
  renderTaskDetails();
  renderArtifacts();
  await loadProjects();
});
