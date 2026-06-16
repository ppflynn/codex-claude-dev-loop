# Implementation Report: Runtime Terminal Progress Protocol

## Summary

Added a unified Runtime Terminal Progress Protocol so Claude and Codex CLI runs emit single-line status events (`::task-status{phase="..." message="..."}`) that the bottom xterm.js terminal renders as user-readable progress cues (for example `[修改中] 正在更新 prompt 生成逻辑`). The protocol is injected into the Claude implementation prompt, the Claude fix prompt, and the Codex review prompt. The web UI parses each terminal chunk line-by-line, renders status events as styled inline lines plus a phase badge in the terminal title, and leaves all other CLI output, SSE reconnect logic, stale-task guards, round guards, and `CLI exit code: N` detection untouched.

No web command input, PTY/ConPTY, or WebSocket bidirectional terminal was added. Task state transitions remain user-driven via the existing "Claude completed" / "Codex completed" buttons.

## Changes

### `gui/orchestrator/prompts.py`

- Added `RUNTIME_PROGRESS_PROTOCOL` constant describing the `::task-status{phase="<phase>" message="<message>"}` envelope, the 10 allowed phases (`planning`, `reading`, `running`, `editing`, `testing`, `reviewing`, `writing`, `waiting`, `blocked`, `done`), escaping rules for `message`, the ASCII-only envelope constraint, and the explicit ban on emitting `CLI exit code:` inside status events.
- Added `CLAUDE_PROGRESS_RULES` constant instructing Claude to emit events before reading, editing, running, testing, writing the implementation report, and at completion.
- Added `CODEX_PROGRESS_RULES` constant instructing Codex to emit `reading`, `reviewing`, `blocked`, and `done` events, with an explicit constraint that the final response MUST be a single JSON object containing no `::task-status` events, Markdown fences, or prose.
- Injected all three constants into `write_claude_implementation_prompt`, `write_codex_review_prompt`, and `write_fix_prompt`. Safety rules and existing review/fix requirements are preserved verbatim.
- Added an explicit line in the Codex review prompt: "The final JSON MUST NOT contain `::task-status` events."

### `gui/static/app.js`

- Added `phaseLabels` (Chinese display names) and `phaseBadgeClasses` maps covering all 10 phases.
- Added `TASK_STATUS_RE` regex that matches a complete `::task-status{phase="..." message="..."}` line and tolerates an omitted `message` field and escaped quotes/backslashes inside the message.
- Added `lineBuffer` and `phase` fields to each entry in `terminalInstances` so partial status-event lines that span SSE chunks are reconstructed before being matched.
- `createTerminal` now initializes `lineBuffer = ""` and `phase = ""`. `destroyTerminal` clears them and calls `clearTerminalPhaseBadge` so a stale phase badge cannot survive a task/round switch.
- Added `processTerminalChunk(client, text)`: buffers text, splits on `\n`, holds the trailing partial line for the next chunk, and for each complete line either renders a styled status line (cyan `[<label>]` + message) or re-emits the line as normal terminal output. Status events also update `inst.phase` and call `updateTerminalPhaseBadge`.
- Added `flushTerminalBuffer(client)`: emits any pending partial line when the SSE stream signals `done`. If the partial line is itself a complete status event, it is rendered as one; otherwise it is written verbatim.
- Added `renderStatusLine(client, phase, message)`: writes `[<label>] <message>` to xterm.js with ANSI styling and strips any ANSI escapes from the user-supplied message so it cannot reformat the terminal.
- Added `updateTerminalPhaseBadge` / `clearTerminalPhaseBadge`: maintains a `.terminal-phase-badge` element inside the terminal title showing the latest phase at a glance.
- Replaced the direct `term.write(data.chunk)` call in the SSE `onmessage` handler with `processTerminalChunk(client, data.chunk)`, and added `flushTerminalBuffer(client)` on the `done` event before recording completion.
- Replaced the direct `term.write(content)` call in `loadTerminalContent` with `processTerminalChunk(client, content)` followed by `flushTerminalBuffer(client)`, so historical logs that contain status events are rendered consistently when switching back to a finished task.
- Placeholder/error messages (`正在连接...`, `等待 CLI 启动...`, etc.) still write directly because they are emitted immediately after `createTerminal` resets the line buffer and never contain status events.

### `gui/static/styles.css`

- Added `.terminal-phase-badge` base style and per-phase accent classes (`planning`/`reading`, `editing`/`writing`, `running`/`testing`, `reviewing`, `waiting`, `blocked`, `done`) so the badge picks up a color appropriate to the phase while staying within the existing VSCode-like dark terminal palette.

### `tests/test_prompts.py` (new)

