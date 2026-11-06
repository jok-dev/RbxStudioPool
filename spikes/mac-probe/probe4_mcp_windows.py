"""Probe 4: does the Studio MCP list every test window and route calls to each by studio_id?

    python3 probe4_mcp_windows.py [--clients 2]

Starts a test server with N clients on the marker place (swapped in at server.rbxl and put back
after), adds one more client from a copied command line, and for every window checks: its MCP
entry, get_studio_state, which DataModel accepts Luau (and how the wrong one fails), and its
LocalPlayer name. The server tags workspace; each client must see that tag.
"""

import argparse
import shutil
import subprocess
import time
from pathlib import Path

from macmcp import STUDIO_BIN, MacStudioMcp, server_rbxl
from probe2_args import args_of, kind_of, studio_children, tree, wait_new

STATE = Path(__file__).resolve().parent / "state"


def try_luau(mcp, sid, code, dm):
    t0 = time.time()
    try:
        return True, mcp.luau(sid, code, datamodel=dm, timeout=20).strip(), time.time() - t0
    except Exception as e:
        return False, str(e)[:160], time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", type=int, default=2)
    args = ap.parse_args()
    run = STATE / f"probe4-{int(time.time())}"
    run.mkdir(parents=True)
    copy = run / "copy.rbxl"
    shutil.copy(STATE / "marker.rbxl", copy)
    procs = []
    with server_rbxl(STATE / "marker.rbxl"):
        mcp = MacStudioMcp()
        try:
            known = {s["id"] for s in mcp.list_studios()}
            server = subprocess.Popen(
                [str(STUDIO_BIN), "-task", "StartServer", "-localProjectFile", str(copy), "-placeId", "0",
                 "-universeId", "0", "-port", "0", "-creatorId", "0", "-creatorType", "0", "-placeVersion", "0",
                 "-numTestServerPlayersUponStartup", str(args.clients)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(server)
            time.sleep(3)
            if server.poll() is not None:
                raise RuntimeError(f"server exited at once with code {server.returncode}")
            ids, t = wait_new(mcp, known, 1 + args.clients, timeout=180)
            print(f"{len(ids)} windows in the MCP after {t:.0f}s")

            # Add one more client from a copy of a client's command line.
            clients = studio_children(server.pid)
            parts = args_of(clients[0]).split()
            parts[parts.index("-instanceId") + 1] = "StudioPlayer_7"
            added = subprocess.Popen(parts, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(added)
            more, t = wait_new(mcp, known | set(ids), 1, timeout=90)
            ids += more
            print(f"added client in the MCP after {t:.0f}s")

            entries = {s["id"]: s for s in mcp.list_studios()}
            server_sid = None
            for sid in ids:
                kind = kind_of(mcp, sid)
                if kind == "Server":
                    server_sid = sid
            ok, out, _ = try_luau(mcp, server_sid, 'workspace:SetAttribute("RspGroup", "g4") return "tagged"', "Server")
            print(f"server tag: {out}")
            time.sleep(1)

            print("\nper window:")
            for sid in ids:
                kind = kind_of(mcp, sid)
                print(f"- {sid} name={entries.get(sid, {}).get('name')!r} focused={kind}")
                for dm in ("Server", "Client"):
                    code = ('return (game.Players.LocalPlayer and game.Players.LocalPlayer.Name or "no LocalPlayer")'
                            ' .. " group=" .. tostring(workspace:GetAttribute("RspGroup"))'
                            ' .. " players=" .. #game.Players:GetPlayers()')
                    ok, out, dt = try_luau(mcp, sid, code, dm)
                    print(f"    {dm}: {'ok' if ok else 'FAIL'} in {dt:.1f}s: {out}")
        finally:
            mcp.close()
            pids = sorted({p for proc in procs for p in tree(proc.pid)})
            for pid in pids:
                subprocess.run(["kill", "-9", str(pid)], capture_output=True)
            print(f"\nsent SIGKILL to {pids}")
            time.sleep(1)


if __name__ == "__main__":
    main()
