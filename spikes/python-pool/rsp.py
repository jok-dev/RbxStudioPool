"""Throwaway spike of rbx-studio-pool: a local daemon that leases Studios from a capped pool, plus a CLI client.

    python rsp.py serve [--cap 3]                 # the daemon (one per machine)
    python rsp.py acquire --worktree DIR [--place-file T.rbxl | --place P --universe U] [--mount NAME=DIR] ...
    python rsp.py sync LEASE DIR                  # (re)sync a worktree into the leased Studio
    python rsp.py luau LEASE [CODE] [--dm Edit|Server|Client] [--file F]
    python rsp.py play LEASE start|stop|status|restart [--wait REGEX]
    python rsp.py acquire --place-file T.rbxl --clients 2    # multi-client test: server + Player1..2
    python rsp.py clients LEASE list | add [N] | remove PlayerN [--kick]
    python rsp.py logs LEASE [--dm D] [--level warning] [--grep RE] [--since N]
    python rsp.py wait-log LEASE REGEX [--dm D] [--timeout S] [--new]
    python rsp.py call LEASE TOOL [JSON]          # any Studio MCP tool, studio_id filled in
    python rsp.py rojo LEASE status|sync-once|live on|off
    python rsp.py tools [NAME]                    # Studio MCP tools (with arguments)
    python rsp.py release LEASE [--close]         # give the Studio back (kept warm unless --close)
    python rsp.py show | hide                     # view the pool desktop (Studios run there; Ctrl+Alt+F12 toggles)
    python rsp.py status | shutdown

HTTP+JSON on 127.0.0.1:7811 (no MCP yet). Leases expire when unused for LEASE_TTL seconds.
Per-repo behaviour (mounts, watches, hooks, prepare) comes from `.rbx-studio-pool/pool.toml`; see repo.py.
Reuses StudioMcp and helpers from pool.py. Windows only, stdlib only.
"""

import argparse
import base64
import collections
import hashlib
import itertools
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cloudplace
import desktop
import pool
import repo
from pool import MARKER_ATTR, TEAM_CREATE_PROBE, StudioMcp, kill_tree, port_open, studio_exe

HOST, PORT = "127.0.0.1", 7811
STATE = Path(__file__).parent / "state"
CLOUD_PLACES = STATE / "cloud-places"  # cloudplace.fetch cache: <placeId>/<version>.rbxl
LEASE_TTL = int(os.environ.get("RSP_LEASE_TTL", 600))
IDLE_TTL = int(os.environ.get("RSP_IDLE_TTL", 900))
VISIBLE = os.environ.get("RSP_VISIBLE") == "1"  # launch Studios on the user's desktop (they will take focus)
PIDS = STATE / "pids.json"
AUTOSAVES = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "RobloxStudio" / "AutoSaves"
# A test server (-task StartServer) always opens this file; -localProjectFile/-localPlaceFile don't change that.
# Studio's own Play (Clients and Servers) writes it just before launching its server, so it is scratch, shared by
# every Studio on the machine.
TEST_SERVER_PLACE = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "server.rbxl"
STUDIO_LOGS = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "logs"
# A person's Play writes TEST_SERVER_PLACE and their server opens it ~3s later; once the file has been untouched this
# long, nobody is about to read it.
PLACE_QUIET_SECS = 15
PLACE_QUIET_WAIT = 180
GROUP_LAUNCH_TRIES = 3
FILE_PREFIX = "file:"
# The RojoControl plugin fork (rojo-plugin/RojoControl.lua): JSON command in, JSON state out, on CoreGui.
ROJO_COMMAND_ATTR = "RojoControlCommand"
ROJO_STATE_ATTR = "RojoControlState"
# Tools an agent never gets through a lease: other Studios, and anything that writes to the Roblox account.
DENIED_TOOLS = {
    "list_roblox_studios": "the lease already pins your Studio",
    "upload_image": "uploads assets to the Roblox account",
    "create_skill": "changes the account's Studio skills",
    "edit_skill": "changes the account's Studio skills",
    "generate_mesh": "creates assets on the Roblox account",
    "generate_texture": "creates assets on the Roblox account",
    "generate_material": "creates assets on the Roblox account",
    "generate_avatar": "creates assets on the Roblox account",
    "generate_scene": "creates assets on the Roblox account",
    "segment_mesh": "creates assets on the Roblox account",
    "subagent": "runs Studio's own assistant",
}
READY_TIMEOUT = 300
SERVER = "server"
GROUP_PREFIX = "group:"
GROUP_ATTR = "RspGroup"  # workspace attribute the server sets; replicates to its clients, so they can be told apart
ROJO_BASE_PORT = 34900
LEVELS = ["output", "info", "warning", "error"]

# Installs (once per DataModel) a LogService capture as _G.RspLogs, then queries it. Not capped by the
# 512-entry LogService history, and filtered/grouped in Studio so a flood of one error stays one line.
# _G (kept per DataModel between execute_luau calls) rather than a BindableFunction in CoreGui: in test-server and
# test-client windows, an instance parented to CoreGui gets the Network capability, and MCP Luau can't invoke it.
LOG_CAPTURE = """
local HttpService = game:GetService("HttpService")
local fn = _G.RspLogs
if not fn then
	local LogService = game:GetService("LogService")
	local LEVEL = {
		[Enum.MessageType.MessageOutput] = 0, [Enum.MessageType.MessageInfo] = 1,
		[Enum.MessageType.MessageWarning] = 2, [Enum.MessageType.MessageError] = 3,
	}
	local MAX = 20000
	local id = HttpService:GenerateGUID(false) -- a new Play DataModel starts a new capture (and new seqs)
	local entries, first, nextSeq = {}, 1, 1
	local function add(message, messageType, t)
		entries[nextSeq] = { seq = nextSeq, level = LEVEL[messageType] or 0, msg = string.sub(message, 1, 2000), t = t }
		nextSeq += 1
		if nextSeq - first > MAX then
			entries[first] = nil
			first += 1
		end
	end
	for _, e in LogService:GetLogHistory() do
		add(e.message, e.messageType, e.timestamp)
	end
	LogService.MessageOut:Connect(function(message, messageType)
		add(message, messageType, DateTime.now().UnixTimestampMillis / 1000)
	end)
	fn = function(q)
		local out, groups = {}, {}
		for seq = math.max(first, (q.since or 0) + 1), nextSeq - 1 do
			local e = entries[seq]
			if e and e.level >= (q.level or 0) then
				local g = q.group and groups[e.msg]
				if g then
					g.count += 1
					g.last = seq
				else
					g = { seq = seq, last = seq, level = e.level, msg = e.msg, t = e.t, count = 1 }
					if q.group then
						groups[e.msg] = g
					end
					table.insert(out, g)
				end
			end
		end
		if q.group then -- most recently active last, so a limit keeps what is still happening
			table.sort(out, function(a, b)
				return a.last < b.last
			end)
		end
		local limit = q.limit or 500
		if #out > limit then
			out = table.move(out, #out - limit + 1, #out, 1, {})
		end
		return { id = id, cursor = nextSeq - 1, dropped = first - 1, entries = out }
	end
	_G.RspLogs = fn
end
return HttpService:JSONEncode(fn(HttpService:JSONDecode(%s)))
"""

log = logging.getLogger("rsp")


class PoolError(Exception):
    status = 400


class PlaceCollision(PoolError):
    """Another Studio wrote TEST_SERVER_PLACE between our write and our test server reading it."""


class Busy(PoolError):
    status = 409


def lua_string(s):
    """A Luau long-string literal holding s verbatim."""
    level = 0
    while f"]{'=' * level}]" in s:
        level += 1
    eq = "=" * level
    return f"[{eq}[{s}]{eq}]"


@dataclass
class Window:
    """One Studio process of a multi-client group: the test server or one player's client."""
    target: str  # "server" or the player's name ("Player1", ...)
    studio_id: str
    pid: int
    proc: object = None  # Popen/HiddenProcess when the pool started it (the server, added clients)
    left: bool = False

    @property
    def dm(self):
        return "Server" if self.target == SERVER else "Client"


@dataclass
class Group:
    """A Clients and Servers test: a test server started from a place file, which starts the first client itself;
    the pool adds the others by copying that client's command line (server port + the server's transport token)."""
    template: Path  # the place file each launch copies
    place_id: str = "0"  # from group_ids: non-zero means the server runs as that place (live DataStores)
    universe_id: str = "0"
    spec: repo.RepoSpec | None = None  # set when the template was built from a worktree (rebuilt on restart)
    windows: dict = field(default_factory=dict)  # target -> Window
    client_cmdline: str | None = None  # holds the transport token: never log or return it
    next_instance: int = 0
    tag: str = ""  # this launch's GROUP_ATTR value

    def live(self):
        return [w for w in self.windows.values() if not w.left]

    def players(self):
        return [w.target for w in self.live() if w.target != SERVER]


@dataclass
class Slot:
    n: int
    place: str
    universe: str
    proc: subprocess.Popen | None = None
    studio_id: str | None = None
    team_create: bool = False
    rojo: subprocess.Popen | None = None
    worktree: str | None = None
    spec: repo.RepoSpec | None = None  # the synced worktree's pool.toml (or defaults)
    sources: dict | None = None  # {mount name: source folder} synced on this slot
    watcher: threading.Event | None = None  # set to stop the slot's watcher
    restart_on_code: bool = False  # restart a running playtest when the worktree's code changes
    playing: bool = False  # last play state set through the pool
    play_lock: threading.Lock = field(default_factory=threading.Lock)  # held by start/stop/restart
    restarting: bool = False
    activity: float = 0.0  # last time the watcher saw a change or finished acting on one
    events: collections.deque = field(default_factory=lambda: collections.deque(maxlen=30))
    event_count: int = 0
    events_seen: int = 0  # events already returned by play status/start/stop/wait
    notices: list = field(default_factory=list)  # problems to hand to the lease's next call
    rojo_seq: int = 0
    lease: str | None = None
    file_copy: Path | None = None  # local-file slots run on a throwaway copy of the template
    launched_at: float | None = None
    state: str = "launching"  # launching | ready | closing
    group: Group | None = None  # a multi-client test (server + clients) instead of one edit Studio
    weight: int = 1  # Studios this slot counts for against the cap (1 + clients for a group)
    idle_since: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def rojo_port(self):
        return ROJO_BASE_PORT + self.n

    def event(self, kind, detail, notice=False):
        self.event_count += 1
        entry = {"n": self.event_count, "t": time.strftime("%H:%M:%S"), "kind": kind, "detail": detail}
        self.events.append(entry)
        if notice:
            self.notices.append(entry)
        (log.warning if notice else log.info)("slot %d: %s: %s", self.n, kind, detail)


