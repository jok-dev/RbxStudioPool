"""Probe 2: do the Windows multi-client launch arguments work on a Mac?

    python3 probe2_args.py [--clients 1] [--add]

Launches a test server on its own copy of state/blank.rbxl with the minimal Windows server line,
waits for the clients it starts, and prints the process tree with each command line (from
`ps -o args=`, children from `pgrep -P`). Each new Studio MCP id is matched to server or client
with get_studio_state. With --add, a copy of a client's command line with a new -instanceId should
join the running test. Transport tokens are redacted in the output. Only the probe's own process
tree is killed at the end.
"""

import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from macmcp import STUDIO_BIN, MacStudioMcp

STATE = Path(__file__).resolve().parent / "state"
TEMPLATE = STATE / "blank.rbxl"
LOGS = Path.home() / "Library" / "Logs" / "Roblox"


def args_of(pid):
    return subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def children(pid):
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def tree(pid):
    out = [pid]
    for c in children(pid):
        out += tree(c)
    return out


def redact(s):
    return re.sub(r"(-rbxTransportToken )\S+", r"\1<redacted>", s)


def studio_children(pid):
    return [c for c in children(pid) if args_of(c).startswith(str(STUDIO_BIN) + " ")]


def kind_of(mcp, sid):
    _, text = mcp.call("get_studio_state", {"studio_id": sid}, timeout=30)
    m = re.search(r"Focused DataModel in the viewport: (\w+)", text)
    return m.group(1) if m else text.strip()[:120]


def wait_new(mcp, known, want, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        new = [s["id"] for s in mcp.list_studios() if s["id"] not in known]
        if len(new) >= want:
            return new, time.time() - t0
        time.sleep(1)
    raise TimeoutError(f"wanted {want} new Studios in the MCP, timed out after {timeout}s")


def opened_places(since):
    """`open place` lines from Studio logs written since `since` (log file → place)."""
    found = {}
    for f in LOGS.glob("*_Studio_*_last.log"):
        if f.stat().st_mtime < since:
            continue
        text = f.read_text(errors="replace")
        first = text.splitlines()[0] if text else ""
        m = re.search(r"-instanceId (\S+)", first)
        places = re.findall(r"open place \(identifier = ([^)]*)\)", text)
        found[f.name] = {"instance": m.group(1) if m else "edit?", "places": sorted(set(places))}
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", type=int, default=1)
    ap.add_argument("--add", action="store_true")
    args = ap.parse_args()

    run = STATE / f"probe2-{int(time.time())}"
    run.mkdir(parents=True)
    place = run / "copy.rbxl"
    shutil.copy(TEMPLATE, place)
    mcp = MacStudioMcp()
    server = None
    t_start = time.time()
    try:
        known = {s["id"] for s in mcp.list_studios()}
        cmd = [str(STUDIO_BIN), "-task", "StartServer", "-localProjectFile", str(place), "-placeId", "0",
               "-universeId", "0", "-port", "0", "-creatorId", "0", "-creatorType", "0", "-placeVersion", "0",
               "-numTestServerPlayersUponStartup", str(args.clients)]
        server = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        if server.poll() is not None:
            raise RuntimeError(f"server exited at once with code {server.returncode}")
        print(f"server pid {server.pid}")

        new, t = wait_new(mcp, known, 1 + args.clients, timeout=150)
        known |= set(new)
        print(f"{len(new)} new Studios in the MCP after {t:.0f}s")
        for sid in new:
            print(f"  {sid}: {kind_of(mcp, sid)}")

        print("\nprocess tree:")
        for pid in tree(server.pid):
            print(f"  {pid} (parent {args_of_parent(pid)}): {redact(args_of(pid))[:600]}")

        clients = studio_children(server.pid)
        print(f"\nStudio clients started by the server: {clients}")

        if args.add and clients:
            line = args_of(clients[0])
            # ps prints the command line joined by spaces; the probe's paths have none.
            parts = line.split()
            i = parts.index("-instanceId")
            parts[i + 1] = "StudioPlayer_9"
            added = subprocess.Popen(parts, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
            if added.poll() is not None:
                print(f"added client exited at once with code {added.returncode}")
            else:
                new, t = wait_new(mcp, known, 1, timeout=90)
                known |= set(new)
                print(f"added client pid {added.pid}: in the MCP after {t:.0f}s as {kind_of(mcp, new[0])}")
                for sid in new:
                    name = mcp.luau(sid, "return game.Players.LocalPlayer.Name", datamodel="Client", timeout=30)
                    print(f"  {sid}: LocalPlayer {name.strip()}")
        # Players as the server sees them (only the server window answers on Server).
        for sid in list(known):
            try:
                names = mcp.luau(sid, 'local t = {} for _, p in game.Players:GetPlayers() do table.insert(t, p.Name .. "=" .. p.UserId) end return table.concat(t, ",")',
                                 datamodel="Server", timeout=20)
                print(f"\nserver {sid} sees players: {names.strip()}")
            except Exception:
                pass

        print("\nplaces opened (from Studio logs):")
        print(json.dumps(opened_places(t_start), indent=2))
    finally:
        mcp.close()
        if server is not None:
            pids = tree(server.pid)
            extra = [p for p in subprocess.run(["pgrep", "-f", "--", f"-localProjectFile {place}"], capture_output=True,
                                               text=True).stdout.split()]
            pids = sorted(set(pids) | {int(p) for p in extra})
            for pid in pids:
                subprocess.run(["kill", "-9", str(pid)], capture_output=True)
            print(f"\nsent SIGKILL to {pids}")


def args_of_parent(pid):
    return subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


if __name__ == "__main__":
    main()
