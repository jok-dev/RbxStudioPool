"""Throwaway spike: launch Studios, sync worktrees into them with Rojo, poke them through the Studio MCP.

Interactive shell so Studio/Rojo processes stay alive between commands:

    python spikes/python-pool/pool.py

Talks to Studio through the official Studio MCP (%LOCALAPPDATA%/Roblox/mcp.bat) as a client.
Rojo auto-connect needs the patched plugin (rojo-plugin/build.sh) installed: `install_plugin <rbxm>`.
Windows only. Standard library only.
"""

import cmd
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

LOCAL = Path(os.environ.get("LOCALAPPDATA", ""))
MCP_BAT = LOCAL / "Roblox" / "mcp.bat"
VERSIONS = LOCAL / "Roblox" / "Versions"
PLUGINS = LOCAL / "Roblox" / "Plugins"
PLUGIN_NAME = "RspRojo.rbxm"
MARKER_ATTR = "RspSlot"
ROJO_PORT_ATTR = "RspRojoPort"
ROJO_BASE_PORT = 34900
# In a Team Create session the Edit DataModel is a network client and collaborators show up in Players.
TEAM_CREATE_PROBE = """
local signals = {}
local function try(key, f)
    local ok, value = pcall(f)
    signals[key] = if ok then value else "ERR " .. tostring(value)
end
try("networkClient", function() return game:FindService("NetworkClient") ~= nil end)
try("players", function() return #game:GetService("Players"):GetPlayers() end)
return game:GetService("HttpService"):JSONEncode(signals)
"""
DEFAULT_PLACE = os.environ.get("RSP_PLACE")
DEFAULT_UNIVERSE = os.environ.get("RSP_UNIVERSE")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def kill_tree(proc):
    if proc and proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)


