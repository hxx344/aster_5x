import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from trading.lock import ProcessLock


class ProcessLockTests(unittest.TestCase):
    def test_other_process_blocked_then_can_acquire_after_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trading.lock"
            owner = ProcessLock(path)
            owner.acquire()
            script = "from trading.lock import ProcessLock; import sys; lock=ProcessLock(sys.argv[1]); lock.acquire(); lock.release()"
            try:
                blocked = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True)
                self.assertNotEqual(blocked.returncode, 0)
            finally:
                owner.release()
            free = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True)
            self.assertEqual(free.returncode, 0, free.stderr)