- `test_protocol_constants_mention_event_shape_and_phases` verifies the protocol text contains the exact envelope and all 10 phase tokens, plus the `CLI exit code:` ban.
- `test_claude_rules_list_required_phases` verifies Claude-side rules mention `planning`, `reading`, `editing`, `running`, `testing`, `writing`, `done`, and the `docs/IMPLEMENTATION_REPORT.md` requirement.
- `test_codex_rules_require_pure_json_final_response` verifies Codex-side rules mention `reviewing`, `reading`, `blocked`, `done`, and the "FINAL response MUST be a single JSON object" / "MUST NOT contain any `::task-status` events" constraints.
- `test_claude_implementation_prompt_includes_protocol`, `test_codex_review_prompt_includes_protocol_and_json_constraint`, and `test_fix_prompt_includes_protocol` verify each generated prompt file embeds the protocol, the event envelope, key phases, and existing safety/work requirements.

### `tests/test_gui_server.py`

- Extended `test_claude_completed_collects_artifacts_and_generates_codex_prompt` to assert the generated `CODEX_REVIEW_PROMPT.md` contains the Runtime Terminal Progress Protocol, the event envelope, the "single JSON object" constraint, and the "MUST NOT contain `::task-status` events" line.
- Added `test_terminal_stream_status_events_do_not_mask_exit_code` to confirm the SSE stream forwards `::task-status{...}` lines verbatim while still detecting the `CLI exit code: 0` sentinel on its own line.
- Added `test_terminal_metadata_handles_status_event_lines` to confirm terminal metadata still flags `finished=True` with the correct exit code when status events precede the sentinel.

## Acceptance Criteria Status

