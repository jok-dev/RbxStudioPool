"""Probe 2b: does `-task EditPlace -placeId P -universeId U` open a cloud place on a Mac?

    python3 probe2_cloud.py PLACE UNIVERSE

Opens the place, waits for it in the Studio MCP, reads game.PlaceId/GameId and the Team Create
signals (read only), then SIGKILLs the Studio so nothing can be saved. Never publishes or saves.
"""

import subprocess
import sys
import time

from macmcp import STUDIO_BIN, MacStudioMcp
from pool import TEAM_CREATE_PROBE

IDS = 'return game.PlaceId .. " " .. game.GameId .. " " .. game.Name'


def main():
    place, universe = sys.argv[1], sys.argv[2]
    mcp = MacStudioMcp()
    proc = None
    try:
        known = {s["id"] for s in mcp.list_studios()}
        t0 = time.time()
        proc = subprocess.Popen([str(STUDIO_BIN), "-task", "EditPlace", "-placeId", place, "-universeId", universe],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        if proc.poll() is not None:
            raise RuntimeError(f"Studio exited at once with code {proc.returncode}")
        sid = None
        while time.time() - t0 < 180 and sid is None:
            new = [s["id"] for s in mcp.list_studios() if s["id"] not in known]
            sid = new[0] if new else None
            time.sleep(1)
        if sid is None:
            raise TimeoutError("no new Studio in the MCP after 180s")
        print(f"studio {sid} in the MCP after {time.time() - t0:.0f}s")
        while True:
            try:
                ids = mcp.luau(sid, IDS, timeout=20)
                break
            except Exception as e:
                if time.time() - t0 > 240:
                    raise TimeoutError(f"Luau never answered: {e}")
                time.sleep(2)
        print(f"Luau after {time.time() - t0:.0f}s: PlaceId GameId Name = {ids.strip()}")
        print(f"Team Create signals: {mcp.luau(sid, TEAM_CREATE_PROBE).strip()}")
        _, state = mcp.call("get_studio_state", {"studio_id": sid})
        print(f"state: {state.strip()[:300]}")
    finally:
        mcp.close()
        if proc is not None:
            subprocess.run(["kill", "-9", str(proc.pid)], capture_output=True)
            print(f"sent SIGKILL to {proc.pid}")


if __name__ == "__main__":
    main()
