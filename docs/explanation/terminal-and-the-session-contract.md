# The terminal and the hub's session contract

The hub defines one contract for working with the AI while it works: typed events going out of a run (`RunStarted`, `Step`, `AgentStarted`,
`AgentEnded`, `Review`, `Waiting`, `Usage`, `Finished`) and typed controls going in (`Stop`, `Resume`, `Steer`, `Approve`, `Refuse`, each able to name
one agent). The requirements, X1 to X23, and the comparison with other terminal agents are on the hub page `working-with-the-ai-while-it-works`. This
page maps this repository's terminal to that contract and lists, in order, the steps that make the terminal, Quest AI chat and the AI task modal consume it.

## What the terminal has today

| Contract part | The terminal's own |
|---|---|
| Stop (`Stop`) | Escape, and Ctrl+C while the AI works; the cancel flag reaches the worker and the session is kept (`textual_ui_actions.action_cancel`) |
| Resume (`Resume`) | `/resume`, `chat --resume`, `chat --continue`; the session id goes back to the worker (`textual_ui_control`) |
| Steer (`Steer`) | a line typed while a live worker runs is claimed once by `TerminalRunControl.claim_inputs` and confirmed with `mark_seen`; a message never confirmed is handed back |
| Steps (`Step`) | `exec` events rolled up as counts (`DeepMixin._summarize_exec_lines`) and the Trail from `work_trace` events, drawn by the hub's renderers |
| Approve (`Approve`, `Refuse`) | a decision ask, answered by the next line once the turn ends |
| Usage and result (`Usage`, `Finished`) | elapsed time; since 2026-11-10 the closing line adds the cost (only when a run reported one) and the files changed (`result_items`) |
| Helpers (`AgentStarted`, `AgentEnded`) | none; the jobs list is flat |

`tests/test_terminal_requirements.py` scores this table: 9 of 23 requirements before the closing line named cost and files, 11 of 23 after
(metric `metric-terminal-requirements`).

## The steps to one contract, in order

1. **Adapter, additive.** A small QAR module turns the events of a deep run (`ProgressEvent` and the stream runner's raw lines) into the hub's session
   events, using the hub's adapters. It changes no screen. Gain: one place that knows QAR's event shapes.
2. **Controls through `Harness.control`.** `TerminalRunControl` is already the stream runner's `RunControl`; wrapping it as a hub `Harness`
   makes Stop, Steer and Resume the same calls for every harness. Gain: switching harness (X11) needs no terminal change.
3. **Draw `Waiting` while the run goes on.** Today a question is written when a step ends and `open_ask_takes` refuses an answer during a turn. With a
   `Waiting` event the question shows at once and Approve or Refuse answers it. Gain: X9, X19, X22.
4. **Helpers as a tree.** Draw `AgentStarted` and `AgentEnded` with their parent beside the jobs list, and `/stop name` and `@name text`. Gain: X7, X8, X15.
5. **Quest AI chat.** The chat stream already carries `status`, `exec`, `work_trace`, `decision`; map them to the same events at the backend so the
   page draws one component for steps, tree, cost and files. Add a pickup event for a queued message (today `pending-input` answers only `queued`), so a
   typed line shows as sent, then taken up. Gain: X6, X7, X10.
6. **AI task modal.** Stop there is an undo and pause or resume lives only on the live card. Route its progress kinds through the same events and show Stop,
   Resume and Steer from the one control set, with changed files and cost in the finished view. Gain: X4, X5, X10.

The hub's reference terminal (`python -m hubofhubs.the_consciousness.terminal.live --demo loop`) is the proof that a screen drawn only from the contract
works for the hub's own loop and for a stream session alike; this repository keeps its Textual screen and its settings.
