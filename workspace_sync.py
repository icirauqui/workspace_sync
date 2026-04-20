#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APP_NAME = "workspace_sync"
DEFAULT_SNAPSHOT_DIRNAME = ".workspace_sync/latest"
MANIFEST_NAME = "manifest.json"
FILELIST_NAME = "non_git_files.txt"
RSYNC_BIN = shutil.which("rsync") or "rsync"
GIT_BIN = shutil.which("git") or "git"
SSH_BIN = shutil.which("ssh") or "ssh"


class SyncError(RuntimeError):
    pass


@dataclass
class RepoInfo:
    path: str
    remote_url: str | None
    branch: str | None
    commit: str | None
    dirty: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "remote_url": self.remote_url,
            "branch": self.branch,
            "commit": self.commit,
            "dirty": self.dirty,
        }


@dataclass
class ExtraPathInfo:
    path: str
    type: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "type": self.type}


def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    if "roots" not in config or not isinstance(config["roots"], list) or not config["roots"]:
        raise SyncError("Config must include a non-empty 'roots' list.")

    config.setdefault("extra_paths", [])
    config.setdefault("exclude_names", [])
    config.setdefault("exclude_path_contains", [])
    config.setdefault("require_clean_git", True)
    config.setdefault("snapshot_dir", str(Path.home() / DEFAULT_SNAPSHOT_DIRNAME))
    config.setdefault("target_home", str(Path.home()))
    return config


def announce(message: str) -> None:
    print(message, flush=True)


def progress(message: str, index: int | None = None, total: int | None = None) -> None:
    if index is not None and total is not None:
        width = len(str(total))
        announce(f"[{index:>{width}}/{total}] {message}")
        return
    announce(f"[*] {message}")


def run(cmd: list[str], *, cwd: Path | None = None, capture: bool = True, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=capture,
        check=False,
    )
    if check and proc.returncode != 0:
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        raise SyncError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n{stderr}"
        )
    return proc


def summarize_sync_error(exc: SyncError) -> str:
    message = str(exc).strip()
    stdout_marker = "\nstdout:\n"
    stderr_marker = "\nstderr:\n"

    stdout_text = ""
    if stdout_marker in message:
        stdout_text = message.split(stdout_marker, 1)[1]
        if stderr_marker in stdout_text:
            stdout_text = stdout_text.split(stderr_marker, 1)[0]

    stderr_text = ""
    if stderr_marker in message:
        stderr_text = message.split(stderr_marker, 1)[1]

    for text in (stderr_text, stdout_text):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return lines[-1]

    return message.splitlines()[0]


def norm_abs(path_str: str) -> Path:
    return Path(path_str).expanduser().absolute()


def rel_to_home(path: Path, home: Path) -> str:
    try:
        return str(path.relative_to(home))
    except ValueError as exc:
        raise SyncError(f"Path must live under home directory: {path}") from exc


