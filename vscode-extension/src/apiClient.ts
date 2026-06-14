import { getServerUrl } from './config';
import { ApiError } from './types';
import type { Project, Task, TaskArtifact } from './types';

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const baseUrl = getServerUrl().replace(/\/+$/, '');
  const url = `${baseUrl}${path}`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        ...options.headers,
      },
    });

    if (!response.ok) {
      let errorMessage = `HTTP ${response.status}`;
      try {
        const body = await response.json() as { error?: string };
        if (body.error) {
          errorMessage = body.error;
        }
      } catch {
        // response body is not JSON, use status text
      }
      throw new ApiError(errorMessage, response.status);
    }

    return response.json() as Promise<T>;
  } catch (err: unknown) {
    if (err instanceof ApiError) {
      throw err;
    }
    if (err instanceof TypeError) {
      throw new ApiError(
        `Cannot connect to Codex-Claude Dev Loop server at ${baseUrl}. Is the server running?`,
        0
      );
    }
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError(`Request to ${baseUrl} timed out.`, 0);
    }
    throw new ApiError(`Unexpected error: ${String(err)}`, 0);
  } finally {
    clearTimeout(timeout);
  }
}

export async function fetchProjects(): Promise<Project[]> {
  const data = await request<{ projects: Project[] }>('/api/projects');
  return data.projects;
}

export async function resolveProject(workspacePath: string): Promise<Project> {
  const data = await request<{ project: Project }>('/api/projects', {
    method: 'POST',
    body: JSON.stringify({ path: workspacePath }),
  });
  return data.project;
}

export async function fetchTasks(projectId?: string): Promise<Task[]> {
  const params = new URLSearchParams();
  if (projectId) {
    params.set('project', projectId);
  }
  const query = params.toString();
  const path = query ? `/api/tasks?${query}` : '/api/tasks';
  const data = await request<{ tasks: Task[] }>(path);
  return data.tasks;
}

export async function createTask(params: {
  projectId: string;
  title: string;
  description: string;
  acceptance: string;
}): Promise<Task> {
  const data = await request<{ task: Task }>('/api/tasks', {
    method: 'POST',
    body: JSON.stringify({
      projectId: params.projectId,
      title: params.title,
      description: params.description,
      acceptance: params.acceptance,
      testCommand: '',
      maxRounds: 3,
    }),
  });
  return data.task;
}

export async function fetchTaskDetail(taskId: string): Promise<Task> {
  const data = await request<{ task: Task }>(`/api/tasks/${taskId}`);
  return data.task;
}

export async function fetchTaskArtifacts(taskId: string): Promise<Record<string, TaskArtifact>> {
  const data = await request<{ artifacts: Record<string, TaskArtifact> }>(`/api/tasks/${taskId}/artifacts`);
  return data.artifacts;
}

export { ApiError };