@dataclass
class Lease:
    id: str
    slot: int
    client: str
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    calls: int = 0


class Pool:
    def __init__(self, cap):
        self.cap = cap
        self.mcp = StudioMcp()
        self.slots: dict[int, Slot] = {}
        self.leases: dict[str, Lease] = {}
        self.cond = threading.Condition()
        self.queue: list[int] = []
        self.tickets = itertools.count()
        self.launch_lock = threading.Lock()
        self.desktop = None if VISIBLE else desktop.PoolDesktop(status=self._bar_lines)
        self.shown_lease = None  # lease last put in front with `show`
        kill_leftovers()
        threading.Thread(target=self._reaper, daemon=True).start()

    def _save_pids(self):
        """Every process the pool owns, so a restarted daemon can kill what a crashed one left behind."""
        with self.cond:
            procs = [(s.proc, "RobloxStudioBeta.exe") for s in self.slots.values()]
            procs += [(s.rojo, "rojo.exe") for s in self.slots.values()]
            # clients the pool added to a group are not in the test server's process tree
            procs += [(w.proc, "RobloxStudioBeta.exe") for s in self.slots.values() if s.group
                      for w in list(s.group.windows.values()) if w.proc and w.target != SERVER]
        live = [{"pid": p.pid, "image": image} for p, image in procs if p and p.poll() is None]
        PIDS.write_text(json.dumps(live))

    # --- leasing ---

    def acquire(self, place=None, universe=None, place_file=None, team_create=False, fresh=False, client="?",
                worktree=None, mounts=None, restart_on_code=False, wait=600, clients=0):
        if clients:
            return self._acquire_group(clients, place_file, worktree, place, universe, client, wait)
        spec = load_spec(worktree) if worktree else None
        built = cloud = None
        if spec and not (place_file or place):
            # no place given: a local copy of the repo's template, or of a `rojo build` of its project
            place_file = spec.template or self._build_template(spec)
            built = not spec.template
        if place_file:
            template, cloud = self._place_file(place_file)
            place, universe = place or (spec and spec.place), universe or (spec and spec.universe)
            # optional place/universe: the copy is told it is that place (SetPlaceId/SetUniverseId), which carries into Play
            place, universe = f"{FILE_PREFIX}{template}@{place or 0}", universe or ""
        elif not (place and universe):
            raise PoolError("pass a worktree, place_file, or place + universe")
        if team_create:
            with self.cond:
                if any(s.place == place for s in self.slots.values() if s.lease or s.team_create):
                    raise Busy(f"Team Create place {place} is already open in the pool")
        slot, lease, reuse = self._take_slot(place, universe, team_create, fresh, client, wait)
        slot.restart_on_code = restart_on_code
        try:
            if not reuse:
                self._launch(slot, allow_team_create=team_create)
            synced = self.sync(lease.id, worktree, mounts) if worktree else None
        except Exception:
            self.release(lease.id, close=True)
            raise
        lease.last_seen = time.time()  # the TTL counts from when the agent gets the Studio
        log.info("lease %s -> slot %d (%s, %s)", lease.id, slot.n, client, "warm" if reuse else "fresh launch")
        out = {"lease": lease.id, "slot": slot.n, "studio_id": slot.studio_id, "reused": reuse,
               "team_create": slot.team_create, "place": slot.place}
        if built:
            out["template"] = "built from the project with `rojo build` (code only)"
        if cloud:
            out["cloud_place"] = cloud
        if synced:
            out.update({k: synced[k] for k in ("worktree", "mounts", "skipped_mounts", "prepared", "watches", "rojo")})
        return out

    def _place_file(self, place_file):
        """A template path from --place-file / pool.toml: a local .rbxl, or "cloud:<placeId>", downloaded (or taken
        from the cache when the place has not changed). Returns (path, cloud info or None)."""
        try:
            cloud_id = cloudplace.parse(str(place_file))
            if cloud_id:
                with self.cond:
                    in_use = {s.place[len(FILE_PREFIX):].rsplit("@", 1)[0] for s in self.slots.values()
                              if s.place.startswith(FILE_PREFIX)}
                    in_use |= {str(s.group.template) for s in self.slots.values() if s.group}
                path, info = cloudplace.fetch(cloud_id, CLOUD_PLACES, in_use)
                log.info("cloud place %s: %s (%s MB, %ss)", cloud_id, info["cache"], info["mb"], info["took"])
                return path.resolve(), info
        except cloudplace.CloudPlaceError as e:
            raise PoolError(f"cloud place: {e}") from None
        template = Path(place_file).resolve()
        if not template.is_file():
            raise PoolError(f"no place file at {template}")
        return template, None

    def _build_template(self, spec):
        repo.prepare(spec)
        out = STATE / "templates" / f"{spec.root.name}-{hashlib.sha1(str(spec.root).encode()).hexdigest()[:8]}.rbxl"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".building.rbxl")
        result = subprocess.run([spec.rojo, "build", str(spec.project), "-o", str(tmp)], cwd=spec.root,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise PoolError(f"rojo build failed: {(result.stdout + result.stderr)[-600:]}")
        os.replace(tmp, out)
        return out

    def _take_slot(self, place, universe, team_create, fresh, client, wait, weight=1):
        """Waits its turn in the FIFO queue for a slot (evicting idle Studios to make room), and leases it.
        Returns (slot, lease, reused)."""
        if weight > self.cap:
            raise PoolError(f"needs {weight} Studios but the pool's cap is {self.cap}")
        ticket = next(self.tickets)
        deadline = time.time() + wait
        with self.cond:
            self.queue.append(ticket)
            log.info("acquire #%d from %s: place %s, %d Studio(s) (queue %d)", ticket, client, place, weight,
                     len(self.queue))
            try:
                while True:
                    if self.queue[0] == ticket:
                        slot, evict, reuse = self._pick(place, universe, team_create, fresh, weight)
                        if slot:
                            break
                    left = deadline - time.time()
                    if left <= 0:
                        raise Busy(f"no room for {weight} Studio(s) within {wait}s")
                    self.cond.wait(min(left, 2))
            finally:
                self.queue.remove(ticket)
                self.cond.notify_all()
            lease = Lease(secrets.token_hex(4), slot.n, client)
            slot.lease = lease.id
            slot.notices.clear()
            self.leases[lease.id] = lease
        for e in evict:
            self._close(e, "evicted for a new launch")
        return slot, lease, reuse

    def _evict_for(self, weight):
        """Under self.cond. Takes idle Studios (oldest first) out of the pool until `weight` more fit under the cap.
        Returns them for the caller to close, or None (nothing taken) if they wouldn't make enough room."""
        free = self.cap - sum(s.weight for s in self.slots.values())
        idle = sorted((s for s in self.slots.values() if s.lease is None and s.state == "ready"),
                      key=lambda s: s.idle_since)
        evict = []
        while free < weight:
            if not idle:
                return None
            evict.append(idle.pop(0))
            free += evict[-1].weight
        for e in evict:
            e.state = "closing"
            del self.slots[e.n]
        return evict

    def _pick(self, place, universe, team_create, fresh, weight=1):
        """Under self.cond. Returns (slot, slots_to_evict, reused)."""
        if not fresh:
            for s in sorted(self.slots.values(), key=lambda s: s.idle_since):
                if s.lease is None and s.state == "ready" and s.group is None and s.place == place \
                        and (team_create or not s.team_create):
                    return s, [], True
        evict = self._evict_for(weight)
        if evict is None:
            return None, [], False
        n = next(i for i in itertools.count(1) if i not in self.slots and all(i != e.n for e in evict))
        slot = Slot(n, place, universe, weight=weight)
        self.slots[n] = slot
        return slot, evict, False

    def lease(self, lease_id):
        with self.cond:
            lease = self.leases.get(lease_id)
            if lease is None:
                raise PoolError(f"unknown or expired lease {lease_id}")
            lease.last_seen = time.time()
            slot = self.slots.get(lease.slot)
        if slot is None or slot.state != "ready":
            raise PoolError(f"lease {lease_id}: its Studio is gone")
        if slot.proc and slot.proc.poll() is not None:
            self.release(lease_id, close=True)
            raise PoolError(f"lease {lease_id}: Studio exited ({slot.proc.returncode})")
        return lease, slot

    def release(self, lease_id, close=False, reason="released"):
        with self.cond:
            lease = self.leases.pop(lease_id, None)
            if lease is None:
                raise PoolError(f"unknown or expired lease {lease_id}")
            slot = self.slots.get(lease.slot)
        if slot is None:
            return {"closed": True}
        self._unsync(slot)
        if not close and slot.state == "ready":
            close = self._dirty_reason(slot)
        if close:
            with self.cond:
                self.slots.pop(slot.n, None)
                slot.state = "closing"
            self._close(slot, f"{reason}: {close if isinstance(close, str) else 'close requested'}")
        with self.cond:
            slot.lease = None
            slot.idle_since = time.time()
            self.cond.notify_all()
        log.info("lease %s %s (slot %d %s)", lease_id, reason, slot.n, "closed" if close else "kept warm")
        return {"closed": bool(close)}

    def _dirty_reason(self, slot):
        """A Studio is only kept warm if it is a plain edit session; anything else is closed."""
        if slot.group:
            return "multi-client test"
        if slot.team_create:
            return "Team Create session"
        try:
            _, text = self.mcp.call("get_studio_state", {"studio_id": slot.studio_id}, timeout=15)
        except Exception as e:
            return f"state unknown ({e})"
        # e.g. "- Current Studio Mode: Edit\n- Available DataModels: Edit\n- Focused DataModel in the viewport: Edit"
        if "Current Studio Mode: Edit" not in text:
            return f"not in edit mode ({text.strip().splitlines()[0] if text.strip() else '?'})"
        return None

    # --- Studio lifecycle ---

    def _launch(self, slot, allow_team_create):
        t0 = time.time()
        with self.launch_lock:  # new-Studio detection must not see two launches at once
            before = {s["id"] for s in self.mcp.list_studios()}
            if slot.place.startswith(FILE_PREFIX):
                self._copy_place(slot, slot.place[len(FILE_PREFIX):].rsplit("@", 1)[0])
                args = [str(slot.file_copy)]
            else:
                args = ["-task", "EditPlace", "-placeId", slot.place, "-universeId", slot.universe]
            slot.launched_at = time.time()
            cmd = [str(studio_exe()), *args]
            slot.proc = self.desktop.launch(cmd) if self.desktop else subprocess.Popen(cmd)
            log.info("slot %d: launched Studio pid %d for place %s", slot.n, slot.proc.pid, slot.place)
            self._save_pids()
            while True:
                new = [s for s in self.mcp.list_studios() if s["id"] not in before]
                if len(new) == 1:
                    slot.studio_id = new[0]["id"]
                    break
                if len(new) > 1:
                    raise PoolError(f"ambiguous: several new Studios {new}")
                if slot.proc.poll() is not None:
                    raise PoolError(f"Studio exited during launch ({slot.proc.returncode})")
                if time.time() - t0 > READY_TIMEOUT:
                    raise PoolError("Studio never appeared in list_roblox_studios")
                time.sleep(1)
        while True:
            try:
                out = self.mcp.luau(slot.studio_id,
                                    f'game:GetService("CoreGui"):SetAttribute("{MARKER_ATTR}", {slot.n}) '
                                    "return tostring(game.PlaceId)", timeout=15)
                break
            except Exception as e:
                if slot.proc.poll() is not None or slot.state == "closing":
                    raise PoolError("Studio exited or was closed while loading") from e
                if time.time() - t0 > READY_TIMEOUT:
                    raise PoolError(f"Studio never accepted Luau: {e}") from e
                time.sleep(2)
        expected = "0" if slot.file_copy else slot.place
        if out.strip() != expected:
            raise PoolError(f"Studio reports PlaceId {out.strip()}, expected {expected}")
        as_place = slot.place.rsplit("@", 1)[1] if slot.file_copy else "0"
        if as_place != "0":
            out = self.mcp.luau(slot.studio_id, f"game:SetPlaceId({as_place}) game:SetUniverseId({slot.universe or 0}) "
                                "return tostring(game.PlaceId)")
            if out.strip() != as_place:
                raise PoolError(f"SetPlaceId did not stick: PlaceId {out.strip()}, wanted {as_place}")
        slot.team_create = self._is_team_create(slot)
        if slot.team_create and not allow_team_create:
            raise PoolError(f"place {slot.place} is Team Create; pass team_create to allow it")
        slot.state = "ready"
        log.info("slot %d: ready in %.0fs%s", slot.n, time.time() - t0, " (TEAM CREATE)" if slot.team_create else "")

    def _copy_place(self, slot, template):
        slot.file_copy = STATE / "places" / f"slot{slot.n}-{secrets.token_hex(3)}.rbxl"
        slot.file_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, slot.file_copy)

    def _spawn(self, cmd):
        return self.desktop.launch(cmd) if self.desktop else subprocess.Popen(cmd)

    # --- multi-client groups (Clients and Servers) ---

    def _acquire_group(self, clients, place_file, worktree, place, universe, client, wait):
        """A test server plus `clients` players, started from a place file (or a `rojo build` of the worktree)
        without an edit Studio. The server runs as the place `group_ids` picks (--place/--universe, else the
        worktree's pool.toml, as for a single lease), with that universe's live DataStores."""
        if clients < 1:
            raise PoolError("--clients must be at least 1 (the pool learns the server's port from the first client)")
        repo_spec = load_spec(worktree) if worktree else None
        place, universe = group_ids(place, universe, repo_spec)
        spec = cloud = None  # spec stays None for a saved template: nothing to rebuild
        if place_file:
            template, cloud = self._place_file(place_file)
        elif repo_spec:
            if repo_spec.template:
                template, cloud = self._place_file(repo_spec.template)
            else:
                spec = repo_spec
                template = self._build_template(spec)
        else:
            raise PoolError("a multi-client test needs --place-file or --worktree")
        group = Group(template, place, universe, spec)
        slot, lease, _ = self._take_slot(f"{GROUP_PREFIX}{template}", "", False, True, client, wait,
                                         weight=1 + clients)
        slot.group = group
        try:
            t0 = time.time()
            self._start_group(slot, clients)
        except Exception:
            self.release(lease.id, close=True)
            raise
        lease.last_seen = time.time()
        log.info("lease %s -> slot %d: test server + %d clients in %.0fs (%s)", lease.id, slot.n, clients,
                 time.time() - t0, client)
        out = {"lease": lease.id, "slot": slot.n, "targets": [SERVER, *group.players()],
               "place_file": str(template), "took": round(time.time() - t0, 1)}
        if spec:
            out["template"] = "built from the worktree with `rojo build` (code only); `play restart` rebuilds it"
        if cloud:
            out["cloud_place"] = cloud
        if group.place_id != "0":
            out["warning"] = (f"the server runs as place {group.place_id}: DataStores are that universe's live data "
                              "(players are UserId -1, -2, ...)")
        return out

    def _start_group(self, slot, clients):
        """Launches the test server with one client, maps both to their Studio MCP ids, then adds the rest one by
        one (a launch at a time, so each new MCP id belongs to a known process)."""
        g = slot.group
        t0 = time.time()
        self._copy_place(slot, g.template)
        for attempt in range(1, GROUP_LAUNCH_TRIES + 1):
            g.windows.clear()
            g.client_cmdline = None
            g.next_instance = 0
            try:
                server_id, client_id, client_pid = self._launch_server(slot, t0)
                break
            except PlaceCollision as e:
                kill_tree(slot.proc)
                if attempt == GROUP_LAUNCH_TRIES:
                    raise PoolError(f"{e} ({attempt} tries)") from e
                slot.event("launch", f"{e}; relaunching", notice=True)
        tag = f"{slot.n}-{secrets.token_hex(3)}"
        self._luau_retry(server_id, "Server", f'workspace:SetAttribute("{GROUP_ATTR}", "{tag}") '
                         f'game:GetService("CoreGui"):SetAttribute("{MARKER_ATTR}", {slot.n})', t0)
        g.windows[SERVER] = Window(SERVER, server_id, slot.proc.pid, slot.proc)
        self._install_capture(slot, g.windows[SERVER])
        name = self._client_name(client_id, tag, t0)
        g.windows[name] = Window(name, client_id, client_pid)
        self._install_capture(slot, g.windows[name])
        g.tag = tag
        log.info("slot %d: test server + %s up in %.0fs", slot.n, name, time.time() - t0)
        for _ in range(clients - 1):
            self._add_client(slot)
        slot.state = "ready"

    def _wait_place_quiet(self, slot):
        """Waits until TEST_SERVER_PLACE has been untouched for PLACE_QUIET_SECS, so a person's Clients and Servers
        test that just wrote it has already been read by its server."""
        t0 = time.time()
        told = False
        while True:
            try:
                age = time.time() - TEST_SERVER_PLACE.stat().st_mtime
            except FileNotFoundError:
                return
            if age >= PLACE_QUIET_SECS:
                return
            if time.time() - t0 > PLACE_QUIET_WAIT:
                raise PoolError(f"{TEST_SERVER_PLACE} kept changing for {PLACE_QUIET_WAIT}s (another Studio keeps "
                                "starting Clients and Servers tests)")
            if not told:
                slot.event("launch", f"another Studio wrote {TEST_SERVER_PLACE.name} {age:.0f}s ago; waiting")
                told = True
            time.sleep(min(PLACE_QUIET_SECS - age, 2) + 0.1)

    def _server_log(self, slot):
        """The launched test server's Studio log: the newest log since the launch whose command line names this
        slot's place copy."""
        needle = slot.file_copy.as_posix()
        for f in sorted(STUDIO_LOGS.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[:20]:
            try:
                if f.stat().st_mtime < slot.launched_at - 5:
                    break
                head = f.read_text(errors="replace")[:20000]
            except OSError:
                continue
            if "-task StartServer" in head and needle in head:
                return f
        return None

    def _launch_server(self, slot, t0):
        """Writes the place to TEST_SERVER_PLACE, launches the test server and waits for it and its first client in
        the Studio MCP. Raises PlaceCollision if another Studio replaced the file before the server had read it."""
        g = slot.group
        cmd = [str(studio_exe()), "-task", "StartServer", "-localProjectFile", slot.file_copy.as_posix(),
               "-placeId", g.place_id, "-universeId", g.universe_id, "-port", "0", "-creatorId", "0",
               "-creatorType", "0", "-placeVersion", "0", "-numTestServerPlayersUponStartup", "1"]
        with self.launch_lock:
            self._wait_place_quiet(slot)
            before = {s["id"] for s in self.mcp.list_studios()}
            # the server loads TEST_SERVER_PLACE, not -localProjectFile (kept: it names the place in the window)
            shutil.copyfile(slot.file_copy, TEST_SERVER_PLACE)
            placed = TEST_SERVER_PLACE.stat()
            slot.launched_at = time.time()
            slot.proc = self._spawn(cmd)
            log.info("slot %d: launched test server pid %d", slot.n, slot.proc.pid)
            self._save_pids()
            server_id = client_id = client_pid = None
            classified = set()
            server_log, read = None, False

            def check_place():
                now = TEST_SERVER_PLACE.stat()
                if (now.st_size, now.st_mtime_ns) != (placed.st_size, placed.st_mtime_ns):
                    raise PlaceCollision(f"another Studio replaced {TEST_SERVER_PLACE.name} before the test server "
                                         "read it")

            while not (server_id and client_id and client_pid):
                if not read:
                    # its server DataModel exists once the place file is read; a change after that is harmless
                    server_log = server_log or self._server_log(slot)
                    if server_log and "StudioGameStateType_PlayServer" in server_log.read_text(errors="replace"):
                        check_place()
                        read = True
                if slot.proc.poll() is not None:
                    raise PoolError(f"test server exited during launch ({slot.proc.returncode})")
                if slot.state == "closing":
                    raise PoolError("closed during launch")
                if time.time() - t0 > READY_TIMEOUT:
                    raise PoolError(f"test group not up after {READY_TIMEOUT}s (server in MCP: {bool(server_id)}, "
                                    f"client in MCP: {bool(client_id)}, client process: {bool(client_pid)})")
                if not client_pid:
                    for p in child_processes(slot.proc.pid):
                        if "-task StartClient" in (p.get("CommandLine") or ""):
                            client_pid, g.client_cmdline = p["ProcessId"], p["CommandLine"]
                            self._save_pids()
                for s in self.mcp.list_studios():
                    if s["id"] in before or s["id"] in classified:
                        continue
                    focus = self._focus(s["id"])  # None until the window answers
                    if focus == "Server" and not server_id:
                        server_id = s["id"]
                    elif focus == "Client" and not client_id:
                        client_id = s["id"]
                    elif focus:
                        raise PoolError(f"unexpected new Studio during the group launch ({focus}): {s}")
                    if focus:
                        classified.add(s["id"])
                time.sleep(1)
            if not read:
                # no log line seen (log not found or format changed): fall back to "unchanged until up"
                log.warning("slot %d: no place-read line in the test server log (%s)", slot.n, server_log)
                check_place()
        return server_id, client_id, client_pid

    def _focus(self, studio_id):
        """'Server' or 'Client' for a test window (the DataModel its viewport shows), 'Edit' for an edit Studio."""
        try:
            _, text = self.mcp.call("get_studio_state", {"studio_id": studio_id}, timeout=15)
        except Exception:
            return None
        m = re.search(r"Focused DataModel in the viewport: (\w+)", text)
        return m.group(1) if m else None

    def _luau_retry(self, studio_id, dm, code, t0):
        while True:
            try:
                return self.mcp.luau(studio_id, code, datamodel=dm, timeout=15)
            except Exception as e:
                if time.time() - t0 > READY_TIMEOUT:
                    raise PoolError(f"{dm} window never accepted Luau: {e}") from e
                time.sleep(1)

    def _client_name(self, studio_id, tag, t0):
        """The client's player name, once it has joined and sees its server's group tag."""
        code = (f'local p = game:GetService("Players").LocalPlayer '
                f'return (p and p.Name or "") .. "|" .. tostring(workspace:GetAttribute("{GROUP_ATTR}"))')
        while True:
            name, _, seen = self._luau_retry(studio_id, "Client", code, t0).strip().partition("|")
            if name and seen == tag:
                return name
            if name and seen not in ("nil", tag):
                raise PoolError(f"client {name} belongs to another group ({seen})")
            if time.time() - t0 > READY_TIMEOUT:
                raise PoolError(f"client never joined (player {name or '?'}, group {seen})")
            time.sleep(1)

    def _install_capture(self, slot, w):
        try:
            self._query_logs(slot, w.dm, {"since": 1e9, "limit": 0}, retry_for=30, studio_id=w.studio_id)
        except Exception as e:
            slot.event("logs", f"could not start log capture in {w.target}: {e}", notice=True)

    def _add_client(self, slot):
        g = slot.group
        if not g.client_cmdline:
            raise PoolError("no client launch line known for this group")
        t0 = time.time()
        with self.launch_lock:
            before = {s["id"] for s in self.mcp.list_studios()}
            g.next_instance += 1
            cmd = re.sub(r"-instanceId StudioPlayer_\d+", f"-instanceId StudioPlayer_{g.next_instance}",
                         g.client_cmdline)
            proc = self._spawn(cmd)
            self._save_pids()
            while True:
                new = [s["id"] for s in self.mcp.list_studios() if s["id"] not in before]
                if len(new) == 1:
                    break
                if len(new) > 1:
                    kill_tree(proc)
                    raise PoolError(f"ambiguous: several new Studios {new}")
                if proc.poll() is not None or time.time() - t0 > READY_TIMEOUT:
                    kill_tree(proc)
                    raise PoolError("added client never appeared in the Studio MCP")
                time.sleep(1)
        name = self._client_name(new[0], g.tag, t0)
        w = g.windows[name] = Window(name, new[0], proc.pid, proc)
        self._install_capture(slot, w)
        slot.event("clients", f"{name} joined ({time.time() - t0:.0f}s)")
        return w

    def clients(self, lease_id, op, target=None, count=1, kick=False):
        """add [count] | remove TARGET [kick] | list."""
        _, slot = self.lease(lease_id)
        g = self._group(slot)
        if op == "list":
            return self._group_status(slot)
        if op == "add":
            with self.cond:
                evict = self._evict_for(count)
                if evict is None:
                    raise Busy(f"no room under the cap ({self.cap}) for {count} more client(s)")
                slot.weight += count
            for e in evict:
                self._close(e, "evicted for a new client")
            added = []
            try:
                for _ in range(count):
                    added.append(self._add_client(slot).target)
            finally:
                with self.cond:
                    slot.weight -= count - len(added)
                    self.cond.notify_all()
            return {"added": added, **self._group_status(slot)}
        if op == "remove":
            w = self._window(slot, target)
            if w.target == SERVER:
                raise PoolError("the server can't be removed; release the lease to end the test")
            t0 = time.time()
            if kick:  # a clean leave; the kicked window stays on its kick screen, so close it after
                self.mcp.luau(g.windows[SERVER].studio_id,
                              f'local p = game:GetService("Players"):FindFirstChild({lua_string(w.target)}) '
                              'if p then p:Kick("Removed by rbx-studio-pool") end', datamodel="Server")
                gone = self._wait_player_gone(slot, w.target, 30)
                self._kill_window(w)
            else:  # like a player closing Roblox: the server notices the dropped connection
                self._kill_window(w)
                gone = self._wait_player_gone(slot, w.target, 90)
            with self.cond:
                slot.weight -= 1
                self.cond.notify_all()
            slot.event("clients", f"{w.target} removed ({'kick' if kick else 'process killed'})")
            return {"removed": w.target, "how": "kick" if kick else "process killed",
                    "server_saw_leave_after": None if gone is None else round(gone - t0, 1),
                    **self._group_status(slot)}
        raise PoolError(f"unknown clients op {op}")

    def _wait_player_gone(self, slot, name, timeout):
        """The time the server stopped listing the player, or None if it still does after timeout."""
        deadline = time.time() + timeout
        code = f'return tostring(game:GetService("Players"):FindFirstChild({lua_string(name)}) ~= nil)'
        while time.time() < deadline:
            try:
                if self.mcp.luau(slot.group.windows[SERVER].studio_id, code, datamodel="Server",
                                 timeout=15).strip() == "false":
                    return time.time()
            except Exception:
                pass
            time.sleep(0.25)
        return None

    def _kill_window(self, w):
        if w.proc:
            kill_tree(w.proc)
        else:
            subprocess.run(["taskkill", "/PID", str(w.pid), "/T", "/F"], capture_output=True)
        w.left = True
        deadline = time.time() + 15
        while time.time() < deadline:  # the Studio MCP keeps listing a killed Studio for a moment
            try:
                if all(s["id"] != w.studio_id for s in self.mcp.list_studios()):
                    return
            except Exception:
                return
            time.sleep(0.5)

    def _stop_group(self, slot):
        """Kills every window of the group (the server's tree takes its own clients; added clients are separate)."""
        g = slot.group
        for w in g.windows.values():
            if w.target != SERVER and w.proc and not w.left:
                kill_tree(w.proc)
        kill_tree(slot.proc)
        for w in g.windows.values():
            w.left = True
        self._save_pids()
        self._remove_place_copy(slot)
        ids = {w.studio_id for w in g.windows.values()}
        deadline = time.time() + 15
        while ids and time.time() < deadline:
            try:
                if not ids & {s["id"] for s in self.mcp.list_studios()}:
                    break
            except Exception:
                break
            time.sleep(0.5)

    def _restart_group(self, slot):
        g = slot.group
        count = max(1, len(g.players()))
        with slot.play_lock:
            t0 = time.time()
            slot.restarting = True
            try:
                self._stop_group(slot)
                with self.cond:
                    slot.weight = 1 + count
                if g.spec:
                    g.template = self._build_template(g.spec)
                self._start_group(slot, count)
            finally:
                slot.restarting = False
        slot.event("restart", f"test restarted with {count} client(s) in {time.time() - t0:.0f}s")
        return {"took": round(time.time() - t0, 1), "rebuilt": bool(g.spec), **self._group_status(slot)}

    def _group(self, slot):
        if not slot.group:
            raise PoolError("not a multi-client lease (acquire with --clients N)")
        return slot.group

    def _window(self, slot, target):
        g = self._group(slot)
        w = g.windows.get(target or SERVER)
        if w is None:
            raise PoolError(f"no target {target!r}; targets: {[SERVER, *g.players()]}")
        if w.left:
            raise PoolError(f"{w.target} left the test")
        if w.proc and w.proc.poll() is not None:
            w.left = True
            raise PoolError(f"{w.target}'s Studio exited")
        return w

    def _target(self, slot, target, dm=None):
        """(studio_id, datamodel, target name) for a call: the lease's Studio, or a group window."""
        if slot.group:
            if dm:
                raise PoolError("--dm does not apply to a multi-client lease; use --target server|PlayerN")
            w = self._window(slot, target)
            return w.studio_id, w.dm, w.target
        if target:
            raise PoolError("--target is only for multi-client leases (acquire with --clients N)")
        return slot.studio_id, dm or self._live_dm(slot), None

    def _group_status(self, slot):
        g = slot.group
        return {"targets": [SERVER, *g.players()], "left": [w.target for w in g.windows.values() if w.left],
                "place_id": g.place_id}

    def _remove_place_copy(self, slot):
        if not slot.file_copy:
            return
        for _ in range(20):  # Studio holds the file briefly after being killed
            try:
                slot.file_copy.unlink(missing_ok=True)
                slot.file_copy.with_name(slot.file_copy.name + ".lock").unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.5)

    def _is_team_create(self, slot):
        try:
            signals = json.loads(self.mcp.luau(slot.studio_id, TEAM_CREATE_PROBE).strip())
        except Exception as e:
            log.warning("slot %d: Team Create probe failed (%s); treating as Team Create", slot.n, e)
            return True
        if any(isinstance(v, str) for v in signals.values()):
            return True
        return bool(signals.get("networkClient")) or signals.get("players", 0) > 0

    def _close(self, slot, why):
        self._unsync(slot)
        if slot.group:
            self._stop_group(slot)
        kill_tree(slot.proc)
        self._save_pids()
        self._remove_autosaves(slot)
        self._remove_place_copy(slot)
        if slot.studio_id:  # the Studio MCP keeps listing a killed Studio for a moment
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    if all(s["id"] != slot.studio_id for s in self.mcp.list_studios()):
                        break
                except Exception:
                    break
                time.sleep(0.5)
        log.info("slot %d: closed (%s)", slot.n, why)

    def _remove_autosaves(self, slot):
        """A killed Studio leaves <place>_AutoRecovery_N.rbxl behind, and the next launch of that place opens with an
        "Auto Recovery File Found" prompt. Remove only files for this slot's place written since it launched."""
        if slot.launched_at is None:
            return
        prefix = slot.file_copy.stem if slot.file_copy else slot.place
        for path in AUTOSAVES.glob(f"{prefix}_AutoRecovery_*"):
            try:
                if path.stat().st_mtime >= slot.launched_at - 1:
                    path.unlink()
                    log.info("slot %d: removed autosave %s", slot.n, path.name)
            except OSError as e:
                log.warning("slot %d: could not remove autosave %s: %s", slot.n, path.name, e)

    # --- Rojo ---

    def sync(self, lease_id, worktree, mounts=None):
        """Serves the worktree (plus the repo's mounts, sources from `mounts` or the repo's defaults) into the Studio."""
        spec = load_spec(worktree)
        try:
            sources, skipped = repo.resolve_mounts(spec, mounts)
            prepared = repo.prepare(spec)
        except repo.RepoError as e:
            raise PoolError(str(e)) from e
        _, slot = self.lease(lease_id)
        with slot.lock:
            self._unsync(slot)
            port = slot.rojo_port
            if port_open(port):
                raise PoolError(f"port {port} already in use")
            t0 = time.time()
            project = (repo.build_project(spec, sources, STATE / "projects" / f"slot{slot.n}.project.json")
                       if sources else spec.project)
            rojo_log = STATE / f"rojo-slot{slot.n}.log"
            with open(rojo_log, "w", encoding="utf-8") as out:
                slot.rojo = subprocess.Popen([spec.rojo, "serve", str(project), "--port", str(port)],
                                             cwd=spec.root, stdout=out, stderr=subprocess.STDOUT)
            while not port_open(port):
                if slot.rojo.poll() is not None:
                    slot.rojo = None
                    tail = rojo_log.read_text(encoding="utf-8", errors="replace").strip()[-600:]
                    raise PoolError(f"rojo serve exited early: {tail}")
                if time.time() - t0 > 60:
                    raise PoolError("rojo serve never opened its port")
                time.sleep(0.25)
            slot.worktree, slot.spec, slot.sources = str(spec.root), spec, sources
            self._save_pids()
            state = self._rojo(slot, {"op": "connect", "port": port, "live": True},
                               datamodel=self._live_dm(slot), timeout=180)
            watches = self._active_watches(slot)
            if watches:
                slot.watcher = threading.Event()
                threading.Thread(target=self._watch, args=(slot, slot.watcher, watches), daemon=True).start()
        log.info("slot %d: synced %s via :%d in %.1fs", slot.n, slot.worktree, port, time.time() - t0)
        return {"worktree": slot.worktree, "port": port, "rojo": state, "prepared": prepared,
                "mounts": {k: str(v) for k, v in sources.items()}, "skipped_mounts": skipped,
                "watches": [describe_watch(w.watch) for w in watches]}

    def _unsync(self, slot):
        if slot.rojo is None:
            return
        try:
            self._rojo(slot, {"op": "disconnect"}, datamodel=self._live_dm(slot), timeout=10)
        except Exception as e:
            log.warning("slot %d: could not disconnect Rojo: %s", slot.n, e)
        kill_tree(slot.rojo)
        slot.rojo = None
        slot.worktree = None
        if slot.watcher:
            slot.watcher.set()
        slot.watcher = slot.spec = slot.sources = None

    # --- play ---

    def play(self, lease_id, op, wait=None, timeout=120):
        """start | stop | status | wait. start/stop wait for the mode change; start can also wait for a Server log
        line. wait returns once file changes have been acted on (restart finished, hooks/commands run)."""
        lease, slot = self.lease(lease_id)
        if slot.group:
            if op == "status":
                return {"mode": "multi-client test", "restarting": slot.restarting, **self._group_status(slot),
                        "new_events": self._new_events(slot)}
            if op == "restart":
                out = self._restart_group(slot)
                if wait:
                    out["matched"] = self.wait_log(lease_id, wait, timeout=timeout)
                return out
            raise PoolError("a multi-client test is always running: use `play LEASE restart`, or release it")
        if op == "restart":
            raise PoolError("restart is for multi-client leases; use stop then start")
        if op == "status":
            return self._play_status(slot)
        if op == "wait":
            t0 = time.time()
            self._settle(slot, timeout)
            return {**self._play_status(slot), "waited": round(time.time() - t0, 1)}
        if op not in ("start", "stop"):
            raise PoolError(f"unknown play op {op}")
        start = op == "start"
        with slot.play_lock:
            t0 = time.time()
            self._start_stop(slot, start)
            took = round(time.time() - t0, 1)
        out = {**self._play_status(slot), "took": took}
        if start and wait:
            out["matched"] = self.wait_log(lease_id, wait, "Server", timeout)
        return out

    def _start_stop(self, slot, start):
        """Under slot.play_lock."""
        self.mcp.call("start_stop_play", {"is_start": start, "studio_id": slot.studio_id}, timeout=120)
        slot.playing = start
        if start:
            for dm in ("Server", "Client"):  # capture logs from the start, before a flood can push them out
                try:
                    self._query_logs(slot, dm, {"since": 1e9, "limit": 0}, retry_for=30)
                except Exception as e:
                    slot.event("logs", f"could not start log capture in {dm}: {e}", notice=True)
            self._connect_play_server(slot)
        else:
            self._wait_mode(slot, "Edit", 30)

    def _wait_mode(self, slot, mode, timeout):
        deadline = time.time() + timeout
        while True:
            try:
                _, text = self.mcp.call("get_studio_state", {"studio_id": slot.studio_id}, timeout=15)
                if f"Current Studio Mode: {mode}" in text:
                    return
            except Exception:
                pass
            if time.time() > deadline:
                raise PoolError(f"Studio did not reach {mode} mode within {timeout}s")
            time.sleep(0.5)

    def _play_status(self, slot):
        try:
            _, text = self.mcp.call("get_studio_state", {"studio_id": slot.studio_id}, timeout=15)
            mode = re.search(r"Current Studio Mode: (\w+)", text)
            mode = mode.group(1) if mode else text.strip()
        except Exception as e:
            mode = f"unknown ({e})"
        return {"mode": mode, "playing": slot.playing, "restarting": slot.restarting,
                "new_events": self._new_events(slot)}

    def _new_events(self, slot):
        new = [e for e in slot.events if e["n"] > slot.events_seen]
        slot.events_seen = slot.event_count
        return new

    def _settle(self, slot, timeout=120):
        """Waits until the watcher has acted on recent file changes: an edit is picked up within ~1s, a command it
        runs can change more files, and a restart takes a few seconds."""
        if slot.watcher is None:
            return
        time.sleep(1.5)
        deadline = time.time() + timeout
        while slot.restarting or time.time() - slot.activity < 1.5:
            if time.time() > deadline:
                raise PoolError(f"file changes still being acted on after {timeout}s (restarting: {slot.restarting})")
            time.sleep(0.25)

    def _restart_play(self, slot, changed):
        names = ", ".join(Path(c[1]).name for c in sorted(changed)[:3])
        slot.restarting = True
        slot.event("restart", f"code changed ({names}), restarting playtest")
        t0 = time.time()
        try:
            with slot.play_lock:
                time.sleep(1.5)  # let Rojo sync the change into the Edit DataModel the next Play copies
                self._start_stop(slot, False)
                self._start_stop(slot, True)
            slot.event("restart", f"playtest restarted in {time.time() - t0:.1f}s")
        except Exception as e:
            slot.playing = False
            slot.event("restart", f"playtest restart failed after {time.time() - t0:.0f}s: {e}. "
                                  "Check `play status`; if Studio is stuck, release with --close.", notice=True)
        finally:
            slot.restarting = False
            slot.activity = time.time()

    def _connect_play_server(self, slot):
        """Point the Play server's Rojo plugin at the slot's rojo serve so file changes reach the running game."""
        if slot.rojo is None:
            return
        try:
            self._rojo(slot, {"op": "connect", "port": slot.rojo_port, "live": True}, datamodel="Server", timeout=60)
            log.info("slot %d: Play server syncing from :%d", slot.n, slot.rojo_port)
        except Exception as e:
            slot.event("rojo", f"could not connect the Play server to Rojo: {e}", notice=True)

    # --- logs ---

    def _query_logs(self, slot, dm, query, retry_for=0, studio_id=None):
        code = LOG_CAPTURE % lua_string(json.dumps(query))
        deadline = time.time() + retry_for
        while True:  # a new Play DataModel may not accept Luau for a moment
            try:
                return json.loads(self.mcp.luau(studio_id or slot.studio_id, code, datamodel=dm, timeout=20).strip())
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)

    def logs(self, lease_id, dm=None, level="output", grep=None, since=0, limit=200, group=True, exclude=None,
             everything=False, target=None):
        """everything: include lines matching log-ignore.txt (noise from installed Studio plugins).
        target: a multi-client lease's window (default server); "all" returns each window's lines."""
        _, slot = self.lease(lease_id)
        args = (level, grep, since, limit, group, exclude, everything)
        if slot.group and target == "all":
            return {"targets": {t: self._logs(slot, *self._target(slot, t), *args)
                                for t in [SERVER, *slot.group.players()]}}
        return self._logs(slot, *self._target(slot, target, dm), *args)

    def _logs(self, slot, studio_id, dm, target, level, grep, since, limit, group, exclude, everything):
        if level not in LEVELS:
            raise PoolError(f"level must be one of {LEVELS}")
        regex = re.compile(grep) if grep else None
        drop = [re.compile(exclude)] if exclude else []
        if not everything:
            drop += log_ignore()
        out = self._query_logs(slot, dm, {"since": since, "level": LEVELS.index(level), "group": group,
                                          "limit": 5000 if (regex or drop) else limit}, studio_id=studio_id)
        entries = [e for e in out["entries"]
                   if (not regex or regex.search(e["msg"])) and not any(d.search(e["msg"]) for d in drop)][-limit:]
        for e in entries:
            e["level"] = LEVELS[e["level"]]
        return {"dm": dm, **({"target": target} if target else {}), "cursor": out["cursor"],
                "dropped_before": out["dropped"], "entries": entries,
                **({"hidden_by_log_ignore": True} if not everything else {})}

    def wait_log(self, lease_id, pattern, dm=None, timeout=60, new=False, fail_on_error=True, target=None):
        """Waits for a log line matching pattern in the current playtest, after pending file changes have been acted
        on (so a restart you just caused counts). new: only lines printed from now on. fail_on_error: stop at the
        game's first error (log-ignore.txt lines excepted) and return it with the lines after it (its stack).
        target: a multi-client lease's window (default server), or "all" for the first match in any window."""
        _, slot = self.lease(lease_id)
        t0 = time.time()
        self._settle(slot, timeout)
        if slot.group and target == "all":
            sources = [self._target(slot, t) for t in [SERVER, *slot.group.players()]]
        else:
            sources = [self._target(slot, target, dm)]
        regex, ignore = re.compile(pattern), log_ignore()
        captures = {}  # studio_id -> [capture id, cursor]
        for sid, d, _ in sources:
            first = self._query_logs(slot, d, {"since": 1e9, "limit": 0}, retry_for=30, studio_id=sid)
            captures[sid] = [first["id"], first["cursor"] if new else 0]
        while True:
            for sid, d, name in sources:
                where = name or d
                capture, cursor = captures[sid]
                out = self._query_logs(slot, d, {"since": cursor, "limit": 5000}, retry_for=30, studio_id=sid)
                if out["id"] != capture:  # Play restarted: a new DataModel, read its capture from the start
                    captures[sid] = [out["id"], 0]
                    out = self._query_logs(slot, d, {"since": 0, "limit": 5000}, retry_for=30, studio_id=sid)
                entries = out["entries"]
                for i, e in enumerate(entries):
                    if regex.search(e["msg"]):
                        return {"dm": d, **({"target": name} if name else {}), "seq": e["seq"],
                                "level": LEVELS[e["level"]], "msg": e["msg"], "waited": round(time.time() - t0, 1)}
                    if fail_on_error and e["level"] == 3 and not any(x.search(e["msg"]) for x in ignore):
                        stack = [x["msg"][:300] for x in entries[i + 1:i + 8]]
                        raise PoolError(f"{where} error before /{pattern}/ matched: {e['msg'][:500]}"
                                        + (f"; next lines: {json.dumps(stack)}" if stack else "")
                                        + " (pass --no-fail-on-error to keep waiting)")
                captures[sid][1] = out["cursor"]
            if time.time() - t0 > timeout:
                sid, d, name = sources[0]
                tail = self._logs(slot, sid, d, name, "output", None, 0, 10, True, None, False)["entries"]
                raise PoolError(f"no {'log line' if len(sources) > 1 else (name or d) + ' log line'} matched "
                                f"/{pattern}/ within {timeout}s; last lines{' (' + (name or d) + ')'}: "
                                + json.dumps([f"[{e['level']}] {e['msg'][:200]}" for e in tail]))
            time.sleep(0.5)

    # --- RojoControl ---

    def _live_dm(self, slot):
        """The DataModel that answers Luau now: the Edit DataModel is unavailable while playing."""
        return "Server" if slot.playing or slot.restarting else "Edit"

    def _rojo(self, slot, command, datamodel="Edit", timeout=60):
        """Sends a RojoControl command and waits until the plugin reports it finished; returns the plugin's state."""
        slot.rojo_seq += 1
        seq = slot.rojo_seq
        payload = json.dumps(json.dumps({**command, "seq": seq}))
        set_code = f'game:GetService("CoreGui"):SetAttribute("{ROJO_COMMAND_ATTR}", {payload})'
        read_code = f'return game:GetService("CoreGui"):GetAttribute("{ROJO_STATE_ATTR}") or "null"'
        deadline = time.time() + timeout
        while True:  # the Play server's DataModel may not accept Luau for a moment after Play starts
            try:
                self.mcp.luau(slot.studio_id, set_code, datamodel=datamodel, timeout=10)
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        while True:
            state = json.loads(self.mcp.luau(slot.studio_id, read_code, datamodel=datamodel, timeout=10).strip())
            if state and state.get("seq") == seq and not state.get("busy"):
                if state.get("error"):
                    raise PoolError(f"Rojo: {state['error']}")
                return state
            if time.time() > deadline:
                raise PoolError(f"Rojo did not finish {command['op']} within {timeout}s (last state {state})")
            time.sleep(0.25)

    def rojo(self, lease_id, op, on=None):
        """Agent-facing control of the lease's sync: status, sync_once, live (on/off). Targets the Play server while
        playing (the Edit DataModel is unavailable then)."""
        _, slot = self.lease(lease_id)
        if slot.rojo is None:
            raise PoolError("this lease has no worktree synced")
        dm = self._live_dm(slot)
        if op == "status":
            raw = self.mcp.luau(slot.studio_id, f'return game:GetService("CoreGui"):GetAttribute("{ROJO_STATE_ATTR}") or "null"',
                                datamodel=dm)
            return {"dm": dm, **(json.loads(raw.strip()) or {})}
        if op == "sync_once":
            return {"dm": dm, **self._rojo(slot, {"op": "sync_once"}, datamodel=dm, timeout=180)}
        if op == "live":
            return {"dm": dm, **self._rojo(slot, {"op": "live", "on": bool(on)}, datamodel=dm, timeout=180)}
        raise PoolError(f"unknown rojo op {op}")

    # --- watches ---

    def _active_watches(self, slot):
        return [repo.WatchSet(slot.spec, w, slot.sources) for w in slot.spec.watches
                if w.action != "restart_play" or slot.restart_on_code]

    def _watch(self, slot, stop, sets):
        """Polls each watch's files every second. `run` watches run their hook in the Play server while playing;
        `restart_play` watches restart a pool-started playtest."""
        befores = [s.snapshot() for s in sets]
        rev = 0
        while not stop.wait(1):
            for i, ws in enumerate(sets):
                after = ws.snapshot()
                changed = sorted(k for k in after.keys() | befores[i].keys() if after.get(k) != befores[i].get(k))
                befores[i] = after
                if not changed or stop.is_set():
                    continue
                slot.activity = time.time()
                if ws.watch.action == "command":
                    names = ", ".join(c[1] for c in changed[:3])
                    try:
                        repo.run_command(slot.spec, ws.watch.command)
                        slot.event("watch", f"{names} changed -> ran `{ws.watch.command}`")
                    except repo.RepoError as e:
                        slot.event("watch", f"{names} changed -> {e}", notice=True)
                    slot.activity = time.time()
                    continue
                if ws.watch.action == "restart_play":
                    if slot.playing:
                        self._restart_play(slot, changed)
                        befores = [s.snapshot() for s in sets]  # the restarted Play loads everything anyway
                        break
                    continue
                rev += 1
                args = {"rev": rev, "changes": [repo.describe_change(slot.spec, k) for k in changed]}
                names = ", ".join(c[1] for c in changed[:5])
                if not slot.playing:
                    slot.event("watch", f"rev {rev} ({names}): not playing, the next Play loads it")
                    continue
                time.sleep(ws.watch.delay)
                try:
                    out = self._run_hook(slot, ws.watch.run, args)
                    slot.event("watch", f"rev {rev} ({names}) -> {ws.watch.run.name}: {out.strip()[:300] or 'ok'}")
                except Exception as e:
                    slot.event("watch", f"rev {rev} ({names}) -> {ws.watch.run.name} failed: {e}", notice=True)
                slot.activity = time.time()

    def _run_hook(self, slot, path, args):
        code = path.read_text(encoding="utf-8")
        wrapped = (f'local __rsp_args = game:GetService("HttpService"):JSONDecode({lua_string(json.dumps(args))})\n'
                   f"return (function(...)\n{code}\nend)(__rsp_args)")
        return self.mcp.luau(slot.studio_id, wrapped, datamodel="Server", timeout=30)

    # --- Studio MCP passthrough ---

    def call(self, lease_id, tool, args, target=None):
        lease, slot = self.lease(lease_id)
        if tool in DENIED_TOOLS:
            raise PoolError(f"{tool} is not available through the pool: {DENIED_TOOLS[tool]}")
        args = dict(args or {})
        if tool == "start_stop_play":
            out = self.play(lease_id, "start" if args.get("is_start") else "stop")
            return {"text": json.dumps(out), "images": []}
        if slot.restarting:  # don't talk to a Studio in the middle of stop/start
            with slot.play_lock:
                pass
        studio_id, dm, _ = self._target(slot, target, args.get("datamodel_type") if not slot.group else None)
        if tool == "execute_luau":
            args["datamodel_type"] = dm
        args["studio_id"] = studio_id  # always pinned to the lease's Studio (or the target's window)
        lease.calls += 1
        result, text = self.mcp.call(tool, args, timeout=300)
        images = []
        for i, c in enumerate(result.get("content", [])):
            if c.get("type") == "image":
                ext = (c.get("mimeType") or "image/png").split("/")[-1]
                path = STATE / "images" / f"{lease_id}-{lease.calls}-{i}.{ext}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(c["data"]))
                images.append(str(path))
        return {"text": text, "images": images}

    def take_notices(self, lease_id):
        with self.cond:
            lease = self.leases.get(lease_id)
            slot = lease and self.slots.get(lease.slot)
        if not slot or not slot.notices:
            return []
        notices, slot.notices = slot.notices, []
        return notices

    # --- housekeeping ---

    def _reaper(self):
        while True:
            time.sleep(5)
            now = time.time()
            if self.shown_lease in self.leases and self.desktop and self.desktop.showing():
                self.leases[self.shown_lease].last_seen = now  # the user is using it
            with self.cond:
                expired = [l.id for l in self.leases.values()
                           if now - l.last_seen > LEASE_TTL and (s := self.slots.get(l.slot)) and s.state == "ready"]
                dead = [l.id for l in self.leases.values()
                        if (s := self.slots.get(l.slot)) and s.proc and s.proc.poll() is not None]
                stale = [s for s in self.slots.values()
                         if s.lease is None and s.state == "ready"
                         and (now - s.idle_since > IDLE_TTL or (s.proc and s.proc.poll() is not None))]
                for s in stale:
                    del self.slots[s.n]
                    s.state = "closing"
                if stale:
                    self.cond.notify_all()
            for lease_id in set(expired) | set(dead):
                try:
                    self.release(lease_id, close=True,
                                 reason="expired" if lease_id in expired else "Studio died")
                except PoolError:
                    pass
            for s in stale:
                self._close(s, "idle too long" if s.proc.poll() is None else "exited while idle")

    def _bar_lines(self):
        with self.cond:
            slots = sorted(self.slots.values(), key=lambda s: s.n)
            clients = {l.id: l.client for l in self.leases.values()}
        return [{"label": f"slot {s.n}: {clients.get(s.lease, 'idle')}"
                          + (f" | test + {len(s.group.players())} clients" if s.group else "")
                          + (f" | {Path(s.worktree).name}" if s.worktree else "")
                          + (" | playing" if s.playing else "") + (f" | {s.state}" if s.state != "ready" else ""),
                 "pid": s.proc.pid if s.proc and s.state == "ready" else None}
                for s in slots]

    def show(self, hide=False, lease_id=None, target=None):
        """Switch the user's screen to the pool desktop (with lease_id: that Studio in front) or back. While the pool
        desktop is showing, the shown lease does not expire: the user is using it, not the agent's commands."""
        if not self.desktop:
            raise PoolError("Studios are on your own desktop (RSP_VISIBLE=1)")
        if hide:
            self.desktop.hide()
            return {"showing": self.desktop.showing()}
        slot = self.lease(lease_id)[1] if lease_id else None
        pid = slot.proc.pid if slot else None
        if slot and slot.group:
            pid = self._window(slot, target).pid
        elif target:
            raise PoolError("--target is only for multi-client leases")
        self.shown_lease = lease_id
        self.desktop.show(pid)
        return {"showing": self.desktop.showing(),
                "front": f"slot {slot.n}" + (f" {target or SERVER}" if slot.group else "") if slot else None,
                "note": "the user is now looking at the pool desktop; this lease won't expire while it shows"}

    def status(self):
        with self.cond:
            now = time.time()
            return {
                "cap": self.cap,
                "desktop": ({"name": desktop.DESKTOP_NAME, "hotkey": desktop.HOTKEY_TEXT,
                             "hotkey_registered": self.desktop.hotkey_registered, "showing": self.desktop.showing()}
                            if self.desktop else "visible (RSP_VISIBLE=1)"),
                "queue": len(self.queue),
                "slots": [{
                    "slot": s.n, "place": s.place, "state": s.state, "team_create": s.team_create,
                    "pid": s.proc.pid if s.proc else None, "studio_id": s.studio_id, "worktree": s.worktree,
                    "lease": s.lease, "playing": s.playing, "restarting": s.restarting,
                    "client": self.leases[s.lease].client if s.lease in self.leases else None,
                    "idle_for": None if s.lease else round(now - s.idle_since),
                    "last_event": s.events[-1] if s.events else None,
                    **({"studios": s.weight, "targets": [SERVER, *s.group.players()]} if s.group else {}),
                } for s in sorted(self.slots.values(), key=lambda s: s.n)],
                "studios_used": sum(s.weight for s in self.slots.values()),
            }

    def shutdown(self):
        with self.cond:
            slots = list(self.slots.values())
            self.slots.clear()
        for s in slots:
            self._close(s, "daemon shutting down")
        self.mcp.close()
        if self.desktop:
            self.desktop.close()