def git_toplevel(path: Path) -> Path | None:
    proc = subprocess.run(
        [GIT_BIN, "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()


def git_info(repo_path: Path, home: Path) -> RepoInfo:
    remote_proc = subprocess.run(
        [GIT_BIN, "-C", str(repo_path), "remote", "get-url", "origin"],
        text=True,
        capture_output=True,
        check=False,
    )
    branch_proc = subprocess.run(
        [GIT_BIN, "-C", str(repo_path), "branch", "--show-current"],
        text=True,
        capture_output=True,
        check=False,
    )
    commit_proc = subprocess.run(
        [GIT_BIN, "-C", str(repo_path), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    status_proc = subprocess.run(
        [GIT_BIN, "-C", str(repo_path), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
    )

    return RepoInfo(
        path=rel_to_home(repo_path, home),
        remote_url=remote_proc.stdout.strip() or None,
        branch=branch_proc.stdout.strip() or None,
        commit=commit_proc.stdout.strip() or None,
        dirty=bool(status_proc.stdout.strip()),
    )


def should_exclude(path: Path, relative: str, exclude_names: set[str], exclude_path_contains: list[str]) -> bool:
    parts = set(path.parts)
    if parts & exclude_names:
        return True
    return any(token in relative for token in exclude_path_contains)


def scan_workspace(config: dict[str, Any]) -> tuple[list[RepoInfo], list[str], list[ExtraPathInfo]]:
    home = Path.home().absolute()
    roots = [norm_abs(p) for p in config["roots"]]
    extra_paths = [norm_abs(p) for p in config.get("extra_paths", [])]
    exclude_names = set(config.get("exclude_names", [])) | {".git"}
    exclude_path_contains = list(config.get("exclude_path_contains", []))
    total_paths = len(roots) + len(extra_paths)

    repos: dict[Path, RepoInfo] = {}
    non_git_files: set[str] = set()
    extra_entries: list[ExtraPathInfo] = []

    for index, root in enumerate(roots, start=1):
        progress(f"Scanning root: {root}", index, total_paths)
        if not root.exists():
            raise SyncError(f"Configured root does not exist: {root}")
        if not root.is_dir():
            raise SyncError(f"Configured root is not a directory: {root}")

        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            rel_current = rel_to_home(current, home)
            dirnames[:] = [d for d in dirnames if d not in exclude_names]
            if should_exclude(current, rel_current, exclude_names, exclude_path_contains):
                dirnames[:] = []
                continue

            top = git_toplevel(current)
            if top is not None and top == current:
                repos.setdefault(top, git_info(top, home))
                dirnames[:] = []
                continue

            for filename in filenames:
                file_path = current / filename
                rel_file = rel_to_home(file_path, home)
                if should_exclude(file_path, rel_file, exclude_names, exclude_path_contains):
                    continue
                if git_toplevel(file_path.parent) is None:
                    non_git_files.add(rel_file)

    for index, extra in enumerate(extra_paths, start=len(roots) + 1):
        progress(f"Scanning extra path: {extra}", index, total_paths)
        if not extra.exists():
            raise SyncError(f"Configured extra path does not exist: {extra}")
        extra_entries.append(ExtraPathInfo(path=rel_to_home(extra, home), type="dir" if extra.is_dir() else "file"))
        if extra.is_file():
            rel_file = rel_to_home(extra, home)
            if not should_exclude(extra, rel_file, exclude_names, exclude_path_contains):
                non_git_files.add(rel_file)
        else:
            for dirpath, dirnames, filenames in os.walk(extra):
                current = Path(dirpath)
                rel_current = rel_to_home(current, home)
                dirnames[:] = [d for d in dirnames if d not in exclude_names]
                if should_exclude(current, rel_current, exclude_names, exclude_path_contains):
                    dirnames[:] = []
                    continue

                if git_toplevel(current) is not None:
                    dirnames[:] = []
                    continue

                for filename in filenames:
                    file_path = current / filename
                    rel_file = rel_to_home(file_path, home)
                    if should_exclude(file_path, rel_file, exclude_names, exclude_path_contains):
                        continue
                    non_git_files.add(rel_file)

    repo_list = sorted(repos.values(), key=lambda r: r.path)
    file_list = sorted(non_git_files)
    progress(f"Scan complete: {len(repo_list)} repos, {len(file_list)} non-git files.")
    return repo_list, file_list, extra_entries


def write_snapshot(config: dict[str, Any], repos: list[RepoInfo], non_git_files: list[str], extra_entries: list[ExtraPathInfo]) -> Path:
    snapshot_dir = norm_abs(config["snapshot_dir"])
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    if config.get("require_clean_git", True):
        dirty = [repo.path for repo in repos if repo.dirty]
        if dirty:
            joined = "\n - ".join([""] + dirty)
            raise SyncError(
                "Refusing to export because these repositories have uncommitted changes:" + joined
            )

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "home": str(Path.home()),
        "repos": [repo.to_dict() for repo in repos],
        "extra_paths": [entry.to_dict() for entry in extra_entries],
        "non_git_files": non_git_files,
    }

    manifest_path = snapshot_dir / MANIFEST_NAME
    filelist_path = snapshot_dir / FILELIST_NAME

    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")

    with filelist_path.open("w", encoding="utf-8") as f:
        for rel_path in non_git_files:
            f.write(rel_path + "\n")

    return snapshot_dir


def rsync_push_non_git(snapshot_dir: Path, non_git_files: list[str]) -> None:
    source_home = Path.home().resolve()
    data_dir = snapshot_dir / "non_git_home"
    data_dir.mkdir(parents=True, exist_ok=True)

    if not non_git_files:
        progress("No non-git files to copy into the snapshot.")
        return

    progress(f"Copying {len(non_git_files)} non-git files into the snapshot.")
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tf:
        for rel_path in non_git_files:
            tf.write(rel_path + "\n")
        temp_name = tf.name

    try:
        run(
            [
                RSYNC_BIN,
                "-a",
                "--delete",
                "--human-readable",
                "--info=progress2",
                f"--files-from={temp_name}",
                f"{source_home}/",
                f"{data_dir}/",
            ],
            capture=False,
        )
    finally:
        os.unlink(temp_name)


def prepare_source(config_path: Path) -> None:
    progress(f"Loading config: {config_path}")
    config = load_config(config_path)
    repos, non_git_files, extra_entries = scan_workspace(config)
    progress(f"Writing snapshot manifest in {norm_abs(config['snapshot_dir'])}")
    snapshot_dir = write_snapshot(config, repos, non_git_files, extra_entries)
    rsync_push_non_git(snapshot_dir, non_git_files)
    announce(f"Prepared snapshot in: {snapshot_dir}")
    announce(f"Repos: {len(repos)}")
    announce(f"Non-git files: {len(non_git_files)}")


def pull_snapshot(source: str, remote_snapshot_dir: str, local_snapshot_dir: Path) -> None:
    local_snapshot_dir.mkdir(parents=True, exist_ok=True)
    progress(f"Pulling snapshot from {source}:{remote_snapshot_dir}")
    run([
        RSYNC_BIN,
        "-az",
        "--delete",
        "--human-readable",
        "--info=progress2",
        "-e",
        SSH_BIN,
        f"{source}:{remote_snapshot_dir.rstrip('/')}/",
        f"{local_snapshot_dir}/",
    ], capture=False)


def load_manifest(snapshot_dir: Path) -> dict[str, Any]:
    manifest_path = snapshot_dir / MANIFEST_NAME
    if not manifest_path.exists():
        raise SyncError(f"Manifest not found: {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def sync_non_git_to_target(snapshot_dir: Path, manifest: dict[str, Any], target_home: Path) -> None:
    src_data = snapshot_dir / "non_git_home"
    if not src_data.exists():
        progress("Snapshot does not include non-git file data; skipping file mirror.")
        return

    non_git_files = manifest.get("non_git_files", [])
    if not non_git_files:
        progress("No non-git files to mirror onto the target.")
        return

    progress(f"Mirroring {len(non_git_files)} non-git files into {target_home}")
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tf:
        for rel_path in non_git_files:
            tf.write(rel_path + "\n")
        temp_name = tf.name

    try:
        run([
            RSYNC_BIN,
            "-a",
            "--delete",
            "--human-readable",
            "--info=progress2",
            f"--files-from={temp_name}",
            f"{src_data}/",
            f"{target_home}/",
        ], capture=False)
    finally:
        os.unlink(temp_name)


def current_branch(repo_path: Path) -> str | None:
    proc = subprocess.run(
        [GIT_BIN, "-C", str(repo_path), "branch", "--show-current"],
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.stdout.strip() or None


def repo_dirty(repo_path: Path) -> bool:
    proc = subprocess.run(
        [GIT_BIN, "-C", str(repo_path), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
    )
    return bool(proc.stdout.strip())


def apply_repo(repo: dict[str, Any], target_home: Path, force_hard_reset: bool) -> tuple[str, str]:
    repo_path = target_home / repo["path"]
    remote_url = repo.get("remote_url")
    desired_branch = repo.get("branch")
    desired_commit = repo.get("commit")

    if not repo_path.exists():
        if not remote_url:
            return repo["path"], "missing remote_url; cannot clone"
        ensure_parent(repo_path)
        run([GIT_BIN, "clone", "--progress", remote_url, str(repo_path)], capture=False)

    if not (repo_path / ".git").exists():
        return repo["path"], "exists but is not a git repo"

    if repo_dirty(repo_path) and not force_hard_reset:
        return repo["path"], "target repo has local changes; skipped"

    run([GIT_BIN, "-C", str(repo_path), "fetch", "--all", "--prune", "--progress"], capture=False)

    if desired_branch:
        local_branch = current_branch(repo_path)
        if local_branch != desired_branch:
            branch_check = subprocess.run(
                [GIT_BIN, "-C", str(repo_path), "show-ref", "--verify", f"refs/heads/{desired_branch}"],
                text=True,
                capture_output=True,
                check=False,
            )
            if branch_check.returncode == 0:
                run([GIT_BIN, "-C", str(repo_path), "checkout", desired_branch])
            else:
                run([GIT_BIN, "-C", str(repo_path), "checkout", "-B", desired_branch, f"origin/{desired_branch}"])

    if desired_commit:
        if force_hard_reset:
            run([GIT_BIN, "-C", str(repo_path), "reset", "--hard", desired_commit])
        else:
            try:
                run([GIT_BIN, "-C", str(repo_path), "merge", "--ff-only", desired_commit])
            except SyncError:
                return repo["path"], f"could not fast-forward to {desired_commit}; rerun with --force-hard-reset if desired"

    return repo["path"], "ok"


def apply_target(config_path: Path, source: str, remote_snapshot_dir: str | None, local_only: bool, force_hard_reset: bool) -> None:
    progress(f"Loading config: {config_path}")
    config = load_config(config_path)
    local_snapshot_dir = norm_abs(config["snapshot_dir"])

    if not local_only:
        if not remote_snapshot_dir:
            remote_snapshot_dir = str(Path.home() / DEFAULT_SNAPSHOT_DIRNAME)
        pull_snapshot(source, remote_snapshot_dir, local_snapshot_dir)
    else:
        progress(f"Using local snapshot from {local_snapshot_dir}")

    manifest = load_manifest(local_snapshot_dir)
    target_home = norm_abs(config.get("target_home", str(Path.home())))

    sync_non_git_to_target(local_snapshot_dir, manifest, target_home)

    repos = manifest.get("repos", [])
    results: list[tuple[str, str]] = []
    if repos:
        progress(f"Applying {len(repos)} repositories into {target_home}")
    else:
        progress("No repositories found in the snapshot.")

    for index, repo in enumerate(repos, start=1):
        progress(f"Syncing repo: {repo['path']}", index, len(repos))
        try:
            results.append(apply_repo(repo, target_home, force_hard_reset))
        except SyncError as exc:
            summary = summarize_sync_error(exc)
            announce(f" ! Skipping {repo['path']}: {summary}")
            results.append((repo["path"], f"error; skipped: {summary}"))

    announce(f"Applied snapshot from: {local_snapshot_dir}")
    announce("Repository results:")
    for path, status in results:
        announce(f" - {path}: {status}")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="On-demand workspace sync between two Ubuntu machines.")
    parser.add_argument(
        "--config",
        default=str(Path.home() / ".config/workspace_sync/config.json"),
        help="Path to JSON config file.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    prep = sub.add_parser("source", help="Prepare a snapshot on the source machine.")
    prep.set_defaults(func=cmd_source)

    target = sub.add_parser("target", help="Pull and apply a snapshot on the target machine.")
    target.add_argument("--from", dest="source_host", required=False, help="SSH source like user@host.")
    target.add_argument("--remote-snapshot-dir", default=None, help="Remote snapshot dir on source machine.")
    target.add_argument("--local-only", action="store_true", help="Apply a snapshot already present locally.")
    target.add_argument("--force-hard-reset", action="store_true", help="Hard reset target repos to manifest commit.")
    target.set_defaults(func=cmd_target)

    return parser


def cmd_source(args: argparse.Namespace) -> None:
    prepare_source(Path(args.config).expanduser().resolve())


def cmd_target(args: argparse.Namespace) -> None:
    if not args.local_only and not args.source_host:
        raise SyncError("target mode requires --from user@host unless --local-only is used.")
    apply_target(
        config_path=Path(args.config).expanduser().resolve(),
        source=args.source_host or "",
        remote_snapshot_dir=args.remote_snapshot_dir,
        local_only=args.local_only,
        force_hard_reset=args.force_hard_reset,
    )


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
