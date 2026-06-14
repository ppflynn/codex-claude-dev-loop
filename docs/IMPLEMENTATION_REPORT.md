# Implementation Report: Runtime Terminal Mirror

## Summary

Added read-only CLI terminal mirroring to the web GUI. Users can now see Claude and Codex CLI output in real time without waiting for completion. The terminal output is scoped to the selected task and follows the active client. No web command input capability is added.

## Modified Files

### Backend

| File | Change |
|---|---|
| `gui/orchestrator/cli_window.py` | `launch_cli_window()` now returns `logPath` and `logName` fields in its result dict |
| `gui/server.py` | Added terminal API endpoints (`GET /api/tasks/{id}/terminal/{client}` and SSE stream), plus helper functions `_resolve_terminal_log`, `_terminal_metadata`, `_terminal_stream` |

### Frontend

| File | Change |
|---|---|
| `gui/static/index.html` | Added `<section class="runtime-terminal-panel">` with Claude/Codex terminal boxes in the inspector sidebar |
| `gui/static/app.js` | Added `terminalConnections` state, `connectTerminal()`, `disconnectTerminal()`, `disconnectAllTerminals()`, `refreshTerminalsForTask()`, `loadTerminalContent()`, and `updateTerminalBadges()` |
| `gui/static/styles.css` | Updated inspector grid to 4 rows, added `.runtime-terminal-panel` and terminal badge styles |

### Tests

| File | Change |
|---|---|
| `tests/test_gui_server.py` | Added `TerminalApiTests` class (8 tests): client validation, log path construction, metadata for missing/existing logs, round-based log naming, API endpoint error handling (404/400), stream endpoint |
| `tests/test_cli_window.py` | Added `test_launch_cli_window_returns_log_metadata` and `test_launch_codex_window_returns_log_metadata` |

## Implementation Details

### API Design

**Terminal metadata**: `GET /api/tasks/{taskId}/terminal/{client}`
- `client` must be `"claude"` or `"codex"`; returns 400 for other values
- Returns `{ taskId, client, logName, exists, size }`
- Log path is constructed from `task_store.task_dir(task.id) / f"{client}_window_round_{task.round}.log"`
- Path safety enforced via `ensure_child_path`

**Terminal stream**: `GET /api/tasks/{taskId}/terminal/{client}/stream`
- SSE endpoint (`text/event-stream`)
- Polls log file every 500ms for new content
- Sends `{ chunk, offset, done }` events
- Stream exits when log contains `"CLI exit code:"` or task status leaves `RUNNING_TASK_STATUSES`
- Reloads task from store on each poll to detect status changes

### Frontend Behavior

- **Task selected + running**: Connects SSE stream for the active client (Claude/Codex)
- **Task selected + completed**: Loads terminal content from artifacts API for static review
- **Task switch**: Closes all old EventSource connections via `disconnectAllTerminals()`
- **Task view change** (active/archived/trash): Disconnects all terminals
- **No task selected**: Shows placeholder text

### Safety Guarantees

- Client parameter validated against `{"claude", "codex"}` whitelist
- Log path validated as child of task directory via `ensure_child_path`
- Missing logs return metadata with `exists: false` instead of crashing
- Invalid task IDs return 404
- Streaming handles `BrokenPipeError`/`ConnectionResetError` gracefully
- No arbitrary file reading via URL — log paths are strictly derived from task ID + client name

### Backward Compatibility

- Old task JSON records without `progress`/`stage`/`activeClient` fields derive values from `status` (existing logic unchanged)
- Existing Claude/Codex launch buttons unchanged
- Task lifecycle statuses unchanged

## Fix Round 2: Codex Findings Resolution

### P1-1: Stale terminal output on task switch (`gui/static/app.js`)

**Issue**: `refreshTerminalsForTask()` disconnected EventSource connections but did not clear both terminal panels to the new task's placeholder state. When a running Claude task was selected, only `connectTerminal('claude')` was called — the Codex panel retained the previous task's output (and vice versa). Additionally, `loadTerminalContent()` used `selectedTask()` inline for its API calls but did not capture the task identity before `await`, so asynchronous responses could write old-task logs into a newly selected task's panels. The EventSource `onmessage` handler in `connectTerminal()` had the same race condition.

