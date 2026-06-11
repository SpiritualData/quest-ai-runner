"""Multimodal + file-attachment handler — the ONE standard path for any attachment.

conceptai (the text provider behind chat) does NOT do multimodal, so the RUNNER owns it. This
module is that owner. Given a list of in-memory attachments and the model/provider that will
actually answer, it produces, per attachment, EITHER:

  * NATIVE image content blocks (base64) — when the answering model is vision-capable AND its
    provider can send native blocks (Anthropic), so the image goes straight to the model; OR
  * TEXT — for everything else: an image the answering model can't take natively is DESCRIBED by
    a separate vision-capable provider/model (describe-fallback), and a non-image file has its
    text EXTRACTED best-effort by type. The text feeds the planner context AND the answer.

It is generic and open-source-clean: no org, no Quest, no secrets. It depends only on the
``ModelProvider`` interface and the ``is_vision_capable`` seam in ``model_registry``. Any filetype
is accepted (no allow/deny list) up to ``max_attachment_bytes``; it NEVER raises on an unknown
type — an unextractable binary becomes a short inventory note.

Attachments are processed CONCURRENTLY (a thread pool), consistent with the orchestrator's sync
model, so a batch of uploads is handled in parallel rather than one at a time.

The in-memory attachment item (what the consumer/backend feeds in) is a plain dict::

    {
        "filename": "diagram.png",      # display name (optional; defaults to "attachment")
        "mime_type": "image/png",       # best-known MIME (optional; guessed from filename if absent)
        "data": b"...",                 # the raw bytes (required)
        "kind": "image" | "file",       # optional; inferred from mime_type when absent
    }

The SAME shape carries both in-chat file uploads and panel-uploaded context-docs, so the backend
feeds both through this one function (per the architecture: one path for both).
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .adapters import ModelProvider
from .model_registry import is_vision_capable

log = logging.getLogger("quest-ai-runner.attachments")

# Default per-attachment size cap (50 MB). Mirrors ``config.MAX_ATTACHMENT_BYTES``; redefined
# here so ``core`` stays import-independent of the top-level config module (the brain depends on
# nothing outside ``core``). A caller may pass a different ``max_attachment_bytes``.
DEFAULT_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

# Image MIME types we can send NATIVELY as Anthropic image blocks. Other image types (e.g. tiff,
# bmp) are still handled — they fall to the describe path, which works on any image bytes the
# vision model accepts, or to the binary note if it can't.
NATIVE_IMAGE_MEDIA_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")

# The centralized DESCRIBE prompt for the vision-fallback path. Generic, no org specifics, no em
# dashes (brand-voice rule). The describing model returns plain text that grounds the answer.
DESCRIBE_PROMPT = (
    "You are transcribing an image so a text-only assistant can use it. Describe this image "
    "thoroughly and factually. Capture all of the following that apply: any text exactly as it "
    "appears (transcribe it verbatim), tables and their values, charts and what they show, "
    "diagrams and their structure, people, objects, and the overall layout. Do not speculate "
    "beyond what is visible and do not add commentary. Write plain prose and short lists only. "
    "Do not use em dashes."
)

# MIME types (and prefixes) whose bytes are plain text we can decode directly. Anything matching
# is read as UTF-8 (errors replaced). Everything text-ish (code, csv, json, xml, yaml, html, …)
# lands here via the text/* prefix or these explicit entries.
_DIRECT_TEXT_TYPES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/javascript",
    "application/x-sh",
    "application/x-python",
    "application/x-www-form-urlencoded",
    "application/csv",
    "image/svg+xml",  # SVG is XML text
}

# How many characters of extracted file text to keep per attachment (keeps the planner context
# and the answer grounding bounded; a huge upload doesn't blow the prompt budget).
_MAX_EXTRACTED_TEXT_CHARS = 20000


@dataclass
class PreparedAttachment:
    """The processed form of one input attachment."""
    filename: str
    mime_type: str
    kind: str                                   # "image" | "file"
    native_block: Optional[Dict[str, Any]] = None   # an Anthropic image content block (native path)
    text: Optional[str] = None                  # description / extracted text / a note (text path)
    error: Optional[str] = None                 # set when rejected (e.g. oversize)


@dataclass
class PreparedAttachments:
    """The result of preparing a batch: native blocks for the answer, text for the planner.

    * ``native_blocks`` — Anthropic image content blocks to append to the final ANSWER message
      (only for images that went the native path). Empty when nothing went native.
    * ``text_context`` — a single text block describing/inventorying ALL attachments (native ones
      get a one-line "image attached" note; described/extracted ones get their text). This feeds
      the PLANNER context and grounds non-native cases. Empty string when there is nothing to say.
    * ``items`` — the per-attachment ``PreparedAttachment`` records (order preserved), for callers
      that want detail (tests, richer UIs).
    """
    native_blocks: List[Dict[str, Any]] = field(default_factory=list)
    text_context: str = ""
    items: List[PreparedAttachment] = field(default_factory=list)

    @property
    def has_native(self) -> bool:
        return bool(self.native_blocks)


# ---------------------------------------------------------------------------
# Normalization of one raw input item.
# ---------------------------------------------------------------------------

def _norm_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a raw input dict into a normalized {filename, mime_type, data, kind}. Never raises."""
    filename = str(raw.get("filename") or "attachment")
    data = raw.get("data")
    if not isinstance(data, (bytes, bytearray)):
        data = b"" if data is None else (
            data.encode("utf-8", "replace") if isinstance(data, str) else b"")
    data = bytes(data)
    mime = (raw.get("mime_type") or "").strip().lower()
    if not mime:
        guessed, _ = mimetypes.guess_type(filename)
        mime = (guessed or "application/octet-stream").lower()
    kind = (raw.get("kind") or "").strip().lower()
    if kind not in ("image", "file"):
        kind = "image" if mime.startswith("image/") else "file"
    return {"filename": filename, "mime_type": mime, "data": data, "kind": kind}


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# File text extraction (best-effort, by type; never raises on unknown).
# ---------------------------------------------------------------------------

