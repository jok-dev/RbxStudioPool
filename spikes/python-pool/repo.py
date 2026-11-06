"""Per-repo pool settings: `.rbx-studio-pool/pool.toml` in a worktree's root. A repo without one gets the defaults.

    project = "default.project.json"      # Rojo project to serve
    place = 123456789                     # optional: what local copies pretend to be (SetPlaceId/SetUniverseId)
    universe = 987654321
    template = "C:/path/to/map.rbxl"      # optional: local template; default is `rojo build` of the project
    # or: template = "cloud"              # download `place` from Roblox (cached by version); "cloud:<id>" for another
    rojo = "C:/path/to/rojo.exe"          # optional: default is the repo's toolchain (rokit.toml / aftman.toml)

    [[prepare]]                           # run in the worktree before Rojo when `creates` is missing
    run = "wally install"
    creates = "Packages"

    [mounts.NAME]                         # an outside folder synced into the DataModel; source per lease (--mount)
    target = "ServerStorage.Mounts.NAME"
    ignore = [".*", "tools"]              # top-level entries of the source to leave out (fnmatch)
    default = "../NAME"                   # optional source when the lease gives none (relative to the repo)

    [[watch]]                             # file changes -> an action
    paths = ["@NAME/**"]                  # globs; "@NAME/..." is relative to that mount's source, others to the repo
    run = "hooks/changed.luau"            # Luau run in the Play server while playing; `...` is {rev, changes}
    # or: command = "blink net/net.blink" # a shell command in the repo (e.g. codegen), whether playing or not
    # or: action = "restart_play"         # restart a pool-started playtest (only when the lease asks for it)

Commands (prepare and watch) run in the repo root with the repo's toolchain (Rokit/Aftman bin) first on PATH.
"""

import fnmatch
import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SPEC_DIR = ".rbx-studio-pool"
SPEC_FILE = "pool.toml"
DEFAULT_PROJECT = "default.project.json"
DEFAULT_CODE_GLOBS = ["src/**/*.lua", "src/**/*.luau"]


class RepoError(Exception):
    pass


@dataclass
class Mount:
    name: str
    target: list[str]
    ignore: list[str]
    default: Path | None

    def included(self, rel_parts):
        # Rojo reserves "$" keys in a project tree, so an entry named like "$env" would make `rojo serve` exit
        if rel_parts[0].startswith("$"):
            return False
        return not any(fnmatch.fnmatch(rel_parts[0], pattern) for pattern in self.ignore)


@dataclass
class Watch:
    paths: list[str]
    action: str  # "run" | "command" | "restart_play"
    run: Path | None = None
    command: str | None = None
    delay: float = 1.5  # let Rojo push the change into the Play server first


@dataclass
class RepoSpec:
    root: Path
    project: Path
    place: str | None = None
    universe: str | None = None
    template: Path | str | None = None  # a path, or "cloud:<placeId>"
    prepare: list[dict] = field(default_factory=list)
    mounts: dict[str, Mount] = field(default_factory=dict)
    watches: list[Watch] = field(default_factory=list)
    from_file: bool = False
    rojo: str = "rojo"


def _template(raw, root, spec_path):
    t = raw.get("template")
    if t is None:
        return None
    if t == "cloud":
        if "place" not in raw:
            raise RepoError(f'{spec_path}: template = "cloud" needs `place`')
        return f"cloud:{raw['place']}"
    if str(t).startswith("cloud:"):
        return str(t)
    return root / t


def load(worktree) -> RepoSpec:
    root = Path(worktree).resolve()
    spec_path = root / SPEC_DIR / SPEC_FILE
    if not spec_path.is_file():
        spec = RepoSpec(root, root / DEFAULT_PROJECT)
    else:
        try:
            raw = tomllib.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise RepoError(f"{spec_path}: {e}") from e
        spec = RepoSpec(
            root,
            root / raw.get("project", DEFAULT_PROJECT),
            place=str(raw["place"]) if "place" in raw else None,
            universe=str(raw["universe"]) if "universe" in raw else None,
            template=_template(raw, root, spec_path),
            prepare=list(raw.get("prepare", [])),
            from_file=True,
        )
        for name, m in raw.get("mounts", {}).items():
            if "target" not in m:
                raise RepoError(f"{spec_path}: mount {name} has no target")
            spec.mounts[name] = Mount(name, m["target"].split("."), list(m.get("ignore", [])),
                                      (root / m["default"]).resolve() if "default" in m else None)
        for w in raw.get("watch", []):
            if "run" in w:
                hook = root / SPEC_DIR / w["run"]
                if not hook.is_file():
                    raise RepoError(f"{spec_path}: hook {w['run']} not found")
                watch = Watch(list(w["paths"]), "run", hook, float(w.get("delay", 1.5)))
            elif "command" in w:
                watch = Watch(list(w["paths"]), "command", command=w["command"])
            elif w.get("action") == "restart_play":
                watch = Watch(list(w["paths"]), "restart_play")
            else:
                raise RepoError(f"{spec_path}: a [[watch]] needs `run`, `command` or `action = \"restart_play\"`")
            for p in watch.paths:
                if p.startswith("@") and p[1:].split("/", 1)[0] not in spec.mounts:
                    raise RepoError(f"{spec_path}: watch path {p} names an unknown mount")
            spec.watches.append(watch)
    if not any(w.action == "restart_play" for w in spec.watches):  # default; --restart-on-code turns it on
        spec.watches.append(Watch(DEFAULT_CODE_GLOBS, "restart_play"))
    if not spec.project.is_file():
        raise RepoError(f"no Rojo project at {spec.project}")
    spec.rojo = raw.get("rojo") if spec.from_file and raw.get("rojo") else toolchain_rojo(root)
    return spec


