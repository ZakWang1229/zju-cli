"""Per-user state directory selection."""
from pathlib import Path
import os


def default_data_dir() -> Path:
    if os.name == "nt":
        return (
            Path(os.environ.get(
                "LOCALAPPDATA",
                Path.home() / "AppData" / "Local",
            ))
            / "zju-cli"
        )
    value = os.environ.get("XDG_STATE_HOME", "")
    root = (
        Path(value)
        if value and Path(value).is_absolute()
        else Path.home() / ".local" / "state"
    )
    return root / "zju-cli"
