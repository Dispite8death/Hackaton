"""Share access to archive bytes across processes and give retention exclusive access."""

import os
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


class ArchiveBusy(RuntimeError):
    pass


@contextmanager
def archive_access(conn, *, exclusive=False):
    location=conn.execute("PRAGMA database_list").fetchone()[2]
    if not location:
        yield
        return
    db=Path(location)
    with db.with_name("."+db.name+".archive.lock").open("a+b") as stream:
        if os.name=="nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes as w

            class Overlapped(ctypes.Structure):
                _fields_=[("Internal",ctypes.c_size_t),("InternalHigh",ctypes.c_size_t),("Offset",w.DWORD),
                          ("OffsetHigh",w.DWORD),("hEvent",w.HANDLE)]

            kernel=ctypes.WinDLL("kernel32",use_last_error=True)
            lock=kernel.LockFileEx
            lock.argtypes=[w.HANDLE,w.DWORD,w.DWORD,w.DWORD,w.DWORD,ctypes.POINTER(Overlapped)]
            lock.restype=w.BOOL
            handle=msvcrt.get_osfhandle(stream.fileno())
            position=Overlapped()
            flags=3 if exclusive else 0  # exclusive and fail immediately, or a blocking shared lock
            if not lock(handle,flags,0,1,0,ctypes.byref(position)):
                error=ctypes.get_last_error()
                if exclusive and error in {32,33,158}:
                    raise ArchiveBusy("Идёт загрузка или проверка материалов")
                raise ctypes.WinError(error)
        else:
            import fcntl
            flags=fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(stream,flags)
            except BlockingIOError as exc:
                raise ArchiveBusy("Идёт загрузка или проверка материалов") from exc
        # The OS releases this lock on close and if the process exits mid-operation.
        yield


def uses_archive(function):
    @wraps(function)
    def guarded(conn,*args,**kwargs):
        with archive_access(conn):
            return function(conn,*args,**kwargs)
    return guarded
