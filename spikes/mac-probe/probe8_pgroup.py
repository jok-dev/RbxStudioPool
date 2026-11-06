"""Probe 8: does killing a test server's process group take its clients with it?

    python3 probe8_pgroup.py

Starts a test server with 2 clients from the bundle binary in a new session (so it leads its own
process group), marker place swapped in at server.rbxl and put back. Prints each process's group,
SIGKILLs the whole group, and after 10s lists what of the probe's is still alive.
Anything of the probe's still alive at the end is SIGKILLed.
"""

import shutil
import subprocess
import time
from pathlib import Path

from macmcp import STUDIO_BIN, MacStudioMcp, server_rbxl
from probe1_multi import studio_pids
from probe2_args import wait_new
from probe7_stop import alive

STATE = Path(__file__).resolve().parent / "state"


def main():
    run = STATE / f"probe8-{int(time.time())}"
    run.mkdir(parents=True)
    copy = run / "copy.rbxl"
    shutil.copy(STATE / "marker.rbxl", copy)
    before = studio_pids()
    ours = set()
    with server_rbxl(STATE / "marker.rbxl"):
        mcp = MacStudioMcp()
        try:
            known = {s["id"] for s in mcp.list_studios()}
            server = subprocess.Popen([str(STUDIO_BIN), "-task", "StartServer", "-localProjectFile", str(copy),
                                       "-placeId", "0", "-universeId", "0", "-port", "0", "-creatorId", "0",
                                       "-creatorType", "0", "-placeVersion", "0",
                                       "-numTestServerPlayersUponStartup", "2"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            wait_new(mcp, known, 3, timeout=150)
            ours = studio_pids() - before
            print(subprocess.run(["ps", "-o", "pid,pgid,ppid,sess,args", "-p", ",".join(map(str, ours))],
                                 capture_output=True, text=True).stdout)
            pgid = server.pid  # start_new_session makes the server its group's leader
            subprocess.run(["kill", "-KILL", f"-{pgid}"])
            time.sleep(10)
            server.wait()
            left = [p for p in ours if alive(p)]
            print(f"kill -KILL -{pgid}: alive 10s later: {left or 'none'}")
            print(subprocess.run(["pgrep", "-fl", "RobloxStudio"], capture_output=True, text=True).stdout)
        finally:
            mcp.close()
            left = [p for p in ours if alive(p)]
            for pid in left:
                subprocess.run(["kill", "-9", str(pid)], capture_output=True)
            print(f"SIGKILLed leftovers: {left}")


if __name__ == "__main__":
    main()