LOG_IGNORE = Path(__file__).parent / "log-ignore.txt"


def log_ignore():
    """Per-machine regexes for log lines that are not the game's (installed Studio plugins). Read on every use."""
    try:
        lines = LOG_IGNORE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [re.compile(l.strip()) for l in lines if l.strip() and not l.lstrip().startswith("#")]


def load_spec(worktree):
    try:
        return repo.load(worktree)
    except repo.RepoError as e:
        raise PoolError(str(e)) from e


def group_ids(place, universe, spec):
    """(place id, universe id) a multi-client test server runs as: the caller's pair, else the pair in the
    worktree's pool.toml, else 0/0 (no place: DataStores off)."""
    if bool(place) != bool(universe):
        raise PoolError("pass both --place and --universe, or neither")
    if place:
        return str(place), str(universe)
    if spec and (spec.place or spec.universe):
        if not (spec.place and spec.universe):
            missing = "universe" if spec.place else "place"
            raise PoolError(f"{spec.root / repo.SPEC_DIR / repo.SPEC_FILE} does not set `{missing}`: a multi-client "
                            "test needs both `place` and `universe` there, or --place and --universe")
        return spec.place, spec.universe
    return "0", "0"


def describe_watch(watch):
    return {"paths": watch.paths, "action": watch.action, **({"run": watch.run.name} if watch.run else {})}


