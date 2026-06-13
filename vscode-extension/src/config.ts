import * as vscode from 'vscode';

export function getServerUrl(): string {
  const config = vscode.workspace.getConfiguration('codexClaudeDevLoop');
  return config.get<string>('serverUrl', 'http://127.0.0.1:8765');
}
