import * as vscode from 'vscode';
import { createTask as apiCreateTask, ApiError } from './apiClient';
import type { Project } from './types';

export async function createTaskCommand(project: Project): Promise<void> {
  const title = await vscode.window.showInputBox({
    prompt: 'Enter task title',
    placeHolder: 'e.g. Add user authentication',
    validateInput: (value: string) => {
      if (!value.trim()) {
        return 'Task title is required.';
      }
      return undefined;
    },
  });

  if (!title) {
    return;
  }

  const description = await vscode.window.showInputBox({
    prompt: 'Enter task description',
    placeHolder: 'Describe what needs to be implemented or fixed...',
    validateInput: (value: string) => {
      if (!value.trim()) {
        return 'Task description is required.';
      }
      return undefined;
    },
  });

  if (description === undefined) {
    return;
  }

  const acceptance = await vscode.window.showInputBox({
    prompt: 'Enter acceptance criteria',
    placeHolder: 'e.g. Tests pass, code review approved...',
  });

  if (acceptance === undefined) {
    return;
  }

  try {
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: 'Creating task...',
        cancellable: false,
      },
      async () => {
        await apiCreateTask({
          projectId: project.id,
          title: title.trim(),
          description: description.trim(),
          acceptance: (acceptance || '').trim(),
        });
      }
    );

    vscode.window.showInformationMessage(`Task "${title.trim()}" created successfully.`);
  } catch (err: unknown) {
    if (err instanceof ApiError) {
      vscode.window.showErrorMessage(`Failed to create task: ${err.message}`);
    } else {
      vscode.window.showErrorMessage(`Failed to create task: ${String(err)}`);
    }
  }
}
