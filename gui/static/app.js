const state = {
  projects: [],
  selectedProjectId: localStorage.getItem("selectedProjectId") || null,
  tasks: [],
  selectedTaskId: localStorage.getItem("selectedTaskId") || null,
  taskView: localStorage.getItem("taskView") || "active",
  artifacts: {},
  activeArtifact: null,
  taskActionPending: false,
  taskActionPendingMessage: "",
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
  IDLE: "无任务",
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
  committed: "已提交",
  merged: "已合并主干",
};

const terminalStatuses = new Set(["PASS", "BLOCKED", "FAILED", "CANCELLED"]);
const runningTaskStatuses = new Set(["CLAUDE_WINDOW_STARTED", "CODEX_WINDOW_STARTED"]);

const terminalConnections = {
  claude: { source: null, done: false, subKey: null, reconnectTimer: null, reconnectDelay: 1000, finished: false, exitCode: null, lastLogUpdateAt: null, promptedTaskId: null },
  codex: { source: null, done: false, subKey: null, reconnectTimer: null, reconnectDelay: 1000, finished: false, exitCode: null, lastLogUpdateAt: null, promptedTaskId: null },
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
  claude: { term: null, fitAddon: null, observer: null, hasOutput: false, lineBuffer: "", phase: "" },
  codex: { term: null, fitAddon: null, observer: null, hasOutput: false, lineBuffer: "", phase: "" },
};

const phaseLabels = {
  planning: "计划中",
  reading: "读取中",
  running: "运行中",
  editing: "修改中",
  testing: "验证中",
  reviewing: "审查中",
  writing: "写入中",
  waiting: "等待中",
  blocked: "已阻塞",
  done: "完成",
};

const phaseBadgeClasses = {
  planning: "terminal-phase-badge planning",
  reading: "terminal-phase-badge reading",
  running: "terminal-phase-badge running",
  editing: "terminal-phase-badge editing",
  testing: "terminal-phase-badge testing",
  reviewing: "terminal-phase-badge reviewing",
  writing: "terminal-phase-badge writing",
  waiting: "terminal-phase-badge waiting",
  blocked: "terminal-phase-badge blocked",
  done: "terminal-phase-badge done",
};

