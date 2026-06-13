import * as vscode from 'vscode';
import * as path from 'path';
import { TaskTreeProvider } from './taskTreeProvider';
import { createTaskCommand } from './createTaskCommand';
import { fetchTaskArtifacts, fetchTasks, ApiError } from './apiClient';
import type { Task, Project } from './types';

export function activate(context: vscode.ExtensionContext): void {
  const taskTreeProvider = new TaskTreeProvider();

  const treeView = vscode.window.createTreeView('codexClaudeDevLoop.tasks', {
    treeDataProvider: taskTreeProvider,
    showCollapseAll: false,
  });

  context.subscriptions.push(treeView);

  context.subscriptions.push(
    vscode.commands.registerCommand('codexClaudeDevLoop.refreshTasks', () => {
      taskTreeProvider.refresh();
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('codexClaudeDevLoop.createTask', async () => {
      const projects = taskTreeProvider.getProjects();
      let project: Project | undefined;

      if (projects.length === 0) {
        if (!vscode.workspace.workspaceFolders || vscode.workspace.workspaceFolders.length === 0) {
          vscode.window.showErrorMessage('No workspace folder is open. Open a project folder first.');
          return;
        }
        vscode.window.showErrorMessage(
          'Could not resolve workspace project. Make sure the Codex-Claude Dev Loop server is running.'
        );
        return;
      }

      if (projects.length === 1) {
        project = projects[0];
      } else {
        const selected = await vscode.window.showQuickPick(
          projects.map((p) => ({ label: p.name, description: p.path, project: p })),
          { placeHolder: 'Select a project for the new task' }
        );
        if (!selected) {
          return;
        }
        project = selected.project;
      }

      await createTaskCommand(project);
      taskTreeProvider.refresh();
    })
  );

  // File-opening commands
  context.subscriptions.push(
    vscode.commands.registerCommand(
      'codexClaudeDevLoop.openPlan',
      async (taskItem?: { task: Task; project: Project }) => {
        if (!taskItem) {
          return;
        }
        const planPath = path.join(taskItem.task.projectPath, 'docs', 'PLAN.md');
        await openFile(planPath);
      }
    )
  );

  context.subscriptions.push(
    vscode.commands.registerCommand(
      'codexClaudeDevLoop.openClaudePrompt',
      async (taskItem?: { task: Task; project: Project }) => {
        if (!taskItem) {
          return;
        }
        const candidates = taskItem.task.round <= 1
          ? ['CLAUDE_IMPLEMENT_PROMPT.md']
          : [`FIX_PROMPT_ROUND_${taskItem.task.round}.md`];
        await openTaskArtifactWithFallback(taskItem.task, candidates, 'Claude prompt', roundFallbackFinder);
      }
    )
  );

  context.subscriptions.push(
    vscode.commands.registerCommand(
      'codexClaudeDevLoop.openCodexPrompt',
      async (taskItem?: { task: Task; project: Project }) => {
        if (!taskItem) {
          return;
        }
        await openTaskArtifactWithFallback(taskItem.task, ['CODEX_REVIEW_PROMPT.md'], 'Codex prompt');
      }
    )
  );

  context.subscriptions.push(
    vscode.commands.registerCommand(
      'codexClaudeDevLoop.openReport',
      async (taskItem?: { task: Task; project: Project }) => {
        if (!taskItem) {
          return;
        }
        const reportPath = path.join(taskItem.task.projectPath, 'docs', 'IMPLEMENTATION_REPORT.md');
        await openFile(reportPath);
      }
    )
  );

  // Initial load
  taskTreeProvider.refresh();
}

export function deactivate(): void {
  // No cleanup needed
}

type ArtifactMap = Record<string, { content?: string }>;

function roundFallbackFinder(artifacts: ArtifactMap): string | undefined {
  const pattern = /^FIX_PROMPT_ROUND_(\d+)\.md$/;
  let best: { name: string; round: number } | undefined;
  for (const name of Object.keys(artifacts)) {
    const match = name.match(pattern);
    if (match) {
      const round = parseInt(match[1], 10);
      if (!best || round > best.round) {
        best = { name, round };
      }
    }
  }
  return best?.name;
}

async function openTaskArtifactWithFallback(
  task: Task,
  candidates: string[],
  label: string,
  fallbackFinder?: (artifacts: ArtifactMap) => string | undefined
): Promise<void> {
  try {
    const artifacts = await fetchTaskArtifacts(task.id);
    for (const candidate of candidates) {
      const taskArtifact = artifacts[candidate];
      if (taskArtifact?.content) {
        const doc = await vscode.workspace.openTextDocument({
          content: taskArtifact.content,
          language: 'markdown',
        });
        await vscode.window.showTextDocument(doc, { preview: false });
        return;
      }
    }
    if (fallbackFinder) {
      const fallbackName = fallbackFinder(artifacts);
      if (fallbackName) {
        const taskArtifact = artifacts[fallbackName];
        if (taskArtifact?.content) {
          const doc = await vscode.workspace.openTextDocument({
            content: taskArtifact.content,
            language: 'markdown',
          });
          await vscode.window.showTextDocument(doc, { preview: false });
          return;
        }
      }
    }
    vscode.window.showWarningMessage(`${label} not found for task "${task.title}".`);
  } catch (err: unknown) {
    if (err instanceof ApiError) {
      vscode.window.showErrorMessage(`Failed to fetch artifacts: ${err.message}`);
    } else {
      vscode.window.showErrorMessage(`Failed to fetch artifacts: ${String(err)}`);
    }
  }
}

async function openFile(filePath: string): Promise<void> {
  const fsPath = vscode.Uri.file(filePath);
  try {
    await vscode.workspace.fs.stat(fsPath);
    const doc = await vscode.workspace.openTextDocument(fsPath);
    await vscode.window.showTextDocument(doc, { preview: false });
  } catch {
    vscode.window.showErrorMessage(`File not found: ${filePath}`);
  }
}