def toolchain_rojo(root: Path):
    """The rojo the repo's toolchain manager pins. Rokit and Aftman both put a `rojo` shim on PATH, and the one that
    wins refuses to run in a repo pinned by the other, so call the matching shim directly."""
    home = Path.home()
    for folder in (root, *root.parents):
        for manifest, shim in (("rokit.toml", home / ".rokit" / "bin" / "rojo.exe"),
                               ("aftman.toml", home / ".aftman" / "bin" / "rojo.exe")):
            if (folder / manifest).is_file() and shim.is_file():
                return str(shim)
    return "rojo"


def resolve_mounts(spec: RepoSpec, given: dict | None):
    """{name: source dir} for this lease: the lease's paths, else the repo's defaults. Mounts with neither are skipped."""
    given = dict(given or {})
    unknown = set(given) - set(spec.mounts)
    if unknown:
        raise RepoError(f"unknown mount(s) {sorted(unknown)}; this repo declares {sorted(spec.mounts) or 'none'}")
    sources, skipped = {}, []
    for name, mount in spec.mounts.items():
        source = Path(given[name]).resolve() if name in given else mount.default
        if source is None:
            skipped.append(name)
            continue
        if not source.is_dir():
            raise RepoError(f"mount {name}: {source} is not a folder")
        sources[name] = source
    return sources, skipped


def run_command(spec: RepoSpec, command):
    """A shell command in the repo root, with the repo's toolchain bin first on PATH (tools like blink are often
    only reachable through the toolchain manager). Raises RepoError on failure."""
    env = dict(os.environ)
    if os.path.isabs(spec.rojo):
        env["PATH"] = str(Path(spec.rojo).parent) + os.pathsep + env.get("PATH", "")
    out = subprocess.run(command, shell=True, cwd=spec.root, env=env, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RepoError(f"`{command}` failed ({out.returncode}): {(out.stdout + out.stderr).strip()[-600:]}")
    return (out.stdout + out.stderr).strip()


def prepare(spec: RepoSpec):
    """Runs the repo's prepare commands, except those whose `creates` path exists. Returns what ran."""
    ran = []
    for step in spec.prepare:
        creates = step.get("creates")
        if creates and (spec.root / creates).exists():
            continue
        try:
            run_command(spec, step["run"])
        except RepoError as e:
            raise RepoError(f"prepare {e}") from e
        ran.append(step["run"])
    return ran


def build_project(spec: RepoSpec, sources: dict, out: Path):
    """The repo's project with absolute paths, plus each mount's source entries under its target."""
    base = spec.project.parent
    project = json.loads(spec.project.read_text(encoding="utf-8"))

    def absolutize(node):
        if isinstance(node, dict):
            if "$path" in node:
                node["$path"] = str((base / node["$path"]).resolve())
            for value in node.values():
                absolutize(value)

    absolutize(project["tree"])
    for name, source in sources.items():
        mount = spec.mounts[name]
        node = project["tree"]
        for i, part in enumerate(mount.target):
            # top level = a service (className = its name); below that, plain Folders
            node = node.setdefault(part, {"$className": part if i == 0 else "Folder"})
        # map entries one by one: globIgnorePaths does not apply to paths outside the project's folder
        for entry in sorted(source.iterdir()):
            if mount.included((entry.name,)):
                node[entry.stem if entry.is_file() else entry.name] = {"$path": str(entry)}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(project, indent=2), encoding="utf-8")
    return out


def glob_regex(pattern):
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


class WatchSet:
    """mtime snapshots of one watch's files."""

    def __init__(self, spec: RepoSpec, watch: Watch, sources: dict):
        self.watch = watch
        self.roots = []  # (label, mount or None, base dir, static prefix, regex)
        for p in watch.paths:
            if p.startswith("@"):
                name, _, rel = p[1:].partition("/")
                if name not in sources:
                    continue  # mount not synced on this lease
                base, mount = sources[name], spec.mounts[name]
            else:
                name, rel, base, mount = None, p, spec.root, None
            static = []
            for part in rel.split("/"):
                if any(c in part for c in "*?["):
                    break
                static.append(part)
            self.roots.append((name, mount, base, "/".join(static), glob_regex(rel or "**")))

    def snapshot(self):
        files = {}
        for name, mount, base, static, regex in self.roots:
            start = base / static if static else base
            if start.is_file():
                candidates = [start]
            elif start.is_dir():
                candidates = (p for p in start.rglob("*") if p.is_file())
            else:
                continue
            for path in candidates:
                rel = path.relative_to(base)
                if mount and not mount.included(rel.parts):
                    continue
                if regex.match(rel.as_posix()):
                    try:
                        files[(name, rel.as_posix())] = path.stat().st_mtime_ns
                    except OSError:
                        pass
        return files


def describe_change(spec: RepoSpec, key):
    """One hook argument entry: {path, mount?, instance?}."""
    name, rel = key
    if name is None:
        return {"path": rel}
    parts = Path(rel).with_suffix("").parts
    return {"mount": name, "path": rel, "instance": ".".join([*spec.mounts[name].target, *parts])}
