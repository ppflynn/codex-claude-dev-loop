import * as assert from 'assert';
import { ApiError, STATUS_LABELS, STATUS_ICONS } from '../src/types';
import type { Task } from '../src/types';

suite('ApiError', () => {
  test('should create with message and status code', () => {
    const error = new ApiError('Not Found', 404);
    assert.strictEqual(error.message, 'Not Found');
    assert.strictEqual(error.statusCode, 404);
    assert.strictEqual(error.name, 'ApiError');
  });

  test('should capture status code 0 for connection errors', () => {
    const error = new ApiError('Connection refused', 0);
    assert.strictEqual(error.statusCode, 0);
    assert.strictEqual(error.message, 'Connection refused');
  });

  test('instanceof checks', () => {
    const error = new ApiError('test', 500);
    assert.ok(error instanceof Error);
    assert.ok(error instanceof ApiError);
  });
});

suite('STATUS_LABELS', () => {
  test('should have labels for all known statuses', () => {
    const knownStatuses = [
      'CREATED', 'WAITING_FOR_CLAUDE', 'CLAUDE_WINDOW_STARTED',
      'WAITING_FOR_CODEX', 'CODEX_WINDOW_STARTED', 'NEEDS_FIX',
      'PASS', 'BLOCKED', 'FAILED', 'CANCELLED',
    ];
    for (const status of knownStatuses) {
      assert.ok(STATUS_LABELS[status], `Missing label for status: ${status}`);
      assert.strictEqual(typeof STATUS_LABELS[status], 'string');
    }
  });
});

suite('STATUS_ICONS', () => {
  test('should have icons for all known statuses', () => {
    const knownStatuses = [
      'CREATED', 'WAITING_FOR_CLAUDE', 'CLAUDE_WINDOW_STARTED',
      'WAITING_FOR_CODEX', 'CODEX_WINDOW_STARTED', 'NEEDS_FIX',
      'PASS', 'BLOCKED', 'FAILED', 'CANCELLED',
    ];
    for (const status of knownStatuses) {
      assert.ok(STATUS_ICONS[status], `Missing icon for status: ${status}`);
      assert.strictEqual(typeof STATUS_ICONS[status], 'string');
    }
  });
});

suite('Task type compatibility', () => {
  test('Task interface should include new progress fields', () => {
    const task: Task = {
      id: 'task_abc123',
      projectId: 'proj1',
      projectPath: '/path/to/project',
      title: 'Test Task',
      description: 'Test description',
      acceptance: 'Must pass',
      testCommand: 'pytest',
      status: 'CREATED',
      round: 1,
      maxRounds: 3,
      createdAt: '2026-06-14T00:00:00Z',
      updatedAt: '2026-06-14T00:00:00Z',
      archivedAt: null,
      deletedAt: null,
      artifacts: [],
      progress: 0,
      stage: 'created',
      activeClient: null,
      lastActivityAt: '2026-06-14T00:00:00Z',
      history: [],
    };

    assert.strictEqual(task.progress, 0);
    assert.strictEqual(task.stage, 'created');
    assert.strictEqual(task.activeClient, null);
    assert.strictEqual(task.lastActivityAt, '2026-06-14T00:00:00Z');
    assert.ok(Array.isArray(task.history));
  });

  test('Task with running client should indicate active work', () => {
    const task: Task = {
      id: 'task_running',
      projectId: 'proj1',
      projectPath: '/path',
      title: 'Running Task',
      description: 'Running',
      acceptance: 'Pass',
      testCommand: '',
      status: 'CLAUDE_WINDOW_STARTED',
      round: 1,
      maxRounds: 3,
      createdAt: '2026-06-14T00:00:00Z',
      updatedAt: '2026-06-14T00:00:00Z',
      archivedAt: null,
      deletedAt: null,
      artifacts: [],
      progress: 20,
      stage: 'claude_running',
      activeClient: 'claude',
      lastActivityAt: '2026-06-14T01:00:00Z',
      history: [],
    };

    assert.strictEqual(task.activeClient, 'claude');
    assert.strictEqual(task.progress, 20);
    assert.strictEqual(task.status, 'CLAUDE_WINDOW_STARTED');
  });

  test('Task with empty diff should be in FAILED no_changes stage', () => {
    const task: Task = {
      id: 'task_no_diff',
      projectId: 'proj1',
      projectPath: '/path',
      title: 'No Diff Task',
      description: 'Failed',
      acceptance: 'Pass',
      testCommand: '',
      status: 'FAILED',
      round: 1,
      maxRounds: 3,
      createdAt: '2026-06-14T00:00:00Z',
      updatedAt: '2026-06-14T00:00:00Z',
      archivedAt: null,
      deletedAt: null,
      artifacts: [],
      progress: 100,
      stage: 'no_changes',
      activeClient: null,
      lastActivityAt: '2026-06-14T01:00:00Z',
      history: [],
    };

    assert.strictEqual(task.status, 'FAILED');
    assert.strictEqual(task.stage, 'no_changes');
    assert.strictEqual(task.progress, 100);
    assert.strictEqual(task.activeClient, null);
  });
});
