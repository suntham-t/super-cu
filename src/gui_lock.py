"""Process-wide GUI mutex shared by every agent working on this plugin.

The desktop is a single shared resource: three agents run against the same
keyboard and mouse. Any state-changing GUI action must hold this lock. Pure
observation (snapshot / screenshot) must NOT take it.

Usage:
    from gui_lock import gui_locked, gui_try_lock
    with gui_locked():            # blocks up to 30s, raises GuiBusy after that
        do_something_that_moves_the_mouse()

Design: an exclusive lock on a file via msvcrt on Windows. The OS releases it
when the holding process dies, so a crashed agent cannot wedge the desktop.
"""
from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

LOCK_PATH = Path(__file__).resolve().parent.parent / ".gui.lock"

if sys.platform == "win32":
    import msvcrt


class GuiBusy(Exception):
    pass


@contextlib.contextmanager
def gui_locked(timeout_s: float = 30.0, poll_s: float = 0.05):
    """Acquire the exclusive GUI lock, yielding once it is held."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                if sys.platform == "win32":
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise GuiBusy(f"GUI busy for more than {timeout_s}s ({LOCK_PATH})")
                time.sleep(poll_s)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid():>10}".encode())
        except OSError:
            pass
        yield
    finally:
        try:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(fd)


def gui_try_lock() -> bool:
    """True if the lock is currently free/acquirable (non-blocking probe)."""
    try:
        with gui_locked(timeout_s=0, poll_s=0):
            return True
    except GuiBusy:
        return False
