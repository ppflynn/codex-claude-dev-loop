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
  CLAUDE_WINDOW_STARTED: "Claude 窗口已启动",
  WAITING_FOR_CODEX: "等待 Codex",
  CODEX_WINDOW_STARTED: "Codex 窗口已启动",
  NEEDS_FIX: "需要修复",
  PASS: "通过",
  BLOCKED: "阻塞",
  FAILED: "失败",
  CANCELLED: "已取消",
};

const terminalStatuses = new Set(["PASS", "BLOCKED", "FAILED", "CANCELLED"]);
const runningTaskStatuses = new Set(["CLAUDE_WINDOW_STARTED", "CODEX_WINDOW_STARTED"]);

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
    button.className = `project-item ${project.id === state.selectedProjectId ? "active" : ""}`;
    button.onclick = () => selectProject(project.id);

    const title = document.createElement("div");
    title.className = "project-title";
    title.innerHTML = `<span>${escapeHtml(project.name)}</span><span class="kind-pill">${kindLabel(project.kind)}</span>`;

    const path = document.createElement("div");
    path.className = "project-path";
    path.textContent = project.path;

    const meta = document.createElement("div");
    meta.className = "project-meta";
    meta.textContent = project.lastResult ? `上次：${project.lastResult}` : "可创建本地任务";

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
  $("project-path-label").textContent = project ? `${project.path} / ${kindLabel(project.kind)}` : "先导入或选择一个项目。";
  $("plan-editor").disabled = !project;
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
      button.className = `project-item ${task.id === state.selectedTaskId ? "active" : ""}`;
      button.onclick = () => selectTask(task.id);

      const title = document.createElement("div");
      title.className = "project-title";
      title.innerHTML = `<span>${escapeHtml(task.title)}</span><span class="kind-pill">${taskBadge(task)}</span>`;

      const path = document.createElement("div");
      path.className = "project-path";
      path.textContent = task.id;

      const meta = document.createElement("div");
      meta.className = "project-meta";
      meta.textContent = taskMeta(task);

      button.append(title, path, meta);
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
  return `轮次 ${task.round}/${task.maxRounds}`;
}

async function selectTask(id) {
  state.selectedTaskId = id;
  localStorage.setItem("selectedTaskId", id);
  renderTasks();
  await loadArtifacts();
}

function renderTaskDetails() {
  const task = selectedTask();
  const status = task?.status || "IDLE";
  $("task-state").textContent = task ? labelStatus(status) : "无任务";
  $("task-state").className = `status-pill ${statusClass(status)}`;
  $("task-title-label").textContent = task?.title || "-";
  $("task-id-label").textContent = task?.id || "-";
  $("task-round").textContent = task ? `${task.round}` : "-";
  $("task-max-rounds").textContent = task ? `${task.maxRounds}` : "-";
  $("task-created").textContent = task?.createdAt || "-";

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

  $("initialize-button").disabled = !project || project.kind !== "git-uninitialized";
  $("save-plan-button").disabled = !project;
  $("remove-project-button").disabled = !project;
  $("create-task-button").disabled = !project || !isActiveView;
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
  const message = `只从工具中移除，不删除本地文件。\n\n项目：${project.name}\n路径：${project.path}\n\n确认移除这个项目记录吗？`;
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