**Fix**:
- Both terminal panels (`claude-output`, `codex-output`) are now explicitly reset to a current-task placeholder before any stream connection or static load.
- `connectTerminal()` captures `taskId` and `taskRound` at call time and verifies `selectedTask()` identity before every SSE message write — stale EventSource data is discarded if the task changed.
- `loadTerminalContent()` accepts explicit `taskId`/`taskRound` parameters and re-verifies `selectedTask()` after each `await` before writing to the DOM.
- For running tasks, the non-active client panel now loads existing log content for the current task (if available) instead of silently retaining output from a different task.

### P2-1: Terminal flicker on every task list poll (`gui/static/app.js`)

**Issue**: `loadTasks()` called `refreshTerminalsForTask()` on every poll, which unconditionally called `disconnectAllTerminals()` + `connectTerminal()`. This closed SSE streams, cleared output to empty, re-read logs from offset 0, and restarted the stream — causing visible flickering and loss of real-time append behavior every time the background poll fired.

**Fix**: `terminalConnections` now stores a `subKey` (composite of `taskId|round|client|status`) for each client. `refreshTerminalsForTask()` builds the current subscription keys and returns early if they match the stored keys — leaving SSE streams and panel content untouched. Disconnect/reconnect only occurs when the selected task, round, active client, or task status actually changes.

### Files Modified

| File | Change |
|---|---|
| `gui/static/app.js` | Added `subKey` tracking to `terminalConnections`; `connectTerminal()` captures task identity and verifies before each SSE message write; `loadTerminalContent()` accepts `taskId`/`taskRound` params with post-await identity verification; `refreshTerminalsForTask()` builds subscription keys, skips if unchanged, resets both panels to placeholder before connecting/loading |

## Test Results

### python -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
```
44 passed, 1 warning in 3.31s
```

### python -m pytest -q
```
118 passed, 4 warnings in 13.24s
```

### cd vscode-extension && npm.cmd run compile
```
(compiled cleanly, no errors)
```

### cd vscode-extension && npm.cmd test
```
8 passing (2ms)
```

## Fix Round 3: Codex Findings Resolution

### P1-1: Terminal subscription keys missing activeClient (`gui/static/app.js`)

**Issue**: `refreshTerminalsForTask()` built subscription keys from `taskId|round|client|status` but did not include `task.activeClient`. When a task stayed in a running status but `activeClient` changed or was corrected by a poll, the early return prevented disconnecting the old EventSource and connecting the newly active terminal. This could leave the web UI streaming Claude while the task was actually running Codex, or keep showing stale output.

**Fix**: Added `task.activeClient ?? ""` to the subscription key format, so changes to the active client field force terminal panels to refresh and reconnect. Key format is now `taskId|round|client|status|activeClient`.

### Files Modified

| File | Change |
|---|---|
| `gui/static/app.js` | `refreshTerminalsForTask()` subscription keys now include `task.activeClient ?? ""` |

### Test Results

```
python -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
44 passed, 1 warning in 5.28s
```

## Fix Round 4: Codex Findings Resolution

### P2-1: Terminal log resolution ignores earlier rounds after round advance (`gui/server.py`)

**Issue**: `_resolve_terminal_log` always constructed the log path using `task.round`. After Codex returns NEEDS_FIX, `complete_codex_task` increments `task.round` (e.g. from 1 to 2) and sets status to WAITING_FOR_CLAUDE. The terminal metadata endpoint then looked for `claude_window_round_2.log` which did not exist yet, so the web terminal showed no output from round 1 — the round whose Claude/Codex output actually led to the fix request.

**Fix**: Added a `fallback` parameter to `_resolve_terminal_log`. When `fallback=True` and the current round's log does not exist, the function walks down to earlier rounds (round-1, round-2, ...) and returns the first existing log. `_terminal_metadata` now passes `fallback=True` so the metadata endpoint reports the latest available log. `_terminal_stream` keeps `fallback=False` (default) so live streaming only watches the current round's log — this prevents the stream from reading a completed earlier-round log containing "CLI exit code:" and terminating immediately.

### P2-2: SSE onerror leaves stale subKey preventing reconnect (`gui/static/app.js`)

