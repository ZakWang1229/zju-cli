"""Atomic audio export."""
from __future__ import annotations

import shutil

from zju_cli.shared.files import output_path, staged_output


def export_audio(source, target, *, overwrite=False, check_stop=None):
    target = output_path(target, overwrite)
    with staged_output(target, overwrite) as temporary:
        shutil.copyfile(source, temporary)
        if check_stop:
            check_stop()
    return target
