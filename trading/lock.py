"""OS-owned per-database process lock; automatically released after a crash."""
import os
from pathlib import Path
import threading

from .models import TradingError


class ProcessLock:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.file = None
        self.guard = threading.Lock()

    def acquire(self):
        with self.guard:
            if self.file is not None:
                raise TradingError("该进程已持有交易服务锁，不能重复启动")
            candidate = self.path.open("a+b")
            try:
                if candidate.tell() == 0:
                    candidate.write(b"0")
                    candidate.flush()
                candidate.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(candidate.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                candidate.close()
                raise TradingError("同一数据目录已经有交易服务运行") from None
            self.file = candidate

    def release(self):
        with self.guard:
            if self.file is not None:
                try:
                    if os.name == "nt":
                        import msvcrt
                        self.file.seek(0)
                        msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
                finally:
                    self.file.close()
                    self.file = None
