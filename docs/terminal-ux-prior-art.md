# Terminal UX prior art for quest-ai-runner (QAR)

Research date: 2026-08-12. Scope: terminal/CLI UX only (not agent logic) of open-source AI coding/tasking harnesses, plus the framework primitives that implement the patterns.

> **Superseded in part (2026-08-14):** this research was written while QAR still shipped two chat
> UIs, and its `prompt_toolkit`/ANSI sections weigh what to do about the second one. That question
> is now settled: the ANSI renderer was **removed**, and Textual is the only chat UI. Read the
> `prompt_toolkit`/ANSI-path passages below as recorded prior art, not as guidance for current
> work. Everything about the Textual side, and about how the other harnesses solve P1-P5, still
> applies.

QAR problems this is aimed at:
- **P1** input box not reliably pinned to the bottom as content scrolls
- **P2** background log/diagnostic noise leaking into the visible transcript
- **P3** no/unclear mid-turn input queuing ("keep typing while the AI works")
- **P4** spinner/cursor-math desync ("status line prints a new line instead of updating in place")
- **P5 (added mid-task)** we want *pluggable execution backends* — Claude Code as one option among several — and we would rather fork/import than build.

---

## 1. Aider (Python + prompt_toolkit + rich) — the closest analog to QAR's ANSI path

Repo: https://github.com/Aider-AI/aider · License: **Apache-2.0** (matters for vendoring, see §Fork/Import).

### 1.1 Fixed/docked input — aider does NOT do this, and that's a finding

Aider uses `prompt_toolkit.PromptSession.prompt()` in **plain inline mode**. There is no full-screen `Application`, no `Layout`/`HSplit`, no `bottom_toolbar`, no `rprompt`. Construction is once in `InputOutput.__init__` (`aider/io.py` ~L345):

```python
session_kwargs = {
    "input": self.input, "output": self.output,
    "lexer": PygmentsLexer(MarkdownLexer),
    "editing_mode": self.editingmode,
}
if self.input_history_file is not None:
    session_kwargs["history"] = FileHistory(self.input_history_file)
self.prompt_session = PromptSession(**session_kwargs)
```

and per turn (`InputOutput.get_input` ~L656):

```python
line = self.prompt_session.prompt(
    show, default=default, completer=completer_instance,
    reserve_space_for_menu=4,
    complete_style=CompleteStyle.MULTI_COLUMN,
    style=style, key_bindings=kb,
    complete_while_typing=True,
    prompt_continuation=get_continuation,
)
```

So aider's prompt is simply "the last line in the scrollback" — it is bottom-anchored *because nothing prints after it*, not because it's docked. **Aider does not solve P1; it dissolves P1** by never having concurrent output. Notable detail: `reserve_space_for_menu=4` reserves 4 lines below the input so the completion popup can't collide with / scroll the prompt line.

Also: `patch_stdout` is **never used anywhere in aider** (grep-confirmed). Aider's protection is structural — the spinner and the markdown `Live` are both explicitly torn down *before* control returns to `prompt()`, so the two rendering surfaces are temporally exclusive by construction.

Terminal capability gate: `is_dumb_terminal()` from `prompt_toolkit.output.vt100` disables `fancy_input` (PromptSession) **and** `pretty` (rich) together as a single all-or-nothing decision (io.py ~L339) — no half-fancy states, which kills a whole bug class.

### 1.2 Streaming without corrupting scrollback — `aider/mdstream.py` (the single most stealable file)

This is the direct answer to P4. `MarkdownStream` splits rendered output into **stable lines committed permanently to real scrollback** and an **unstable tail kept inside a `rich.Live` region**. Verbatim docstring:

> Splits the output into "stable" older lines and the "last few" lines which aren't considered stable. They may shift around as new chunks are appended to the markdown text. The stable lines emit to the console above the Live window. The unstable lines emit into the Live window so they can be repainted. **Markdown going to the console works better in terminal scrollback buffers. The live window doesn't play nice with terminal scrollback.**

Key state and the loop:

```python
class MarkdownStream:
    live = None
    when = 0
    min_delay = 1.0 / 20    # 20fps cap
    live_window = 6         # lines kept volatile at the bottom
    def __init__(self, mdargs=None):
        self.printed = []   # lines already committed to scrollback
        self.live = None; self._live_started = False
```

```python
lines = self._render_markdown_to_lines(text)   # re-render ALL text so far
self.min_delay = min(max(render_time * 10, 1.0 / 20), 2)   # adaptive throttle
num_lines = len(lines)
if not final:
    num_lines -= self.live_window
if final or num_lines > 0:
    show = lines[len(self.printed):num_lines]
    self.live.console.print(Text.from_ansi("".join(show)))  # -> REAL scrollback
    self.printed = lines[:num_lines]
if final:
    self.live.update(Text("")); self.live.stop(); self.live = None; return
self.live.update(Text.from_ansi("".join(lines[num_lines:])))  # only the tail repaints
```

Points worth internalising:
- `Live` is **lazily created on first `update()`** (`_live_started`), so the spinner owns the terminal until real content arrives — no two live regions at once.
- Only `refresh_per_second` is passed to `Live()`; no `vertical_overflow` tuning.
- Rendering is via a private `Console(file=StringIO(), force_terminal=True)` so ANSI styling survives as text and lines can be sliced.
- **Adaptive throttle**: `min_delay = min(max(render_time*10, 1/20), 2)`. Because it re-renders the *whole* accumulated text every frame (O(n²)-ish over a long response), the delay self-tunes up to 2s when rendering gets expensive. Commit message for this is literally "delay rendering md when it gets slow" (`3fc5cf8b`/`891868b0`/`15430991`, 2025-01-07).
- `__del__` safety net calls `self.live.stop()` in a `try/except`.
- **No SIGWINCH/resize handling at all** — every frame re-renders from scratch, so resize just reflows on the next tick.

### 1.3 Log/diagnostic noise routing (P2)

Aider does **not** use stdlib `logging` in its UI path at all. Noise suppression is concentrated in `aider/llm.py`:

```python
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
os.environ["LITELLM_MODE"] = "PRODUCTION"
...
self._lazy_module = importlib.import_module("litellm")
self._lazy_module.suppress_debug_info = True
self._lazy_module.set_verbose = False
self._lazy_module.drop_params = True
self._lazy_module._logging._disable_debugging()   # private API, deliberate
```

Three layers: (a) lazy import of litellm (also a 1.5s startup win), (b) belt-and-suspenders suppression including reaching into litellm's *private* `_logging._disable_debugging()` because `suppress_debug_info` alone is known-insufficient upstream, (c) `--verbose` gates only aider's **own** diagnostics, and is deliberately *not* wired to litellm verbosity.

Separately, `InputOutput.chat_history_file` is an append-only markdown transcript written independently of terminal rendering — so a rendering bug never loses the record.

### 1.4 Mid-turn input (P3) — aider does NOT support it

Important negative result: aider's I/O is single-threaded and synchronous. `send_message()` blocks until the stream completes; `get_input()` is only called after. **No background keystroke reader, no follow-up queue.** If you assumed aider solved concurrent typing, it didn't — it sidesteps it by making generation blocking.

Interrupt handling is good though:
```python
except KeyboardInterrupt:
    interrupted = True
    break
...
if interrupted:
    self.cur_messages[-1]["content"] += "\n^C KeyboardInterrupt"
    self.cur_messages += [dict(role="assistant",
        content="I see that you interrupted my previous reply.")]
```
An interrupted stream is written into conversation history as an explicit marker rather than silently dropped — so the next turn stays coherent.

Double-Ctrl-C to exit, with a telling defensive line:
```python
def keyboard_interrupt(self):
    Console().show_cursor(True)   # spinner hid the cursor; Ctrl-C may skip its cleanup
```
That single line is evidence of a real bug class: any spinner that calls `show_cursor(False)` will leave a hidden cursor if interrupted before its cleanup runs. Fix it unconditionally at the interrupt handler, not only in the spinner's `finally`.

### 1.5 Spinner (`aider/waiting.py`) — how to not print new lines

- Pure `\r` + backspace overwrite. **No ANSI cursor-position codes at all.** This is a deliberate robustness choice: `\r`/`\b` can't desync the way absolute cursor addressing can.
- Clips to `console.width - 2` to avoid wrap-induced corruption on narrow terminals.
- **500ms grace delay** before the spinner becomes visible — a fast response never flashes a spinner.
- Frame rate capped at 10Hz.
- Unicode support probed once via a real write/backspace/erase round-trip in `try/except UnicodeEncodeError`, falling back to ASCII frames.
- `WaitingSpinner` is a daemon-thread wrapper; `.start()`/`.stop()` are idempotent; `.stop()` joins with timeout then force-calls `spinner.end()` regardless, so the line always gets cleared even if the thread is slow. Also a context manager.
- Spinner ownership is **centralized** in `base_coder.py` (`_stop_waiting_spinner()`), called both the instant the first content chunk arrives and unconditionally in `finally:`.

### 1.6 Documented bug/fix history (the gold)

- `8e64f171` "refactor: improve markdown streaming with stable/unstable line handling" (2025-01-07) — origin of the split in §1.2. Before it, aider live-rendered everything, which "doesn't play nice with terminal scrollback."
- `3fc5cf8b` / `891868b0` / `15430991` (2025-01-07) — the adaptive throttle; bug was that a big/slow markdown render (large fenced code block) made the live loop itself the bottleneck → visible stutter.
- `e1ab9cc0` "fix: Correct spinner erasure and update animation" (2025-05-08) — an **off-by-one backspace** left a stray trailing character when the spinner shrank/stopped:
  ```diff
  -  num_backspaces = max(0, num_backspaces)
  +  num_backspaces = max(0, num_backspaces) + 1
  ```
  Also bumped spin interval 0.1 → 0.15s to reduce flicker/CPU.
