"""Whether Shift is physically held, read from the local keyboard devices.

GNOME Terminal, Ptyxis and every other VTE terminal send the same carriage return for Shift+Enter
as for Enter, and speak neither the kitty keyboard protocol nor xterm's modifyOtherKeys, so the byte
stream cannot tell the two apart. The kernel can: ``EVIOCGKEY`` on an evdev node returns which keys
are down right now. The prompt asks this at the moment an Enter arrives, which is a few milliseconds
after the key went down and while Shift is still held.

This only means something when the keyboard is attached to this machine, so it answers False over
SSH, off Linux, and for a user who cannot read ``/dev/input`` (reading it needs the ``input`` group).
"""
from __future__ import annotations

import fcntl
import os
import sys
from typing import Dict, Optional

INPUT_DIR = "/dev/input"

EV_KEY = 0x01
KEY_LEFTSHIFT = 42
KEY_RIGHTSHIFT = 54
KEY_MAX = 0x2FF
KEY_BITMAP_BYTES = KEY_MAX // 8 + 1

_IOC_READ = 2


def _ior(nr: int, size: int) -> int:
    return (_IOC_READ << 30) | (size << 16) | (ord("E") << 8) | nr


EVIOCGKEY = _ior(0x18, KEY_BITMAP_BYTES)
EVIOCGBIT_EV_KEY = _ior(0x20 + EV_KEY, KEY_BITMAP_BYTES)


def _bit(bitmap: bytes, code: int) -> bool:
    return bool(bitmap[code // 8] & (1 << (code % 8)))


def _shift_in(bitmap: bytes) -> bool:
    return _bit(bitmap, KEY_LEFTSHIFT) or _bit(bitmap, KEY_RIGHTSHIFT)


def keyboard_is_local() -> bool:
    """The terminal's keyboard is this machine's keyboard: Linux, and not an SSH session."""
    if not sys.platform.startswith("linux"):
        return False
    return not any(os.environ.get(k) for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))


class PhysicalShiftProbe:
    """Holds the keyboards' event nodes open and reports whether either Shift key is down.

    Nodes are rescanned whenever ``/dev/input`` gains or loses an event node, so a keyboard plugged
    in (or a Bluetooth one reconnecting) after the prompt started is still seen.
    """

    def __init__(self, input_dir: str = INPUT_DIR) -> None:
        self.input_dir = input_dir
        self.enabled = keyboard_is_local()
        self.keyboards: Dict[str, int] = {}
        self.scanned: Optional[frozenset] = None

    def _event_nodes(self) -> frozenset:
        try:
            return frozenset(n for n in os.listdir(self.input_dir) if n.startswith("event"))
        except OSError:
            return frozenset()

    def _rescan(self, nodes: frozenset) -> None:
        self.close()
        for name in sorted(nodes):
            path = os.path.join(self.input_dir, name)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                caps = bytearray(KEY_BITMAP_BYTES)
                fcntl.ioctl(fd, EVIOCGBIT_EV_KEY, caps, True)
            except OSError:
                os.close(fd)
                continue
            if _shift_in(bytes(caps)):
                self.keyboards[path] = fd
            else:
                os.close(fd)
        self.scanned = nodes

    def shift_held(self) -> bool:
        if not self.enabled:
            return False
        nodes = self._event_nodes()
        if nodes != self.scanned:
            self._rescan(nodes)
        for path, fd in list(self.keyboards.items()):
            state = bytearray(KEY_BITMAP_BYTES)
            try:
                fcntl.ioctl(fd, EVIOCGKEY, state, True)
            except OSError:
                # The device went away between scans; forget it and rescan next time.
                os.close(fd)
                del self.keyboards[path]
                self.scanned = None
                continue
            if _shift_in(bytes(state)):
                return True
        return False

    def close(self) -> None:
        for fd in self.keyboards.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self.keyboards = {}
        self.scanned = None


_shared: Optional[PhysicalShiftProbe] = None


def shared_probe() -> PhysicalShiftProbe:
    global _shared
    if _shared is None:
        _shared = PhysicalShiftProbe()
    return _shared
