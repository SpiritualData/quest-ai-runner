"""PhysicalShiftProbe reads Shift from evdev and stays silent where that would be wrong."""

from __future__ import annotations

import fcntl

from quest_ai_runner import keyboard_state
from quest_ai_runner.keyboard_state import (
    EVIOCGBIT_EV_KEY,
    EVIOCGKEY,
    KEY_BITMAP_BYTES,
    KEY_LEFTSHIFT,
    KEY_RIGHTSHIFT,
    PhysicalShiftProbe,
)


def bitmap(*codes: int) -> bytes:
    out = bytearray(KEY_BITMAP_BYTES)
    for code in codes:
        out[code // 8] |= 1 << (code % 8)
    return bytes(out)


def test_ioctl_numbers_match_linux_input_h():
    # _IOC(_IOC_READ, 'E', 0x18, 96) and _IOC(_IOC_READ, 'E', 0x21, 96), from linux/input.h.
    assert EVIOCGKEY == 0x80604518
    assert EVIOCGBIT_EV_KEY == 0x80604521


def fake_devices(monkeypatch, tmp_path, caps: dict, down: dict):
    for name in caps:
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "mice").write_bytes(b"")
    fd_names = {}
    real_open = keyboard_state.os.open

    def fake_open(path, flags):
        fd = real_open(path, keyboard_state.os.O_RDONLY)
        fd_names[fd] = path.rsplit("/", 1)[1]
        return fd

    def fake_ioctl(fd, request, buf, mutate):
        name = fd_names[fd]
        src = caps[name] if request == EVIOCGBIT_EV_KEY else down[name]
        buf[:] = src
        return 0

    monkeypatch.setattr(keyboard_state.os, "open", fake_open)
    monkeypatch.setattr(fcntl, "ioctl", fake_ioctl)
    monkeypatch.setattr(keyboard_state, "keyboard_is_local", lambda: True)


def test_reports_either_shift_on_a_keyboard(monkeypatch, tmp_path):
    caps = {"event3": bitmap(KEY_LEFTSHIFT, KEY_RIGHTSHIFT, 28), "event7": bitmap(272)}
    down = {"event3": bitmap(), "event7": bitmap(KEY_LEFTSHIFT)}
    fake_devices(monkeypatch, tmp_path, caps, down)
    probe = PhysicalShiftProbe(str(tmp_path))
    assert list(probe.keyboards) == []
    # The mouse node reports no Shift capability, so its (bogus) Shift bit is never consulted.
    assert probe.shift_held() is False
    assert [p.rsplit("/", 1)[1] for p in probe.keyboards] == ["event3"]

    down["event3"] = bitmap(KEY_RIGHTSHIFT)
    assert probe.shift_held() is True
    down["event3"] = bitmap(KEY_LEFTSHIFT, 28)
    assert probe.shift_held() is True
    probe.close()


def test_a_keyboard_plugged_in_later_is_picked_up(monkeypatch, tmp_path):
    caps = {"event3": bitmap(KEY_LEFTSHIFT)}
    down = {"event3": bitmap(), "event18": bitmap(KEY_LEFTSHIFT)}
    fake_devices(monkeypatch, tmp_path, caps, down)
    probe = PhysicalShiftProbe(str(tmp_path))
    assert probe.shift_held() is False
    caps["event18"] = bitmap(KEY_LEFTSHIFT)
    (tmp_path / "event18").write_bytes(b"")
    assert probe.shift_held() is True
    probe.close()


def test_silent_over_ssh(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    assert keyboard_state.keyboard_is_local() is False
    assert PhysicalShiftProbe().shift_held() is False


def test_silent_without_input_group_access(monkeypatch, tmp_path):
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "event0").write_bytes(b"")
    (tmp_path / "event0").chmod(0)
    probe = PhysicalShiftProbe(str(tmp_path))
    assert probe.shift_held() is False