// Matches a single complete `::task-status{phase="..." message="..."}` line.
// The message capture uses an escaped-string pattern so `\"` and `\\` inside
// the message do not terminate the capture prematurely.
const TASK_STATUS_RE = /^::task-status\{\s*phase="([^"]*)"(?:\s+message="((?:\\.|[^"\\])*)")?\s*\}\s*$/;

// Status-event lines must begin with this prefix on their own line. We only
// buffer partial chunks that could grow into a status event; everything else
// streams straight to xterm so progress bars / prompts / carriage-return
// updates render in real time.
const STATUS_EVENT_PREFIX = "::task-status";

let taskRefreshTimer = null;
let loadGeneration = 0;
let refreshInFlight = false;

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

  terminalInstances[client] = { term, fitAddon, observer, hasOutput: false, lineBuffer: "", phase: "" };
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
  inst.lineBuffer = "";
  inst.phase = "";
  clearTerminalPhaseBadge(client);
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

function unescapeStatusMessage(raw) {
  // The protocol allows \\" and \\\\ inside the message. Decode them back.
  return String(raw || "")
    .replace(/\r/g, "")
    .replace(/\\(.)/g, (_, ch) => ch);
}

function renderStatusLine(client, phase, message) {
  const term = terminalInstances[client].term;
  if (!term) return;
  const label = phaseLabels[phase] || phase || "状态";
  // Strip any ANSI escapes from the message so user-supplied text cannot
  // reformat the terminal unexpectedly.
  const safeMessage = String(message || "").replace(/\x1b\[[0-9;]*[A-Za-z]/g, "");
  const line = safeMessage
    ? `\x1b[36m\x1b[1m[${label}]\x1b[0m \x1b[37m${safeMessage}\x1b[0m`
    : `\x1b[36m\x1b[1m[${label}]\x1b[0m`;
  term.write(line + "\r\n");
}

function updateTerminalPhaseBadge(client, phase) {
  const box = document.getElementById(`${client}-terminal-box`);
  if (!box) return;
  const title = box.querySelector(".terminal-title");
  if (!title) return;

  let badge = title.querySelector(".terminal-phase-badge");
  if (!badge) {
    badge = document.createElement("span");
    title.appendChild(badge);
  }
  const label = phaseLabels[phase] || phase || "";
  badge.textContent = label;
  badge.className = phaseBadgeClasses[phase] || "terminal-phase-badge";
  // The base CSS rule sets `display: none`. Override it explicitly here so
  // the badge becomes visible; the empty-string fallback would inherit the
  // hidden base state and the badge would never appear.
  badge.style.display = label ? "inline-flex" : "none";
}

function clearTerminalPhaseBadge(client) {
  const box = document.getElementById(`${client}-terminal-box`);
  if (!box) return;
  const title = box.querySelector(".terminal-title");
  if (!title) return;
  const badge = title.querySelector(".terminal-phase-badge");
  if (badge) {
    badge.textContent = "";
    badge.className = "terminal-phase-badge";
    badge.style.display = "none";
  }
}

function couldBeStatusEventPrefix(line) {
  // True when `line` could still grow into a `::task-status{...}` event:
  // either it is a prefix of the sentinel ("::", "::ta", "::task-status" ...)
  // or it already starts with the sentinel and may continue into `{...}`.
  if (!line) return false;
  return STATUS_EVENT_PREFIX.startsWith(line) || line.startsWith(STATUS_EVENT_PREFIX);
}

function processTerminalChunk(client, text) {
  const inst = terminalInstances[client];
  const term = inst && inst.term;
  if (!term || typeof text !== "string" || text.length === 0) return;

  // Combine any buffered partial status-event line with the new text. The
  // buffer only ever holds a line that could grow into a status event, so
  // ordinary CLI output is never delayed here.
  const combined = inst.lineBuffer + text;
  inst.lineBuffer = "";

  // Split on \n. The last element is the trailing partial line (no \n).
  const lines = combined.split("\n");
  const trailing = lines.pop();

  for (let i = 0; i < lines.length; i++) {
    const rawLine = lines[i];
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    const m = TASK_STATUS_RE.exec(line);
    if (m) {
      const phase = m[1] || "";
      const message = unescapeStatusMessage(m[2] || "");
      renderStatusLine(client, phase, message);
      if (phase) {
        inst.phase = phase;
        updateTerminalPhaseBadge(client, phase);
      }
    } else {
      // Preserve embedded \r (carriage-return updates) by writing the raw
      // line; xterm with convertEol handles the trailing newline.
      term.write(rawLine + "\r\n");
    }
  }

  // Trailing partial line: only buffer if it could grow into a status event.
  // Otherwise stream it to xterm immediately so non-newline output such as
  // prompts, progress bars, and token-style updates render in real time.
  if (trailing) {
    if (couldBeStatusEventPrefix(trailing)) {
      inst.lineBuffer = trailing;
    } else {
      term.write(trailing);
    }
  }
}

function flushTerminalBuffer(client) {
  const inst = terminalInstances[client];
  if (!inst || !inst.lineBuffer) return;
  const pending = inst.lineBuffer;
  inst.lineBuffer = "";
  const term = inst.term;
  if (!term) return;
  const trimmed = pending.endsWith("\r") ? pending.slice(0, -1) : pending;
  const m = TASK_STATUS_RE.exec(trimmed);
  if (m) {
    const phase = m[1] || "";
    const message = unescapeStatusMessage(m[2] || "");
    renderStatusLine(client, phase, message);
    if (phase) {
      inst.phase = phase;
      updateTerminalPhaseBadge(client, phase);
    }
  } else if (pending.length > 0) {
    term.write(pending);
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
    // Stale-stream guard: if this EventSource was already replaced (reconnect,
    // task switch, etc.), discard its queued message before it can reach the
    // chunk processor — otherwise it would resolve `terminalInstances[client]`
    // at call time and write into the new active terminal, duplicating or
    // reordering output and updating the new terminal's phase badge.
    if (terminalConnections[client].source !== es) {
      es.close();
      return;
    }
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
        processTerminalChunk(client, data.chunk);
      }
      if (data.done) {
        if (terminalConnections[client].source === es) {
          flushTerminalBuffer(client);
          terminalConnections[client].done = true;
          if (data.exitCode != null) {
            terminalConnections[client].finished = true;
            terminalConnections[client].exitCode = data.exitCode;
          }
          es.close();
          terminalConnections[client].source = null;
        }
        updateTerminalBadges();
        if (data.exitCode != null) {
          showCompletionPrompt(client, taskId);
        }
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
  conn.finished = false;
  conn.exitCode = null;
  conn.lastLogUpdateAt = null;
  conn.promptedTaskId = null;
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
    clearClientTitleBadge("claude");
    clearClientTitleBadge("codex");
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

function clearClientTitleBadge(client) {
  const box = document.getElementById(`${client}-terminal-box`);
  if (!box) return;
  const title = box.querySelector(".terminal-title");
  if (!title) return;
  const badge = title.querySelector(".terminal-client-badge");
  if (badge) {
    badge.textContent = "待启动";
    badge.className = "terminal-client-badge";
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
  const conn = terminalConnections[client];

  if (isConnected) {
    badge.textContent = "实时";
    badge.className = "terminal-client-badge active";
  } else if (conn.finished) {
    if (conn.exitCode === 0 || conn.exitCode === "0") {
      badge.textContent = "已退出";
      badge.className = "terminal-client-badge";
    } else if (conn.exitCode != null) {
      badge.textContent = "退出码 " + conn.exitCode;
      badge.className = "terminal-client-badge warn";
    } else {
      badge.textContent = "已退出";
      badge.className = "terminal-client-badge";
    }
  } else if (isActive) {
    badge.textContent = "运行中";
    badge.className = "terminal-client-badge active";
  } else {
    badge.textContent = "待启动";
    badge.className = "terminal-client-badge";
  }
}

function showCompletionPrompt(client, taskId) {
  const task = selectedTask();
  const conn = terminalConnections[client];

  // Guard: return early if this call is for a different task — must precede
  // any DOM mutations so a stale call cannot clear the current task's pulse.
  if (!task || task.id !== taskId) return;
  if (!conn.finished) return;

  const buttonId = client === "claude" ? "claude-completed-button" : "codex-completed-button";
  const button = document.getElementById(buttonId);

  // Clear pulse if this is a different task than the one last prompted
  if (button && conn.promptedTaskId && conn.promptedTaskId !== taskId) {
    button.classList.remove("attention-pulse");
    conn.promptedTaskId = null;
  }

  const isActiveForClient =
    (client === "claude" && task.activeClient === "claude" && task.status === "CLAUDE_WINDOW_STARTED") ||
    (client === "codex" && task.activeClient === "codex" && task.status === "CODEX_WINDOW_STARTED");

  if (!isActiveForClient) return;
  if (!button) return;

  const clientName = clientLabels[client] || client;
  const exitInfo = conn.exitCode != null ? " (退出码 " + conn.exitCode + ")" : "";
  button.classList.add("attention-pulse");
  conn.promptedTaskId = taskId;
  toast(clientName + " CLI 已退出" + exitInfo + "，请点击 \"" + clientName + " 已完成\" 推进任务");
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
      updateTerminalBadges();
      return;
    }
    const artifacts = await api(`/api/tasks/${taskId}/artifacts`);
    const current2 = selectedTask();
    if (!current2 || current2.id !== taskId || current2.round !== taskRound) return;

    const conn = terminalConnections[client];
    conn.finished = meta.finished || false;
    conn.exitCode = meta.exitCode ?? null;
    conn.lastLogUpdateAt = meta.lastLogUpdateAt ?? null;

    const logName = meta.logName;
    if (artifacts.artifacts && artifacts.artifacts[logName] && artifacts.artifacts[logName].exists) {
      processTerminalChunk(client, artifacts.artifacts[logName].content);
      flushTerminalBuffer(client);
    }
    terminalInstances[client].hasOutput = true;
    updateTerminalBadges();
    showCompletionPrompt(client, taskId);
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
  // Clear stale completion prompts from the previous task
  $("claude-completed-button").classList.remove("attention-pulse");
  $("codex-completed-button").classList.remove("attention-pulse");
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

  const groups = groupProjectsByRepo(state.projects);
  groups.forEach((group) => {
    const container = document.createElement("div");
    container.className = "repo-group";
    if (group.repoId) container.dataset.repoId = group.repoId;

    if (group.repoId && (group.primary || group.worktrees.length)) {
      const header = document.createElement("div");
      header.className = "repo-group-header";
      const label = document.createElement("span");
      label.className = "repo-group-title";
      const primary = group.primary;
      const repoName = primary ? primary.name : (group.worktrees[0]?.name || "Git 仓库");
      label.textContent = `📦 ${repoName}`;
      header.append(label);
      if (primary) {
        const meta = document.createElement("span");
        meta.className = "repo-group-meta";
        const parts = [];
        if (primary.branch) parts.push(`主干分支：${primary.branch}`);
        parts.push(`${group.worktrees.length + (primary ? 1 : 0)} 个工作区`);
        meta.textContent = parts.join(" · ");
        header.append(meta);
      }
      container.append(header);
    }

    if (group.primary) {
      container.append(renderProjectButton(group.primary, true));
    } else if (group.solo.length === 0 && group.worktrees.length) {
      // No primary registered; show a hint banner.
      const hint = document.createElement("div");
      hint.className = "repo-group-hint";
      hint.textContent = "未找到主工作区记录，仅显示已登记的 worktree。";
      container.append(hint);
    }
    group.worktrees.forEach((wt) => container.append(renderProjectButton(wt, false)));
    group.solo.forEach((p) => container.append(renderProjectButton(p, false)));

    list.append(container);
  });
}

function groupProjectsByRepo(projects) {
  const order = [];
  const buckets = new Map();
  projects.forEach((project) => {
    const key = project.repoId || `solo-${project.id}`;
    if (!buckets.has(key)) {
      buckets.set(key, {
        repoId: project.repoId || null,
        primary: null,
        worktrees: [],
        solo: [],
      });
      order.push(key);
    }
    const bucket = buckets.get(key);
    if (!project.repoId) {
      bucket.solo.push(project);
    } else if (project.worktreeType === "primary") {
      if (!bucket.primary) {
        bucket.primary = project;
      } else {
        bucket.worktrees.push(project);
      }
    } else if (project.worktreeType === "worktree") {
      bucket.worktrees.push(project);
    } else {
      bucket.solo.push(project);
    }
  });
  return order.map((key) => buckets.get(key));
}

function renderProjectButton(project, isPrimary) {
  const button = document.createElement("button");
  button.type = "button";
  const isAvailable = project.available !== false;
  const cls = ["project-item"];
  if (isPrimary) cls.push("project-primary");
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
  return button;
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
    if (project.repoId) parts.push(`仓库：${project.repoId}`);
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
  updateProjectHeaderActions();
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

async function loadTasks(skipArtifacts = false) {
  const project = selectedProject();
  if (!project) return;

  const gen = ++loadGeneration;
  const capturedProjectId = project.id;
  const capturedView = state.taskView;

  const data = await api(taskEndpointForView());

  // Stale-response guard: if generation, project, or view changed during the request, discard
  if (gen !== loadGeneration) return;
  const currentProject = selectedProject();
  if (!currentProject || currentProject.id !== capturedProjectId) return;
  if (state.taskView !== capturedView) return;

  const prevIds = state.tasks.map(t => t.id + "|" + t.status + "|" + t.activeClient + "|" + t.progress).join(",");
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
  const currIds = state.tasks.map(t => t.id + "|" + t.status + "|" + t.activeClient + "|" + t.progress).join(",");
  const tasksChanged = prevIds !== currIds;
  renderTasks();
  if (!skipArtifacts || tasksChanged) await loadArtifacts(gen, capturedProjectId, capturedView, state.selectedTaskId);
  // Recheck after artifact loading — loadArtifacts has its own stale guard, but
  // we still need to verify before calling refreshTerminalsForTask / manageAutoRefresh
  if (gen !== loadGeneration) return;
  const currentProject2 = selectedProject();
  if (!currentProject2 || currentProject2.id !== capturedProjectId) return;
  if (state.taskView !== capturedView) return;
  refreshTerminalsForTask();
  manageAutoRefresh();
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
  stopAutoRefresh();
  state.selectedTaskId = id;
  localStorage.setItem("selectedTaskId", id);
  renderTasks();
  const gen = ++loadGeneration;
  const project = selectedProject();
  await loadArtifacts(gen, project?.id, state.taskView, id);
  if (gen !== loadGeneration) return;
  refreshTerminalsForTask();
  manageAutoRefresh();
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

  const stageLabel = state.taskActionPendingMessage || labelStage(task?.stage);
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

  $("task-repo-label").textContent = task?.repoId || "-";
  const wtParts = [];
  if (task?.worktreeType) wtParts.push(worktreeTypeLabel(task.worktreeType) || task.worktreeType);
  if (task?.worktreeBranch) wtParts.push(`分支：${task.worktreeBranch}`);
  $("task-worktree-label").textContent = wtParts.length ? wtParts.join(" · ") : "-";

  const commitParts = [];
  if (task?.commitSha) {
    const sha = task.commitShortSha || task.commitSha.slice(0, 10);
    commitParts.push(sha);
    if (task.committedAt) commitParts.push(formatTime(task.committedAt));
  }
  if (task?.commitMessage) commitParts.push(`· ${task.commitMessage}`);
  $("task-commit-label").textContent = commitParts.length ? commitParts.join(" ") : "-";

  const mergeParts = [];
  if (task?.mergeCommitSha) {
    const sha = task.mergeShortSha || task.mergeCommitSha.slice(0, 10);
    mergeParts.push(`${task.mergeSourceBranch || "?"} → ${task.mergeTargetBranch || "?"} @ ${sha}`);
    if (task.mergedAt) mergeParts.push(formatTime(task.mergedAt));
  }
  $("task-merge-label").textContent = mergeParts.length ? mergeParts.join(" ") : "-";

  const history = $("task-history");
  history.textContent = task?.history?.length
    ? task.history.map((item) => `[${item.at}] ${item.event}: ${item.message}`).join("\n")
    : "暂无任务历史。";
  updateActionStates();
}

async function loadArtifacts(gen, capturedProjectId, capturedView, capturedTaskId) {
  const task = selectedTask();
  if (!task || state.taskView === "trash") {
    state.artifacts = {};
    state.activeArtifact = null;
    renderArtifacts();
    return;
  }
  try {
    const data = await api(`/api/tasks/${task.id}/artifacts`);
    // Stale-response guard: if gen, project, view, or task changed during fetch, discard
    if (gen !== undefined) {
      if (gen !== loadGeneration) return;
      const currentProject = selectedProject();
      if (!currentProject || currentProject.id !== capturedProjectId) return;
      if (state.taskView !== capturedView) return;
      if (state.selectedTaskId !== capturedTaskId) return;
    }
    state.artifacts = data.artifacts || {};
    const keys = Object.keys(state.artifacts);
    if (!state.activeArtifact || !state.artifacts[state.activeArtifact]) {
      state.activeArtifact = keys[0] || null;
    }
    renderArtifacts();
  } catch (error) {
    // Stale check in error path too
    if (gen !== undefined) {
      if (gen !== loadGeneration) return;
      const currentProject = selectedProject();
      if (!currentProject || currentProject.id !== capturedProjectId) return;
      if (state.taskView !== capturedView) return;
      if (state.selectedTaskId !== capturedTaskId) return;
    }
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

function updateProjectHeaderActions() {
  const project = selectedProject();
  const worktreeForm = $("add-worktree-form");
  if (!worktreeForm) return;
  const canCreateWorktree = project
    && project.worktreeType === "primary"
    && project.available !== false;
  worktreeForm.hidden = !canCreateWorktree;
  if (canCreateWorktree) {
    $("worktree-branch").placeholder = `分支名，例如 feature/${(project.name || "task").toLowerCase().replace(/[^a-z0-9._-]/g, "-").slice(0, 16)}-1`;
  }
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
  $("launch-claude-button").disabled = state.taskActionPending || !task || !isActiveView || status !== "WAITING_FOR_CLAUDE";
  $("claude-completed-button").disabled = state.taskActionPending || !task || !isActiveView || status !== "CLAUDE_WINDOW_STARTED";
  $("launch-codex-button").disabled = state.taskActionPending || !task || !isActiveView || status !== "WAITING_FOR_CODEX";
  $("codex-completed-button").disabled = state.taskActionPending || !task || !isActiveView || status !== "CODEX_WINDOW_STARTED";

  // Git lifecycle: commit available for PASS tasks (active view only) that
  // have not been committed yet; merge available for committed tasks that
  // have not been merged yet.
  const canCommit = !!task
    && !state.taskActionPending
    && isActiveView
    && status === "PASS"
    && !task.commitSha;
  const canMerge = !!task
    && !state.taskActionPending
    && isActiveView
    && !!task.commitSha
    && !task.mergedAt;
  $("commit-task-button").disabled = !canCommit;
  $("merge-task-button").disabled = !canMerge;

  // Clear completion prompts when buttons become disabled
  if ($("claude-completed-button").disabled) $("claude-completed-button").classList.remove("attention-pulse");
  if ($("codex-completed-button").disabled) $("codex-completed-button").classList.remove("attention-pulse");
  $("cancel-task-button").disabled = state.taskActionPending || !task || !isActiveView || terminalStatuses.has(status);
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
  refreshInFlight = false;
  ++loadGeneration;
  await loadTasks();
}

function manageAutoRefresh() {
  if (state.taskView !== "active") {
    stopAutoRefresh();
    return;
  }
  const task = selectedTask();
  const hasRunning = state.tasks.some(t => runningTaskStatuses.has(t.status));
  if (hasRunning || (task && runningTaskStatuses.has(task.status))) {
    startAutoRefresh();
  } else {
    stopAutoRefresh();
  }
}

function startAutoRefresh() {
  if (taskRefreshTimer) return;
  taskRefreshTimer = setInterval(() => {
    if (state.taskView !== "active") { stopAutoRefresh(); return; }
    if (refreshInFlight) return;
    refreshInFlight = true;
    loadTasks(true).finally(() => { refreshInFlight = false; });
  }, 4000);
}

function stopAutoRefresh() {
  if (taskRefreshTimer) {
    clearInterval(taskRefreshTimer);
    taskRefreshTimer = null;
  }
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
  if (!task || state.taskActionPending) return;
  state.taskActionPending = true;
  state.taskActionPendingMessage = {
    "claude-completed": "正在收集改动并运行测试，请稍候...",
    "codex-completed": "正在处理审查结果，请稍候...",
    "launch-claude": "正在启动 Claude...",
    "launch-codex": "正在启动 Codex...",
  }[endpoint] || "正在处理，请稍候...";
  updateActionStates();
  $("task-stage-label").textContent = state.taskActionPendingMessage;
  toast(state.taskActionPendingMessage);
  try {
    const data = await api(`/api/tasks/${task.id}/${endpoint}`, { method: "POST", body: "{}" });
    state.selectedTaskId = data.task.id;
    await loadTasks();
    toast(message);
  } catch (error) {
    toast(error.message);
  } finally {
    state.taskActionPending = false;
    state.taskActionPendingMessage = "";
    renderTaskDetails();
    updateActionStates();
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

// server.py emits task.stage = f"fix_round_{next_round}" when Codex returns NEEDS_FIX,
// so resolve those dynamically instead of showing the raw key.
function labelStage(stage) {
  if (!stage) return "-";
  if (stageLabels[stage]) return stageLabels[stage];
  const m = /^fix_round_(\d+)$/.exec(stage);
  if (m) return `第 ${m[1]} 轮修复`;
  return stage;
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

let inspectorOverlayBackdrop = null;

function toggleInspector() {
  const inspector = document.querySelector(".inspector");
  if (!inspector) return;

  if (inspector.classList.contains("overlay")) {
    inspector.classList.remove("overlay");
    if (inspectorOverlayBackdrop) {
      inspectorOverlayBackdrop.remove();
      inspectorOverlayBackdrop = null;
    }
  } else {
    inspector.classList.add("overlay");
    const backdrop = document.createElement("div");
    backdrop.className = "overlay-backdrop";
    backdrop.addEventListener("click", toggleInspector);
    document.body.appendChild(backdrop);
    inspectorOverlayBackdrop = backdrop;
  }
}

function switchInspectorTab(tab) {
  document.querySelectorAll(".inspector-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  document.querySelectorAll(".inspector-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `inspector-${tab}`);
  });
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
  $("add-worktree-form").addEventListener("submit", addWorktreeFromForm);
  $("initialize-button").addEventListener("click", initializeProject);
  $("remove-project-button").addEventListener("click", removeProject);
  $("toggle-inspector-button").addEventListener("click", toggleInspector);
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
  $("commit-task-button").addEventListener("click", openCommitForm);
  $("commit-confirm-button").addEventListener("click", confirmCommitTask);
  $("commit-cancel-button").addEventListener("click", closeCommitForm);
  $("merge-task-button").addEventListener("click", mergeSelectedTask);
  $("active-tasks-tab").addEventListener("click", () => setTaskView("active"));
  $("archived-tasks-tab").addEventListener("click", () => setTaskView("archived"));
  $("trash-tasks-tab").addEventListener("click", () => setTaskView("trash"));
  document.querySelectorAll(".inspector-tab").forEach((btn) => {
    btn.addEventListener("click", () => switchInspectorTab(btn.dataset.tab));
  });
  renderTaskDetails();
  renderArtifacts();
  await loadProjects();
});

async function addWorktreeFromForm(event) {
  event.preventDefault();
  const project = selectedProject();
  if (!project) {
    toast("请先选择一个主工作区项目");
    return;
  }
  if (project.worktreeType !== "primary") {
    toast("只能从主工作区创建 worktree");
    return;
  }
  const branch = $("worktree-branch").value.trim();
  const targetPath = $("worktree-path").value.trim();
  if (!branch || !targetPath) {
    toast("分支名和目标路径都必填");
    return;
  }
  try {
    const data = await api(`/api/projects/${project.id}/worktrees`, {
      method: "POST",
      body: JSON.stringify({ branch, path: targetPath }),
    });
    $("add-worktree-form").reset();
    await loadProjects();
    // Codex P2-1 round 18: branch on ``registeredAutomatically`` so the
    // partial-success case (worktree created but project registration
    // failed) is surfaced explicitly rather than presented as a normal
    // registration.  The previous code always reported success and then
    // attempted ``selectProject(undefined)``, silently masking the
    // orphan worktree state and leaving the user with no path to
    // re-import it.
    if (data && data.registeredAutomatically === false) {
      // Partial success: the worktree directory exists on disk but the
      // backend could not register it as a project.  Do NOT navigate
      // (there is no project to select); surface the recovery
      // instructions verbatim so the user can act on them.
      //
      // Codex P2-2 round 19: read ``data.path`` first (the top-level
      // path field the backend now always emits in this branch) and
      // fall back to ``data.project.path`` only for compatibility
      // with older backends.  Do NOT call ``selectProject``: there is
      // no project to select (``data.project`` is ``null``), so any
      // attempt would either silently no-op or select an unrelated
      // project whose id we never received.
      const recovery = String(data.recoveryInstructions || "").trim();
      const createdPath = (data.path && String(data.path).trim())
        || (data.project && data.project.path ? String(data.project.path) : "")
        || "(路径不可用)";
      const branchLabel = data.branch || branch;
      // Use ``window.alert`` rather than ``toast`` because the message
      // is long and must be reproducible: the user needs to copy the
      // orphan worktree path and follow the recovery instructions.
      window.alert(
        [
          "Worktree 已创建但未能自动注册为项目。",
          `分支：${branchLabel}`,
          `路径：${createdPath}`,
          "",
          "后续步骤：",
          recovery || "打开项目列表并手动添加该路径以导入它。",
        ].join("\n"),
      );
      // Also emit a transient toast so the user gets immediate visual
      // feedback even if they dismiss the alert quickly.
      toast(`Worktree 已创建（注册失败）：${branchLabel} → ${createdPath}`);
      return;
    }
    if (data?.project?.id) {
      await selectProject(data.project.id);
    }
    toast(`已创建 worktree：${data.branch}`);
  } catch (error) {
    toast(error.message);
  }
}

function openCommitForm() {
  const task = selectedTask();
  if (!task) return;
  if (task.status !== "PASS" || task.commitSha) {
    toast("只有 PASS 且未提交的任务才能提交");
    return;
  }
  const form = $("commit-form");
  form.hidden = false;
  const messageInput = $("commit-message");
  const defaultName = (task.title || "task").trim().replace(/\s+/g, " ").slice(0, 60);
  messageInput.value = messageInput.value || defaultName;
  messageInput.focus();
  messageInput.select();
}

function closeCommitForm() {
  $("commit-form").hidden = true;
}

async function confirmCommitTask() {
  const task = selectedTask();
  if (!task) return;
  const message = $("commit-message").value.trim();
  if (!message) {
    toast("提交信息不能为空");
    return;
  }
  try {
    await api(`/api/tasks/${task.id}/commit`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    closeCommitForm();
    await loadTasks();
    toast("任务改动已提交（未推送）");
  } catch (error) {
    toast(error.message);
  }
}

async function mergeSelectedTask() {
  const task = selectedTask();
  if (!task) return;
  if (!task.commitSha || task.mergedAt) {
    toast("只有已提交且未合并的任务才能合并");
    return;
  }
  const sourceBranch = task.worktreeBranch || "(未知分支)";
  const confirmMessage = [
    `确认把分支 '${sourceBranch}' 合并回主干吗？`,
    "",
    "安全边界：",
    "· 通过 git merge-tree 计算结果、git commit-tree 生成提交、CAS update-ref 推进 HEAD，再 guarded read-tree 物化；恢复日志持续到任务元数据和审计落盘，全程不推送、不删除 worktree 或分支。",
    "· 主干 dirty、分支缺失、产生冲突、缺少当前轮次的 reviewed base、命中自定义合并驱动 / smudge 过滤时，合并会被拒绝。",
    "· 重启或下次相关操作会核对真实 ref、index、worktree 和不可变操作身份；仅在完全匹配时完成或反向 CAS，发现 drift、用户编辑或探测失败时保留日志并要求人工对账。",
  ].join("\n");
  if (!window.confirm(confirmMessage)) return;
  try {
    await api(`/api/tasks/${task.id}/merge`, { method: "POST", body: "{}" });
    await loadTasks();
    toast("分支已合并到主干（未推送）");
  } catch (error) {
    toast(error.message);
  }
}