**Issue**: The EventSource `onerror` handler closed the connection and set `source = null` but did not clear `subKey`. On the next `refreshTerminalsForTask()` poll, the unchanged `subKey` matched the current subscription key, triggering the early return and preventing any reconnect attempt. The terminal panel was permanently stuck in a disconnected state.

**Fix**: Added `terminalConnections[client].subKey = null` in the `onerror` handler so the next poll detects the mismatch and reconnects.

### Files Modified

| File | Change |
|---|---|
| `gui/server.py` | `_resolve_terminal_log` gains `fallback` parameter; walks back to earlier rounds when current round log is missing; `_terminal_metadata` enables fallback |
| `gui/static/app.js` | EventSource `onerror` clears `subKey` to allow reconnect on next poll |

### Test Results

```
python -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
44 passed, 1 warning in 3.30s
```

```
python -m pytest -q
118 passed, 4 warnings in 15.24s
```

## Fix Round 5: Codex Findings Resolution

### P2-1: SSE onerror leaves terminal permanently disconnected with no auto-reconnect (`gui/static/app.js`)

**Issue**: The EventSource `onerror` handler cleared `subKey` and closed the connection, but `loadTasks()` is only called in response to user actions — there is no periodic task polling loop. After a transient SSE error (network blip, server restart), the terminal stayed disconnected until the user manually selected a different task or triggered another action. The `subKey` clear from Round 4 helped only when a poll did fire, but no poll ran automatically.

**Fix**: Added exponential backoff reconnect in the `onerror` handler. When the error fires and the task is still selected and running, a `setTimeout` schedules `connectTerminal()` after the current backoff delay (starting at 1s, doubling to max 30s). The delay resets to 1s on each successful data chunk reception. Session-scoped `reconnectTimer` handles are stored on `terminalConnections[client]` and cleared by both `disconnectTerminal()` and the start of `connectTerminal()` to prevent double-reconnect on user-initiated terminal refresh.

### P2-2: Silent SSE stream when terminal log doesn't exist yet (`gui/server.py`)

**Issue**: `_terminal_stream` sent no SSE event when the log file was missing — it just slept 0.5s and retried. Meanwhile `connectTerminal` cleared `outputEl.textContent` to empty on the frontend. If the CLI launcher failed before creating the log, the user saw a blank terminal panel indefinitely with no signal that the stream was alive but waiting.

**Fix**:
- **Backend**: Added a `waiting_sent` flag. On the first poll where the log doesn't exist yet, `_terminal_stream` sends a `{"waiting": true, "offset": 0, "done": false, "chunk": ""}` SSE event so the frontend knows the connection is alive and waiting for the log file.
- **Frontend**: `connectTerminal` now sets an initial `"正在连接..."` placeholder instead of clearing to empty. Added `hasOutput` tracking to `terminalConnections[client]`. When a `waiting` event arrives before any real output, the panel shows `"等待 CLI 启动并创建日志文件..."`. The first real `chunk` replaces the placeholder; subsequent chunks append normally.

### Files Modified

| File | Change |
|---|---|
| `gui/static/app.js` | `terminalConnections` gains `reconnectTimer`/`reconnectDelay`/`hasOutput` fields; `onerror` schedules exponential-backoff `setTimeout` reconnect for still-running selected tasks; `connectTerminal` clears pending reconnect on start, sets placeholder text, tracks `hasOutput`; `onmessage` resets backoff on each data chunk, replaces placeholder on first output, appends thereafter; `disconnectTerminal` clears pending reconnect timer |
| `gui/server.py` | `_terminal_stream` gains `waiting_sent` flag; sends `{"waiting": true}` SSE event on first poll where log file is missing, so frontend knows stream is alive but waiting for the launcher |

### Test Results

```
python -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
44 passed, 1 warning in 3.86s
```

## Fix Round 6: Codex Findings Resolution

### P2-1: Terminal metadata fallback shows stale earlier-round logs for WAITING_FOR_CODEX (`gui/server.py`)

