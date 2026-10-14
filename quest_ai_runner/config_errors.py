"""Config errors and limits shared by the config modules.

Split out of ``config.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations




# Per-attachment size cap for chat/file uploads and panel context-docs. Large by design so any
# reasonable document or image is accepted; anything over this is rejected by the multimodal
# handler (``core.attachments.prepare_attachments``) with a clear note rather than processed.
# 50 MB. Centralized here so both the runner config and the handler read the same number.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024


class ConfigFileError(ValueError):
    """A config FILE problem: missing, unreadable, invalid TOML, or naming a key ``RunnerConfig``
    cannot take from a file. A subclass of ``ValueError`` (not a bare ``Exception``) so code that
    already wraps config construction in ``except ValueError`` keeps working; check the type when a
    config-file problem needs different handling than any other invalid config.

    Every one of these is intentionally LOUD — raised, never logged-and-quietly-ignored. This repo
    has scar tissue about a config field that silently did nothing because nobody validated the key
    that set it (see the quest-backend ``UpdateAutopilotRequest`` incident referenced in
    ``docs/writing-a-consumer.md``): a typo'd key in a config file must fail the process at
    startup, loudly naming the mistake, not quietly run with a default nobody chose.
    """
