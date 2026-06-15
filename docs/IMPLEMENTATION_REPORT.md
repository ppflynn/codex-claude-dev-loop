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

## Round 10: xterm.js Terminal Upgrade & VSCode Workbench Layout

### Summary

Replaced the plain `<pre>`-based terminal display with xterm.js (v5.3.0) rendering. Each CLI client (Claude/Codex) now has an independent xterm Terminal instance with VSCode Terminal dark theme. The existing SSE log streaming, task state machine, and PowerShell CLI window launching are untouched. No web command input capability is added.

### xterm.js Asset Management

- Downloaded xterm v5.3.0 and xterm-addon-fit v0.7.0 UMD bundles locally via npm
- Copied to `gui/static/xterm/` directory:
  - `xterm.js` — UMD bundle exposing `window.Terminal`
  - `xterm.css` — base xterm stylesheet
  - `xterm-addon-fit.js` — UMD bundle exposing `window.FitAddon`
- Loaded via `<script>` tags in `index.html`; no CDN dependencies

### Frontend Architecture Changes

**Terminal lifecycle** (`gui/static/app.js`):

- `terminalInstances` object stores per-client `{ term, fitAddon, observer, hasOutput }` state
- `createTerminal(client)` — Creates a new xterm Terminal with VSCode dark theme options (`fontSize: 13`, `scrollback: 10000`, `disableStdin: true`, `convertEol: true`), attaches `FitAddon`, opens in the container DOM element, and sets up a `ResizeObserver` for auto-fit on container resize. Destroys any existing terminal first.
- `destroyTerminal(client)` — Disconnects the ResizeObserver, calls `term.dispose()`, and nulls out references.
- `writeToTerminal(client, text)` — Writes text directly to the xterm instance via `term.write()`.
- `clearAndWriteTerminal(client, text)` — Resets the terminal (clears buffer and viewport) then writes text.
- `writeTerminalPlaceholder(client, text)` — Resets terminal and writes dimmed placeholder text using ANSI escape `\x1b[2m`.
- `destroyAllTerminals()` — Destroys both terminals (called on task view switch).

**Modified functions**:

- `connectTerminal(client)` — Creates a fresh xterm instance via `createTerminal()`, writes dimmed `"正在连接..."` placeholder, then streams SSE chunks with `term.write(data.chunk)`. On first real chunk, calls `term.reset()` to clear the placeholder. All existing stale-guard, reconnect, and `subKey` logic preserved.
- `loadTerminalContent(client, taskId, taskRound)` — Creates a fresh xterm instance, writes dimmed placeholder for missing logs, or writes full historical content via `term.write()` for existing logs. Post-await identity verification preserved.
- `refreshTerminalsForTask()` — Destroys all old terminals, creates fresh ones with dimmed placeholder text, then either connects SSE streams or loads static content. Subscription key logic preserved.
- `setTaskView()` — Now calls `destroyAllTerminals()` in addition to `disconnectAllTerminals()`.

### HTML Changes

- Replaced `<pre id="claude-output" class="terminal-output">` with `<div id="claude-output" class="terminal-output">`
- Replaced `<pre id="codex-output" class="terminal-output">` with `<div id="codex-output" class="terminal-output">`
- Added `<link rel="stylesheet" href="/xterm/xterm.css">` in `<head>`
- Added `<script src="/xterm/xterm.js">` and `<script src="/xterm/xterm-addon-fit.js">` before `</body>`

### CSS Changes