**Issue**: `_terminal_metadata` always passed `fallback=True` to `_resolve_terminal_log`. When a task was in `WAITING_FOR_CODEX` status, the current-round Codex log had not been created yet, so the fallback walked to earlier rounds and returned metadata for `codex_window_round_{N-1}.log` — making the Codex panel show output from a previous round instead of indicating "Codex hasn't started."

**Fix**: `_terminal_metadata` now only enables fallback when `task.status == Status.WAITING_FOR_CLAUDE` (the state where round was bumped after NEEDS_FIX and reviewing previous-round output is intentional). For all other statuses including `WAITING_FOR_CODEX`, the current-round metadata is returned with `exists=false`.

### P2-2: Stale EventSource handlers clear active connection reference (`gui/static/app.js`)

**Issue**: The `onmessage` and `onerror` handlers in `connectTerminal()` set `terminalConnections[client].source = null` unconditionally when closing their EventSource. If a queued event from an old stream fired after a task switch or reconnect (where a new EventSource had already been stored), this wiped the new connection's reference — orphaning it so `disconnectTerminal` couldn't close it and badges/reconnect state went stale.

**Fix**: All three mutation points in the EventSource handlers now guard with `if (terminalConnections[client].source === es)` before clearing the source reference (and for `onerror`, the `subKey`). The `done` handler also wraps the `done=true` + `close()` + `source=null` block in the same guard so a stale done event cannot mark a new stream as complete.

### Files Modified

| File | Change |
|---|---|
| `gui/server.py` | `_terminal_metadata` now passes `fallback=True` only when `task.status == Status.WAITING_FOR_CLAUDE`; all other statuses use `fallback=False` to avoid stale previous-round log metadata |
| `gui/static/app.js` | EventSource `onmessage` stale-task and `done` handlers, and `onerror` handler, all verify `terminalConnections[client].source === es` before clearing `source`/`subKey`/`done` |

### Test Results

```
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
44 passed, 1 warning in 2.65s
```

```
py -3 -m pytest -q
118 passed, 4 warnings in 12.89s
```

## Fix Round 7: Codex Findings Resolution

### P2-1: _terminal_stream switches log path on round advance (`gui/server.py`)

**Issue**: `_terminal_stream` reloaded the task from the store on every poll iteration and re-resolved the log path via `_resolve_terminal_log(task_store, task, client)` — which used the current `task.round`. When Codex completed with NEEDS_FIX, `complete_codex_task` advanced `task.round` (e.g. from 1 to 2) while the SSE stream was still draining the round-1 log. The stream then resolved to `codex_window_round_2.log` and skipped unread bytes from `codex_window_round_1.log` before sending `done`.

**Fix**: The log path is now captured once at stream start (via an initial task load + `_resolve_terminal_log` call). The task is reloaded on each iteration only for the status/completion check. The captured log path remains stable for the lifetime of the stream, so a round advance during streaming cannot redirect reads to a different log file.

**Test**: `test_terminal_stream_captured_round_survives_round_advance` — appends data to the round-1 log, advances `task.round` to 2, and verifies the stream still emits the round-1 data without ever reading from the poison round-2 log.

### P2-2: Stale EventSource onerror schedules reconnect after replacement (`gui/static/app.js`)

**Issue**: The `onerror` handler cleared `source` and `subKey` under a `terminalConnections[client].source === es` guard, but then unconditionally proceeded to schedule a `setTimeout` reconnect. A stale error from an old EventSource — one already replaced by a new connection — could pass through the reconnect logic. Its `setTimeout` would then call `connectTerminal(client)`, whose initial `disconnectTerminal(client)` closes the active stream.

**Fix**: Added `const wasCurrent = terminalConnections[client].source === es` before clearing. If `!wasCurrent`, the handler returns immediately without scheduling any reconnect. Additionally, the reconnect condition now verifies that the client is still the active one using the same logic as `refreshTerminalsForTask()` (`activeClient === client` or status directly matches the client), so an inactive client's stale error doesn't reconnect either.

### Files Modified

| File | Change |
|---|---|
| `gui/server.py` | `_terminal_stream` now captures `task` and `log_path` once before the loop; task is reloaded only for the status check at the end of each iteration |
| `gui/static/app.js` | `onerror` handler captures `wasCurrent` before mutation; returns early if EventSource was already replaced; adds `activeForClient` condition to reconnect scheduling |
| `tests/test_gui_server.py` | Added `Status` import from `state_machine`; added `test_terminal_stream_captured_round_survives_round_advance` regression test |