- `6e1327f6` / `21a05ead` / `befff1f2` (2025-05-08) — spinner logic extracted to its own module and centralized in `BaseCoder`, after duplicated start/stop logic caused double-spinner / no-spinner races.
- Issue [#3889](https://github.com/Aider-AI/aider/issues/3889) — Ctrl-C during a **nested** `confirm_ask()` prompt escapes uncaught, because the global handler intentionally ignores `KeyboardInterrupt` and that call site had no local `try/except`. Directly applicable: every secondary/confirmation `prompt()` needs its own guard.
- Issue [#2538](https://github.com/Aider-AI/aider/issues/2538) / [#3104](https://github.com/Aider-AI/aider/issues/3104) — the spinner was retrofitted under user pressure; the original design had dead silence during LLM latency.
- No maintainer blog post on terminal internals exists; the rationale lives in code comments and commit messages quoted above.

### 1.7 Other

- Thinking vs answer is **not** a separate UI region — reasoning is wrapped in a synthetic `<REASONING_TAG>` inside the *same* markdown stream and post-processed by `replace_reasoning_tags()` before `MarkdownStream.update()`.
- Diffs and applied-edit summaries are plain `console.print()` scrollback text, not live widgets. Only the two genuinely dynamic surfaces (spinner, streaming markdown) get live treatment. Worth copying as a simplicity rule.
- Multiline is a *mode toggle* that flips which of Enter / Alt-Enter submits, plus a legacy `{...}` fenced-paste syntax.
- `AutoCompleter` wraps in `ThreadedCompleter` so file tokenization never blocks the UI; word completion only fires after 3+ characters.

Files: [`aider/io.py`](https://github.com/Aider-AI/aider/blob/main/aider/io.py), [`aider/mdstream.py`](https://github.com/Aider-AI/aider/blob/main/aider/mdstream.py), [`aider/waiting.py`](https://github.com/Aider-AI/aider/blob/main/aider/waiting.py), [`aider/llm.py`](https://github.com/Aider-AI/aider/blob/main/aider/llm.py), [`aider/coders/base_coder.py`](https://github.com/Aider-AI/aider/blob/main/aider/coders/base_coder.py).

---

## 2. OpenCode — disambiguation first

There is **one lineage with a mid-2025 split**, not two unrelated projects:

1. **Origin (mid-2025)**: `opencode-ai/opencode` — Go CLI with a Bubble Tea TUI. Created primarily by **Kujtim Hoxha**; **Dax Raad** and **Adam Doty** (SST / Anomaly Innovations) drove the `opencode.ai` brand and distribution.
2. **The split**: Charm hired Kujtim Hoxha and tried to move the project under the Charm org. Raad/Doty controlled the brand and objected. Resolution: **Charm renamed its continuation "Crush"** (`charmbracelet/crush`, Go/Bubble Tea, kept the original codebase), and the **SST side kept the "OpenCode" name and rewrote in TypeScript/Bun** (`sst/opencode`, relaunched 2025-06-19; also published under the `anomalyco` org).
3. So: `opencode-ai/opencode` → **`charmbracelet/crush`** (Go, Charm's continuation) **and** **`sst/opencode`** (TS/Bun, kept the name).
4. Any other repo literally named "opencode" is almost certainly an old fork/mirror, not a maintained lineage.

Sources: [crush#1097 "Any chance you can dispel confusion around opencode?"](https://github.com/charmbracelet/crush/issues/1097), [BigGo writeup](https://biggo.com/news/202507310715_Charm_Crush_AI_Coding_Agent), [tessl.io on Crush](https://tessl.io/blog/does-developer-delight-matter-in-a-cli-the-case-of-charm-s-crush/).

### 2.1 sst/opencode terminal UX

**The architecturally interesting bit: a real client/server split.** The agent core runs as a **Bun/TypeScript HTTP server**; the terminal UI is a *separate client* that talks to it over HTTP via the generated `@opencode-ai/sdk` (OpenAPI-derived). The client "never touches LLM APIs directly; its only job is to render the interface and forward input to the server." That's why the same server drives a TUI, a desktop app, IDE extensions, and CI runners. **This is directly relevant to QAR's multi-backend goal** (see §7).

Currency caveat: the TUI was originally Go/Bubble Tea; secondary sources report that as of v1.0 it was replaced with an in-house TS/Zig framework called **OpenTUI**. Verify against the current repo before relying on "it's Bubble Tea."

- **Docked input**: `BottomPane`/composer. A `PromptProvider` owns composer state (text, attachments, `@`-referenced context); `sendFollowupDraft` converts it to `requestParts` for the server. Slash-prefixed input is intercepted client-side and routed to `client.session.command`.
- **Streaming**: optimistic-update store. `applyOptimisticAdd` renders the user's message immediately client-side; `mergeOptimisticPage` reconciles/dedupes against server-confirmed messages. Streaming state lives in a **client-side store separate from the confirmed transcript** — no mutation of already-drawn cells.
- **Logs (P2)**: `~/.local/share/opencode/log/*.log`, timestamped, most recent 10 kept. `opencode --log-level DEBUG`; `opencode debug paths` prints every config/log/data path. Known rough edge: [anomalyco/opencode#6583](https://github.com/anomalyco/opencode/issues/6583) — `--log-level DEBUG` can paradoxically suppress the log file.
- **Mid-turn queuing**: not documented in what could be retrieved. Treat as unconfirmed, not absent.
- **Standout patterns**:
  - **Leader-key scheme** to dodge terminal key conflicts: default leader `ctrl+x`, then a second key (`ctrl+x n` = new session), with configurable `leader_timeout` (default 2000ms). Fully remappable via `tui.json`. But frequently-used actions (`session_child_cycle`) got bare `left`/`right` bindings *specifically because leader-prefixed navigation was too slow* — a nice "bind by interaction frequency" principle.
  - **`/share`** — shareable link to the live conversation, copied to clipboard, private by default.
  - **`/undo` / `/redo`** — reverts actual **file edits**, not just chat turns; chainable.
  - **Plan vs Build mode**, toggled with `Tab`, shown as a corner indicator.
  - `@`-fuzzy file reference in the composer.
  - LSP diagnostics fed back to the agent + shown in a right panel, though flaky ([#12288](https://github.com/anomalyco/opencode/issues/12288), [#9027](https://github.com/anomalyco/opencode/issues/9027)).

Docs: [opencode.ai/docs](https://opencode.ai/docs/), [keybinds](https://opencode.ai/docs/keybinds/), [DeepWiki TUI bootstrap](https://deepwiki.com/sst/opencode/6.3-tui-application-bootstrap).

---

## 3. Crush (charmbracelet) — Go + Bubble Tea v2. The richest single source of portable patterns.

Repo: https://github.com/charmbracelet/crush · **License: FSL-1.1-MIT** (Functional Source License, converts to MIT after an embargo). **This is a real fork blocker** during the embargo window — it restricts building competing offerings from the code. Study only.

Currency note: there is **no `internal/tui/` anymore** — it's `internal/ui/`, and the stack moved to **Bubble Tea v2 / Bubbles v2 / Lip Gloss v2** (module path `charm.land/bubbletea/v2`) plus a new low-level primitives library, **`ultraviolet`**.

### 3.1 Docked bottom input — constraint layout, not string joining

Crush is **always a full-screen alt-screen app**. `internal/ui/model/ui.go`'s `View()` unconditionally sets `v.AltScreen = true`. (`tui.transparent` only controls whether the background is painted opaque; it does *not* toggle inline mode.)

The docking is **not** `lipgloss.JoinVertical` over strings. It's constraint-based layout (`ultraviolet`'s `layout` package, Cassowary algorithm) producing `image.Rectangle` regions handed to each component's `Draw(scr, area)`. From `generateLayout(w, h int)` (~L3458), recomputed on every `tea.WindowSizeMsg`:

```go
editorHeight := m.textarea.Height() + editorHeightMargin   // fixed size
layout.Vertical(
    layout.Len(mainRect.Dy()-editorHeight),   // chat area: computed remainder
    layout.Fill(1),
).Split(mainRect).Assign(&mainRect, &editorRect)
```

**The portable rule**: give the input a **fixed height** driven by its own `Height()` (Crush's textarea grows 3–15 lines, `TextareaMinHeight`/`TextareaMaxHeight = 3/15`), give the transcript **flex-fill**, and re-split on every resize. This is exactly Textual's `dock: bottom` + `height: 1fr` idiom — Textual gets it for free where Crush had to build it.

Component split:
- Input: `charm.land/bubbles/v2/textarea` — a real Bubbles component.
- Transcript: **NOT `bubbles/viewport`** — zero `viewport` imports in the whole tree. They wrote their own lazy scroller, `internal/ui/list/list.go`. Per `internal/ui/AGENTS.md`: "List renders only visible items (lazy evaluation); no list-level cache exists — items must cache internally," and "`list.TotalHeight` is expensive (renders all items); use bounded `list.Overflows` instead." A deliberate departure from the stock viewport for chat-transcript scale.

### 3.2 Streaming without corrupting scrollback — confirms the structural argument

Crush's own [`internal/ui/AGENTS.md`](https://raw.githubusercontent.com/charmbracelet/crush/main/internal/ui/AGENTS.md) states the policy:

> "This codebase uses a hybrid rendering system combining screen-based and string-based approaches. The `UI` model serves as the **sole Bubble Tea component**... Sub-components are **NOT** Bubble Tea models."

Leaf components expose imperative methods and either render to strings or `Draw(scr uv.Screen, area uv.Rectangle)` into a cell buffer. `View()` builds a `uv.ScreenBuffer`, calls every component's `Draw()`, then flattens via `canvas.Render()`.

**So every frame recomputes the entire screen as a complete cell grid; app code never emits cursor-movement escapes.** The only cursor math happens once, centrally, in Bubble Tea's renderer, by diffing two complete cell buffers. This is the direct confirmation of the structural point: **in a compositor-based framework, "cursor math desync" is not a bug you can write.**

[`ultraviolet`](https://github.com/charmbracelet/ultraviolet) provides "cell-based rendering, cross-platform input handling, and a diffing renderer inspired by ncurses — without the need for `terminfo` or `termcap`... only redraws what changed. Optimizes cursor movement, uses ECH/REP/ICH/DCH when available, and supports scroll optimizations."

Bubble Tea v2's renderer (the **"Cursed Renderer"**, rebuilt on ncurses' diffing algorithm — [charm.land/blog/v2](https://charm.land/blog/v2/), [discussion #1374](https://github.com/charmbracelet/bubbletea/discussions/1374)): "Rendering is faster and more efficient by orders of magnitude... The v2 branches have been powering Crush, our AI coding agent, in production from the very beginning." It also added **synchronized output (terminal Mode 2026)**: "atomically updating the terminal window once all the update sequences are pushed out."

**Streaming markdown specifically** — `internal/ui/chat/streaming_markdown.go` is more careful than "re-render everything per token," because re-parsing full markdown mid-construct is both expensive and *unsafe* (an unclosed code fence). Verbatim comments:
- "`stablePrefix` is always a literal byte prefix of the most recently rendered content."
- "Incremental boundary search: only scan the delta after the stable prefix... The cached cumulative state lets us validate candidates in O(delta) instead of re-scanning."
- **"Fence parity: base count + delta count must be even. Any odd count means we'd be cutting inside a fence"** — it refuses to treat text as a safe boundary while inside an open code fence.
- "No safe boundary anywhere yet. Full render; do not modify the cache (a future flush may find one)."
- "Two renders concatenated are NOT generally equal to a single render" — they know naive splicing is unsound and engineer around it.

Plus a `chatDrawCache` in `chat.go` that "memoizes the decoded form of the last `list.Render` output so repeat frames with byte-identical content skip the per-cell ANSI reparse."

**The fence-parity check is the one detail nobody else has**, and it's the difference between aider's line-count heuristic (`live_window=6`) and a correct safe-boundary detector.

**`tea.Printf`/`tea.Println`** (print above the program, unmanaged, persists across renders) exists — but the docs note "**If the altscreen is active no output will be printed.**" Since Crush is always alt-screen, this primitive is unusable there, which is exactly *why* logs must go to a file (§3.3).

### 3.3 Log routing (P2) — Charm's stated answer to QAR's exact bug

From Bubble Tea's own docs ([`tea.LogToFile`](https://pkg.go.dev/github.com/charmbracelet/bubbletea#LogToFile)):

> "**You can't really log to stdout with Bubble Tea because your TUI is busy occupying that!** You can, however, log to a file..."
> "To see what's being logged in real time, run `tail -f debug.log` while you run your program in another window."

Crush's implementation (`internal/log/log.go`): Go `log/slog` + `lumberjack` rotation:
```go
logRotator := &lumberjack.Logger{
    Filename: logFile, MaxSize: 10 /*MB*/, MaxBackups: 0, MaxAge: 30 /*days*/,
}
```
Plus a `RecoverPanic()` writing timestamped crash dumps `crush-panic-{name}-{timestamp}.log`.

CLI surface:
- Log file at **`./.crush/logs/crush.log`** (project-relative).
- `crush logs` (last 1000 lines), `crush logs --tail 500`, **`crush logs --follow`/`-f`** (live tail via `nxadm/tail`: first reads existing content non-following, then switches to `Follow: true, ReOpen: true, Location: SeekEnd`).
- **Lines are written as JSON (slog); human formatting is applied only at display time.** Machine-structured on disk, readable on demand.
- `--debug` flag or `crushrc` `option debug true` / `option debug-lsp true` bumps the level.
- There is **no in-TUI live log pane** — the "log view" is the separate `crush logs -f` subcommand in another terminal.

Python analog: `logging.handlers.RotatingFileHandler` is the direct `lumberjack` equivalent.

### 3.4 Mid-turn queuing (P3) — a real server-tracked queue

Confirmed in `internal/ui/model/ui.go`. `sendMessage()` marks the agent busy optimistically and fires `AgentRun` fire-and-forget — "it returns once the prompt has been accepted (HTTP 202) or synchronously with a validation or transport error." The comment states the contract:

> "Optimistically mark the agent busy: the prompt we are about to submit **either starts a run or is enqueued behind one.**"

Queue state on the `UI` struct: `promptQueue int`, `promptQueueItems []string`, plus generation counters (`promptQueueGen`) to discard stale off-thread probes. Surfaced as a **pill** in `pills.go`: "Show pending prompt count as 'N Queued' with gradient triangle indicators."

**The escape-key state machine is the most portable single pattern here** (`cancelAgent()`):

```go
// First press sets isCanceling and starts a timer. The second press
// (before the timer expires) actually cancels the agent.
if m.isCanceling {                       // second esc -> really cancel
    m.isCanceling = false
    m.com.Workspace.AgentCancel(m.session.ID)
}
if m.promptQueue > 0 {                   // no run armed, but queued -> clear queue
    m.com.Workspace.AgentClearQueue(m.session.ID)
    m.promptQueue = 0
    return nil
}
m.isCanceling = true                     // first esc -> arm, start disarm timer
return cancelTimerCmd()
```

And the help text adapts live: `"esc" → "press again to cancel"` when armed; `"esc" → "clear queue"` when prompts are queued. Precedence: esc#1 arms a confirm timer → esc#2 within the window cancels; if nothing is running but prompts are queued, a single esc clears the queue.

**The input is never disabled while the agent runs.** Editor bindings (`keys.go`): `enter` sends/enqueues, `shift+enter`/`ctrl+j` newline, `ctrl+o` open `$EDITOR`, `up`/`down` walk prompt history when the cursor is at buffer edges.

### 3.5 Other standout patterns

- **Tool calls** (`chat/tools.go`): collapsible blocks; collapsed view truncates to `responseContextHeight = 10` lines with a hidden-lines counter; status icon per line; spinner runs only while `!toolCall.Finished && status != ToolStatusCanceled`; **permission-denied renders `WARN`, actual failure renders `ERROR`** (a distinction most tools blur); hook indicators show which hooks fired and whether they rewrote output.
- **Diffs** (`internal/ui/diffview/`, 500+ golden test fixtures): unified *and* side-by-side, auto-choosing split when width ≥ 140 cols, with horizontal scroll (`shift+←/→`).
- **Permissions** (`dialog/permissions.go`): modal with three explicit actions — Allow once (`a`), Allow for session (`s`), Deny (`d`/`esc`) — routing to tool-specific renderers (diff for edits, syntax-highlighted command for bash, URL+path for fetch), in a scrollable viewport with a fullscreen toggle (`f`). CLI equivalents in `crushrc`; plus a documented `--yolo` ("Be very, very careful with this feature").
- **Thinking vs answer** (`chat/assistant.go`): genuinely distinct, not just styled. **Separate render caches** for thinking vs content so streaming one doesn't invalidate the other. Bordered `ThinkingBox` style, three collapse states (`thinkingCollapsed` = last 10 lines, `thinkingTailWindow` = last 200, `thinkingFullExpanded`), a `"… (%d lines hidden) [click or space to expand]"` affordance, and a `"Thought for <duration>"` footer when done.
- **Responsive breakpoints**: compact mode (width < 120 or height < 30) collapses the sidebar to a 1-line header.
- **Multi-client**: sessions carry `IsBusy` and `AttachedClients`, so `crush serve` + multiple TUIs shows who else is attached to the same workspace.
- **Status bar** is deliberately minimal — contextual help plus a 5-second-TTL toast (`ansi.Truncate` for overflow). Model/token/cwd live in the header/sidebar instead.
- **Desktop notifications** (`internal/ui/notification/`): "sent when a tool call requires permission and when the agent finishes its turn... **only sent when the terminal window isn't focused**" — configurable `auto`/`native`/`osc`/`bell`/`disabled`, with an OSC-escape-sequence backend as a cross-platform fallback.
- **Command palette** `ctrl+p`; `@` triggers file-mention completions; `/` triggers slash commands.

---

## 4. Codex CLI (OpenAI) — Rust + ratatui, and the best mid-turn-queuing UX found

### 4.1 Why they rewrote from Ink → Rust/ratatui

Per the public discussion [openai/codex#1174 "Codex CLI is Going Native"](https://github.com/openai/codex/discussions/1174), three drivers — and note that "Ink is broken" was *not* the headline reason:
1. **Zero-dependency install** — requiring Node v22+ was a real adoption blocker.
2. **Performance** — "the Rust runtime's allocator is deterministic; GC pauses do not interrupt streaming output mid-turn," plus near-instant binary startup for `codex exec` fan-out in CI.
3. **Security** — native Rust sandboxing bindings.

Rendering quality was a benefit, not the primary motive. Useful calibration: **don't assume a framework rewrite is the answer to a rendering bug.**

### 4.2 Docked input

`BottomPane` (`codex-rs/tui/src/bottom_pane/mod.rs`) is the interactive footer, owning `ChatComposer` plus an overlay stack for transient popups/modals. Structurally separate from the scrollable history (`ChatWidget`). **Every tool researched converges on this same composer/history split.**

### 4.3 Streaming without corrupting scrollback

ratatui does **immediate-mode, diff-based repainting**: every frame is redrawn from an in-memory buffer, only changed cells are written out, "no retained state to go stale." `ChatWidget` keeps a mutable **`active_cell`** updated in place while a tool call or assistant message streams, rather than re-rendering the whole history per token. ratatui also exposes **`insert_before()`** for genuinely appending to real scrollback ([openai/codex#1247](https://github.com/openai/codex/issues/1247)) — the ratatui analog of Ink's `<Static>`.

**Useful trick**: because diff repaint only touches changed cells, tools scraping Codex over a PTY get partial frames; the workaround is to **force a spurious resize (cols → cols−1 → cols)** so the framework believes dimensions changed and does one full clean repaint. Handy if you ever need a deterministic snapshot of a Textual screen. ([source](https://www.tylercrosse.com/ideas/2026/usage-bar))

### 4.4 Log routing (P2) — three separate channels

- Interactive TUI: `RUST_LOG=codex_core=info,codex_tui=info` → **`~/.codex/log/codex-tui.log`** (tail in a second terminal).
- Non-interactive `codex exec`: `RUST_LOG=error` printed inline to stderr.
- Session transcripts: JSONL at `~/.codex/sessions/YYYY/MM/DD/rollout-<id>.jsonl`, used for resume (replays the transcript rather than restoring in-memory state) and audit.

Three independent streams — live TUI, debug log, durable transcript — never interleaved.

### 4.5 Mid-turn queuing (P3) — "Steer Mode," the clearest example found

Landed as multiple releases in Jan 2026. You can type a follow-up mid-turn, and **the UX distinguishes two intents with two keys**:
- **Enter → interrupt and send immediately** (steer now)
- **Tab → queue** for injection at the next step/tool-call boundary, without interrupting
- **Esc → interrupt the running task** (overloaded system-wide; `Ctrl+C`/`/quit` exits)

Queued items render as a **visible itemized list above the input box**, each with a per-item "inject now" action, and the composer shows explanatory copy: *"Messages to be submitted after next tool call (press esc to interrupt and send immediately)."*

Still maturing: [#28864](https://github.com/openai/codex/issues/28864) (want per-item edit/remove), [#26683](https://github.com/openai/codex/issues/26683) (queued messages get stuck), [#4490](https://github.com/openai/codex/issues/4490) (edit-queued shortcut unreliable on macOS Terminal/iTerm).

### 4.6 Other

- A dedicated `/status` **history cell** — status snapshots (model, cwd, token usage, permissions, rate limits) become permanent scrollback entries, not transient overlays.
- `tui.status_line` config for footer fields, currently plain-text only (open requests for colored/bar rendering: #20140, #27984, #31118).
- A **single unified "task running" indicator** drives both spinner and interrupt hints — deliberately avoiding duplicate progress indicators during multi-step tool/commentary streaming.

---

## 5. Gemini CLI (Google) — Ink, and the `<Static>` pattern precisely

### 5.1 What `<Static>` is and why it matters

Ink's default behavior re-renders the entire component tree on every state change and redraws it in place. Fine for a dashboard, terrible for a growing chat transcript.

`<Static>` **permanently commits its rendered output to real terminal scrollback and then stops touching it.** From [Ink's README](https://github.com/vadimdemedes/ink):

> "`<Static>` component permanently renders its output above everything else. It's useful for displaying activity like completed tasks or logs — things that don't change after they're rendered... `<Static>` only renders new items in the `items` prop and ignores items that were previously rendered."

```jsx
<Static items={tests}>
  {test => <Box key={test.id}><Text color="green">✔ {test.title}</Text></Box>}
</Static>
{/* dynamic content below, re-rendered normally */}
<Box marginTop={1}><Text dimColor>Completed tests: {tests.length}</Text></Box>
```

Net effect: **two rendering regimes side by side.** Finished content graduates into real, un-managed terminal scrollback (cheap, stable, natively scrollable, immune to the framework's cursor math); only the live tail (input box, in-progress message, spinner) stays under live redraw.

**This is structurally the same idea as aider's `MarkdownStream` stable/unstable split (§1.2)** — two independent implementations converging on the same architecture is a strong signal.

### 5.2 Gemini CLI's application

`InputPrompt.tsx` owns the bottom input. `useGeminiStream.ts` runs a `for await` loop over `ServerGeminiEventType.Content`, buffering into `geminiMessageBuffer` and pushing incremental updates via `setPendingHistoryItem()` — the pending item is the live tail. On finalization the turn moves into the `<Static>`-rendered history. A documented **`findLastSafeSplitPoint()`** chunks very large streaming responses before committing, to avoid overflow/coherence issues when a huge block graduates from live to static.

### 5.3 Where it breaks — resize (a warning for QAR)

- [#22615](https://github.com/google-gemini/gemini-cli/issues/22615) "UI duplication and flickering on terminal resize" — Ink's incremental renderer leaves stale diff artifacts (duplicated footers) after dimension changes. **Fix: explicitly call `rerender()` from Ink's `useApp()` hook inside the resize listener.**
- [#21924](https://github.com/google-gemini/gemini-cli/issues/21924) — proposes a `RenderStatic` variant and updating history in **small batches** on resize rather than all at once.
- [#27378](https://github.com/google-gemini/gemini-cli/issues/27378) — scrolling broken when content exceeds terminal height.
- [#18896](https://github.com/google-gemini/gemini-cli/issues/18896) — Windows height-calc glitching.

**Resize is the universal breaking point** across every incremental/diff renderer surveyed. The consistent fix is "force a full repaint on SIGWINCH rather than trusting the diff."

---

## 6. Goose (block/goose) — the deliberate non-TUI

Rust, but **deliberately not a full-screen TUI framework app**: no ratatui, no crossterm widget layer. It's a normal line-editor stack — `rustyline` (input line/history), `cliclack`, `console`, `bat` (syntax highlighting), `indicatif` (spinners/progress), `anstream` (ANSI-aware streaming). A **linear scrollback REPL**, architecturally the opposite of Codex/opencode/Crush.

- **Docked input**: none in the alt-screen sense — a standard readline prompt at the bottom of normal scrollback.
- **Streaming**: no full-screen redraw loop, so streaming is just incremental appends. Nothing above the cursor is ever repainted, so nothing can desync. (The "solve P4 by never repainting" strategy — same as aider.)
- **Logs / sessions**: config at `~/.config/goose/config.yaml`; sessions as JSONL at `~/.config/goose/sessions/<id>.jsonl`; `goose info` prints resolved paths. Logs are separate files.
- **Interrupt**: `Ctrl+C` is **contextually overloaded** — clears the input line if you've typed something, interrupts the in-flight request if the agent is working, exits if the line is empty and nothing is running. `Ctrl+J` inserts a newline (remappable via `GOOSE_CLI_NEWLINE_KEY`). `Ctrl+R` reverse history search. **No mid-turn queuing.**
- **Standout**: `/mode` switches autonomy level (`auto` / `approve` / `chat`) mid-session. **Recipes** — a `recipe.yaml` bundles instructions, required extensions, parameters, retry logic into a shareable rerunnable config, and **`/recipe` generates one from your live session history**. "Turn what I just did interactively into a repeatable definition" is a strong pattern for QAR's tasking use case specifically.

Sources: [DeepWiki block/goose CLI](https://deepwiki.com/block/goose/3.2-command-line-interface), [goose CLI commands](https://goose-docs.ai/docs/guides/goose-cli-commands/).

---

## 7. Cline and Continue — short verdicts

- **Cline** — the bare `cline` command opens "an interactive terminal session" with no documented framework, docking mechanism, or streaming architecture. The genuinely documented mode is **headless**: `--json` (JSON-lines output), piped stdin, redirected stdout, `-y` for full autonomy, designed as a Unix pipe stage (`git diff | cline ...`) for CI. **Verdict: no meaningful terminal-native UX to study**, but the headless JSON-lines contract is a good machine-integration pattern for a scriptable sibling mode. ([docs](https://docs.cline.bot/usage/cli-overview))
- **Continue (`cn`)** — this one *is* a real TUI, and it's **Ink-based**, same family as Gemini CLI and pre-rewrite Codex. Also has `cn -p` headless, `cn serve` (HTTP server mode), `cn remote`. **Verdict: a third Ink data point, but no maintainer writeup about its rendering internals** — presumably the standard Ink pattern from §5, without its own war story. ([DeepWiki](https://deepwiki.com/continuedev/continue/10.1-cli-architecture-and-commands))

---

## 8. Amp (Sourcegraph) — the best-documented queuing/steering model

[ampcode.com/manual](https://ampcode.com/manual). Ink-based TUI (per npm package notes; the manual itself doesn't name the framework).

**Its three-tier steering model is the single clearest UX articulation found anywhere:**

> "If you send a message when the agent is still working, your message is **queued** and will be sent when the agent is done."
> "Press **Enter Enter** to **steer** it sooner, which sends the message when the agent is done with its **current step** (such as a command or thinking block)."
> "Press **Esc Esc** to **forcibly stop** the agent and send your message immediately, when you want to interrupt its work."

So: *queue at end of turn* / *steer at end of current step* / *interrupt now* — three distinct intents on three escalating gestures, all from the same typed message. Compare Codex's two-key model (§4.5); Amp's is the more complete taxonomy.

Other notable bits:
- `↑`/`↓` **navigate queued and previous messages for editing** — queued messages are first-class editable objects.
- `Ctrl+O` command palette; `Ctrl+G` open current prompt in `$EDITOR`; `Ctrl+S` switch agent modes; `Ctrl+R` prompt history; **`Alt+T` expand thinking/tool blocks** (collapsed by default — maps to Textual `Collapsible`); `Ctrl+\` show/hide thread sidebar; `Ctrl+C Ctrl+N` archive thread and start new.
- Keybindings customizable via an `amp.keymap` setting.
- `amp -x/--execute` headless; `--stream-json` emits one object per line on stdout for programmatic monitoring; `--stream-json-thinking` includes thinking blocks; `amp --no-tui` runs a "runner-only" instance that accepts remotely created threads.

---

## 9. Claude Code — the benchmark, including its bug history

Primary source, and unusually forthcoming: **[code.claude.com/docs/en/fullscreen](https://code.claude.com/docs/en/fullscreen)**.

### 9.1 It has TWO renderers — this resolves the inline-vs-alt-screen question

- **Fullscreen renderer** (default for users who started on/after 2026-05-06; `/tui fullscreen`, env `CLAUDE_CODE_NO_FLICKER`): "draws the interface on the terminal's **alternate screen buffer**, like `vim` or `htop`, and only renders messages that are currently visible" (virtualized → flat memory). Consequence, stated explicitly: **"the conversation lives in the alternate screen buffer instead of your terminal's scrollback."** So `Cmd+F`/tmux search don't see it; you use in-app transcript mode (`Ctrl+O`) + `/` search, or **`[` to dump the transcript into native scrollback**. Native click-drag copy is replaced by in-app mouse selection.
- **Classic renderer** (`/tui default`, or force with `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`): "keeps the conversation in your terminal's native scrollback so `Cmd+f` and tmux copy mode work as usual."

**Crucially, on the fixed input bar:** *"The input box stays fixed at the bottom of the screen instead of moving as output streams in. If the input doesn't move while Claude is working, fullscreen rendering is active."* Claude Code uses "input doesn't move" as the **operational definition of fullscreen mode** — which implies the classic/inline renderer's input *does* move with output. **The fixed bottom input QAR is being compared against is specifically the alt-screen mode.**

- **Diff-based redraw**: "Fullscreen rendering sends only the cells that changed between frames." Documented failure mode on ConPTY/Windows Terminal where positioned writes coalesce incorrectly and leave fragments; escape hatch `CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT=1` (full repaint every frame). **Again: the fix for a diff-renderer artifact is "offer a full-repaint mode."**

### 9.2 Auto-follow — exactly Textual's `anchor()`

> "Scrolling up pauses auto-follow so new output doesn't pull you back to the bottom. A `Jump to bottom` button floats over the bottom edge... and shows a count such as `3 new messages`... While auto-follow is paused, the view also stays where you scrolled it when a response finishes streaming."

Disableable via `/config` → Auto-scroll off. This is functionally identical to `Widget.anchor()` in Textual — confirming that's the right primitive, plus the "N new messages / jump to bottom" affordance is the missing half most naive implementations skip.

### 9.3 Message queuing (P3)

Confirmed via [anthropics/claude-code#36326](https://github.com/anthropics/claude-code/issues/36326) ("Docs say Enter interrupts mid-task, but it only queues the message"): typing + Enter while Claude works **queues**, shown as a line above the input, firing in typed order when the turn ends. **Esc** is the actual interrupt, and Claude keeps whatever work it already did (partial tool calls are not rolled back). Labeled `area:tui`/`bug`/`has repro` and **closed as "not planned"** — i.e. queue-on-Enter is intended behavior and the docs wording was the error. Related open requests: #62349 (`/cancel` to clear queued messages), #64624 (real-time steering without queueing), #50246 (message queue mode toggle).

### 9.4 Implementation (community consensus, NOT Anthropic-confirmed)

Multiple independent deep-dives of decompiled/leaked source converge on: started from **Ink** (React for terminal) then forked heavily ("beyond recognition"), reportedly ~144 UI components and ~104 custom React hooks, using **Yoga** (the flexbox engine React Native uses) for terminal panel layout. Treat as strong community reverse-engineering, not primary source.

### 9.5 The bug history — this is the most valuable part

QAR's exact P4 symptom is **a known, reproduced, closed-as-not-planned Claude Code bug**:

[#16578](https://github.com/anthropics/claude-code/issues/16578) "Terminal rendering breaks in long conversations — status lines print on new lines instead of updating in place" (CC 2.0.76, macOS, iTerm2 *and* Terminal.app). Symptom:
```
✳ Infusing…  (esc to interrupt · 5s · ↓ 217 tokens · thinking)
✳ Infusing…  (esc to interrupt · 5s · ↓ 217 tokens · thinking)
✶ Infusing…  (esc to interrupt · 5s · ↑ 217 tokens · thinking)
```
Triggers only after the conversation reaches a certain length; restart fixes it temporarily. Diagnosis: **ANSI cursor-movement escape codes stop working** once the rendered frame outgrows what the renderer's cursor math can address. Closed as not planned.

[#41965](https://github.com/anthropics/claude-code/issues/41965) "v2.1.89 regression: flicker-free rendering destroys terminal scrollback by default" — PTY testing at 24×120 showed the startup banner reappearing 3× and the whole conversation fully re-rendering (destroying scrollback) **whenever content exceeded the visible terminal height (~24 rows)**. Root cause: flicker-free mode shipped enabled by default when it should have been opt-in. Workaround `CLAUDE_CODE_NO_FLICKER=0`. Closed as duplicate of #41814.

Independent root-cause writeup: [angular.schule, "Claude Code: How to Actually Fix the Endless Scrolling Problem"](https://angular.schule/blog/2026-02-claude-code-scrolling/) — the sharpest statement of the failure class:

> "Multiple things render simultaneously: streaming responses, status line updates, spinner animations, input field renders. Each writes escape sequences when they overlap, producing **contradictory cursor positions**."
> "React assumes that rendering is cheap. In the browser, that's true: DOM diffing is fast, the browser compositor ensures flicker-free display. **In the terminal, there is no compositor.**"

Reported severity by surface: VS Code/Cursor (xterm.js) worst, can crash; tmux showing 4,000–6,700 scroll events/second; iTerm2 occasional; Ghostty none (GPU rendering masks it). Notably, **DEC 2026 Synchronized Output was insufficient**: "Each individual update is atomic, but the overall picture is still broken. It's like a film projector showing clean individual frames, but each frame shows a different part of the scene." Anthropic's Jan-2026 differential renderer improved ~1/3 of sessions. The community workaround (`claude-chill`) is a **PTY proxy running an in-memory VT100 emulator, diffing screen states frame-to-frame and wrapping changes in Synchronized Output blocks** — i.e. bolting on the compositor the terminal doesn't have.

There is also a long tail of resize-related issues (search-surfaced, titles only, not individually verified): #39315, #42002, #42670, #16939, #57133, #60829, #14617, #57145, #29937, #18493, #58906, #54732, #9727, #49086, #20094. The recurring attributed cause is **SIGWINCH handling not clearing the previous frame before redrawing**, pushing duplicate content into scrollback.

**The lesson for QAR is large and slightly counterintuitive: the tool everyone is comparing us to has this exact bug class, unfixed, because it hand-rolls cursor math over a React reconciler. Textual has a compositor. Do not hand-roll.**

---

## 10. Textual framework primitives — the concrete answers

### 10.1 `dock: bottom` (fixes P1)

Confirmed, and it's the only supported mechanism. Per [textual.textualize.io/styles/dock](https://textual.textualize.io/styles/dock/):

> "Docking a widget **removes it from the layout** and fixes its position, aligned to either the top, right, bottom, or left edges of a container... Docked widgets will not scroll out of view, making them ideal for sticky headers, footers, and sidebars."

Because the docked widget is pulled out of flow, a sibling scroller with `height: 1fr` **automatically fills the remaining space** — no manual height math, no `on_resize` handler.

```python
class ChatApp(App):
    CSS = """
    RichLog { height: 1fr; }      /* fills space above the dock */
    Input   { dock: bottom; height: auto; }
    """
    def compose(self) -> ComposeResult:
        yield RichLog(wrap=True, markup=True)
        yield Input(placeholder="Message...")
```

**Mistakes that make the input scroll away or clip:**
- Putting the `Input` **inside** the scrolling container — it scrolls with the transcript. It must be a sibling.
- **Dock is relative to the parent container.** Docking inside a nested container docks it to *that container's* bounds, not the screen. If the input must sit at the bottom of the terminal, dock it at screen/root level.
- No explicit height (`height: auto` or a fixed row count) on the docked widget — it can collapse or misbehave.
- The transcript widget not sized `1fr` (e.g. fixed-height or `auto`) — then the scroll region doesn't shrink and the dock overlaps.

### 10.2 Stick-to-bottom: use `anchor()`, not `auto_scroll`

- **`Widget.anchor()` exists** and is the right primitive. Changelog: `[0.62.0] 2024-05-20` added `Widget.anchor`, `Widget.clear_anchor`, `Widget.is_anchored`; `[0.88.0] 2024-11-29` fixed an infinite loop in it; `[4.0.0] 2025-07-12` added `Widget.release_anchor`. It pins the container to the bottom and **breaks automatically when the user scrolls up** — exactly Claude Code's auto-follow semantics (§9.2).
- **`RichLog(auto_scroll=True)` is NOT the same thing.** Per [Textualize/textual#6311](https://github.com/Textualize/textual/issues/6311), `RichLog` snaps back to the bottom on new writes *even while the user is scrolled up*, whereas `Log` preserves position. So for "pinned unless scrolled up," prefer `anchor()` on a `VerticalScroll` over `RichLog.auto_scroll`.
- `VerticalScroll.scroll_end(animate=False)` is the one-shot jump-to-bottom (pair it with a "N new messages" button as Claude Code does).

### 10.3 Streaming markdown: `Markdown.get_stream()` (fixes P4 properly)

Two successive releases:
- `[4.0.0] 2025-07-12` — added `Markdown.append`
- **`[5.0.0] 2025-07-25` — added `Markdown.get_stream`** (the batching/backpressure-aware one)
- `[5.0.1] 2025-07-25` — fixed appending to Markdown widgets constructed with an existing document
- `[5.2.0] 2025-08-01` — added a `stream` **layout** (unrelated CSS layout mode)

QAR is on Textual 8.2.7, so **this API is already available.** Canonical example from [the Markdown widget docs](https://textual.textualize.io/widgets/markdown/):

```python
@work
async def stream_markdown(self) -> None:
    markdown_widget = self.query_one(Markdown)
    container = self.query_one(VerticalScroll)
    container.anchor()

    stream = Markdown.get_stream(markdown_widget)
    try:
        while (chunk := await self.get_chunk()) is not None:
            await stream.write(chunk)
    finally:
        await stream.stop()
```

Rationale from the docs: *"if you append to the Markdown document many times a second, the widget won't be able to update as fast as you write (this occurs around 20 appends per second)... [get_stream] will combine several updates into one as necessary to keep up with the incoming data."*

Design rationale from Will McGugan, ["Efficient streaming of Markdown in the terminal"](https://willmcgugan.github.io/streaming-markdown/) — four optimizations, and note how closely they track aider's independently-derived design:
1. Only the **last block** can change type as content arrives; all earlier blocks are finalized.
2. Update the last block **in place** rather than replacing widgets (replacing one widget per token is too expensive at 100+ tokens/sec).
3. Parse only **from the stored line number of the last block** onward → "parsing was always sub 1ms" regardless of document size.
4. **Buffer between producer and consumer** — when tokens arrive faster than the widget can display, concatenate and defer.

`RichLog` note: "rendering of content will be deferred until the size of the RichLog is known," so `write()` calls in `compose`/`on_mount` won't render immediately. Use `RichLog`/`Log` for raw high-rate plain text; `MarkdownStream` for formatted content.

### 10.4 Log/print noise (fixes P2)

Per [the devtools guide](https://textual.textualize.io/guide/devtools/):
- Textual does **not** silently swallow stdout — "by default, Textual logs to stdout, but you cannot see it because your application will be on screen." That's the leak.
- **`textual console`** in a second terminal + **`textual run --dev app.py`** pipes anything you print into it, plus live CSS hot-reload.
- **`self.log(...)` / `App.log` / `Widget.log`** — structured logging shortcut routed to the devtools console.
- **`TextualHandler`** — the stdlib-`logging` bridge:
  ```python
  from textual.logging import TextualHandler
  import logging
  logging.basicConfig(level="NOTSET", handlers=[TextualHandler()])
  ```
  Docs caveat: strings only, no Rich renderables. And: "If there is an active Textual app, then log messages will go via the app... If there is no active app, then log messages will go to stderr or stdout."
- **There is NO first-party "redirect all stdout into this widget" API.** For stray third-party `print()` you don't control, you must redirect `sys.stdout`/`sys.stderr` yourself (`contextlib.redirect_stdout(io.StringIO())` or a custom sink). Textual discussions [#2810](https://github.com/Textualize/textual/discussions/2810), [#3568](https://github.com/Textualize/textual/discussions/3568), [#2072](https://github.com/Textualize/textual/discussions/2072) show users building custom file-like shims to a `RichLog` widget for exactly this.

**Concrete three-part recipe for QAR:** (1) install `TextualHandler` on the **root** logger at app startup so every stdlib `logging` call anywhere in the dependency tree routes safely; (2) wrap risky third-party call sites in `contextlib.redirect_stdout(...)` to a sink; (3) treat `textual run --dev`/`textual console` as dev-only — production runs have no console attached, so unrouted output still hits the real terminal.

### 10.5 Workers — keep the input live while the AI runs (enables P3)

Per [the workers guide](https://textual.textualize.io/guide/workers/):
- `@work` turns a coroutine (or function) into a Worker — asyncio task by default, thread with `thread=True` (must be explicit since 0.31.0).
- `@work(exclusive=True)` **cancels any previous in-flight worker of the same name** — the standard guard against out-of-order responses when a new message interrupts an in-flight call.
- Thread workers: "avoid calling methods on your UI directly from a threaded worker, or setting reactive variables" — use `App.call_from_thread(...)`; `post_message()` is thread-safe and the recommended cross-thread UI path.
- `self.run_worker(coro, exclusive=True)` is the imperative equivalent.
- **The Input stays responsive by construction** — because the AI call runs in a worker rather than inline in the `Input.Submitted` handler, the event loop keeps processing keystrokes and redraws. This is the whole point; nothing else to configure.
- **No built-in message queue.** The idiomatic approach is your own `list`/`asyncio.Queue` on the App/Screen, appended on every `Input.Submitted`, drained by a worker. `Input.disabled` + a `placeholder` swap is the standard "AI is working, your message will be queued" signal.

### 10.6 Other Textual pieces worth using

- **`Collapsible`** — expand/collapse sections, ideal for tool-call and thinking blocks (cf. Amp's `Alt+T`).
- **`LoadingIndicator`**, **`ProgressBar`**.
- **`App.notify(...)`** — toast notifications for background events (e.g. a task finishing while the user is scrolled away).
- **Command palette** — built in by default (`ctrl+p`), extensible via `Provider`/`SystemCommand` (cf. Amp's `Ctrl+O`, opencode's leader keys).
- **`TextArea` vs `Input`**: `TextArea` does *not* submit on Enter (Enter inserts a newline). Chat-style apps either bind a custom send key (`ctrl+enter`/`ctrl+j`) or use `Input` for the compose box.
- **Inline mode: `app.run(inline=True)`**, added in **Textual 0.55.0** ("Added `inline` parameter to `run` and `run_async` to run app inline (under the prompt)"). Styling hooks: `:inline` CSS pseudo-selector, `INLINE_PADDING = 0` to remove default padding. Mechanism, per ["Behind the Curtain of Inline Terminal Applications"](https://textual.textualize.io/blog/2024/04/20/behind-the-curtain-of-inline-terminal-applications/): frames are written as `\n`-terminated lines **except the last**, where an escape code repositions the cursor back rather than emitting a trailing newline, so redraws overwrite in place; when a frame shrinks, an escape code clears lines from the cursor downward first. **Not supported on Windows.** This is the "keep native scrollback" option, i.e. Textual's equivalent of Claude Code's classic renderer.

### 10.7 Textual-based AI/chat TUIs (prior art)

- **[Elia](https://github.com/darrenburns/elia)** — Darren Burns (ex-Textualize). "A snappy, keyboard-centric terminal user interface for interacting with large language models." ~2.1k stars, SQLite conversation store, supports ChatGPT/Claude/Llama/Mistral/Gemma/Ollama. **Notably supports inline mode** — chat rendered under your shell prompt without going fullscreen. Real-world validation of `run(inline=True)` for a chat UI. Fork: [mrgrumpyowl/textual-chat](https://github.com/mrgrumpyowl/textual-chat).
- **[Textual's own "Anatomy of a Textual User Interface"](https://textual.textualize.io/blog/2024/09/15/anatomy-of-a-textual-user-interface/)** (2024-09-15) — official worked example building a chat-to-AI TUI, using `@work(thread=True)` to read the LLM response piecewise and update the Markdown widget for a streaming effect. **Predates `get_stream`** — read for structure, but replace its streaming technique with §10.3.
- [PAR LLAMA](https://github.com/paulrobello/parllama), [gptui](https://github.com/happyapplehorse/gptui), [tldw_chatbook](https://github.com/rmusser01/tldw_chatbook), [axio-tui](https://github.com/axio-agent/axio-tui) — other Textual LLM TUIs, less studied.

---

## 11. What to fork / vendor / import (the "don't build it" evaluation)

Verified by fetching actual `LICENSE` files. Star/commit counts are approximate (AI-summarized GitHub reads) — spot-check with `gh repo view` before quoting externally.

### 11.1 The headline find: **Toad** — a Textual TUI that is already an ACP client

[github.com/batrachianai/toad](https://github.com/batrachianai/toad) by **Will McGugan** (creator of Rich and Textual). Announced July 2025 ([announcing-toad](https://willmcgugan.github.io/announcing-toad/), [toad-released](https://willmcgugan.github.io/toad-released/)).

- **Python, on Textual.** Same stack as QAR.
- Explicitly "a universal UI for agentic coding in the terminal," using **ACP as its plug-in layer** to run Claude Code, Gemini CLI, Codex, OpenHands and dozens of others interchangeably, discovered and launched from inside the TUI.
- Features map almost exactly onto QAR's wishlist: markdown prompt editor with syntax highlighting, fuzzy file picker, side-by-side/unified diffs, **concurrent multi-agent sessions**, session resumption.
- Architecture: Textual frontend process ↔ agent subprocesses over ACP. Precisely the shape being proposed for QAR.
- ~3.4k stars / 154 forks, actively developed.
- **License: AGPL-3.0**, with a separate commercial license available.

**Verdict: this is the single most relevant project in the entire research, and it's also the one you probably cannot fork.** AGPL-3.0's network-use copyleft would attach to QAR if you reused the code. Two honest options: (a) treat Toad as the **architectural reference implementation** and build the same shape on the Apache-2.0 ACP Python SDK yourself, or (b) talk to McGugan about the commercial license. Do not quietly vendor AGPL code into an open-source-but-not-AGPL project.

### 11.2 Ranked shortlist

| Rank | What | License | Action | Why |
|---|---|---|---|---|
| 1 | **`textual.widgets.Markdown.get_stream()` / `MarkdownStream`** | MIT (already a dependency) | **Just use it** | Already available on Textual 8.2.7. Solves streaming-into-a-chat-widget with batching/backpressure. Zero new deps, zero license question. See §10.3. |
| 2 | **`agent-client-protocol` Python SDK** (`pip install agent-client-protocol`) | Apache-2.0 | **Add the dependency** | Turns QAR into a pluggable multi-backend client. Pydantic models + async `Agent`/`Client` base classes + stdio JSON-RPC plumbing. See §12. |
| 3 | **Textual `examples/mother.py`** | MIT (first-party) | **Copy verbatim as the skeleton** | Tiny canonical chat demo: `Input` docked at bottom, `@work(thread=True)` + `call_from_thread()` streaming. Matches our Textual major version. [source](https://raw.githubusercontent.com/Textualize/textual/main/examples/mother.py) |
| 4 | **aider `aider/mdstream.py`** | Apache-2.0 | **Vendor the single file** — for the prompt_toolkit/ANSI fallback only | Self-contained `rich.Live`+`Console` two-tier stable/unstable streaming to real scrollback. Solves the non-Textual half of P4. Strip the one unused `from aider.dump import dump` import. Keep the Apache-2.0 notice on the file + a NOTICE entry, and mark it as modified. |
| 5 | **`paulrobello/parllama`** | MIT | **Read the source before deciding** | **Currently on Textual 8.2.7 — exact version match with QAR.** Multi-tab, real-time streaming, session management. Least porting friction of any Python candidate. 484 stars, 226 commits, v0.9.2. |
| 5= | **`ggozad/oterm`** | MIT | **Read the source** | 2.4k stars, 857 commits, actively developed (recent: MCP support, faster markdown streaming, chat UI redesign). Multi-provider. |
| 6 | **Toad** | **AGPL-3.0** | **Reference architecture only** (or license commercially) | See §11.1. |
| 7 | **Elia** (`darrenburns/elia`) | Apache-2.0 | **Study only** | Good architecture (`ChatScreen` → `VerticalScroll("chat-container")` with one `Chatbox` per message, `Input` yielded last). But **pinned to `textual==0.79.1`** vs our 8.2.8, last push Oct 2024 (~22 months stale). Streams manually (`append_chunk` + `scroll_end(animate=False)`), predating `MarkdownStream`. Porting cost > value. |
| 7= | **Posting** (`darrenburns/posting`) | Apache-2.0 | **Study only** | Not chat, but modern-Textual, 12.3k stars, and the best-regarded Textual UX in the wild. Mine it for command palette, keybinding help, jump-mode navigation, status bar, theming. |
| 8 | **gptme** | MIT, active (v0.32.1, Jul 2026) | **Study only, for the ANSI fallback** | Plain CLI/rich-print loop, not a TUI. Informs scrollback-native streaming/interrupt style only. |
| 9 | **`simonw/llm`** | Apache-2.0 | Optional model-abstraction dep, not a UI source | `llm chat` is a readline loop, not a TUI. |
| — | **charmbracelet/crush** | **FSL-1.1-MIT** | **DO NOT FORK** | Real license blocker during the embargo (restricts competing offerings), independent of the Go/Python gap. Study only — but study it hard, it's the richest pattern source (§3). |
| — | **openai/codex** | Apache-2.0 | Study only | Rust + ratatui. No license blocker, but a language switch. Its TUI isn't drivable from Python except via the separate app-server JSON-RPC protocol for IDE integrations. |
| — | **google-gemini/gemini-cli** | Apache-2.0 | Study only | TypeScript + Ink monorepo (`packages/cli` ↔ `packages/core`), a package boundary not a network split. |
| — | **opencode** (`anomalyco/opencode`, formerly `sst/opencode`) | MIT | Study only | Now TypeScript; the Go codebase was archived in the split. No living Python or Go version to reuse. But note its **documented HTTP+SSE server API** is a real "one API, many frontends" precedent (§12.3). |
| — | `gptextual`, `tunacode`, `llm-tui` | MIT / n/a | Skip | Stale (Mar 2024), tiny/young, or Rust. |

**Nothing found is a clean wholesale fork for QAR.** The realistic plan is: adopt two dependencies (Textual's `MarkdownStream`, the ACP Python SDK), copy one MIT example as a skeleton, vendor one Apache-2.0 file for the fallback path, and take *patterns* (not code) from Crush, Codex, Amp, and Claude Code.

---

## 12. Pluggable execution backends — how to let users choose what runs the code

This addresses the mid-task requirement: Claude Code as *one* option among several.

### 12.1 Agent Client Protocol (ACP) — the answer

Home: [agentclientprotocol.com](https://agentclientprotocol.com); spec at [github.com/agentclientprotocol/agent-client-protocol](https://github.com/agentclientprotocol/agent-client-protocol). Created by Zed Industries (Aug 2025), **Apache-2.0**, and notably **moved off the `zed-industries` org into its own multi-vendor org** — a governance signal that it's broadening past Zed.

**What it is**: JSON-RPC 2.0 over stdin/stdout (newline-delimited JSON) between a **client** (editor, TUI, orchestrator) and an **agent** (a subprocess). Explicitly modeled on LSP: *unbundle agent intelligence from the UI the way LSP unbundled language intelligence from the IDE.* MCP is the *vertical* connection (agent ↔ tools); ACP is the *horizontal* one (client/UI ↔ agent). They compose — an ACP agent typically speaks MCP internally, and ACP reuses MCP's JSON conventions where sensible.

**Protocol surface** (confirmed against the schema docs):
- Handshake: `initialize` (version + capability negotiation), optional `authenticate`.
- Sessions: `session/new` (wires MCP servers, sets cwd, returns `sessionId`), `session/load` (resume, capability `loadSession`).
- Turns: `session/prompt` (client → agent; text/images/files/resources; blocks until a `stopReason`), **`session/update`** (agent → client stream: message chunks tagged agent / user / **"thought"**, tool-call lifecycle, plan updates), **`session/cancel`** (abort mid-turn; agent replies `StopReason::Cancelled`).
- Permissions: `session/request_permission` with structured options (allow-once, allow-always, reject); `elicitation/create` for structured form/URL input.
- Client-provided filesystem (optional capability): `fs/read_text_file`, `fs/write_text_file` — the *client* mediates file access.
- Client-provided terminal (optional capability): `terminal/create`, `terminal/output`, `terminal/wait_for_exit`, `terminal/kill`, `terminal/release`.

Note how directly `session/update`'s typed stream maps onto the UI needs in §13: separate "thought" chunks (→ a `Collapsible` thinking block), tool-call lifecycle events (→ collapsible tool blocks with status icons), and plan updates (→ a todo panel). **The protocol hands you the exact event taxonomy the good TUIs display.**

**Maturity, honestly**: protocol version is `1` and the JSON Schema just reached a v1.0 milestone (breaking changes were codegen representation, not wire format). ~1 year old. **Remote-agent support is still "a work in progress"** — solid for local subprocess use (exactly QAR's `claude -p` case), not yet a finished networked story.

**Agents already shipping ACP adapters** (per [zed.dev/acp](https://zed.dev/acp) and the [ACP registry](https://zed.dev/blog/acp-registry), 60+ listed): Claude (Anthropic, via `@agentclientprotocol/claude-agent-acp` — renamed from `@zed-industries/claude-code-acp`; the old package looks stale), **Gemini CLI**, **Codex CLI**, GitHub Copilot CLI, **OpenCode**, **Goose**, Cursor, Cline, OpenHands, Docker cagent, Factory Droid, JetBrains Junie.
- The Claude adapter wraps the official **Claude Agent SDK** and vendors the Claude Code CLI internally; exposes tool calls with permission requests, interactive and background terminals, `@`-mentions/images, edit review, TODOs, client-side MCP servers.
- Zed built the Gemini CLI integration as a **JSON-RPC relay layer instead of screen-scraping terminal escape codes** ([bring-your-own-agent-to-zed](https://zed.dev/blog/bring-your-own-agent-to-zed)) — worth noting given QAR currently shells out.
- **Goose is adopting ACP as its *primary* interface** across desktop and CLI (`goose-acp-server`), and auto-merges MCP servers configured by the ACP client with its own extensions. An existing agent converging *onto* ACP rather than inventing its own protocol.

**Clients already speaking ACP**: Zed (reference), JetBrains (IntelliJ/PyCharm/WebStorm), Neovim (CodeCompanion), Emacs, VS Code, plus a long tail.

**Python SDK — it exists, and it's the right shape:**
- `pip install agent-client-protocol` ([PyPI](https://pypi.org/project/agent-client-protocol/), [docs](https://agentclientprotocol.github.io/python-sdk/)), Python 3.10+.
- Generated **Pydantic models** validated against the canonical schema, **async base classes** (`Agent`, `Client`) for both sides, JSON-RPC/stdio plumbing.
- A client is genuinely small: `spawn_agent_process(YourClient(), executable, args...)` manages the subprocess and framing; you implement `session_update()` and `request_permission()`, then call `conn.initialize()` and `conn.new_session()`.
- Ships runnable agent, client, and Gemini-CLI-integration examples.

**Honest assessment of "QAR becomes an ACP client and gets every backend for free":** largely true, with bounded gaps.
- *True*: thin transport, an existing Python SDK doing the tedious parts, and every backend QAR would want (Claude, Gemini CLI, Codex, Goose, OpenHands, OpenCode) already ships a maintained adapter. QAR writes **zero** backend-specific integrations.
- *Gaps*: (1) protocol is ~1 year old, remote agents unfinished; (2) the Python SDK is new/thin with no documented large production track record beyond examples; (3) ACP's permission/fs/terminal model assumes the **client** wants to mediate file and terminal access — QAR must decide whether it wants that layer (most adapters support both ways); (4) a bespoke "plain API loop" backend would still need a small ACP *agent* wrapper (trivial with the SDK's `Agent` base class); (5) **QAR's existing `claude -p` path already works and is deeply tuned** — the value of ACP is the *other* backends, not replacing the one that works.

### 12.2 Claude Code's own programmatic interfaces, compared

| Interface | Token streaming | Tool-call events | Mid-turn message injection |
|---|---|---|---|
| `claude -p --output-format stream-json --input-format stream-json` | Yes, with `--include-partial-messages` + `--verbose` (`stream_event`/`text_delta` lines) | Yes (`tool_use`/`tool_result` blocks; subagent messages tagged by `parent_tool_use_id`) | **No.** Mid-turn stdin messages are ignored by the running turn. True steering is an open feature request, not shipped: [#69124](https://github.com/anthropics/claude-code/issues/69124), [#71726](https://github.com/anthropics/claude-code/issues/71726) |
| **Claude Agent SDK for Python** (`claude_agent_sdk`) | Yes — `include_partial_messages=True` yields `StreamEvent` token deltas | Yes — full `hooks` system (`HookEvent`/`HookMatcher`), `can_use_tool` permission callback, `include_hook_events` | **Yes.** `ClaudeSDKClient.interrupt()` aborts the current turn (`terminal_reason: aborted_streaming`/`aborted_tools`), and `client.query()` accepts an **async generator** so you can `yield` additional user messages into an in-flight streaming-input turn |
| ACP adapter (`claude-agent-acp`) | Yes, via `session/update` chunks | Yes, via ACP tool-call reporting + `session/request_permission` | Via `session/cancel` + fresh `session/prompt` — interrupt-and-resubmit, not seamless injection |

**This is the decisive finding for P3 on the Claude backend.** QAR shells out to `claude -p`, and **that path structurally cannot do mid-turn steering.** If real steering is a goal, the Claude backend must move to the **Python Agent SDK** (which supports it natively via `interrupt()` + generator input) or to the ACP adapter (which sits on the same SDK). Both are Anthropic-maintained. Docs: [headless](https://code.claude.com/docs/en/headless), [Agent SDK Python](https://code.claude.com/docs/en/agent-sdk/python), [streaming output](https://code.claude.com/docs/en/agent-sdk/streaming-output).

### 12.3 Other multi-backend patterns, and where the boundaries are

- **OpenCode's HTTP+SSE server** ([opencode.ai/docs/server](https://opencode.ai/docs/server/)): a Hono TS server auto-generating an OpenAPI 3.1.1 spec; clients drive it with plain HTTP for actions plus **Server-Sent Events** for the live stream (model responses, tool execution, session updates). A real, typed, documented "one API, many frontends" precedent — but it's OpenCode's bespoke API, not a cross-vendor standard. (OpenCode is *also* an ACP agent, so it's reachable both ways.)
- **LiteLLM / `llm`** are a *different layer*: they abstract **LLM API calls** across providers, not agentic backends. Worth naming only to draw the boundary — **LiteLLM solves "same code, many models"; ACP solves "same UI, many agents."** Not substitutes.
- **AGENTS.md** — a separate convergence at the instruction layer, now under the same Linux Foundation Agentic AI Foundation that stewards MCP, read natively by Claude Code, Codex CLI, Cursor, Aider, Gemini CLI, Copilot. Evidence the ecosystem is standardizing at three layers (instructions = AGENTS.md, tools = MCP, UI↔agent = ACP), which makes an ACP bet look like riding a trend rather than a one-off.

---

## 13. Synthesis — prioritized, actionable

Ordered by (value to QAR × confidence) ÷ effort. Each item names the QAR problem it fixes and the actual primitive.

### Tier 1 — do these first; they are small, high-confidence, and framework-native

**1. Fix P1 (fixed-bottom input) with `dock: bottom`. It already exists; QAR just isn't using it correctly.**
Textual CSS `dock: bottom` **removes the widget from layout flow**, so a sibling transcript sized `height: 1fr` automatically fills the remainder — no `on_resize` handler, no height math. The four things that break it: (a) the input is *inside* the scrolling container instead of a sibling; (b) it's docked to a nested container, so it docks to that container's box rather than the screen; (c) no explicit `height: auto`/fixed on the docked widget; (d) the transcript isn't `1fr`, so the scroll region doesn't shrink. Check QAR's `#prompt` against exactly those four. Ref §10.1. Every tool surveyed converges on the same fixed-height-composer + flex-fill-transcript split (Crush's `layout.Len(...)`/`layout.Fill(1)`, Codex's `BottomPane`/`ChatWidget`, opencode's `BottomPane`) — Textual gives it to you in two CSS lines.

**2. Fix the *other* half of P1 with `Widget.anchor()`, not `RichLog.auto_scroll`.**
`anchor()` (Textual ≥0.62.0; `release_anchor()` since 4.0.0) pins a scroll container to the bottom and **breaks automatically when the user scrolls up**. `RichLog.auto_scroll` does *not* do that — per [textual#6311](https://github.com/Textualize/textual/issues/6311) it snaps back to the bottom even while the user is scrolled up. Then add the affordance everyone else has and naive implementations skip: a floating **"Jump to bottom · N new messages"** button, exactly as Claude Code documents ("Scrolling up pauses auto-follow... shows a count such as `3 new messages`"). Ref §9.2, §10.2.

**3. Fix P2 (log noise) with a two-part routing rule, plus a `qar logs -f` command.**
This is Charm's stated answer to precisely this bug: *"You can't really log to stdout with Bubble Tea because your TUI is busy occupying that! You can, however, log to a file."*
   - Install **`textual.logging.TextualHandler`** on the **root** logger at app startup (`logging.basicConfig(level="NOTSET", handlers=[TextualHandler()])`) so every stdlib `logging` call anywhere in the dependency tree is captured. Caveat: strings only, no Rich renderables.
   - Textual has **no** first-party "redirect all stdout" API. For third-party `print()` you don't control, wrap those call sites in `contextlib.redirect_stdout(...)` to a sink yourself.
   - Write to a **rotating file** (`logging.handlers.RotatingFileHandler`, the `lumberjack` analog) in **JSON/structured form**, and format for humans only at display time — Crush's exact split. Ship `qar logs`, `qar logs --tail N`, `qar logs -f` as a separate subcommand rather than an in-TUI pane (Crush deliberately has no in-TUI log view).
   - Steal aider's third layer for library noise specifically: lazy-import the noisy client and hard-disable its logging at import (`suppress_debug_info`, `set_verbose=False`, even private `_logging._disable_debugging()`), and keep `--verbose` gating **only QAR's own** diagnostics, never the library's. Ref §1.3, §3.3, §10.4.

**4. Fix P4 (spinner/cursor desync) in the Textual path by switching to `Markdown.get_stream()`.**
Added in **Textual 5.0.0** (2025-07-25) — QAR is on 8.2.7, so it's already there.
```python
container.anchor()
stream = Markdown.get_stream(markdown_widget)
try:
    while (chunk := await get_chunk()) is not None:
        await stream.write(chunk)
finally:
    await stream.stop()
```
It batches ("will combine several updates into one as necessary"), re-parses only from the last block's stored line number (sub-1ms regardless of document size), and updates the last block in place instead of replacing widgets. Ref §10.3. **Delete any hand-rolled cursor math in the Textual path** — Textual has a compositor; that's the whole reason to be on it (§13, item 9).

### Tier 2 — a week or two, high value

**5. Fix P3 (mid-turn queuing) with the Amp three-tier model + Crush's queue mechanics.**
The UX taxonomy to copy is Amp's, which is the most complete found:
   - **Enter → queue**, sent when the agent finishes the turn
   - **Enter Enter → steer**, sent at the end of the current *step* (command / thinking block)
   - **Esc Esc → interrupt now**
   Codex's two-key variant (Enter = send now, **Tab = queue**) is the alternative; Amp's is more expressive. Whichever you pick, copy these three affordances:
   - Queued messages render as a **visible itemized list above the input**, not a toast, with per-item "inject now" (Codex) and `↑`/`↓` to navigate and **edit** queued messages (Amp). Crush shows a **"N Queued" pill**.
   - **Never disable the Input** while the agent runs (Crush explicitly doesn't).
   - Copy **Crush's escape state machine** verbatim in spirit: esc#1 arms a cancel-confirm timer (`self.set_timer(...)` to disarm) → esc#2 within the window cancels; if nothing is running but prompts are queued, a single esc **clears the queue**; and the **help text changes live** (`"press again to cancel"` vs `"clear queue"`).
   Implementation in Textual: run the agent call in `@work(exclusive=True)` (the event loop keeps handling keystrokes by construction — that's the whole point of workers); keep your own `asyncio.Queue`/list on the App drained by that worker. Textual has no built-in queue widget. Ref §3.4, §4.5, §8, §10.5.

**6. Adopt the ACP Python SDK to make execution backends pluggable (the mid-task requirement).**
`pip install agent-client-protocol` (Apache-2.0). Implement `session_update()` and `request_permission()`, use `spawn_agent_process(...)`, and QAR immediately gets **Claude, Gemini CLI, Codex CLI, Goose, OpenCode, Copilot CLI, OpenHands** as backends with zero per-backend integration code. **Toad already proves this exact shape works** (Python + Textual + ACP, ~3.4k stars) — but it's **AGPL-3.0**, so use it as a reference implementation, not a fork base, unless you license it commercially. Bonus: ACP's `session/update` stream is *already typed* with the distinctions the good TUIs display — separate "thought" chunks, tool-call lifecycle, plan updates — so it hands you the event taxonomy for item 7. Keep the existing `claude -p` path as-is while you build this; ACP's value is the *other* backends. Ref §11.1, §12.1.

**7. Display structure: collapsible tool calls, and thinking separated from the answer.**
   - **`Collapsible`** (built into Textual) for each tool call. Collapsed shows a truncated view with a hidden-lines counter (Crush uses 10 lines); a status icon per line; the spinner runs **only while the call is unfinished and not canceled**. Distinguish **permission-denied (`WARN`) from actual failure (`ERROR`)** — Crush does, most tools blur it.
   - **Thinking is a separate region with its own render cache**, so streaming content doesn't invalidate it (Crush's `assistant.go`). Three collapse states (last 10 / last 200 / all), an expand affordance, and a **"Thought for <duration>"** footer when done. Bind an expand-all key (Amp uses `Alt+T`). Note aider's *counter*-example: it inlines reasoning as a synthetic tag inside the same markdown stream — simpler, but you lose independent caching and collapse.
   - Everything else (diffs, edit summaries, status) should be **plain scrollback text, not live widgets** — aider's discipline: only the two genuinely dynamic surfaces (spinner, streaming answer) get live treatment.
   Ref §1.7, §3.5, §10.6.

**8. Spinner hygiene, if you keep a hand-rolled one anywhere (the ANSI path).**
Copy aider's `waiting.py` rules wholesale: **`\r` + backspace only, never absolute cursor-position codes**; clip to `console.width - 2`; a **500ms grace delay** so fast responses never flash a spinner; cap at 10Hz; probe unicode support once with a real write/backspace round-trip and fall back to ASCII; make `.start()`/`.stop()` idempotent with `.stop()` force-calling `end()` after a join timeout; and **centralize spinner ownership in one place** (aider had double-spinner races until they did). Add aider's defensive `Console().show_cursor(True)` at the top of your Ctrl-C handler — a spinner that hides the cursor will leave it hidden if interrupted before cleanup. And guard **every nested/confirmation prompt** with its own `try/except KeyboardInterrupt` ([aider#3889](https://github.com/Aider-AI/aider/issues/3889)). Ref §1.5.

### Tier 3 — decisions and defenses

**9. Decide inline vs alt-screen deliberately, and know what you're trading.**
Claude Code's own docs make this a first-class user choice with two renderers:
   - **Alt-screen** (Textual default `app.run()`): flicker-free, flat memory via virtualization, mouse support, **and it is what makes the input stay fixed** — Claude Code literally defines it that way ("If the input doesn't move while Claude is working, fullscreen rendering is active"). Cost: **the conversation leaves your terminal's scrollback**, so `Cmd+F`/tmux copy-mode can't see it. Mitigate as Claude Code does: an in-app transcript view with search, plus a **key that dumps the transcript into native scrollback** (`[` in Claude Code).
   - **Inline** (`app.run(inline=True)`, Textual ≥0.55.0; `:inline` CSS pseudo-selector, `INLINE_PADDING = 0`): keeps native scrollback. **Not supported on Windows.** Elia ships this for chat, so it's proven. Mechanism ([Behind the Curtain of Inline Terminal Applications](https://textual.textualize.io/blog/2024/04/20/behind-the-curtain-of-inline-terminal-applications/)): frames are `\n`-terminated except the last line, where an escape repositions the cursor back; when a frame shrinks, lines are cleared from the cursor downward first.
   **Recommendation: alt-screen as default (that's what gets you the fixed input), inline as an opt-in flag**, mirroring Claude Code's classic/fullscreen split. Ref §9.1, §10.6.

**10. Defend against resize — it is the universal breaking point.**
Every incremental/diff renderer surveyed has open resize bugs: Gemini CLI [#22615](https://github.com/google-gemini/gemini-cli/issues/22615) (duplicated footers; fix was an explicit `rerender()` in the resize listener), [#21924](https://github.com/google-gemini/gemini-cli/issues/21924) (batch history updates on resize instead of one big repaint); Claude Code's long tail (#57145, #58906, #54732, #49086, #9727, #18493 …) attributed to **SIGWINCH handling not clearing the previous frame before redrawing**; Codex/ratatui's PTY-scraping workaround is to **force a spurious resize (cols → cols−1 → cols) to trigger a full clean repaint**. Textual's compositor handles this far better than hand-rolled renderers, but: test QAR under resize *while streaming*, and provide a **full-repaint escape hatch** (Claude Code shipped `CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT=1` for exactly this on ConPTY). Ref §5.3, §9.5.

**11. Three separate output channels, never interleaved.** Universal across every serious tool: (a) the live TUI, (b) a debug log file in a dotdir, (c) a durable **JSONL session transcript** for resume/audit. Codex: `~/.codex/log/codex-tui.log` + `~/.codex/sessions/YYYY/MM/DD/rollout-<id>.jsonl`. opencode: `~/.local/share/opencode/log/`. Goose: `~/.config/goose/sessions/<id>.jsonl`. aider: `chat_history_file`. **The transcript being independent of rendering means a rendering bug never loses data** — worth having regardless of which UI path QAR uses. Add an `opencode debug paths`-style command that prints every resolved path.

**12. Smaller wins worth queuing up.**
   - **Notifications only when unfocused** (Crush): desktop notification on permission-needed and turn-complete, but *only* when the terminal isn't focused. Textual has `App.notify()` for in-app toasts; pair with an OSC-escape fallback for OS-level.
   - **Command palette** is built into Textual (`ctrl+p`), extensible via `Provider`/`SystemCommand` — free parity with Amp's `Ctrl+O` and Crush's `ctrl+p`.
   - **Open the current prompt in `$EDITOR`** (`ctrl+o` in Crush, `Ctrl+G` in Amp, `ctrl+x ctrl+e` in aider) — cheap, universally loved.
   - **`reserve_space_for_menu=4`** on the prompt_toolkit path so the completion popup can't collide with the input line.
   - **Responsive breakpoints** (Crush: compact mode below 120 cols / 30 rows collapses the sidebar to a one-line header).
   - **Goose's `/recipe`** — generate a reusable `recipe.yaml` from your live session history. For a *tasking* runner specifically, "turn what I just did interactively into a repeatable task definition" is a natural fit.
   - **`/undo` on file edits, not just chat turns** (opencode) — chainable.
   - Bind by **interaction frequency**, not by scheme purity: opencode uses a `ctrl+x` leader for most things but gave frequently-used session cycling bare `left`/`right` because leader-prefixed navigation was too slow.

### The one structural conclusion

The tool everyone is implicitly benchmarking against — Claude Code — has QAR's exact P4 bug, reproduced and closed as not planned ([#16578](https://github.com/anthropics/claude-code/issues/16578): "status lines print on new lines instead of updating in place," triggering only once conversations get long), plus a whole family of resize/scrollback-corruption issues. The root cause, per the sharpest public analysis ([angular.schule](https://angular.schule/blog/2026-02-claude-code-scrolling/)):

> "Multiple things render simultaneously: streaming responses, status line updates, spinner animations, input field renders. Each writes escape sequences when they overlap, producing contradictory cursor positions."
> "React assumes that rendering is cheap... **In the terminal, there is no compositor.**"

Even DEC 2026 Synchronized Output wasn't enough ("each individual update is atomic, but the overall picture is still broken"). The community fix is a PTY proxy running an in-memory VT100 emulator that diffs frames — i.e. **bolting on a compositor**.

**Textual already is that compositor.** Crush's `internal/ui/AGENTS.md` states the same discipline as policy: one top-level model owns the frame buffer, leaf components never do their own cursor math, everything is diffed and painted centrally once per frame. So the strategic read is: QAR's Textual UI is on the *right* architecture, and most of its remaining bugs are it fighting the framework rather than using it — `dock: bottom` instead of manual positioning, `anchor()` instead of manual scrolling, `Markdown.get_stream()` instead of manual repaints, `TextualHandler` + a log file instead of stray stdout. The prompt_toolkit/ANSI fallback is the path that genuinely *can't* win this fight, which is an argument for keeping it deliberately simple (aider's model: nothing ever writes concurrently with the prompt, `\r`+backspace only, vendor `mdstream.py` for streaming) rather than trying to make it match the Textual UI feature-for-feature.

