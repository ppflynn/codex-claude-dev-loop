import * as assert from 'assert';
import { ApiError, STATUS_LABELS, STATUS_ICONS } from '../src/types';

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
