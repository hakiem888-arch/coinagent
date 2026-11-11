"""OS lock shared by Streamlit and the optional headless worker on one disk."""
import os
from pathlib import Path
from .scanner import ScanBusy


class ProcessLock:
    def __init__(self, db):
        self.path = Path(str(db) + ".scanner.lock")
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    handle.write(b"0"); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            raise ScanBusy("Scanner/worker lain sedang memakai jurnal ini") from None
        self.handle = handle

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None
