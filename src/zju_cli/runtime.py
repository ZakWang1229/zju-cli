"""Per-user local RPC identity, independent of school credentials."""
import hashlib
import os
from pathlib import Path
import secrets
import subprocess


def runtime_dir():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "zju-cli" / "run"
    return Path(os.environ.get("XDG_RUNTIME_DIR", Path.home() / ".local/state")) / "zju-cli-run"


def protect(path):
    if os.name == "nt":
        identity = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"],
            text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        import csv
        sid = next(csv.reader([identity.strip()]))[1]
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


def local_endpoint():
    root = runtime_dir()
    root.mkdir(parents=True, exist_ok=True)
    protect(root)
    secret = root / "rpc.key"
    if not secret.exists():
        temporary = root / ("key-" + secrets.token_hex(16))
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_hex(32))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, secret)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    key = secret.read_text().strip()
    if len(key) != 64:
        raise RuntimeError("Invalid local RPC key")
    identity = str(root.resolve()).casefold().encode()
    port = 20000 + int.from_bytes(hashlib.sha256(identity).digest()[:4], "big") % 40000
    return port, key
