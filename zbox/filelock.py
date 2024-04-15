import errno
import fcntl
import time
from datetime import datetime
from typing import Optional

from typeguard import typechecked


class FileLock:
    """
    A simple file locker class that takes a fcntl() lock on given file with polling and timeout.
    The lock file should always be separate from the resource being locked (if the resource
    is also a file).

    The file is created on first access or truncated if it exists, and never removed thereafter
    to avoid any complications. Lock files on NFS files may or may not work as expected
    depending on the NFS server characteristics, so this class can safely be used only
    with the lock file on the local filesystem.

    Usage:
        with FileLock("file.lock", timeout_secs=100):
          <code>
    """

    @typechecked
    def __init__(self, lock_file: str, timeout_secs: float = 300.0, poll_interval: float = 1.0):
        """
        Initialize the lock giving a file which should be a separate lock file from the
        actual resource to be locked. This file is created or truncated on acquisition.

        :param lock_file: the lock file which can be any unique file corresponding to
                          the resource being locked
        :param timeout_secs: lock timeout in seconds (use negative for infinite wait)
        :param poll_interval: polling interval at which to check for lock to be available
        """
        self._lock_file = lock_file
        self._timeout = timeout_secs
        self._poll = poll_interval

    def __enter__(self):
        self._lock_fd = open(self._lock_file, "w+")
        start_time: Optional[datetime] = None
        remaining_time = self._timeout
        while remaining_time != 0:
            try:
                fcntl.lockf(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as ex:
                if ex.errno == errno.EACCES or ex.errno == errno.EAGAIN:
                    # start proper timing only after first failure
                    if not start_time:
                        start_time = datetime.now()
                    # wait for poll time, then try again
                    time.sleep(self._poll)
                    # treat negative timeout as infinite where remaining_time will never reach 0
                    if remaining_time > 0:
                        remaining_time -= self._poll
                        if remaining_time < 0:
                            remaining_time = 0
                else:
                    raise
        wait_time = (datetime.now() - start_time).total_seconds() if start_time else 0.0
        raise TimeoutError(f"Failed to lock '{self._lock_file}' in {wait_time} seconds")

    def __exit__(self, ex_type, ex_value, ex_traceback):
        fcntl.lockf(self._lock_fd, fcntl.LOCK_UN)
        self._lock_fd.close()
