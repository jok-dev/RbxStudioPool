"""Shared bits for the Mac probes: the spike's StudioMcp client pointed at the Mac StudioMCP binary."""

import contextlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python-pool"))

import pool  # noqa: E402

STUDIO_APP = Path("/Applications/RobloxStudio.app")
STUDIO_BIN = STUDIO_APP / "Contents" / "MacOS" / "RobloxStudio"
STUDIO_MCP = STUDIO_APP / "Contents" / "MacOS" / "StudioMCP"


class MacStudioMcp(pool.StudioMcp):
    def __init__(self):
        super().__init__([str(STUDIO_MCP)])

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()


SERVER_RBXL = Path.home() / "Documents" / "Roblox" / "server.rbxl"


@contextlib.contextmanager
def server_rbxl(place):
    """Put `place` at server.rbxl (the file every test server opens) and put the user's file back after."""
    backup = SERVER_RBXL.with_name("server.rbxl.bak")
    if backup.exists():
        raise SystemExit(f"{backup} already exists; not overwriting it")
    had_file = SERVER_RBXL.exists()
    if had_file:
        SERVER_RBXL.rename(backup)
    try:
        shutil.copy(place, SERVER_RBXL)
        yield
    finally:
        SERVER_RBXL.unlink(missing_ok=True)
        if had_file:
            backup.rename(SERVER_RBXL)
        print(f"restored the user's server.rbxl: {SERVER_RBXL.exists() == had_file}")
