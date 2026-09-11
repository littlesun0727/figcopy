"""Serialize review and layout mutations across local processes."""

from contextlib import contextmanager
from pathlib import Path
import os

from .errors import CollageError


@contextmanager
def project_edit_lock(directory: Path):
    """Use an exclusive file so parallel tabs cannot accept different revisions."""
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".editing.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise CollageError("PROJECT_BUSY", "项目正在更新；请等待当前任务结束") from exc
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(descriptor)
        lock.unlink(missing_ok=True)
