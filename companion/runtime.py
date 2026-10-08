from contextlib import contextmanager
from pathlib import Path


@contextmanager
def process_lock(path):
    """OS lock, automatically released even if the process crashes."""
    import os

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    stream.seek(0)
    if os.fstat(stream.fileno()).st_size == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise SystemExit("Этот экземпляр бота уже запущен с той же базой.") from None
    try:
        yield
    finally:
        stream.close()
