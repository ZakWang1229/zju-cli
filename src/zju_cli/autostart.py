"""Compatibility launcher for existing Windows logon registrations."""
from zju_cli.frontend.autostart import main

if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(1)
