"""The multimodal handler: prepare_attachments native vs describe vs extract vs reject."""
from quest_ai_runner.core.attachments import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    prepare_attachments,
)


class _VisionProvider:
    """A vision-capable provider whose answer() (used as the describer) returns a fixed text."""

    def __init__(self, description="A FIXED DESCRIPTION of the image.", native=True):
        self.description = description
        self.answer_calls = []
        if native is not None:
            self.supports_native_images = native

    def plan(self, *a, **k):
        return {}

    def answer(self, messages, *, model, system=None):
        self.answer_calls.append({"messages": messages, "model": model})
        return self.description

    def list_models(self):
        return ["claude-sonnet-4-6"]


def _img(name="pic.png", mime="image/png", data=b"\x89PNG\r\n\x1a\n fake bytes"):
    return {"filename": name, "mime_type": mime, "data": data, "kind": "image"}


# --- native path (vision model + native-capable provider) -------------------

def test_image_to_vision_model_goes_native():
    provider = _VisionProvider(native=True)
    out = prepare_attachments([_img()], model="claude-sonnet-4-6", provider=provider)
    assert out.has_native is True
    assert len(out.native_blocks) == 1
    block = out.native_blocks[0]
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    assert block["source"]["data"]  # base64 string present
    # Native images are NOT described (no extra answer() call for transcription).
    assert provider.answer_calls == []
    # A one-line note still goes to the planner context.
    assert "pic.png" in out.text_context


# --- describe-fallback (non-vision answering model) -------------------------

def test_image_to_nonvision_model_is_described():
    # The answering model is text-only; a separate vision provider transcribes the image.
    answering = _VisionProvider(native=False)        # stands in as the (non-vision) answerer
    describer = _VisionProvider(description="TRANSCRIBED: a bar chart.", native=True)
    out = prepare_attachments(
        [_img()],
        model="gpt-3.5-turbo",                       # NOT vision-capable
        provider=answering,
        vision_provider=describer,
        vision_model="claude-sonnet-4-6",
    )
    assert out.has_native is False                   # nothing sent natively
    assert "TRANSCRIBED: a bar chart." in out.text_context
    assert len(describer.answer_calls) == 1
    # The describer was handed the image as a native block + the describe prompt.
    sent = describer.answer_calls[0]["messages"][0]["content"]
    assert any(b.get("type") == "image" for b in sent)


def test_keyless_text_only_provider_falls_back_to_describe():
    # Provider declares it can't send native blocks even for a vision model id → describe path.
    text_only = _VisionProvider(native=False)
    describer = _VisionProvider(description="DESC", native=True)
    out = prepare_attachments(
        [_img()],
        model="claude-sonnet-4-6",                   # vision-capable model id...
        provider=text_only,                          # ...but provider can't transmit blocks
        vision_provider=describer,
        vision_model="claude-sonnet-4-6",
    )
    assert out.has_native is False
    assert "DESC" in out.text_context
    assert len(describer.answer_calls) == 1


# --- non-image text extraction ----------------------------------------------

def test_text_file_is_extracted_inline():
    provider = _VisionProvider()
    att = {"filename": "notes.txt", "mime_type": "text/plain",
           "data": b"line one\nline two", "kind": "file"}
    out = prepare_attachments([att], model="claude-sonnet-4-6", provider=provider)
    assert out.has_native is False
    assert "line one" in out.text_context
    assert "line two" in out.text_context
    assert "notes.txt" in out.text_context


def test_json_file_extracted_via_direct_text_type():
    provider = _VisionProvider()
    att = {"filename": "data.json", "mime_type": "application/json",
           "data": b'{"a": 1}', "kind": "file"}
    out = prepare_attachments([att], model="claude-sonnet-4-6", provider=provider)
    assert '{"a": 1}' in out.text_context


# --- oversize rejection ------------------------------------------------------

def test_oversize_attachment_is_rejected_not_processed():
    provider = _VisionProvider()
    big = _img(data=b"x" * 1024)
    out = prepare_attachments([big], model="claude-sonnet-4-6", provider=provider,
                              max_attachment_bytes=512)
    assert out.has_native is False                   # never produced a native block
    assert provider.answer_calls == []               # never described
    assert "exceeds" in out.text_context
    assert out.items[0].error == "oversize"


# --- unknown binary note -----------------------------------------------------

def test_unknown_binary_file_gets_clear_note():
    provider = _VisionProvider()
    att = {"filename": "blob.bin", "mime_type": "application/octet-stream",
           "data": bytes(range(256)) * 4, "kind": "file"}   # high-entropy binary
    out = prepare_attachments([att], model="claude-sonnet-4-6", provider=provider)
    assert "content not extractable" in out.text_context
    assert "blob.bin" in out.text_context


def test_any_filetype_accepted_never_raises():
    provider = _VisionProvider()
    weird = {"filename": "thing.xyz", "mime_type": "application/x-made-up",
             "data": b"\x00\x01\x02\x03", "kind": "file"}
    # Must not raise for an unknown type.
    out = prepare_attachments([weird], model="claude-sonnet-4-6", provider=provider)
    assert out.items and out.items[0].kind == "file"


# --- batch + empties ---------------------------------------------------------

def test_empty_input_returns_empty_result():
    provider = _VisionProvider()
    out = prepare_attachments(None, model="claude-sonnet-4-6", provider=provider)
    assert out.native_blocks == [] and out.text_context == "" and out.items == []


def test_mixed_batch_processes_all_concurrently():
    provider = _VisionProvider(native=True)
    atts = [
        _img(name="a.png"),
        {"filename": "b.txt", "mime_type": "text/plain", "data": b"hello", "kind": "file"},
        _img(name="c.png"),
    ]
    out = prepare_attachments(atts, model="claude-sonnet-4-6", provider=provider)
    assert len(out.items) == 3
    assert len(out.native_blocks) == 2               # two images went native
    assert "hello" in out.text_context               # the text file extracted


def test_default_cap_is_50mb():
    assert DEFAULT_MAX_ATTACHMENT_BYTES == 50 * 1024 * 1024
