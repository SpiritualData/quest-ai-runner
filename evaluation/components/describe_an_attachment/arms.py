"""Shared machinery of the arms: the case's image rendered from its spec, a vision provider on the Claude Code subscription, and the
text-only answering step that sees what ``prepare_attachments`` produced."""
import base64
import io
import json
import subprocess

import common  # noqa: F401  (puts the repository on the path)
from PIL import Image, ImageDraw, ImageFont
from quest_ai_runner import usage
from quest_ai_runner.adapters import claude_cli_provider as cli
from quest_ai_runner.core.attachments import prepare_attachments

VISION_MODEL = "claude-haiku"   # the family alias the claude CLI resolves; the describing model of this arm
ANSWER_MODEL = "claude-haiku"
TEXT_ONLY_ID = "text-only-model"   # the id the library sees for the answerer: not vision capable, so it must describe or name an image
ANSWER_TAG = "eval.answer"
SYSTEM = ("You are Quest AI, the assistant inside a goal tracking app, answering one message from the person. You can read only text: "
          "anything about an attached image is in the attachment notes below. Answer in at most 60 words. If the notes do not tell "
          "you what the image shows, say you cannot see it and do not guess. Never use em dashes.")


def render(spec):
    """PNG bytes of the case's image: ``rows`` (a table, first row the header), ``lines`` (plain text) or ``bars`` (a labelled bar chart)."""
    font = ImageFont.load_default(size=22)
    if "bars" in spec:
        bars = spec["bars"]
        img = Image.new("RGB", (120 + 110 * len(bars), 380), "white")
        draw = ImageDraw.Draw(img)
        top = max(v for _, v in bars)
        draw.text((20, 15), spec["title"], fill="black", font=font)
        for i, (label, value) in enumerate(bars):
            x, height = 60 + 110 * i, int(250 * value / top)
            draw.rectangle([x, 330 - height, x + 70, 330], fill=(60, 110, 200))
            draw.text((x, 330 - height - 28), str(value), fill="black", font=font)
            draw.text((x, 340), label, fill="black", font=font)
    else:
        rows = spec.get("rows") or [[line] for line in spec["lines"]]
        widths = [max(int(draw_len(font, str(r[c])) if c < len(r) else 0) for r in rows) + 24 for c in range(max(len(r) for r in rows))]
        img = Image.new("RGB", (sum(widths) + 20, 50 * len(rows) + 30), "white")
        draw = ImageDraw.Draw(img)
        for ri, row in enumerate(rows):
            x = 10
            for ci, cell in enumerate(row):
                draw.text((x, 15 + 50 * ri), str(cell), fill="black", font=font)
                x += widths[ci]
            if len(rows) > 1:
                draw.line([10, 45 + 50 * ri, sum(widths), 45 + 50 * ri], fill=(150, 150, 150))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def draw_len(font, text):
    return font.getlength(text)


class VisionCli(cli.ClaudeCliProvider):
    """``claude -p`` that takes image blocks: the messages go to the CLI as one stream-json user turn. Text-only messages take the
    parent's path. Usage is added up exactly as the parent does it."""
    supports_native_images = True

    def answer(self, messages, *, model, system=None, layers=None):
        blocks = [b for m in messages if isinstance(m.get("content"), list) for b in m["content"]]
        if not any(b.get("type") == "image" for b in blocks):
            return super().answer(messages, model=model, system=system, layers=layers)
        binary = self._resolve_binary()
        cmd = [binary, "-p", "--output-format", "stream-json", "--input-format", "stream-json", "--verbose"]
        name = cli.cli_model(model)
        if name:
            cmd += ["--model", name]
        supported = cli._supported_flags(binary)
        if "--exclude-dynamic-system-prompt-sections" in supported:
            cmd += ["--system-prompt", system or cli._PURE_COMPLETION_SYSTEM]
        for flag, args in cli._ONE_SHOT_FLAGS.items():
            if flag in supported and flag != "--effort":
                cmd += args
        turn = {"type": "user", "message": {"role": "user", "content": blocks}}
        proc = subprocess.run(cmd, input=(json.dumps(turn) + "\n").encode(), env=self._build_env(), cwd=cli._neutral_cwd(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.timeout_seconds)
        out = (proc.stdout or b"").decode("utf-8", errors="replace")
        envelope = None
        for line in out.splitlines():
            try:
                got = json.loads(line)
            except ValueError:
                continue
            if isinstance(got, dict) and got.get("type") == "result":
                envelope = got
        if proc.returncode != 0 or not envelope or envelope.get("is_error"):
            raise RuntimeError(f"claude CLI vision call failed: {(envelope or {}).get('result') or (proc.stderr or b'').decode()[:160]}")
        self._accumulate_usage(envelope)
        return envelope.get("result") or ""


def text_only_answerer(provider):
    """The answering model's side of the keyless path: it cannot take an image, so ``prepare_attachments`` must describe or name it."""
    provider.supports_native_images = False
    return provider


def run(case, answerer, vision, prepare=prepare_attachments):
    """Prepare the image as the library does, then let the text-only answering model answer the question from what it was given."""
    item = {"filename": case["filename"], "mime_type": "image/png", "data": render(case["image"]), "kind": "image"}
    prepared = prepare([item], model=TEXT_ONLY_ID, provider=text_only_answerer(answerer),
                                   vision_provider=vision, vision_model=VISION_MODEL if vision else None)
    grounding = prepared.text_context
    prompt = f"Attachment notes:\n{grounding}\n\nThe person says: {case['question']}"
    usage.tag_spec(ANSWER_TAG)
    try:
        reply = answerer.answer([{"role": "user", "content": prompt}], model=ANSWER_MODEL, system=SYSTEM)
    finally:
        usage.tag_spec("")
    return {"reply": reply or "", "grounding": grounding, "described": vision is not None}
