# Edited Files Extraction: Multi-Strategy Discovery

This document describes the edited files extraction system for automatic context card updates after deep execution.

## Overview

After a deep runner completes execution (Claude Code, Managed Agents, direct API calls, etc.), we automatically discover which files were edited and categorize them into relevant context cards for learning.

The system uses **three fallback strategies** to maximize compatibility with different deep runners:

1. **Structured metadata** — if the runner returns `result.data["edited_files"]` (explicit, clean)
2. **Output parsing** — if the runner's text output contains the standard format
3. **Claude Code session parsing** — if running via Claude Code, parse the JSONL transcript

## For Non-Claude-Code Deep Runners

Include this prompt instruction in your system prompt or final instruction:

```python
from quest_ai_runner.adapters.context_card_updater import get_edited_files_prompt_instruction

# Add to system prompt or final instruction
instruction = get_edited_files_prompt_instruction()
```

The instruction tells the runner to report edited files in one of two formats:

### YAML Format (Recommended)

```
EDITED_FILES:
- path/to/file1.py
- path/to/file2.ts
- path/to/file3.md
```

### JSON Format

```json
{
  "status": "completed",
  "edited_files": ["path/to/file1.py", "path/to/file2.ts"]
}
```

Key guidelines:
- **Only list files you directly modified** — don't include read-only files or files merely referenced
- **Empty edits are OK** — write `EDITED_FILES:` with nothing after it if no files were modified
- **Placement** — can appear anywhere in output; the parser extracts it

## For Claude Code

When running via Claude Code, the system automatically:

1. Gets the session ID and project path from the deep runner context
2. Parses `~/.claude/projects/<project>/<session_id>/messages.jsonl`
3. Extracts all Edit tool calls to build the file list

No additional work needed — it's transparent.

## Integration in Orchestrator

The orchestrator now calls:

```python
def _update_context_cards_after_deep(
    self,
    deep_result: OrchestratorResult,
    context_meta: Optional[Dict[str, Any]],
    project_path: Optional[str] = None,        # For Claude Code
    session_id: Optional[str] = None,          # For Claude Code
) -> None:
```

Example call:

```python
# After deep execution completes
self._update_context_cards_after_deep(
    res,
    context_meta,
    project_path=os.path.expanduser("~"),
    session_id=session_id_from_runner,
)
```

## How It Works

```
┌─ Deep execution completes (Claude Code / Managed Agents / API)
│
├─ Strategy 1: Check result.data["edited_files"]
│  └─ If present → use it, skip other strategies
│
├─ Strategy 2: Parse result.output for YAML or JSON format
│  └─ If found → extract file list, skip other strategies
│
├─ Strategy 3: Parse Claude Code session JSONL
│  └─ Only if project_path + session_id provided
│
└─ Discovered files → LLM categorization → context card updates (background thread)
```

Each strategy is tried in order; the first one that succeeds wins. This allows flexibility:

- **Claude Code**: transparent JSONL parsing (no prompt needed)
- **Managed Agents**: include the prompt instruction in system prompt → get structured output
- **Direct API**: return `result.data["edited_files"]` → instant discovery
- **Hybrid**: any combination works

## Testing

Run the test suite to verify extraction strategies:

```bash
cd /home/joshua/hq/stories/spiritual_data/product/launch_code/quest-ai-runner
python3 test_edited_files_extraction.py
```

Tests cover:
- YAML format parsing
- JSON format parsing
- Structured metadata extraction
- Fallback chaining
- Claude Code JSONL parsing
- Prompt instruction generation

## Modules

### `quest_ai_runner.adapters.context_card_updater`

**New functions:**

- `get_edited_files_prompt_instruction()` → str
  - Returns the prompt instruction to include in deep runner context
  - Explains both YAML and JSON formats

- `extract_edited_files(deep_result, project_path=None, session_id=None)` → List[str]
  - Unified entry point that tries all strategies
  - Returns empty list if no files discovered

- `discover_claude_code_edits(project_path, session_id)` → List[str]
  - Parses Claude Code session JSONL
  - Extracts file paths from Edit tool calls

- `_parse_edited_files_from_output(output_text)` → List[str]
  - Parses YAML and JSON formats from output text
  - Internal helper used by extract_edited_files()

**Existing functions (unchanged):**

- `categorize_files_with_llm()` — LLM-based file categorization
- `update_context_cards()` — idempotent card updates

### `quest_ai_runner.core.orchestrator`

**Updated method:**

- `_update_context_cards_after_deep(deep_result, context_meta, project_path=None, session_id=None)`
  - Now uses `extract_edited_files()` for multi-strategy discovery
  - Accepts project_path and session_id for Claude Code support
  - Categorizes and updates cards in background (non-blocking)

## Example Usage

### Claude Code (Automatic)

```python
# No changes needed — session JSONL parsing is automatic
result = orchestrator.run(user_message, ...)
# → Files are discovered and cards updated automatically
```

### Managed Agents (With Prompt)

```python
from quest_ai_runner.adapters.context_card_updater import get_edited_files_prompt_instruction

# Include in agent system prompt
system_prompt = f"""
You are a coding assistant. Do your work.

{get_edited_files_prompt_instruction()}
"""

# Deep runner will output:
# EDITED_FILES:
# - src/app.py
# - tests/app_test.py
```

### Direct API (With Metadata)

```python
# Return result with edited_files in metadata
deep_result = DeepResult(
    met=True,
    output="Task completed",
    data={"edited_files": ["src/main.py", "docs/README.md"]}
)

orchestrator._update_context_cards_after_deep(result, context_meta)
# → Files are extracted and cards updated automatically
```

## Design Rationale

**Why multiple strategies?**

1. **Claude Code transparency** — no changes to the runner; just parse its existing transcript
2. **Structured metadata** — fastest, most reliable when available
3. **Output parsing** — works with any runner that follows the standard format
4. **Flexibility** — runners can use whichever fits their architecture

**Why background threading?**

- Context card updates are non-blocking learning; they shouldn't slow down task completion
- Failures don't affect the primary task result
- LLM categorization is relatively fast (~1-2 seconds) but still worth threading

**Why the prompt instruction is optional?**

- Claude Code doesn't need it (JSONL parsing is automatic)
- Other runners can still be useful without it (they just won't update cards)
- Keeps the system graceful: more data when available, still works when not

## Troubleshooting

**No files discovered?**

1. Check that the deep runner actually modified files
2. Verify the output format matches YAML or JSON spec
3. For Claude Code, ensure `.claude/projects/` session structure exists
4. Check logs: `log.debug()` messages trace each discovery step

**Files not categorized correctly?**

- This is normal for ambiguous files
- The LLM categorizer uses the context card descriptions to decide placement
- Cards with clearer descriptions get better matches
- Files not matching any card are simply not added (idempotent behavior)

**Performance concerns?**

- Background thread doesn't block the main task
- LLM categorization runs once per task (not per file)
- JSONL parsing is fast (~10-100ms even for large sessions)
- No impact on normal request latency

## Future Extensions

Possible enhancements:

1. **Custom discovery plugins** — allow runners to register custom file-discovery logic
2. **Batch categorization** — when many tasks complete in parallel, batch LLM calls
3. **Confidence scoring** — categorizer could score how confident it is in each placement
4. **Distributed card updates** — when multiple agents run in parallel, coordinate updates to avoid conflicts
