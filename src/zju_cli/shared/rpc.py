"""Shared loopback RPC constants and endpoint discovery."""
from zju_cli.shared.runtime import local_endpoint
PROTOCOL = 1
MAX_REQUEST = 4 * 1024 * 1024

def endpoint(settings):
    return local_endpoint()
