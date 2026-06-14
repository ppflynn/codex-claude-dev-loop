import * as vscode from 'vscode';
import * as path from 'path';
import { fetchTasks, resolveProject, ApiError } from './apiClient';
import type { Task, Project } from './types';
import { STATUS_ICONS, STATUS_LABELS } from './types';

class TaskTreeItem extends vscode.TreeItem {
  constructor(
    public readonly task: Task,
    public readonly project: Project,
    collapsibleState: vscode.TreeItemCollapsibleState
  ) {
    super(task.title, collapsibleState);

    const parts: string[] = [];
    parts.push(`R ${task.round}/${task.maxRounds}`);
    parts.push(STATUS_LABELS[task.status] || task.status);
    if (task.activeClient) {
      parts.push(task.activeClient);
    }
    this.description = parts.join('  ');

    const tooltipLines: string[] = [
      `Title: ${task.title}`,
      `Status: ${STATUS_LABELS[task.status] || task.status}`,
      `Round: ${task.round}/${task.maxRounds}`,
      `Progress: ${task.progress != null ? task.progress + '%' : 'N/A'}`,
      `Stage: ${task.stage || 'N/A'}`,
      `Project: ${project.name}`,
    ];
    if (task.activeClient) {
      tooltipLines.push(`Running: ${task.activeClient}`);
    }
    if (task.lastActivityAt) {
      tooltipLines.push(`Updated: ${task.lastActivityAt}`);
    }
    this.tooltip = tooltipLines.join('\n');
    this.contextValue = 'taskItem';
    this.resourceUri = vscode.Uri.file(task.projectPath);

    const isRunning = task.status === 'CLAUDE_WINDOW_STARTED' || task.status === 'CODEX_WINDOW_STARTED';
    const icon = isRunning ? 'sync~spin' : (STATUS_ICONS[task.status] || 'circle-outline');
    this.iconPath = new vscode.ThemeIcon(icon);
  }
}

class ProjectTreeItem extends vscode.TreeItem {
  constructor(
    public readonly project: Project,
    public readonly tasks: Task[]
  ) {
    super(project.name, vscode.TreeItemCollapsibleState.Expanded);
    this.description = `${tasks.length} task${tasks.length !== 1 ? 's' : ''}`;
    this.contextValue = 'projectItem';
    this.iconPath = new vscode.ThemeIcon('folder');
  }
}

class ErrorTreeItem extends vscode.TreeItem {
  constructor(message: string) {
    super(message, vscode.TreeItemCollapsibleState.None);
    this.iconPath = new vscode.ThemeIcon('error');
    this.contextValue = 'errorItem';
  }
}

class EmptyTreeItem extends vscode.TreeItem {
  constructor() {
    super('No tasks found. Click + to create a task.', vscode.TreeItemCollapsibleState.None);
    this.iconPath = new vscode.ThemeIcon('info');
    this.contextValue = 'emptyItem';
  }
}

export class TaskTreeProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private _onDidChangeTreeData = new vscode.EventEmitter<vscode.TreeItem | undefined | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private projects: Project[] = [];
  private tasksByProject: Map<string, Task[]> = new Map();
  private connectionError: string | null = null;

  refresh(): void {
    this._onDidChangeTreeData.fire();
  }

  async getChildren(element?: vscode.TreeItem): Promise<vscode.TreeItem[]> {
    if (element) {
      return [];
    }

    return this.buildRootItems();
  }

  getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  private async buildRootItems(): Promise<vscode.TreeItem[]> {
    this.connectionError = null;
    this.projects = [];
    this.tasksByProject.clear();

    if (!vscode.workspace.workspaceFolders || vscode.workspace.workspaceFolders.length === 0) {
      return [new EmptyTreeItem()];
    }

    const items: vscode.TreeItem[] = [];

    for (const folder of vscode.workspace.workspaceFolders) {
      try {
        const project = await resolveProject(folder.uri.fsPath);
        this.projects.push(project);

        const tasks = await fetchTasks(project.id);
        this.tasksByProject.set(project.id, tasks);

        const projectItem = new ProjectTreeItem(project, tasks);
        items.push(projectItem);

        for (const task of tasks) {
          const taskItem = new TaskTreeItem(task, project, vscode.TreeItemCollapsibleState.None);
          items.push(taskItem);
        }
      } catch (err: unknown) {
        if (err instanceof ApiError) {
          this.connectionError = err.message;
          items.push(new ErrorTreeItem(err.message));
        } else {
          const msg = `Failed to load tasks: ${String(err)}`;
          this.connectionError = msg;
          items.push(new ErrorTreeItem(msg));
        }
        return items;
      }
    }

    if (items.length === 0) {
      return [new EmptyTreeItem()];
    }

    return items;
  }

  getProjects(): Project[] {
    return this.projects;
  }

  getConnectionError(): string | null {
    return this.connectionError;
  }
}