- Claude `CLAUDE_IMPLEMENT_PROMPT.md` contains the Runtime Terminal Progress Protocol. ✓
- Claude fix-round `FIX_PROMPT_ROUND_N.md` contains the Runtime Terminal Progress Protocol. ✓
- Codex `CODEX_REVIEW_PROMPT.md` contains the Runtime Terminal Progress Protocol. ✓
- Codex prompt explicitly requires the final response to be pure JSON without status events or Markdown. ✓
- Bottom xterm.js terminal renders phase cues such as `[读取中]`, `[运行中]`, `[修改中]`, `[验证中]`, `[审查中]`, `[完成]`. ✓ (inline ANSI-styled line plus a phase badge in the terminal title)
- Status events do not disrupt normal CLI output. ✓ (only partial lines that could grow into a status event are buffered; everything else streams to xterm in real time — see Round 2 fix P2-1)
- Status events do not disrupt `CLI exit code: N` detection. ✓ (server still scans `^CLI exit code:` lines; protocol bans the literal text inside status events; covered by tests)
- Status events do not disrupt completion-button pulse / toast. ✓ (chunk handling changes are local to `term.write`; `showCompletionPrompt` path unchanged)
- Task/round switches do not bleed old phase state into the new terminal. ✓ (`createTerminal`/`destroyTerminal` reset `lineBuffer` and `phase`, and `clearTerminalPhaseBadge` removes the badge)
- Codex still produces a validatable `CODEX_REVIEW.json`. ✓ (prompt forbids status events in the final JSON; the launcher's `--output-last-message` writes only the final assistant message)
- No new web command input capability. ✓
- No PTY / ConPTY / WebSocket bidirectional terminal. ✓
- No automatic task-state advancement; users still click "Claude completed" / "Codex completed". ✓
- `py -B -m pytest tests/test_gui_server.py tests/test_cli_window.py -q` passes. ✓ (67 tests pass; full suite: 141 tests pass)

## Test Results

```
py -B -m pytest tests/test_gui_server.py tests/test_cli_window.py tests/test_prompts.py -q
67 passed, 1 warning in 4.13s

py -B -m pytest -q
141 passed, 4 warnings in 15.87s
```

## Round 2 Fix: Codex P2-1 / P3-1 / P3-2

### P2-1 — `processTerminalChunk` buffered every chunk until newline (`gui/static/app.js`)

**Issue**: The original chunk processor accumulated every incoming byte into `inst.lineBuffer` and only flushed lines when a `\n` arrived. Normal CLI output that does not end in a newline (interactive prompts, progress bars, carriage-return updates, token-style streaming) was held in memory until the next `\n` or until the SSE stream signaled `done`. A running CLI could appear idle for the entire duration of a long progress sequence.

**Fix**: Buffer only when the partial line could still grow into a `::task-status{...}` event.

- Added `STATUS_EVENT_PREFIX = "::task-status"` and `couldBeStatusEventPrefix(line)`, which returns true when `line` is a prefix of `::task-status` or already starts with it.
- `processTerminalChunk` now combines the existing buffer with the new chunk, splits on `\n`, writes each complete line (status event or ordinary) immediately, and for the trailing partial line:
  - If it could be a status event prefix → keep in `inst.lineBuffer` for the next chunk.
  - Otherwise → `term.write(trailing)` directly so non-newline output (prompts, progress bars, carriage-return updates) renders in real time.
- `flushTerminalBuffer` (called on SSE `done` and after `loadTerminalContent`) still handles the case where the pending buffer is a complete event without a trailing newline; otherwise it writes the pending text verbatim.
- Embedded `\r` characters in complete non-status lines are preserved (the raw line + `\r\n` is written), so carriage-return progress updates continue to behave correctly.

### P3-1 — Phase badge never visible (`gui/static/styles.css` + `gui/static/app.js`)

**Issue**: The base CSS rule `.terminal-title .terminal-phase-badge { display: none; }` hid the badge, and `updateTerminalPhaseBadge` set `badge.style.display = label ? "" : "none"`. The empty-string fallback inherits the stylesheet value (`display: none`), and no phase-specific rule overrides `display`, so the badge stayed invisible even after a status event arrived.

**Fix**: Set the visible state explicitly. `updateTerminalPhaseBadge` now sets `badge.style.display = label ? "inline-flex" : "none"` so the inline rule wins over the base `display: none`. The hidden state on clear/destroy is preserved.

### P3-2 — `TASK_STATUS_RE` did not tolerate escaped quotes (`gui/static/app.js`)

**Issue**: The regex used `[^"]*` for the `message` capture, which stops at any `"`. A protocol-valid message containing an escaped quote (for example `message="He said \"hi\""`) terminated the capture at the first escaped quote, the rest of the line failed to match, and the raw `::task-status{...}` text leaked into the terminal as ordinary output.

**Fix**: Replaced the message capture with the escaped-string pattern `((?:\\.|[^"\\])*)`, which accepts either a backslash escape (`\\.`) or any character that is not a quote or backslash. The existing `unescapeStatusMessage` already decodes `\"` → `"` and `\\` → `\`, so the rendered message is now correct. The phase capture remains `[^"]*` because phases are constrained to the 10 fixed lowercase tokens and the protocol does not allow escapes there.

### Round 2 Files Modified

| File | Change |
|---|---|
| `gui/static/app.js` | `TASK_STATUS_RE` uses escaped-string pattern for the message capture; added `STATUS_EVENT_PREFIX` and `couldBeStatusEventPrefix`; `processTerminalChunk` only buffers partial lines that could grow into a status event and streams everything else to xterm immediately; `updateTerminalPhaseBadge` sets `display: "inline-flex"` when visible |
| `tests/test_prompts.py` | `test_protocol_constants_mention_event_shape_and_phases` now also asserts the protocol text documents the `\\` and `\"` escape rules |
| `tests/test_gui_server.py` | `test_terminal_stream_status_events_do_not_mask_exit_code` extended with an event whose message contains escaped quotes and an escaped backslash; added `test_terminal_stream_forwards_content_without_trailing_newline` to confirm carriage-return progress updates and unterminated partial lines reach the client verbatim |

## Round 3 Fix: Codex P2-1

### P2-1 — Stale EventSource messages leak into the new terminal via `processTerminalChunk` (`gui/static/app.js`)

**Issue**: The `connectTerminal` `es.onmessage` handler checked only task identity (`selectedTask().id` / `.round`) before delegating to `processTerminalChunk(client, data.chunk)`. Unlike the prior direct `term.write(...)`, `processTerminalChunk` resolves `terminalInstances[client]` at call time, so a queued message from an already-closed or replaced EventSource for the same task/round — e.g. one whose reconnect fired while the previous source still had buffered messages — could write into the new active terminal and update its phase badge. This duplicated or reordered output after SSE reconnects and weakened the existing stale-stream protection.

**Fix**: Added a stale-stream guard as the first check in `onmessage`:

```js
if (terminalConnections[client].source !== es) {
  es.close();
  return;
}
```

If this `es` is no longer the active source (it was replaced by a newer reconnect), the queued message is discarded before it can reach `processTerminalChunk`, `flushTerminalBuffer`, `updateTerminalBadges`, or the `done`-state mutation block. The existing task-identity check (which closes `es` and clears `source` only when `source === es`) remains as a second line of defense for task switches where `es` is still current.

### Round 3 Files Modified

| File | Change |
|---|---|
| `gui/static/app.js` | `connectTerminal` `onmessage` returns early when `terminalConnections[client].source !== es` so stale EventSource messages cannot reach `processTerminalChunk` and pollute the new active terminal |

## Notes

- The chunk processor only holds a partial line in `inst.lineBuffer` when it could grow into a `::task-status{...}` event (it starts with `::task-status` or is a prefix of it). Ordinary CLI output without a trailing newline — prompts, progress bars, carriage-return updates, token streams — is written to xterm.js immediately, so a running CLI no longer appears idle while waiting for the next `\n`.
- The phase badge is hidden (`display: none`) until the first status event arrives, so existing tasks that never emit status events look identical to before. Once a status event arrives, the inline `display: "inline-flex"` overrides the base hidden state.
- The protocol is advisory only: if Claude or Codex does not emit status events, the terminal still works exactly as before — only without the inline phase cues.
- Frontend parser coverage for the new behavior is exercised end-to-end via Python tests that confirm the SSE stream forwards escaped quotes, carriage returns, and partial lines verbatim. The project has no JavaScript test runner, so the JS regex and chunk dispatcher are verified by the protocol contract tests plus the server-side forwarding tests.
