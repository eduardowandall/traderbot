"""Um processo de execução por modo (`run` ou `serve`): lock do sistema.

Os buckets de um modo dividem a carteira e o ledger; o cache de saldo, a
alocação e a reconciliação (B2) só valem se um processo só executa ordens.
`ModeLock` usa o lock do sistema operacional (`msvcrt.locking` no Windows,
`fcntl.flock` nos outros) num arquivo em `data_dir()`: se o processo morre, o
sistema solta o lock, sem arquivo velho para limpar.
"""

import os
from pathlib import Path
from typing import IO

from trader.shared.paths import data_dir

if os.name == "nt":
    import msvcrt

    def _try_lock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ModeBusyError(RuntimeError):
    """Outro processo já executa ordens neste modo."""


def lock_path(mode: str) -> Path:
    return data_dir() / f"trader-{mode}.lock"


class ModeLock:
    def __init__(self, mode: str):
        self.mode = str(mode)
        self.path = lock_path(self.mode)
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            _try_lock(handle)
        except OSError:
            handle.close()
            raise ModeBusyError(
                f"outro processo já executa o modo {self.mode} (`run` ou `serve`); "
                f"para várias specs ao mesmo tempo use `serve {self.mode}` + `connect`"
            ) from None
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> ModeLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