def child_processes(parent_pid):
    """[{ProcessId, CommandLine}] of a process's children (command lines can hold secrets: don't log them)."""
    ps = (f'Get-CimInstance Win32_Process -Filter "ParentProcessId={int(parent_pid)}" '
          "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=30).stdout.strip()
    if not out:
        return []
    data = json.loads(out)
    return [data] if isinstance(data, dict) else data


def kill_leftovers():
    try:
        leftovers = json.loads(PIDS.read_text())
    except (OSError, ValueError):
        return
    for entry in leftovers:
        found = subprocess.run(["tasklist", "/FI", f"PID eq {entry['pid']}", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True).stdout
        if entry["image"].lower() in found.lower():  # guards against a reused pid
            subprocess.run(["taskkill", "/PID", str(entry["pid"]), "/T", "/F"], capture_output=True)
            log.warning("killed leftover %s pid %d from a previous daemon", entry["image"], entry["pid"])
    PIDS.write_text("[]")


# --- HTTP ---

def make_handler(p: Pool):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/status":
                return self._reply(200, p.status())
            if self.path == "/tools":
                return self._reply(200, p.mcp.request("tools/list", {}))
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            routes = {
                "/acquire": lambda: p.acquire(**body),
                "/sync": lambda: p.sync(body["lease"], body["worktree"], body.get("mounts")),
                "/call": lambda: p.call(body["lease"], body["tool"], body.get("args"), body.get("target")),
                "/play": lambda: p.play(body["lease"], body["op"], body.get("wait"), body.get("timeout", 120)),
                "/logs": lambda: p.logs(body["lease"], body.get("dm"), body.get("level", "output"), body.get("grep"),
                                        body.get("since", 0), body.get("limit", 200), body.get("group", True),
                                        body.get("exclude"), body.get("all", False), body.get("target")),
                "/wait_log": lambda: p.wait_log(body["lease"], body["pattern"], body.get("dm"),
                                                body.get("timeout", 60), body.get("new", False),
                                                body.get("fail_on_error", True), body.get("target")),
                "/clients": lambda: p.clients(body["lease"], body["op"], body.get("target"), body.get("count", 1),
                                              body.get("kick", False)),
                "/release": lambda: p.release(body["lease"], close=body.get("close", False)),
                "/heartbeat": lambda: {"ok": bool(p.lease(body["lease"]))},
                "/rojo": lambda: p.rojo(body["lease"], body["op"], body.get("on")),
                "/show": lambda: p.show(hide=body.get("hide", False), lease_id=body.get("lease"),
                                        target=body.get("target")),
                "/shutdown": lambda: threading.Thread(target=self.server.shutdown).start() or {"ok": True},
            }
            route = routes.get(self.path)
            if route is None:
                return self._reply(404, {"error": "not found"})
            try:
                out = route()
                if isinstance(out, dict) and body.get("lease") and self.path != "/release":
                    notices = p.take_notices(body["lease"])
                    if notices:
                        out["notices"] = notices
                self._reply(200, out)
            except PoolError as e:
                self._reply(e.status, {"error": str(e)})
            except Exception as e:
                log.exception("%s failed", self.path)
                self._reply(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


def serve(cap):
    STATE.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(STATE / "daemon.log")])
    try:
        server = ThreadingHTTPServer((HOST, PORT), None)
    except OSError as e:
        sys.exit(f"cannot bind {HOST}:{PORT} (daemon already running?): {e}")
    p = Pool(cap)
    server.RequestHandlerClass = make_handler(p)
    log.info("rbx-studio-pool spike listening on http://%s:%d, cap %d", HOST, PORT, cap)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("shutting down")
        p.shutdown()


# --- client ---

def fail(message, status=None):
    print(json.dumps({"error": message, **({"status": status} if status else {})}, indent=2))
    sys.exit(1)


def request(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://{HOST}:{PORT}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        fail(json.loads(e.read()).get("error"), e.code)
    except urllib.error.URLError as e:
        fail(f"daemon not reachable at {HOST}:{PORT} ({e.reason}); ask the user to start it (`python rsp.py serve`)")


def parse_pairs(pairs):
    for pair in pairs:
        if "=" not in pair:
            fail(f"expected NAME=DIR, got {pair!r}")
    return dict(pair.split("=", 1) for pair in pairs) or None


def print_notices(out):
    for n in out.get("notices", []):
        print(f"NOTICE [{n['t']} {n['kind']}] {n['detail']}", file=sys.stderr)


def print_logs(out):
    print(f"# {out.get('target') or out['dm']} log of the current playtest, cursor {out['cursor']} (--since {out['cursor']} for newer lines)"
          + (f", {out['dropped_before']} oldest lines dropped" if out["dropped_before"] else "")
          + (", plugin noise hidden (--all shows it)" if out.get("hidden_by_log_ignore") else ""))
    for e in out["entries"]:
        count = f" x{e['count']}, last #{e['last']}" if e.get("count", 1) > 1 else ""
        print(f"#{e['seq']} [{e['level']}{count}] {e['msg']}")


def print_tools(tools, name):
    if name:
        match = [t for t in tools if t["name"] == name]
        if not match:
            fail(f"no tool {name}")
        print(json.dumps(match[0], indent=2))
        return
    for t in tools:
        props = t.get("inputSchema", {}).get("properties", {})
        required = set(t.get("inputSchema", {}).get("required", []))
        args = ", ".join(f"{k}{'' if k in required else '?'}: {v.get('type', '?')}" for k, v in props.items()
                         if k != "studio_id")
        denied = " (blocked by the pool)" if t["name"] in DENIED_TOOLS else ""
        print(f"{t['name']}({args}){denied}\n    {(t.get('description') or '').strip().splitlines()[0][:160]}")


def cloud_key(op):
    """Runs locally (not through the daemon): the daemon reads the keychain itself on each cloud acquire."""
    if op == "set":
        if sys.stdin.isatty():
            import getpass
            key = getpass.getpass("Roblox Open Cloud API key (input hidden): ")
        else:
            key = sys.stdin.readline()
        key = key.strip()
        if not key:
            fail("no key given")
        cloudplace.set_key(key)
        print(f"stored in the OS keychain ({cloudplace.KEYRING_SERVICE} / {cloudplace.KEYRING_USER})")
    elif op == "clear":
        print("removed" if cloudplace.clear_key() else "no key was stored")
    else:
        try:
            key, source = cloudplace.api_key()
        except cloudplace.CloudPlaceError as e:
            fail(str(e))
        print(f"key found in {source} ({len(key)} chars)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--cap", type=int, default=3)
    a = sub.add_parser("acquire")
    a.add_argument("--worktree", help="repo to sync; alone, it also builds a local template from the project")
    a.add_argument("--place-file", help="template .rbxl, or cloud:<placeId> (downloaded, cached by version); each "
                                        "slot opens its own throwaway copy")
    a.add_argument("--place")
    a.add_argument("--universe")
    a.add_argument("--mount", action="append", default=[], metavar="NAME=DIR",
                   help="source folder for a mount the repo's pool.toml declares (repeatable)")
    a.add_argument("--team-create", action="store_true")
    a.add_argument("--restart-on-code", action="store_true",
                   help="restart a running playtest when the worktree's code changes")
    a.add_argument("--fresh", action="store_true", help="never reuse a warm Studio")
    a.add_argument("--client", default="cli")
    a.add_argument("--wait", type=int, default=600)
    a.add_argument("--clients", type=int, default=0, metavar="N",
                   help="multi-client test: a test server + N players (Clients and Servers), from --place-file or "
                        "a `rojo build` of --worktree; targets are server, Player1..PlayerN")
    y = sub.add_parser("sync")
    y.add_argument("lease")
    y.add_argument("worktree")
    y.add_argument("--mount", action="append", default=[], metavar="NAME=DIR")
    lu = sub.add_parser("luau", help="run Luau; prints what it returns")
    lu.add_argument("lease")
    lu.add_argument("code", nargs="?", help="Luau source, or - for stdin")
    lu.add_argument("--file", help="read the Luau from this file")
    lu.add_argument("--dm", choices=["Edit", "Server", "Client"], help="default: Edit, or Server while playing")
    lu.add_argument("--target", help="multi-client lease: server (default), PlayerN")
    pl = sub.add_parser("play", help="start | stop | status")
    pl.add_argument("lease")
    pl.add_argument("op", choices=["start", "stop", "status", "wait", "restart"],
                    help="wait: until file changes have been acted on (e.g. a restart-on-code restart finished); "
                         "restart: relaunch a multi-client test (rebuilt from the worktree if it was built)")
    pl.add_argument("--wait", metavar="REGEX", help="after start, wait for a Server log line matching this")
    pl.add_argument("--timeout", type=int, default=120)
    lg = sub.add_parser("logs", help="captured log lines, repeats grouped")
    lg.add_argument("lease")
    lg.add_argument("--dm", choices=["Edit", "Server", "Client"], help="default: Edit, or Server while playing")
    lg.add_argument("--level", choices=LEVELS, default="output", help="minimum level")
    lg.add_argument("--grep", metavar="REGEX")
    lg.add_argument("--since", type=int, default=0, metavar="CURSOR", help="only lines after this cursor")
    lg.add_argument("--limit", type=int, default=200)
    lg.add_argument("--no-group", action="store_true", help="every line, in order, repeats not grouped")
    lg.add_argument("--exclude", metavar="REGEX", help="hide lines matching this")
    lg.add_argument("--all", action="store_true", help="also show lines matching log-ignore.txt (plugin noise)")
    lg.add_argument("--json", action="store_true")
    lg.add_argument("--target", help="multi-client lease: server (default), PlayerN, or all")
    wl = sub.add_parser("wait-log", help="wait for a log line matching REGEX")
    wl.add_argument("lease")
    wl.add_argument("pattern", metavar="REGEX")
    wl.add_argument("--dm", choices=["Edit", "Server", "Client"])
    wl.add_argument("--timeout", type=int, default=60)
    wl.add_argument("--new", action="store_true", help="only lines printed after this command starts")
    wl.add_argument("--no-fail-on-error", action="store_true", help="keep waiting past game errors")
    wl.add_argument("--target", help="multi-client lease: server (default), PlayerN, or all (first match in any window)")
    c = sub.add_parser("call")
    c.add_argument("lease")
    c.add_argument("tool")
    c.add_argument("args", nargs="?", default="{}")
    c.add_argument("--target", help="multi-client lease: server (default), PlayerN")
    cl = sub.add_parser("clients", help="multi-client lease: list | add [N] | remove PlayerN [--kick]")
    cl.add_argument("lease")
    cl.add_argument("op", choices=["list", "add", "remove"])
    cl.add_argument("arg", nargs="?", help="add: how many (default 1); remove: the player, e.g. Player2")
    cl.add_argument("--kick", action="store_true",
                    help="remove with Player:Kick() (a clean leave) instead of killing the client's process")
    r = sub.add_parser("release")
    r.add_argument("lease")
    r.add_argument("--close", action="store_true")
    rj = sub.add_parser("rojo", help="control the lease's Rojo sync: status | sync-once | live on|off")
    rj.add_argument("lease")
    rj.add_argument("op", choices=["status", "sync-once", "live"])
    rj.add_argument("on", nargs="?", choices=["on", "off"])
    sub.add_parser("status")
    t = sub.add_parser("tools", help="Studio MCP tools and their arguments")
    t.add_argument("name", nargs="?", help="show this tool's full schema")
    sh = sub.add_parser("show", help="switch the screen to the pool desktop (Ctrl+Alt+F12), LEASE's Studio in front")
    sh.add_argument("lease", nargs="?")
    sh.add_argument("--target", help="multi-client lease: server (default), PlayerN")
    sub.add_parser("hide", help="switch back to your desktop")
    sub.add_parser("shutdown", help="close every pool Studio and stop the daemon")
    ck = sub.add_parser("cloud-key", help="the Roblox API key for --place-file cloud:<id>, in the OS keychain: "
                                          "set | status | clear")
    ck.add_argument("op", choices=["set", "status", "clear"])
    args = ap.parse_args()

    if args.cmd == "serve":
        return serve(args.cap)
    if args.cmd == "cloud-key":
        return cloud_key(args.op)
    if args.cmd == "acquire":
        out = request("POST", "/acquire", {
            "place": args.place, "universe": args.universe, "place_file": args.place_file,
            "worktree": args.worktree, "mounts": parse_pairs(args.mount),
            "team_create": args.team_create, "restart_on_code": args.restart_on_code, "fresh": args.fresh,
            "client": args.client, "wait": args.wait, "clients": args.clients})
    elif args.cmd == "sync":
        out = request("POST", "/sync", {"lease": args.lease, "worktree": args.worktree,
                                        "mounts": parse_pairs(args.mount)})
    elif args.cmd == "luau":
        if args.file:
            code = Path(args.file).read_text(encoding="utf-8")
        elif args.code == "-":
            code = sys.stdin.read()
        elif args.code:
            code = args.code
        else:
            fail("pass CODE, - (stdin) or --file")
        tool_args = {"code": code, **({"datamodel_type": args.dm} if args.dm else {})}
        out = request("POST", "/call", {"lease": args.lease, "tool": "execute_luau", "args": tool_args,
                                        "target": args.target})
        print_notices(out)
        print(out["text"])
        return
    elif args.cmd == "play":
        out = request("POST", "/play", {"lease": args.lease, "op": args.op, "wait": args.wait,
                                        "timeout": args.timeout})
    elif args.cmd == "logs":
        out = request("POST", "/logs", {"lease": args.lease, "dm": args.dm, "level": args.level, "grep": args.grep,
                                        "since": args.since, "limit": args.limit, "group": not args.no_group,
                                        "exclude": args.exclude, "all": args.all, "target": args.target})
        print_notices(out)
        if args.json:
            return print(json.dumps(out, indent=2))
        for logs in (out["targets"].values() if "targets" in out else [out]):
            print_logs(logs)
        return
    elif args.cmd == "wait-log":
        out = request("POST", "/wait_log", {"lease": args.lease, "pattern": args.pattern, "dm": args.dm,
                                            "timeout": args.timeout, "new": args.new,
                                            "fail_on_error": not args.no_fail_on_error, "target": args.target})
    elif args.cmd == "call":
        try:
            tool_args = json.loads(args.args)
        except json.JSONDecodeError as e:
            fail(f"ARGS is not JSON: {e}")
        out = request("POST", "/call", {"lease": args.lease, "tool": args.tool, "args": tool_args,
                                        "target": args.target})
    elif args.cmd == "clients":
        body = {"lease": args.lease, "op": args.op, "kick": args.kick}
        if args.op == "add":
            body["count"] = int(args.arg or 1)
        elif args.op == "remove":
            if not args.arg:
                fail("remove needs the player, e.g. `clients LEASE remove Player2`")
            body["target"] = args.arg
        out = request("POST", "/clients", body)
    elif args.cmd == "release":
        out = request("POST", "/release", {"lease": args.lease, "close": args.close})
    elif args.cmd == "rojo":
        out = request("POST", "/rojo", {"lease": args.lease, "op": args.op.replace("-", "_"), "on": args.on == "on"})
    elif args.cmd in ("show", "hide"):
        out = request("POST", "/show", {"hide": args.cmd == "hide", "lease": getattr(args, "lease", None),
                                        "target": getattr(args, "target", None)})
    elif args.cmd == "shutdown":
        out = request("POST", "/shutdown", {})
    elif args.cmd == "status":
        out = request("GET", "/status")
    else:
        return print_tools(request("GET", "/tools")["tools"], args.name)
    print_notices(out)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("spike is Windows-only")
    for stream in (sys.stdout, sys.stderr):  # game logs and tool descriptions are not cp1252
        stream.reconfigure(encoding="utf-8", errors="replace")
    main()