def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _truncate(text: str, limit: int = _MAX_EXTRACTED_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… (truncated, {len(text)} chars total)"


def _looks_textual(mime: str) -> bool:
    return mime.startswith("text/") or mime in _DIRECT_TEXT_TYPES


def _extract_pdf(data: bytes) -> Optional[str]:
    """Extract text from a PDF IF a light extractor is already installed; else None.

    No hard dependency: tries pypdf then PyPDF2 (common, optional). Returns None if neither is
    available or extraction yields nothing, so the caller falls back to a binary note.
    """
    for modname, reader_attr in (("pypdf", "PdfReader"), ("PyPDF2", "PdfReader")):
        try:
            mod = __import__(modname)
        except ImportError:
            continue
        try:
            import io
            reader = getattr(mod, reader_attr)(io.BytesIO(data))
            pages = [(p.extract_text() or "") for p in reader.pages]
            text = "\n".join(pages).strip()
            return text or None
        except Exception:  # noqa: BLE001 — a broken/encrypted PDF just falls to the note
            return None
    return None


def _extract_docx(data: bytes) -> Optional[str]:
    """Extract text from a .docx IF python-docx is installed; else None."""
    try:
        import docx  # python-docx, optional
    except ImportError:
        return None
    try:
        import io
        document = docx.Document(io.BytesIO(data))
        text = "\n".join(p.text for p in document.paragraphs).strip()
        return text or None
    except Exception:  # noqa: BLE001
        return None


def _binary_note(filename: str, mime: str, size: int) -> str:
    """The clear, honest note for a file whose content we can't extract here."""
    return f"binary file {filename} ({mime}, {_human_size(size)}); content not extractable"


def _extract_file_text(item: Dict[str, Any]) -> str:
    """Best-effort text for a non-image file. Any type accepted; NEVER raises.

    Direct-decode text/code/csv/json; pdf/docx via a light extractor if one is installed; anything
    else (or a failed extract) becomes a clear binary note.
    """
    filename, mime, data = item["filename"], item["mime_type"], item["data"]
    label = f"FILE: {filename} ({mime}, {_human_size(len(data))})"
    try:
        if _looks_textual(mime):
            return f"{label}\n{_truncate(data.decode('utf-8', errors='replace'))}"
        if mime == "application/pdf" or filename.lower().endswith(".pdf"):
            text = _extract_pdf(data)
            return f"{label}\n{_truncate(text)}" if text else _binary_note(filename, mime, len(data))
        if (mime in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",)
                or filename.lower().endswith(".docx")):
            text = _extract_docx(data)
            return f"{label}\n{_truncate(text)}" if text else _binary_note(filename, mime, len(data))
        # Unknown type: try a tolerant UTF-8 decode; if it yields little printable content treat
        # it as binary. This catches stray text files with odd MIME types without spewing garbage
        # for true binaries.
        decoded = data.decode("utf-8", errors="ignore")
        printable = sum(1 for c in decoded if c.isprintable() or c in "\n\r\t")
        if decoded and printable >= 0.85 * len(decoded) and len(decoded.strip()) > 0:
            return f"{label}\n{_truncate(decoded)}"
        return _binary_note(filename, mime, len(data))
    except Exception:  # noqa: BLE001 — extraction must never raise into the handler
        return _binary_note(filename, mime, len(data))


# ---------------------------------------------------------------------------
# Image handling (native block vs. describe-fallback).
# ---------------------------------------------------------------------------

def _native_image_block(item: Dict[str, Any]) -> Dict[str, Any]:
    """Build an Anthropic native image content block from image bytes."""
    media = item["mime_type"]
    if media not in NATIVE_IMAGE_MEDIA_TYPES:
        media = "image/png"  # safe default label; the bytes are passed as-is
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media, "data": _b64(item["data"])},
    }


