# file_editing

Breadcrumb: [Top](../../../../esoi/README.md) > [World](../../README.md) > [Tools](../README.md) > file_editing

Part of [Tools](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Contained backed-up writer | `file-editing:files` | Resolves every path through the same containment function the reader uses, refuses secret-like names, binary files and oversize payloads, copies the previous content to a backup outside the corpus before replacing it, and returns a result value instead of raising. | [`quest_ai_runner/the_world/tools/file_editing/files_writer.py`](files_writer.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Tools](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
