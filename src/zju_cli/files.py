from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .errors import LocalFileError


def output_path(value: str | Path, overwrite: bool) -> Path:
    target = Path(value).expanduser().absolute()
    if target.is_dir():
        raise LocalFileError(f"目标是文件夹：{target}")
    if os.path.lexists(target) and not overwrite:
        raise LocalFileError(f"目标文件已存在：{target}；需要覆盖请使用 --overwrite")
    return target


@contextmanager
def staged_output(target: Path, overwrite: bool):
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{target.stem}-", suffix=target.suffix, dir=target.parent)
        os.close(fd)
        temporary = Path(name)
        yield temporary
        if overwrite:
            os.replace(temporary, target)
        else:
            # Publishing with a hard link is atomic and refuses an existing target.
            os.link(temporary, target)
    except OSError as exc:
        raise LocalFileError(f"输出文件无法写入或已存在：{target}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