def _describe_image(item: Dict[str, Any], vision_provider: ModelProvider,
                    vision_model: str) -> str:
    """Describe an image with a vision-capable provider/model into grounding text. Never raises."""
    block = _native_image_block(item)
    messages = [{
        "role": "user",
        "content": [block, {"type": "text", "text": DESCRIBE_PROMPT}],
    }]
    try:
        desc = vision_provider.answer(messages, model=vision_model)
    except Exception as e:  # noqa: BLE001 — a describe failure degrades to a note, not a crash
        log.warning("image describe failed for %s (%s)", item.get("filename"), type(e).__name__)
        return (f"IMAGE: {item['filename']} ({item['mime_type']}); "
                "could not be transcribed automatically")
    return f"IMAGE: {item['filename']} (transcribed):\n{(desc or '').strip()}"


# ---------------------------------------------------------------------------
# Per-attachment processing + the public entry point.
# ---------------------------------------------------------------------------

def _prepare_one(
    raw: Dict[str, Any],
    *,
    can_send_native: bool,
    vision_provider: Optional[ModelProvider],
    vision_model: Optional[str],
    max_attachment_bytes: int,
) -> PreparedAttachment:
    item = _norm_item(raw)
    filename, mime, data, kind = item["filename"], item["mime_type"], item["data"], item["kind"]

    # Size cap first — reject oversize with a clear note, never process it.
    if len(data) > max_attachment_bytes:
        note = (f"attachment {filename} rejected: {_human_size(len(data))} exceeds the "
                f"{_human_size(max_attachment_bytes)} limit")
        return PreparedAttachment(filename=filename, mime_type=mime, kind=kind,
                                  text=note, error="oversize")
    if not data:
        return PreparedAttachment(filename=filename, mime_type=mime, kind=kind,
                                  text=f"attachment {filename}: empty (no content)")

    if kind == "image":
        native_ok = (can_send_native and mime in NATIVE_IMAGE_MEDIA_TYPES)
        if native_ok:
            # Native path: the answering model takes the image directly. We still add a one-line
            # text note so the PLANNER knows an image is present without re-encoding it.
            return PreparedAttachment(
                filename=filename, mime_type=mime, kind="image",
                native_block=_native_image_block(item),
                text=f"IMAGE: {filename} ({mime}); attached natively to the answer",
            )
        # Describe-fallback: the answering model can't take this image natively (non-vision model,
        # a non-native provider like the keyless CLI, or a non-native image type). Transcribe it.
        if vision_provider is not None and vision_model:
            return PreparedAttachment(
                filename=filename, mime_type=mime, kind="image",
                text=_describe_image(item, vision_provider, vision_model),
            )
        # No vision provider available to describe with — honest note instead of dropping it.
        return PreparedAttachment(
            filename=filename, mime_type=mime, kind="image",
            text=(f"IMAGE: {filename} ({mime}, {_human_size(len(data))}); the answering model "
                  "cannot view images and no describer is configured"),
        )

    # Non-image file: extract text best-effort.
    return PreparedAttachment(filename=filename, mime_type=mime, kind="file",
                              text=_extract_file_text(item))