class StudioMcp:
    """Minimal MCP stdio client: newline-delimited JSON-RPC."""

    def __init__(self):
        self.proc = subprocess.Popen(
            ["cmd", "/c", str(MCP_BAT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.next_id = 0
        self.pending = {}
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "rbx-studio-pool-spike", "version": "0"},
            },
            timeout=60,
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg):
        with self.write_lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def _read(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            slot = self.pending.get(msg.get("id"))
            if slot is not None:
                slot["msg"] = msg
                slot["event"].set()

    def request(self, method, params, timeout=120):
        with self.lock:
            self.next_id += 1
            rid = self.next_id
        slot = {"event": threading.Event(), "msg": None}
        self.pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not slot["event"].wait(timeout):
            del self.pending[rid]
            raise TimeoutError(f"{method}: no response in {timeout}s")
        del self.pending[rid]
        msg = slot["msg"]
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg["result"]

    def call(self, tool, args, timeout=120):
        result = self.request("tools/call", {"name": tool, "arguments": args}, timeout)
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise RuntimeError(f"{tool}: {text}")
        return result, text

    def list_studios(self):
        _, text = self.call("list_roblox_studios", {})
        return json.loads(text).get("studios", [])

    def luau(self, studio_id, code, timeout=60, datamodel="Edit"):
        _, text = self.call(
            "execute_luau",
            {"code": code, "datamodel_type": datamodel, "studio_id": studio_id},
            timeout,
        )
        return text

    def close(self):
        kill_tree(self.proc)


@dataclass
class Slot:
    n: int
    place: str
    proc: subprocess.Popen
    studio_id: str | None = None
    rojo: subprocess.Popen | None = None
    rojo_port: int | None = None
    worktree: Path | None = None
    team_create: bool | None = None
    started: float = field(default_factory=time.time)


def studio_exe():
    exes = [p / "RobloxStudioBeta.exe" for p in VERSIONS.iterdir() if (p / "RobloxStudioBeta.exe").exists()]
    if not exes:
        raise FileNotFoundError(f"no RobloxStudioBeta.exe under {VERSIONS}")
    return max(exes, key=lambda p: p.stat().st_mtime)


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Pool(cmd.Cmd):
    intro = "rbx-studio-pool spike. `help` for commands, `quit` to close everything."
    prompt = "pool> "

    def __init__(self):
        super().__init__()
        self.slots: dict[int, Slot] = {}
        self._mcp = None

    @property
    def mcp(self):
        if self._mcp is None:
            log("starting Studio MCP...")
            self._mcp = StudioMcp()
        return self._mcp

    def _slot(self, arg):
        n = int(arg)
        if n not in self.slots:
            raise KeyError(f"no slot {n}")
        return self.slots[n]

    def onecmd(self, line):
        try:
            return super().onecmd(line)
        except Exception as e:  # spike: report and keep the shell alive
            log(f"error: {e}")

    def emptyline(self):
        pass

    # --- commands ---

    def do_launch(self, arg):
        """launch [place_id] [universe_id]  -- open a Studio on a published place; refuses Team Create places
        (defaults: RSP_PLACE/RSP_UNIVERSE)"""
        self._launch(arg, allow_team_create=False)

    def do_launch_team_create(self, arg):
        """launch_team_create <place_id> <universe_id>  -- like launch, but Team Create places are allowed.
        Only one slot per Team Create place: edits there are live for every collaborator and autosave to the cloud."""
        self._launch(arg, allow_team_create=True)

    def _launch(self, arg, allow_team_create):
        parts = arg.split()
        place = parts[0] if parts else DEFAULT_PLACE
        universe = parts[1] if len(parts) > 1 else DEFAULT_UNIVERSE
        if not place or not universe:
            raise ValueError("need place and universe ids (args or RSP_PLACE/RSP_UNIVERSE)")
        if allow_team_create:
            # Team Create status is only known after launch, so any slot already on this place blocks it.
            for other in self.slots.values():
                if other.place == place:
                    raise RuntimeError(f"slot {other.n} already has place {place} open")
            for st in self.mcp.list_studios():
                if f"placeId: {place}" in (st.get("name") or ""):
                    log(f"warning: a Studio outside the pool has place {place} open: {st['name']}")
        n = next(i for i in range(1, 100) if i not in self.slots)
        before = {s["id"] for s in self.mcp.list_studios()}
        t0 = time.time()
        proc = subprocess.Popen(
            [str(studio_exe()), "-task", "EditPlace", "-placeId", place, "-universeId", universe]
        )
        slot = Slot(n, place, proc)
        self.slots[n] = slot
        try:
            self._wait_ready(slot, before, t0)
            team_create = self._is_team_create(slot)
        except Exception:
            self.do_close(str(n))
            raise
        slot.team_create = team_create
        if team_create and not allow_team_create:
            self.do_close(str(n))
            raise RuntimeError(f"place {place} is (or may be) Team Create; use launch_team_create to allow it")
        tc = " (TEAM CREATE: edits are live and autosave)" if team_create else ""
        log(f"slot {n}: ready in {time.time() - t0:.0f}s{tc}")

    def _wait_ready(self, slot, before, t0):
        log(f"slot {slot.n}: launched Studio pid {slot.proc.pid}, waiting for it to show up in the MCP...")
        while time.time() - t0 < 300:
            new = [s for s in self.mcp.list_studios() if s["id"] not in before]
            if len(new) == 1:
                slot.studio_id = new[0]["id"]
                break
            if len(new) > 1:
                raise RuntimeError(f"ambiguous: several new Studios {new}")
            time.sleep(2)
        else:
            raise TimeoutError("Studio never appeared in list_roblox_studios")
        log(f"slot {slot.n}: studio_id {slot.studio_id} after {time.time() - t0:.0f}s, waiting for DataModel...")
        while True:
            try:
                out = self.mcp.luau(
                    slot.studio_id,
                    f'game:GetService("CoreGui"):SetAttribute("{MARKER_ATTR}", {slot.n}) return tostring(game.PlaceId)',
                    timeout=15,
                )
                break
            except Exception as e:
                if time.time() - t0 > 300:
                    raise
                log(f"  luau not ready yet ({e}); retrying")
                time.sleep(3)
        if out.strip() != slot.place:
            raise RuntimeError(f"Studio reports PlaceId {out.strip()}, expected {slot.place}")

    def _is_team_create(self, slot):
        """Unverified heuristics; anything unclear counts as Team Create (fail closed)."""
        try:
            raw = self.mcp.luau(slot.studio_id, TEAM_CREATE_PROBE)
            signals = json.loads(raw.strip())
        except Exception as e:
            log(f"slot {slot.n}: Team Create probe failed ({e}); treating as Team Create")
            return True
        log(f"slot {slot.n}: Team Create signals {signals}")
        if any(isinstance(v, str) and v.startswith("ERR") for v in signals.values()):
            return True
        return bool(signals.get("networkClient")) or signals.get("players", 0) > 0

    def do_sync(self, arg):
        """sync <slot> <worktree>  -- rojo serve the worktree and auto-connect the slot's Studio"""
        n, path = shlex.split(arg, posix=False)
        slot = self._slot(n)
        worktree = Path(path.strip('"')).resolve()
        if not (worktree / "default.project.json").exists():
            raise FileNotFoundError(f"no default.project.json in {worktree}")
        self._unsync(slot)
        port = ROJO_BASE_PORT + slot.n
        if port_open(port):
            raise RuntimeError(f"port {port} already in use")
        rojo = shutil.which("rojo") or "rojo"
        slot.rojo = subprocess.Popen([rojo, "serve", "default.project.json", "--port", str(port)], cwd=worktree)
        slot.rojo_port, slot.worktree = port, worktree
        t0 = time.time()
        while not port_open(port):
            if slot.rojo.poll() is not None:
                raise RuntimeError("rojo serve exited early")
            if time.time() - t0 > 60:
                raise TimeoutError("rojo serve never opened its port")
            time.sleep(0.5)
        log(f"slot {slot.n}: rojo serving {worktree} on {port} ({time.time() - t0:.1f}s)")
        self.mcp.luau(slot.studio_id, f'game:GetService("CoreGui"):SetAttribute("{ROJO_PORT_ATTR}", {port})')
        log(f"slot {slot.n}: set {ROJO_PORT_ATTR}={port}; plugin should connect (watch Studio)")

    def do_unsync(self, arg):
        """unsync <slot>  -- stop the slot's rojo serve"""
        self._unsync(self._slot(arg))

    def _unsync(self, slot):
        if slot.rojo:
            kill_tree(slot.rojo)
            log(f"slot {slot.n}: stopped rojo on {slot.rojo_port}")
        slot.rojo = slot.rojo_port = slot.worktree = None

    def do_list(self, arg):
        """list  -- slots owned by this shell, plus every Studio the MCP sees"""
        for s in self.slots.values():
            alive = "alive" if s.proc.poll() is None else f"exited {s.proc.returncode}"
            rojo = f"rojo :{s.rojo_port} {s.worktree}" if s.rojo else "no rojo"
            tc = ", TEAM CREATE" if s.team_create else ""
            print(f"  slot {s.n}: place {s.place}{tc}, pid {s.proc.pid} ({alive}), studio {s.studio_id}, {rojo}")
        print("  MCP sees:")
        for st in self.mcp.list_studios():
            print(f"    {st['id']}  {st.get('name')}")

    def do_luau(self, arg):
        """luau <slot> <code>  -- run Luau in the slot's Edit DataModel"""
        n, code = arg.split(" ", 1)
        print(self.mcp.luau(self._slot(n).studio_id, code))

    def do_tools(self, arg):
        """tools  -- list the Studio MCP tools"""
        for t in self.mcp.request("tools/list", {})["tools"]:
            print(f"  {t['name']}: {(t.get('description') or '').splitlines()[0][:100]}")

    def do_call(self, arg):
        """call <slot> <tool> [json args]  -- any Studio MCP tool, studio_id filled in for the slot"""
        parts = arg.split(" ", 2)
        slot = self._slot(parts[0])
        args = json.loads(parts[2]) if len(parts) > 2 else {}
        args.setdefault("studio_id", slot.studio_id)
        result, text = self.mcp.call(parts[1], args, timeout=300)
        print(text or json.dumps(result, indent=2)[:4000])
        for c in result.get("content", []):
            if c.get("type") == "image":
                print(f"  (image content, {len(c.get('data', ''))} base64 chars, {c.get('mimeType')})")

    def do_close(self, arg):
        """close <slot>  -- stop rojo and kill the slot's Studio"""
        slot = self._slot(arg)
        self._unsync(slot)
        kill_tree(slot.proc)
        del self.slots[slot.n]
        log(f"slot {slot.n}: closed")

    def do_install_plugin(self, arg):
        """install_plugin <path to built RspRojo.rbxm>  -- copy into the Studio plugins folder"""
        src = Path(arg.strip().strip('"'))
        PLUGINS.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, PLUGINS / PLUGIN_NAME)
        log(f"installed {PLUGINS / PLUGIN_NAME}; restart open Studios to load it")

    def do_quit(self, arg):
        """quit  -- close all slots and exit"""
        for n in list(self.slots):
            self.do_close(str(n))
        if self._mcp:
            self._mcp.close()
        return True

    do_EOF = do_quit


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("spike is Windows-only")
    pool = Pool()
    try:
        pool.cmdloop()
    except KeyboardInterrupt:
        pool.do_quit("")