### Test Results

```
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
45 passed, 1 warning in 3.80s
```

```
py -3 -m pytest -q
119 passed, 4 warnings in 16.63s
```

## Fix Round 8: Codex Findings Resolution

### P2-1: Text-mode seek/read uses byte offsets unsafely in `_terminal_stream` (`gui/server.py`)

**Issue**: `_terminal_stream` called `log_path.stat().st_size` to get a byte count, then opened the UTF-8 log in text mode (`"r"`) and called `handle.seek(sent_bytes)` and `handle.read(current_size - sent_bytes)`. In text mode, `read()` counts **characters**, not bytes, and `seek()` with an arbitrary byte offset is undefined behavior per Python docs. If a log grew between `stat()` and `read()` — especially with Chinese or other multibyte CLI output — the stream could over-read, then seek back to the old byte offset and emit duplicated or corrupted terminal text.

**Fix**: Changed to binary mode (`"rb"`): read exact bytes from the seek position, then decode the raw bytes with `utf-8` and `errors="replace"`. This guarantees byte offsets are always safe (binary seek is always valid) and the byte count matched by `read()` is exact. The `errors="replace"` ensures a split multibyte sequence produces a `�` replacement character instead of crashing.

**Test**: `test_terminal_stream_multibyte_no_duplicate_or_corrupt` — appends Chinese text (`"第2行中文内容\n"`) between poll cycles, then verifies each unique line appears exactly once in the SSE output without duplication or corruption.

### Files Modified

| File | Change |
|---|---|
| `gui/server.py` | `_terminal_stream` now opens log in binary mode, reads raw bytes, decodes with `errors="replace"` |
| `tests/test_gui_server.py` | Added `test_terminal_stream_multibyte_no_duplicate_or_corrupt` regression test |

### Test Results

```
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
46 passed, 1 warning in 3.69s
```

```
py -3 -m pytest -q
120 passed, 4 warnings in 16.00s
```

## Fix Round 9: Codex Findings Resolution

### P2-1: Per-chunk UTF-8 decode corrupts split multibyte characters in `_terminal_stream` (`gui/server.py`)

**Issue**: `_terminal_stream` decoded each newly read byte chunk independently with `raw.decode("utf-8", errors="replace")` and then advanced `sent_bytes` to the exact byte position. If a multibyte UTF-8 character (e.g., a CJK character occupying 3 bytes) was partially written when the poll fired, the incomplete bytes were replaced with `�` and skipped. When the continuation bytes arrived on the next poll, they were also invalid in isolation and produced another `�` — corrupting the output permanently.

**Fix**: Replaced the one-shot `raw.decode("utf-8", errors="replace")` with an incremental `codecs.getincrementaldecoder("utf-8")(errors="replace")` created once before the loop. Each poll feeds raw bytes via `decoder.decode(raw, final=False)`, which buffers incomplete sequences internally and emits them only when complete. On both exit paths (CLI exit code detected, or task status leaves running), `decoder.decode(b"", final=True)` flushes any remaining bytes in the buffer. The `"CLI exit code:"` sentinel check remains safe because it's pure ASCII and never spans a multibyte boundary.

**Test**: `test_terminal_stream_split_multibyte_character_no_corruption` — writes the first 2 bytes of `"中"` (`\xe4\xb8`) in one poll cycle and the 3rd byte (`\xad`) plus newline in the next. Verifies the output contains `"中"` exactly once with no `�` replacement characters.

### Files Modified

| File | Change |
|---|---|
| `gui/server.py` | Added `import codecs`; `_terminal_stream` now creates an incremental UTF-8 decoder before the loop, feeds bytes via `decoder.decode(raw, final=False)`, and flushes with `final=True` on both exit paths |
| `tests/test_gui_server.py` | Added `test_terminal_stream_split_multibyte_character_no_corruption` regression test |

### Test Results

```
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
47 passed, 1 warning in 4.13s
```

```
py -3 -m pytest -q
121 passed, 4 warnings in 17.90s
```