def prepare_attachments(
    attachments: Optional[List[Dict[str, Any]]],
    *,
    model: str,
    provider: ModelProvider,
    vision_provider: Optional[ModelProvider] = None,
    vision_model: Optional[str] = None,
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_workers: int = 8,
) -> PreparedAttachments:
    """Prepare a batch of in-memory attachments for the answering ``model``/``provider``.

    Per attachment, decides native-vs-describe-vs-extract:

      * image + ``is_vision_capable(model)`` AND ``provider`` can send native blocks AND a
        natively-supported image type → a native base64 image block (for the answer call);
      * image otherwise → DESCRIBED via ``vision_provider``/``vision_model`` into grounding text
        (the default ``vision_provider`` is ``provider`` itself when it is vision-capable; a
        caller wiring a keyless/non-vision answering provider should pass a vision-capable
        ``vision_provider``);
      * non-image file → best-effort text extraction by type (any type accepted, never raises).

    Enforces ``max_attachment_bytes`` (oversize → a clear note, not processed) and runs the batch
    CONCURRENTLY. Returns ``PreparedAttachments`` with ``native_blocks`` (answer) and
    ``text_context`` (planner + grounding). Returns an empty result for ``None``/empty input.

    Args:
        model: the id of the model that will ANSWER (capability decided via ``is_vision_capable``).
        provider: the answering ``ModelProvider``. Whether it can SEND native image blocks is read
            from its optional ``supports_native_images`` attribute/method; absent → inferred from
            the model's vision capability (the Anthropic reference provider does support them).
        vision_provider/vision_model: the describer for the fallback path. If ``vision_provider``
            is None it defaults to ``provider`` when that provider+model are vision-capable.
        max_attachment_bytes: per-attachment hard cap (default 50 MB).
        max_workers: thread-pool width for concurrent processing.
    """
    if not attachments:
        return PreparedAttachments()

    model_vision = is_vision_capable(model)
    can_send_native = model_vision and _provider_sends_native(provider, model)

    # Resolve the describer for the fallback path. Default: reuse the answering provider/model when
    # it is itself vision-capable (the common single-provider case). When the answering model is
    # NOT vision-capable, a caller must supply a vision_provider/model to get descriptions; absent
    # one, images degrade to honest notes (handled in _prepare_one).
    eff_vision_provider = vision_provider
    eff_vision_model = vision_model
    if eff_vision_provider is None and model_vision:
        eff_vision_provider = provider
        eff_vision_model = eff_vision_model or model
    elif eff_vision_provider is not None and not eff_vision_model:
        # A describer was supplied without an explicit model — let it pick its own default by
        # leaving the model empty only if it can; otherwise reuse the answering model id.
        eff_vision_model = model if is_vision_capable(model) else model

    items: List[Dict[str, Any]] = list(attachments)
    results: List[Optional[PreparedAttachment]] = [None] * len(items)

    def work(idx_raw):
        idx, raw = idx_raw
        try:
            return idx, _prepare_one(
                raw if isinstance(raw, dict) else {},
                can_send_native=can_send_native,
                vision_provider=eff_vision_provider,
                vision_model=eff_vision_model,
                max_attachment_bytes=max_attachment_bytes,
            )
        except Exception as e:  # noqa: BLE001 — one bad item must not sink the batch
            log.warning("attachment prepare failed (%s)", type(e).__name__)
            fn = (raw.get("filename") if isinstance(raw, dict) else None) or "attachment"
            return idx, PreparedAttachment(filename=str(fn), mime_type="application/octet-stream",
                                           kind="file", text=f"attachment {fn}: processing error",
                                           error=type(e).__name__)

    if len(items) == 1:
        idx, prepared = work((0, items[0]))
        results[idx] = prepared
    else:
        workers = max(1, min(max_workers, len(items)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for idx, prepared in pool.map(work, list(enumerate(items))):
                results[idx] = prepared

    prepared_items = [r for r in results if r is not None]
    native_blocks = [r.native_block for r in prepared_items if r.native_block is not None]
    text_parts = [r.text for r in prepared_items if r.text]
    text_context = ""
    if text_parts:
        text_context = ("--- ATTACHMENTS ("
                        f"{len(prepared_items)}) ---\n" + "\n\n".join(text_parts))
    return PreparedAttachments(native_blocks=native_blocks, text_context=text_context,
                               items=prepared_items)


def _provider_sends_native(provider: ModelProvider, model: str) -> bool:
    """Whether ``provider`` can transmit native image content blocks for ``model``.

    Read from an optional ``supports_native_images`` attribute/method on the provider so a consumer
    can declare it explicitly (e.g. the keyless CLI provider sets it False). When the provider does
    not declare it, we infer from the model's vision capability: the reference ``AnthropicProvider``
    passes content blocks straight to the SDK, so a vision-capable model on an undeclared provider
    is assumed native-capable. A provider that is text-only should set the flag False.
    """
    flag = getattr(provider, "supports_native_images", None)
    if callable(flag):
        try:
            return bool(flag(model))
        except Exception:  # noqa: BLE001
            return False
    if isinstance(flag, bool):
        return flag
    return is_vision_capable(model)