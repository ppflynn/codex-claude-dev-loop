export class ApiError extends Error {
  constructor(
    message: string,
    public statusCode: number
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export interface Task {
  id: string;
  projectId: string;
  projectPath: string;
  title: string;
  description: string;
  acceptance: string;
  testCommand: string;
  status: string;
  round: number;
  maxRounds: number;
  createdAt: string;
  updatedAt: string;
  archivedAt: string | null;
  deletedAt: string | null;
  artifacts: TaskArtifact[];
  progress: number;
  stage: string;
  activeClient: string | null;
  lastActivityAt: string | null;
  history?: TaskHistoryItem[];
}

export interface TaskHistoryItem {
  at: string;
  event: string;
  message: string;
  previous?: string;
  status?: string;
}

export interface TaskArtifact {
  name: string;
  path: string;
  kind?: string;
  exists: boolean;
  content: string;
}

export interface Project {
  id: string;
  name: string;
  path: string;
  kind: string;
  worktreeType: string | null;
  branch: string | null;
  available: boolean;
}

export interface ApiErrorResponse {
  error: string;
}

export const STATUS_LABELS: Record<string, string> = {
  CREATED: 'Created',
  WAITING_FOR_CLAUDE: 'Waiting for Claude',
  CLAUDE_WINDOW_STARTED: 'Claude Running',
  WAITING_FOR_CODEX: 'Waiting for Codex',
  CODEX_WINDOW_STARTED: 'Codex Running',
  NEEDS_FIX: 'Needs Fix',
  PASS: 'Passed',
  BLOCKED: 'Blocked',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
};

export const STATUS_ICONS: Record<string, string> = {
  CREATED: 'circle-outline',
  WAITING_FOR_CLAUDE: 'sync',
  CLAUDE_WINDOW_STARTED: 'terminal',
  WAITING_FOR_CODEX: 'sync',
  CODEX_WINDOW_STARTED: 'terminal',
  NEEDS_FIX: 'warning',
  PASS: 'pass-filled',
  BLOCKED: 'error',
  FAILED: 'error',
  CANCELLED: 'circle-slash',
};
