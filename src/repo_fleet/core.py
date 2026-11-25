"""Core library: config loading, repo discovery, status, pull and bundle backup.

Design rules (each is covered by a test):
  * Never push, merge-commit, rebase or switch branches. Pull is fast-forward only, on the default
    branch, on a clean tree.
  * A class marked ``pull_only`` gets its push URL disabled every run, so an accident cannot publish it.
  * A bundle is only kept if ``git bundle verify`` passes; the previous good bundle is kept as ``.prev``.
  * Bundles are rebuilt only when the repository's refs changed (hash of ``for-each-ref``).
  * A backup run reports failure if fewer than ``min_copies`` destinations received the data.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
DISABLED_PUSH = "DISABLED-pull-only"


# ----------------------------------------------------------------------------- config
@dataclass
class Root:
    cls: str
    path: Path
    pull_only: bool = False
    local_only: bool = False      # repos here intentionally have no remote (not flagged by status)


@dataclass
class Destination:
    """Where bundle copies go: a local directory or an ``ssh`` target ``host:path``."""
    target: str
    require_mount: str | None = None      # skip (don't fail) when this mount point is absent
    skip_classes: list[str] = field(default_factory=list)   # e.g. ["private"] for shared destinations

    @property
    def is_remote(self) -> bool:
        return ":" in self.target and not self.target.startswith("/")


@dataclass
class Config:
    roots: list[Root]
    backup_dir: Path
    state_dir: Path
    private: set[str] = field(default_factory=set)       # repo names kept out of shared destinations
    destinations: list[Destination] = field(default_factory=list)
    min_copies: int = 2                                  # counting the primary backup_dir copy


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p)))


def load_config(path: str | os.PathLike) -> Config:
    raw = tomllib.loads(Path(path).read_text())
    roots = [Root(r["class"], _expand(r["path"]), bool(r.get("pull_only", False)), bool(r.get("local_only", False))) for r in raw.get("root", [])]
    if not roots:
        raise ValueError("config needs at least one [[root]] table")
    b = raw.get("backup", {})
    dests = [Destination(d["target"], d.get("require_mount"), list(d.get("skip_classes", [])))
             for d in b.get("destination", [])]
    return Config(
        roots=roots,
        backup_dir=_expand(b.get("dir", "~/backups/repo-fleet")),
        state_dir=_expand(raw.get("state_dir", "~/.local/state/repo-fleet")),
        private=set(b.get("private", [])),
        destinations=dests,
        min_copies=int(b.get("min_copies", 2)),
    )


# ----------------------------------------------------------------------------- subprocess helpers
def sh(cmd: list[str], cwd: str | None = None, timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, env=ENV, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError as e:
        return 127, str(e)


def git(repo: str | os.PathLike, *args: str, timeout: int = 120) -> tuple[int, str]:
    return sh(["git", "-C", str(repo), *args], timeout=timeout)


# ----------------------------------------------------------------------------- discovery & state
@dataclass
class Repo:
    cls: str
    name: str
    path: str
    pull_only: bool = False
    local_only: bool = False


def discover(cfg: Config) -> list[Repo]:
    """Every direct child of each root that is a git repo; symlinked duplicates are counted once."""
    seen: set[str] = set()
    out: list[Repo] = []
    for root in cfg.roots:
        if not root.path.is_dir():
            continue
        for child in sorted(root.path.iterdir()):
            real = os.path.realpath(child)
            if real in seen or not (Path(real) / ".git").exists():
                continue
            seen.add(real)
            out.append(Repo(root.cls, child.name, real, root.pull_only, root.local_only))
    return out


def has_remote(p: str) -> bool:
    return bool(git(p, "remote")[1])


def has_commits(p: str) -> bool:
    return git(p, "rev-parse", "-q", "--verify", "HEAD")[0] == 0


def default_branch(p: str) -> str | None:
    rc, o = git(p, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and "/" in o:
        return o.split("/", 1)[1]
    for b in ("main", "master"):
        if git(p, "rev-parse", "--verify", "-q", f"refs/heads/{b}")[0] == 0:
            return b
    return None


def info(p: str) -> dict:
    d: dict = {"branch": git(p, "branch", "--show-current")[1] or "(detached)"}
    d["dirty"] = len([x for x in git(p, "status", "--porcelain")[1].splitlines() if x])
    d["ahead"] = d["behind"] = None
    rc, o = git(p, "rev-list", "--left-right", "--count", "@{u}...HEAD")
    if rc == 0:
        behind, ahead = o.split()
        d["ahead"], d["behind"] = int(ahead), int(behind)
    return d


def attention_flags(repo: Repo, i: dict) -> list[str]:
    flags = []
    if i["dirty"]:
        flags.append(f"DIRTY({i['dirty']})")
    if i["ahead"]:
        flags.append(f"ahead {i['ahead']}")
    if i["behind"]:
        flags.append(f"behind {i['behind']}")
    if not repo.local_only and not has_remote(repo.path):
        flags.append("no-remote")
    return flags


# ----------------------------------------------------------------------------- commands
def cmd_status(cfg: Config, fetch: bool = False, out=print) -> int:
    repos = discover(cfg)
    bad = 0
    for r in repos:
        if fetch and has_remote(r.path):
            git(r.path, "fetch", "--all", "--prune", "-q", timeout=90)
        i = info(r.path)
        flags = attention_flags(r, i)
        bad += bool(flags)
        out(f"{r.cls:10} {r.name:32} {i['branch']:20} {' '.join(flags) or 'ok'}")
    out(f"== {len(repos)} repos, {bad} need attention")
    return bad


def cmd_pull(cfg: Config, out=print) -> int:
    fails = 0
    for r in discover(cfg):
        if not has_remote(r.path) or not has_commits(r.path):
            continue
        if r.pull_only:
            git(r.path, "config", "--local", "remote.origin.pushurl", DISABLED_PUSH)
        rc, o = git(r.path, "fetch", "origin", "--prune", "-q", timeout=180)
        for extra in git(r.path, "remote")[1].split():     # secondary remotes: best effort
            if extra != "origin" and git(r.path, "fetch", extra, "--prune", "-q", timeout=90)[0] != 0:
                out(f"note      {r.cls}/{r.name}: secondary remote '{extra}' unreachable (ignored)")
        if rc != 0:
            fails += 1
            out(f"FETCH FAIL {r.cls}/{r.name}: {o.splitlines()[-1] if o else rc}")
            continue
        i = info(r.path)
        db = default_branch(r.path)
        if isinstance(i["behind"], int) and i["behind"] > 0:
            if i["dirty"] or i["branch"] != db or i["ahead"]:
                out(f"behind    {r.cls}/{r.name} by {i['behind']} (left alone: dirty, ahead, or not on {db})")
            else:
                rc, o = git(r.path, "merge", "--ff-only", "-q")
                out(f"updated   {r.cls}/{r.name} +{i['behind']}" if rc == 0 else f"FF FAIL {r.cls}/{r.name}: {o}")
                fails += rc != 0
    return fails


def refs_hash(p: str) -> str:
    return hashlib.sha256(git(p, "for-each-ref", "--format=%(refname) %(objectname)")[1].encode()).hexdigest()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(directory: Path) -> None:
    files = sorted(f for f in os.listdir(directory) if f.endswith(".bundle"))
    with open(directory / "SHA256SUMS", "w") as o:
        for f in files:
            o.write(f"{_sha256(directory / f)}  {f}\n")


def verify_checksums(directory: Path) -> list[str]:
    """Return the names whose bundle no longer matches SHA256SUMS (empty list = all good)."""
    bad = []
    sums = directory / "SHA256SUMS"
    if not sums.exists():
        return ["SHA256SUMS missing"]
    for line in sums.read_text().splitlines():
        digest, _, name = line.partition("  ")
        f = directory / name
        if not f.exists() or _sha256(f) != digest:
            bad.append(name)
    return bad


def _copy_to(dest: Destination, src: Path) -> bool:
    excludes = ["--exclude=*.prev"] + [f"--exclude={c}/" for c in dest.skip_classes]
    base = ["rsync", "-a", "--delete", *excludes]
    if dest.is_remote:
        host, _, rpath = dest.target.partition(":")
        ssh = "ssh -o BatchMode=yes -o ConnectTimeout=8"
        rc, _ = sh([*base, "-e", ssh, f"{src}/", f"{dest.target}/"], timeout=900)
        if rc in (12, 23, 255):                      # first run: remote directory missing
            sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, f"mkdir -p {rpath}"])
            rc, _ = sh([*base, "-e", ssh, f"{src}/", f"{dest.target}/"], timeout=900)
        return rc == 0
    Path(dest.target).mkdir(parents=True, exist_ok=True)
    return sh([*base, f"{src}/", f"{dest.target}/"], timeout=900)[0] == 0


def cmd_bundle(cfg: Config, out=print) -> int:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    state_file = cfg.state_dir / "refs.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    made = same = fails = 0
    for r in discover(cfg):
        if not has_commits(r.path):
            continue
        bucket = "private" if r.name in cfg.private else r.cls
        dst = cfg.backup_dir / bucket / f"{r.name}.bundle"
        dst.parent.mkdir(parents=True, exist_ok=True)
        h = refs_hash(r.path)
        if state.get(f"{r.cls}/{r.name}") == h and dst.exists():
            same += 1
            continue
        tmp = Path(str(dst) + ".tmp")
        rc, o = git(r.path, "bundle", "create", str(tmp), "--all", timeout=600)
        if rc != 0 or git(r.path, "bundle", "verify", str(tmp))[0] != 0:
            fails += 1
            out(f"BUNDLE FAIL {r.cls}/{r.name}: {o.splitlines()[-1] if o else rc}")
            tmp.unlink(missing_ok=True)
            continue
        if dst.exists():
            os.replace(dst, str(dst) + ".prev")
        os.replace(tmp, dst)
        state[f"{r.cls}/{r.name}"] = h
        made += 1
    state_file.write_text(json.dumps(state, indent=1))
    for d in cfg.backup_dir.iterdir():
        if d.is_dir():
            write_checksums(d)
    copies = 1                                          # the primary backup_dir
    for dest in cfg.destinations:
        if dest.require_mount and not os.path.ismount(dest.require_mount):
            out(f"SKIP {dest.target} (mount {dest.require_mount} absent)")
            continue
        if _copy_to(dest, cfg.backup_dir):
            copies += 1
        else:
            out(f"COPY FAIL {dest.target}")
    out(f"bundles: {made} new, {same} unchanged, {fails} failed; copies landed: {copies}/{1 + len(cfg.destinations)}")
    return fails + (copies < cfg.min_copies)


def cmd_all(cfg: Config, out=print) -> int:
    res = {}
    for name, fn in (("pull", cmd_pull), ("bundle", cmd_bundle)):
        try:
            res[name] = fn(cfg, out=out)
        except Exception as e:                           # one stage failing must not hide the others
            out(f"{name} crashed: {e}")
            res[name] = 1
    cmd_status(cfg, out=out)
    return int(any(res.values()))
