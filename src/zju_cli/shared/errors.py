class ZjuError(Exception):
    """Base error shown to CLI users without a traceback."""
    exit_code = 1


class ConfigError(ZjuError):
    exit_code = 3


class AuthenticationError(ZjuError):
    exit_code = 3


class SessionExpired(AuthenticationError):
    """A read operation may log in again once after this error."""


class RemoteError(ZjuError):
    exit_code = 4


class LocalFileError(ZjuError):
    exit_code = 5


class InputError(ZjuError):
    exit_code = 2


class BusyError(ZjuError):
    exit_code = 6


class CancelledError(ZjuError):
    exit_code = 4