- Removed old `<pre>`-specific terminal box styles
- Added `--term-bg`, `--term-header`, `--term-tab-active`, `--term-tab-inactive`, `--term-tab-border` CSS variables for VSCode-like dark theme
- Terminal grid now uses `background: var(--term-tab-border)` as separator between stacked terminals
- Terminal box uses `background: var(--term-bg)` (#1e1e1e — VSCode terminal background)
- Terminal title bar restyled: `background: var(--term-tab-active)` (#252526), light text
- Updated `runtime-terminal-panel` to use `display: grid; grid-template-rows: auto 1fr` for proper sizing
- `.terminal-output` set to `min-height: 0; overflow: hidden` so xterm fills space correctly
- `.terminal-output .xterm` gets left padding for visual comfort
- Inspector grid row adjusted: `minmax(200px, 0.55fr)` for terminal panel

### Backend Enhancement

- `_terminal_metadata()` now returns additional fields:
  - `round` — current task round number
  - `status` — current task status string
  - `active` — boolean, true when this client is the task's activeClient and task is in a running status
  - `updatedAt` — ISO 8601 timestamp of the log file's last modification, or null if log doesn't exist
- Existing fields (`taskId`, `client`, `logName`, `exists`, `size`) preserved for backward compatibility
- Added `from datetime import datetime, timezone` import

### Test Changes

- `test_terminal_metadata_for_missing_log` — Added assertions for `round`, `status`, `active` (false), `updatedAt` (null)
- `test_terminal_metadata_for_existing_log` — Sets `CLAUDE_WINDOW_STARTED` status and `activeClient`, asserts `active` is true and `updatedAt` is not null
- `test_terminal_metadata_uses_task_round` — Added assertion for `meta["round"] == 3`
- `test_terminal_api_endpoint_returns_metadata` — Added assertions for `round`, `status`, `active`, `updatedAt`
- `test_terminal_metadata_active_client_reflects_running_state` — New test verifying `active` flag is true only for the matching client in running status

### Safety & Behavior Guarantees Preserved

- Client parameter validation (`{"claude", "codex"}`) — unchanged
- Log path safety via `ensure_child_path` — unchanged
- Stale task guard (captures `taskId`/`taskRound`, verifies before every SSE write and after every `await` in `loadTerminalContent`) — preserved
- Subscription key deduplication (`taskId|round|client|status|activeClient`) — preserved, prevents unnecessary reconnect/flicker
- SSE reconnect with exponential backoff (1s → 30s max, reset on data chunk) — preserved
- `subKey` clearing on SSE error to allow re-poll reconnect — preserved
- `onerror` `wasCurrent` guard before scheduling reconnect — preserved
- Incremental UTF-8 decoder in `_terminal_stream` — unchanged, ensures Chinese/multibyte output renders correctly in xterm
- Stream round capture (log path frozen at stream start) — unchanged

### Test Results

```
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
48 passed, 1 warning in 3.64s
```

```
py -3 -m pytest -q
122 passed, 4 warnings in 16.21s
```

```
cd vscode-extension && npm.cmd run compile
(compiled cleanly, no errors)
```

```
cd vscode-extension && npm.cmd test
8 passing (3ms)
```

### Round 2 Fix: xterm.js Constructor & subKey Cleanup

**P1-1** — The UMD bundle for `xterm-addon-fit` exposes the constructor as `window.FitAddon.FitAddon`, not `window.FitAddon`. Calling `new FitAddon()` threw `TypeError`, causing `createTerminal()` to fail and leaving both terminal panels empty (no xterm instances rendered).

Fix in `gui/static/app.js:97`:
```
const FitAddonCtor = window.FitAddon?.FitAddon || window.FitAddon;
const fitAddon = new FitAddonCtor();
```
This resolves the constructor defensively so the code works with both UMD module shapes.

**P2-1** — `setTaskView()` called `destroyAllTerminals()` but did not clear `terminalConnections.*.subKey`. When `loadTasks()` subsequently triggered `refreshTerminalsForTask()` for the same task, the subKey matched and the function returned early without recreating terminals, leaving the panel empty.

Fix in `gui/static/app.js:812` — clear both subKeys after destroying terminals:
```
terminalConnections.claude.subKey = null;
terminalConnections.codex.subKey = null;
```

### Test Results (Round 2)

```
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
48 passed, 1 warning in 3.62s
```

## Round 11: Terminal Layout & CLI Completion Awareness

### Summary

Improved terminal display area and added CLI completion detection with auto-refresh. The right-side inspector now allocates significantly more space to the runtime terminal panel. The backend parses `CLI exit code: N` from log files and exposes it in both the metadata API and SSE done events. The frontend displays exit status badges, prompts user to click completion buttons when the active client exits, and auto-refreshes task state every 4 seconds while tasks are running.

### Terminal Layout Changes

**Problem**: The `.inspector` grid allocated `minmax(200px, 0.55fr)` to the terminal row and gave more fractional space to the lower panels (0.65fr and 0.8fr). With two terminals stacked vertically (`1fr 1fr`), each xterm viewport got only ~5-6 lines at 13px font — barely usable.

**Fix** (`gui/static/styles.css`):
- Changed inspector grid rows to: `auto minmax(380px, 1.4fr) minmax(80px, 0.3fr) minmax(80px, 0.3fr)`
  - Terminal row gets 1.4fr of the 2.0fr total = ~64% of remaining space after the auto-sized task status row
  - On a 1080px viewport: each terminal gets ~17 lines; on 900px: ~13 lines
  - Lower panels (task history, artifacts) get `minmax(80px, 0.3fr)` — smaller but with internal scrolling
- `.terminal-grid` min-height increased from `120px` to `260px`
- `.terminal-box` added `overflow: hidden` to prevent content overflow
- Task history (`#task-history`) and artifact panels continue to scroll internally when content exceeds their area

### CLI Completion Awareness

**Backend** (`gui/server.py`):

- `_terminal_metadata()` now reads the log file and scans for `CLI exit code: N` via regex. When found, returns `finished: true` and `exitCode: N` (integer). When the log doesn't exist or doesn't contain the sentinel, returns `finished: false` and `exitCode: null`. Also returns `lastLogUpdateAt` (ISO 8601 from log file mtime, same as `updatedAt`).
- `_terminal_stream()` SSE done event now includes `exitCode` parsed from the `CLI exit code:` line. Uses the same `re.search(r"CLI exit code:\s*(-?\d+)")` pattern applied to the decoded chunk + tail buffer.

**Frontend** (`gui/static/app.js`):

- `terminalConnections` gains `finished`, `exitCode`, `lastLogUpdateAt` per client. These are cleared by `disconnectTerminal()`.
- `updateClientTitleBadge()` now shows context-aware badges:
  - "实时" (blue) — SSE stream active
  - "已退出" (grey) — terminal finished with exit code 0
  - "退出码 N" (yellow/warn) — terminal finished with non-zero exit code
  - "运行中" (blue) — task is running, this is the active client
  - "待启动" (grey) — no activity
- `loadTerminalContent()` captures `finished`/`exitCode`/`lastLogUpdateAt` from metadata API response and updates connection state.
- SSE `onmessage` done handler captures `data.exitCode` into connection state.
- `showCompletionPrompt(client, taskId)` — when the active client's terminal finishes (SSE done or static load), checks if this client is still the task's active client in running status. If so, adds an `attention-pulse` glow animation to the corresponding completion button and shows a toast: "Claude CLI 已退出 (退出码 0)，请点击 "Claude 已完成" 推进任务".
- `updateActionStates()` clears the `attention-pulse` class from completion buttons when they become disabled (task state changed).
- New CSS classes: `.terminal-client-badge.warn` (yellow/warning background for non-zero exit codes), `.attention-pulse` (glow animation for the completion button).

### Task Auto-Refresh

**Problem**: The task list and details only updated on user action (selecting a task, clicking a button). Running task progress, status transitions, and terminal activity weren't reflected without manual interaction.

**Fix** (`gui/static/app.js`):

- `manageAutoRefresh()` starts a 4-second polling interval when:
  - The task view is "active" (not archived or trash)
  - Any task has a running status OR the selected task is in a running status
- The interval calls `loadTasks(true)` (skip artifacts during auto-refresh to avoid unnecessary artifact re-fetch).
- `loadTasks(skipArtifacts)` now accepts an optional parameter. During auto-refresh, artifacts are only reloaded when the task list actually changed (detected via composite ID comparison).
- Auto-refresh stops when switching to archived/trash views or when no running tasks remain.
- **Safety**: The `subKey` deduplication in `refreshTerminalsForTask()` prevents xterm recreation on every poll — terminals are only recreated when the task identity, round, status, or active client actually changes.

### Tests

**New tests** (`tests/test_gui_server.py`):

| Test | Coverage |
|---|---|
| `test_terminal_metadata_parses_cli_exit_code_zero` | Metadata returns `finished=true, exitCode=0` when log contains `CLI exit code: 0` |
| `test_terminal_metadata_parses_nonzero_exit_code` | Metadata returns `finished=true, exitCode=3` when log contains `CLI exit code: 3` |
| `test_sse_done_event_includes_exit_code` | SSE stream emits `done: true, exitCode: 5` in the final event |

**Updated existing tests**:
- `test_terminal_metadata_for_missing_log` — asserts `finished=false, exitCode=null, lastLogUpdateAt=null`
- `test_terminal_metadata_for_existing_log` — asserts `finished=false, exitCode=null` (no exit code in log), `lastLogUpdateAt` is not null

### What Was NOT Changed

- No web command input capability added (SSE and metadata remain read-only)
- Task status is NOT automatically advanced — user must still click "Claude 已完成" / "Codex 已完成"
- No automatic commits, pushes, or merges
- PowerShell CLI window launching via `subprocess.Popen` preserved
- All existing `subKey` deduplication, stale-guard, reconnect, and incremental UTF-8 decoder logic preserved
- VS Code extension unchanged

### Future Considerations

The current architecture (PowerShell window + file-based SSE streaming) works for the current use case but has limitations:
- The 500ms polling in `_terminal_stream` adds latency
- No PTY/WebSocket for true real-time streaming
- File-based logs require the PowerShell window to flush output (line-buffered or manual)

**If real-time interaction is needed in the future:**
- Replace the file-based approach with a WebSocket server that receives `subprocess.Popen` stdout directly
- Or use a PTY (pseudo-terminal) with WebSocket relay for sub-100ms latency
- Either approach would require significant architectural changes to both the launcher and the GUI server

### Test Results

```
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
51 passed, 1 warning in 3.71s
```

```
py -3 -m pytest -q
125 passed, 4 warnings in 16.75s
```

```
cd vscode-extension && npm.cmd run compile
(compiled cleanly, no errors)
```

```
cd vscode-extension && npm.cmd test
8 passing (4ms)
```

## Round 12: Sentinel Parsing Hardening (P2-1 Fix)

### Summary

Hardened the `CLI exit code: N` sentinel parsing in both `_terminal_metadata()` and `_terminal_stream()` to use anchored line matching (`^...$`) instead of unanchored regex search. This prevents false positives when Claude/Codex normal output contains the string `CLI exit code:` as embedded text, and handles the case where the sentinel line is split across file read boundaries in the SSE stream.

### Root Cause (P2-1)

The original SSE parser used a raw substring check `"CLI exit code:" in chunk` followed by unanchored `re.search(r"CLI exit code:\s*(-?\d+)", chunk + tail)`. This had two failure modes:

1. **False positive**: If Claude/Codex output contained `CLI exit code:` in normal text (e.g., `"The CLI exit code: 5 was unexpected"`), the SSE stream would emit `done` with a wrong exit code and prompt the user while the CLI was still running.
2. **Split-across-reads**: If the sentinel line `CLI exit code: 0\n` was split across two file poll cycles (e.g., `CLI exit ` + `code: 0\n`), the raw substring check would fire on the first half but the regex on the combined content might still work — however, the line-based approach is more robust.

The metadata parser had the same unanchored search risk.

### Changes

**`gui/server.py` `_terminal_metadata()`**:
- Changed from `re.search(r"CLI exit code:\s*(-?\d+)", content)` to scanning lines in reverse with `re.match(r"^CLI exit code:\s*(-?\d+)\s*$", line)`
- Scanning from the end ensures the *last* sentinel line is used, which is the correct one (the launcher writes it as the final output)

**`gui/server.py` `_terminal_stream()`**:
- Added a rolling `line_buffer` variable that persists across poll cycles
- Replaced `"CLI exit code:" in chunk` substring check with complete-line parsing: each decoded chunk is split on `\n`, the incomplete last line is retained in `line_buffer`, and complete lines are checked against `^CLI exit code:\s*(-?\d+)\s*$`
- This handles the split-across-reads case: if `CLI exit ` arrives in one cycle and `code: 0\n` in the next, the rolling buffer reassembles the complete line before matching
- The anchored regex ensures embedded text like `"The CLI exit code: 3 was returned"` does not trigger a false done event

### Test Additions

| Test | Coverage |
|---|---|
| `test_terminal_metadata_ignores_non_sentinel_text` | Metadata does NOT flag finished when `CLI exit code:` appears embedded in other text |
| `test_terminal_metadata_uses_last_sentinel` | When multiple sentinel lines exist, metadata uses the last (correct) one |
| `test_terminal_stream_sentinel_split_across_reads` | SSE detects sentinel correctly when the line is split across file writes |
| `test_terminal_stream_ignores_embedded_sentinel` | SSE only matches sentinel on its own line; embedded text is ignored |

### What Was NOT Changed

- No architectural changes — PowerShell windows, file-based logs, and 500ms polling preserved
- Frontend (CSS/JS) unchanged
- VS Code extension unchanged
- Task auto-refresh, completion prompts, and terminal badges unchanged

### Test Results

```
py -3 -m pytest tests/test_gui_server.py -q
49 passed, 1 warning in 4.34s
```

```
py -3 -m pytest -q
129 passed, 4 warnings in 19.82s
```

## Round 13: Completion Prompt Stale on Task Switch (P2-1 Fix)

### Summary

Fixed a bug where the `attention-pulse` completion button glow persisted incorrectly when switching between two tasks in the same running state. The pulse was only cleared when the button became disabled, so switching from one `CLAUDE_WINDOW_STARTED` task (with an exited Claude) to another `CLAUDE_WINDOW_STARTED` task (with Claude still running) left the old pulse visible — misleading the user into clicking the completion action for the wrong task.

### Root Cause (P2-1)

`showCompletionPrompt()` added the `attention-pulse` CSS class to the completion button for a specific task/client combination, but `updateActionStates()` only removed it when the button transitioned to `disabled`. When both the old and new tasks had the same status (e.g., `CLAUDE_WINDOW_STARTED`), the button stayed enabled across the switch and the stale pulse was never cleared.

### Changes

**`gui/static/app.js`**:

- **Track prompted task**: Added `promptedTaskId: null` field to `terminalConnections.claude` and `terminalConnections.codex`. This records which task ID last triggered a completion prompt for each client.

- **Clear stale pulse in `showCompletionPrompt()`**: Before adding a new pulse, the function now checks if `conn.promptedTaskId` differs from the current `taskId`. If it does, the old pulse is removed. When adding a pulse, `conn.promptedTaskId` is set to the new task ID.

- **Clear pulses in `refreshTerminalsForTask()`**: When the subKey changes (indicating a different task is now selected), both `claude-completed-button` and `codex-completed-button` have their `attention-pulse` class removed immediately. This provides instant cleanup on task switch without waiting for the next `showCompletionPrompt()` call.

- **Clear `promptedTaskId` in `disconnectTerminal()`**: The field is reset to `null` alongside other connection state when a terminal is disconnected, preventing stale state from leaking across connections.

### What Was NOT Changed

- No architectural changes — file-based logs, SSE streaming, and PowerShell windows preserved
- `updateActionStates()` still clears pulses on button disable (additional safety net)
- VS Code extension unchanged
- Backend (server.py) unchanged

### Test Results

```
py -3 -m pytest tests/test_gui_server.py -q
49 passed, 1 warning in 3.42s
```

```
py -3 -m pytest -q
129 passed, 4 warnings in 15.61s
```

## Round 14: Auto-Refresh Stale-Response Guard (P2-1 Fix)

### Summary

Hardened the auto-refresh polling in `loadTasks()` against race conditions when the user switches task views (active → archived/trash) or triggers a task action while an auto-refresh request is in flight. Without this guard, a stale API response could mutate `state.tasks` and recreate terminal streams for the wrong view.

### Root Cause (P2-1)

`startAutoRefresh()` called `loadTasks(true)` on a 4-second interval without any in-flight serialization or stale-response guard. If a poll began in the active view, then the user switched to archived/trash before the response resolved, the stale response could still:
1. Overwrite `state.tasks` with active-view tasks while the UI shows archived/trash
2. Call `refreshTerminalsForTask()` and reconnect terminal SSE streams for an outdated task
3. Call `manageAutoRefresh()` and potentially restart the polling loop after it was stopped

### Changes

**`gui/static/app.js`**:

- **Added `loadGeneration` counter and `refreshInFlight` flag**: Two module-level variables to coordinate concurrent requests.
  - `loadGeneration` — monotonically increasing counter incremented before each `await api()` call. After the `await`, the captured generation is compared against the current value; a mismatch means a newer request (e.g., from a view switch) superseded this one.
  - `refreshInFlight` — boolean set `true` when an auto-refresh poll starts and `false` in a `finally` block after completion. The interval callback skips if already `true`, preventing overlapping polls.

- **Stale-response guard in `loadTasks()`**: Before `await api(...)`, captures `gen = ++loadGeneration`, `capturedProjectId`, and `capturedView`. After the `await`, returns early without mutating state if:
  - `gen !== loadGeneration` (a newer request started)
  - The current project ID differs from the captured one
  - `state.taskView` differs from the captured view

- **View-switch invalidation in `setTaskView()`**: Before calling `loadTasks()`, resets `refreshInFlight = false` and increments `loadGeneration`. This ensures any in-flight auto-refresh response from the previous view is discarded by the stale-response guard.

- **`refreshInFlight` is only managed for `skipArtifacts=true` calls** (auto-refresh polls). Direct user-triggered `loadTasks(false)` calls are not gated by the flag, but they still increment `loadGeneration`, which cancels any concurrent auto-refresh poll.

### What Was NOT Changed

- No architectural changes — file-based logs, SSE streaming, and PowerShell windows preserved
- The `subKey` deduplication in `refreshTerminalsForTask()` remains as a second line of defense
- Backend (server.py) unchanged
- VS Code extension unchanged
- No JavaScript tests added (the test suite is Python-only; browser-level tests would require a separate test framework)

### Test Results

```
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
55 passed, 1 warning in 3.67s
```

```
py -3 -m pytest -q
129 passed, 4 warnings in 16.69s
```

## Round 15: Sentinel Parsing Hardening (P2-1) & Stale-Response Guard (P3-1)

### Summary

Hardened the sentinel parser to handle the case where CLI output doesn't end with a newline, causing the `CLI exit code: N` marker to be glued directly to the last output text. Also added a stale-response recheck after `loadArtifacts()` to prevent race conditions when the user switches views during auto-refresh artifact loading.

### P2-1: Sentinel Without Preceding Newline

**Root Cause**: The launcher's `Write-NativeChunk` function writes CLI stdout/stderr as-is via `[System.IO.File]::AppendAllText`, without guaranteeing a trailing newline. The `Add-Content` call that writes `CLI exit code: N` appends its value after any existing content — if the last CLI output didn't end with `\n`, the resulting log line looks like `final outputCLI exit code: 0`. The anchored regex `^CLI exit code:...$` (added in Round 12 to prevent false positives) requires the sentinel at line start, so it misses this case entirely.

**Changes**:

**`gui/orchestrator/cli_window.py`** (launcher fix):
- Prepended `` `n `` to the sentinel value: `("`nCLI exit code: {{0}}" -f $Code)`. This guarantees the sentinel always starts on its own line regardless of whether the preceding CLI output had a trailing newline.

**`gui/server.py` `_terminal_metadata()`** (parser fallback):
- After the strict `re.match(r"^CLI exit code:\s*(-?\d+)\s*$", line)` check, added a fallback `re.search(r"CLI exit code:\s*(-?\d+)\s*$", line)` that matches the sentinel at the end of any line. The `$` anchor ensures the sentinel pattern is at end-of-line, not embedded in the middle (e.g., `"Result: CLI exit code: 3 was returned"` will NOT match). This fallback handles log files written by older launcher scripts that don't have the newline fix.

**`gui/server.py` `_terminal_stream()`** (SSE parser fallback):
- Same fallback added to the line-checking loop in the SSE stream generator. Combined with `line_buffer` (cross-read rolling buffer from Round 12), this catches the sentinel both when split across reads AND when glued to preceding output without a newline.

### P3-1: Stale-Response After Artifact Loading

**Root Cause**: `loadTasks()` had a stale-response guard (gen/project/view check) after `await api(...)` but not after `await loadArtifacts()`. If auto-refresh reached artifact loading and the user switched views during that `await`, the stale response could render artifacts, refresh terminals, and restart auto-refresh for the wrong view.

**Changes** (`gui/static/app.js`):

- Added a full stale-response recheck (generation counter + project + view) immediately after `await loadArtifacts()`. If any of these changed during the artifact fetch, the function returns early before calling `refreshTerminalsForTask()` and `manageAutoRefresh()`.

### Test Additions

| Test | Coverage |
|---|---|
| `test_terminal_metadata_sentinel_without_preceding_newline` | Metadata detects `CLI exit code: 0` when glued to preceding output: `"final outputCLI exit code: 0\n"` |
| `test_terminal_stream_sentinel_without_preceding_newline` | SSE stream emits `done: true, exitCode: 2` when sentinel is glued to output without preceding newline |

### What Was NOT Changed

- No architectural changes — PowerShell windows, file-based logs, and 500ms polling preserved
- Frontend CSS/HTML unchanged
- VS Code extension unchanged
- The existing `re.search` fallback uses `$` anchor to avoid false positives on embedded text (defense in depth with the launcher fix)

### Test Results

```
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
57 passed, 1 warning in 3.74s
```

```
py -3 -m pytest -q
131 passed, 4 warnings in 16.79s
```

## Round 16: Stale-Response Guard, SSE False-Positive, Sentinel Test Sync (Codex Round 6 Fixes)

### Summary

Three fixes from Codex review Round 6: hardened the `loadArtifacts()` stale-response guard to prevent state mutation by in-flight auto-refresh responses; removed the unanchored `re.search` fallback from SSE sentinel detection to prevent false-positive done events during live streaming; added a test assertion for the launcher's leading-newline sentinel format.

### P2-1: Stale-Response Guard in loadArtifacts()

**Root Cause**: `loadArtifacts()` fetched data via `await api(...)` and immediately mutated `state.artifacts`, `state.activeArtifact`, and called `renderArtifacts()` — all before the caller's stale-response recheck. If an auto-refresh poll reached artifact loading and the user switched views (active → archived/trash) during that `await`, the stale response would render artifacts for the wrong view.

**Fix** (`gui/static/app.js`):

- `loadArtifacts()` now accepts optional stale-guard parameters: `gen`, `capturedProjectId`, `capturedView`, `capturedTaskId`.
- After `await api(...)`, before mutating any state, the function validates: generation counter, current project ID, task view, and selected task ID all match the captured values. If any mismatch, the function returns early without touching state.
- The same stale check applies in both the success path and the `catch` error path.
- When called without parameters (legacy path), the guard is skipped (backward compatible).
- `loadTasks()` passes its captured `gen`/`capturedProjectId`/`capturedView`/`state.selectedTaskId` to `loadArtifacts()`. The post-`loadArtifacts()` recheck in `loadTasks()` remains as a second line of defense before `refreshTerminalsForTask()` and `manageAutoRefresh()`.
- `selectTask()` now also passes stale-guard parameters and checks the generation counter after `loadArtifacts()` returns, preventing stale artifact rendering on rapid task switches.

### P2-2: SSE False-Positive Sentinel Detection

**Root Cause**: `_terminal_stream()` used a fallback `re.search(r"CLI exit code:\s*(-?\d+)\s*$", line)` when the anchored `^CLI exit code:` regex didn't match. The `$` anchor requires the pattern at end-of-line, but output lines like `"I see the CLI exit code: 0"` (where the sentinel-like text coincidentally ends a line) would trigger a false `done` event, close the terminal stream, and prompt the user even though the CLI was still running.

The launcher (Round 15) already guarantees `\nCLI exit code: N` via the `Add-Content -Value ("\`nCLI exit code: {0}" -f $Code)` change, so the sentinel is always on its own line in live streams.

**Fix** (`gui/server.py`):

- Removed the `re.search` fallback from `_terminal_stream()`. The SSE parser now only matches `^CLI exit code:\s*(-?\d+)\s*$` anchored at line start.
- The `re.search` fallback is preserved in `_terminal_metadata()` for backward compatibility with old log files that may have the sentinel glued to preceding output.

**Test update** (`tests/test_gui_server.py`):

- `test_terminal_stream_sentinel_without_preceding_newline`: Updated to verify the SSE stream correctly **ignores** a glued sentinel (`doneCLI exit code: 2\n`). The stream closes via task status transition (not sentinel detection), and the done event carries no `exitCode`. This validates that the anchored-only parser won't false-trigger on improperly formatted sentinels.

### P3-1: Sentinel Format Test Assertion

**Root Cause**: The launcher's sentinel format changed from `CLI exit code: N` to `\`nCLI exit code: N` in Round 15, but `tests/test_cli_window.py` had no assertion verifying the leading newline escape in the generated script.

**Fix** (`tests/test_cli_window.py`):

- Added `self.assertIn("\`nCLI exit code:", content)` to `test_generate_claude_launcher_inside_task_dir`, verifying the generated PowerShell script writes the sentinel with the leading backtick-n escape. This prevents future regressions of the parser contract.

### What Was NOT Changed

- No architectural changes — PowerShell windows, file-based logs, and 500ms polling preserved
- `_terminal_metadata()` still uses the `re.search` fallback for backward compat with old logs
- Frontend CSS/HTML unchanged
- VS Code extension unchanged
- No changes to task state machine or CLI window launching

### Test Results

```
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
57 passed, 1 warning in 3.60s
```

```
py -3 -m pytest -q
131 passed, 4 warnings in 16.42s
```

## Round 17: SSE Done Event Hardening & Auto-Refresh Selection Guard (Codex Round 7 Fixes)

### Summary

Two fixes from Codex review Round 7: hardened the SSE `done` event handler to only show completion prompts when an actual CLI exit code is detected (preventing false prompts from generic stream closures), and paused auto-refresh polling during `selectTask()` to prevent background polls from invalidating foreground task selections.

### P2-1: SSE Done Event False Prompts

**Root Cause**: The SSE `onmessage` handler called `showCompletionPrompt()` for every `done` event, including those emitted by `_terminal_stream()` when the task status or active client changed (which carry no `exitCode`). Since `conn.done` was set to `true` on any done event and `showCompletionPrompt()` checked `!conn.finished && !conn.done`, a generic stream closure (e.g., from task cancellation) would pass the guard and incorrectly show an "exited" badge and completion prompt.

**Fix** (`gui/static/app.js`):

- **SSE `onmessage` handler**: Only sets `conn.finished = true` and `conn.exitCode` when `data.exitCode != null` (CLI actually exited with a known code). `conn.done` is still set for generic stream closure tracking (used by reconnect logic), but `conn.exitCode` is no longer overwritten with `null` on done-without-exitCode events — preserving the value set by `loadTerminalContent()` from metadata.
  - `showCompletionPrompt()` is now only called when `data.exitCode != null`, not on every done event.

- **`showCompletionPrompt()` guard**: Changed from `if (!conn.finished && !conn.done) return;` to `if (!conn.finished) return;`. The prompt now requires explicit knowledge that the CLI exited (via `conn.finished`), not just that the EventSource received any done event.

- **`updateClientTitleBadge()`**: Changed badge condition from `conn.finished || isDone` to just `conn.finished`. The "已退出" / "退出码 N" badge only appears when the CLI exit code was parsed from the log sentinel, not when the EventSource merely closed.

### P2-2: Auto-Refresh Invalidates Foreground Selection

**Root Cause**: `selectTask()` did not pause auto-refresh polling. If an auto-refresh poll fired during `selectTask()`'s `await loadArtifacts()`, the poll's `loadTasks(true)` incremented `loadGeneration`, causing `selectTask()`'s subsequent generation check to fail. The foreground selection was then silently abandoned — `refreshTerminalsForTask()` was never called, and the user saw stale state from whatever the background poll rendered last.

**Fix** (`gui/static/app.js`):

- **`selectTask()`**: Added `stopAutoRefresh()` at the start (before any async work) and `manageAutoRefresh()` at the end (after `refreshTerminalsForTask()`). Since JavaScript is single-threaded, `clearInterval` (called by `stopAutoRefresh`) executes synchronously and prevents any new timer callback from being queued during `selectTask()`'s synchronous preamble and subsequent `await`. The existing `++loadGeneration` increment remains as defense-in-depth against any callback that was already queued before `stopAutoRefresh()` ran.

### What Was NOT Changed

- No architectural changes — PowerShell windows, file-based logs, SSE streaming preserved
- `conn.done` flag retained for EventSource closure tracking (used by reconnect logic)
- `_terminal_stream()` and `_terminal_metadata()` unchanged
- No changes to task state machine or CLI window launching
- VS Code extension unchanged

### Test Results

```
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
57 passed, 1 warning in 3.76s
```

```
py -3 -m pytest -q
131 passed, 4 warnings in 16.27s
```

## Round 18: Sentinel Fallback Gate & Auto-Refresh Cleanup Timing (Codex Round 8 Fixes)

### Summary

Two fixes from Codex review Round 8: gated the `_terminal_metadata()` fallback `re.search` so it cannot mark active/running logs as finished (preventing false completion badges and prompts from model output that coincidentally ends with `CLI exit code: N`), and moved `refreshInFlight` cleanup from `loadTasks()`'s inner `finally` to the interval callback's `.finally()` so the flag stays true for the entire auto-refresh operation.

### P2-1: Metadata Fallback False-Positive on Active Tasks

**Root Cause**: `_terminal_metadata()` used a fallback `re.search(r"CLI exit code:\s*(-?\d+)\s*$", line)` when the anchored `^CLI exit code:...$` didn't match. The `$` anchor requires the pattern at end-of-line, but `re.search` allows it anywhere on the line — so a model output line like `"Expected CLI exit code: 0"` (where the sentinel-like text ends the line) would match, setting `finished=true` for a still-running active client and showing a false completion badge and prompt.

The launcher (Round 15) guarantees `\nCLI exit code: N` for all new logs, so the fallback is only needed for backward compatibility with old log files. For actively running tasks, the strict `^CLI exit code: N$` match is always correct.

**Fix** (`gui/server.py`):

- **`_terminal_metadata()` fallback gated by `not active`**: Changed `if not m:` to `if not m and not active:`. When the task is running and this is the active client (`active=True`), only the strict anchored `^CLI exit code: N$` match can set `finished=true`. The fallback `re.search` is only used for non-running tasks (backward compat with old logs).

**Test addition** (`tests/test_gui_server.py`):

- `test_terminal_metadata_ignores_fallback_when_active`: Sets task status to `CLAUDE_WINDOW_STARTED` with `activeClient="claude"`, writes `"Expected CLI exit code: 0\n"` (ends with sentinel pattern but doesn't start with it), and verifies `finished=false, exitCode=null`.

### P3-1: refreshInFlight Cleared Too Early

**Root Cause**: `loadTasks(true)` cleared `refreshInFlight = false` in a `finally` block immediately after `await api(...)` returned, but before artifact loading, terminal refresh, and `manageAutoRefresh()` completed. A subsequent interval callback could start another poll during this window, increment `loadGeneration`, and cause the first poll's `refreshTerminalsForTask()` to be silently abandoned — leaving terminal streams or badges stale under slow responses.

**Fix** (`gui/static/app.js`):

- **Removed `refreshInFlight = false` from `loadTasks()` inner `finally`**: The `try { data = await api(...); } finally { if (skipArtifacts) refreshInFlight = false; }` was simplified to `const data = await api(...);`.

- **Interval callback uses `.finally()`**: Changed `loadTasks(true)` (fire-and-forget) to `loadTasks(true).finally(() => { refreshInFlight = false; })`. The flag now stays true until ALL work completes: API fetch, stale checks, task render, artifact loading, terminal refresh, and auto-refresh management. Early returns (stale guard, no project) still trigger the `.finally()` cleanup.

### What Was NOT Changed

- No architectural changes — PowerShell windows, file-based logs, SSE streaming preserved
- `_terminal_stream()` SSE parser unchanged (already uses strict anchored-only matching since Round 16)
- `_terminal_metadata()` fallback behavior for non-running tasks unchanged (backward compat)
- `refreshInFlight` not managed for direct user-triggered `loadTasks(false)` calls (they don't set it)
- VS Code extension unchanged

### Test Results

```
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
58 passed, 1 warning in 4.11s
```

```
py -3 -m pytest -q
132 passed, 4 warnings in 16.84s
```

## Round 19: Stale Badge Cleanup & Panel Flex Layout (Codex Round 10 Fixes)

### Summary

Two fixes from Codex review Round 10: cleared stale per-client terminal badges when switching away from a task, and converted the history/artifact panels to flexbox layout so the inspector grid rows can actually shrink to their `minmax(80px, 0.3fr)` lower bound without being forced larger by child min-heights.

### P3-1: Stale Terminal Badges on Task Deselect

**Root Cause**: `updateTerminalBadges()` returned early when `!task`, only updating the aggregate `#terminal-state` label to "待选择". The per-client badge DOM elements inside each `.terminal-title` were left untouched, so switching to an empty archived/trash/active view after a finished CLI could leave stale "已退出" or "退出码 N" badges visible.

**Fix** (`gui/static/app.js`):

- Added `clearClientTitleBadge(client)` helper that resets a client's terminal title badge to "待启动" with the default badge class.
- Called `clearClientTitleBadge("claude")` and `clearClientTitleBadge("codex")` in the `!task` branch of `updateTerminalBadges()` before the early return.

### P3-2: Inspector Panel Children Force Overflow Instead of Scrolling

**Root Cause**: The inspector grid rows 3 and 4 (`minmax(80px, 0.3fr)`) could theoretically shrink to 80px, but the children inside `.terminal-panel` and `.artifact-panel` had large fixed `min-height` values: `#task-history` at 130px, `.task-list` at 130px, and `#artifact-content` at 170px. Since the panels had no `overflow: hidden` or flex constraint, these children forced the grid rows to expand beyond their allocated fractional space, causing the inspector/page to overflow instead of internally scrolling.

**Fix** (`gui/static/styles.css`):

- `.terminal-panel, .artifact-panel` — added `display: flex; flex-direction: column; min-height: 0; overflow: hidden;` so the panels respect their grid row's height constraint and children can shrink.
- `#task-history` — replaced `height: calc(50% - 62px)` / `min-height: 130px` with `flex: 1; min-height: 0;`. The element now fills available space and scrolls internally when content exceeds it.
- `.task-list` — replaced `height: calc(50% - 18px)` / `min-height: 130px` with `flex: 1; min-height: 0;`.
- `#artifact-content` — replaced `height: calc(100% - 84px)` / `min-height: 170px` with `flex: 1; min-height: 0;`.
- The `.panel-heading` and `.task-view-toggle` / `.tabs` children auto-size within the flex column, and the remaining space is split equally between the scrollable areas via `flex: 1`.

### What Was NOT Changed

- No architectural changes — PowerShell windows, file-based logs, and 500ms polling preserved
- The inspector grid row allocation (`minmax(380px, 1.4fr)` for terminals, `minmax(80px, 0.3fr)` for lower panels) unchanged
- Backend (server.py) unchanged
- VS Code extension unchanged
- No HTML changes needed

### Test Results

```
py -3 -m pytest -q
132 passed, 4 warnings in 16.49s
```

## Round 20: CLI Exit Sentinel Coverage (P2-1 Fix)

### Summary

Ensured every termination path in the PowerShell launcher script writes the `CLI exit code: N` sentinel. Previously, the command-not-found branch (exit 127) and the `catch` block (exception) both exited or continued without appending the sentinel, so the SSE stream never emitted an `exitCode`, metadata reported `finished=false`, and the frontend never prompted the user to advance the task.

### Root Cause (P2-1)

The launcher script (`generate_launcher_script()` in `cli_window.py`) had two termination paths that skipped the sentinel:

1. **Command-not-found** (line 291-296): `if (-not (Get-Command $CommandName ...))` wrote an error message to the log, then `exit 127` — without writing `` `nCLI exit code: 127 ``.
2. **Catch block** (line 341-344): `catch { Write-Host ...; Add-Content ... $_.Exception.Message }` logged the exception but never wrote the sentinel. The script then fell through to the `Read-Host` prompt and exited without the sentinel in the log.

### Changes

**`gui/orchestrator/cli_window.py`**:

- **Command-not-found path**: Added `Add-Content -LiteralPath $LogFile -Value ("\`nCLI exit code: 127") -Encoding UTF8` before the `Read-Host` / `exit 127`. The backend parser now detects the sentinel and the frontend shows the completion prompt.
- **Catch block**: Added `if ($null -eq $Code) { $Code = 1 }` guard (handles exceptions thrown before `$Code` was assigned) followed by `Add-Content -LiteralPath $LogFile -Value ("\`nCLI exit code: {0}" -f $Code) -Encoding UTF8`. If the exception occurred after `$Code` was set (e.g., in the output-missing check), the original exit code is preserved; otherwise it defaults to 1.

**`tests/test_cli_window.py`**:

| Test | Change |
|---|---|
| `test_generate_claude_launcher_inside_task_dir` | Added assertions for `` `nCLI exit code: 127 `` (command-not-found sentinel) and `$null -eq $Code` (catch block guard) |
| `test_generate_codex_launcher_mentions_output_file` | Same two assertions added |

### What Was NOT Changed

- No architectural changes — PowerShell windows, file-based logs, and 500ms polling preserved
- Backend sentinel parsing (`_terminal_metadata()`, `_terminal_stream()`) unchanged
- Frontend (CSS/JS) unchanged
- VS Code extension unchanged
- No changes to task state machine or CLI window launching API

### Test Results

```
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
58 passed, 1 warning in 3.78s
```

```
py -3 -m pytest -q
132 passed, 4 warnings in 14.95s
```
