# workspace_sync

On-demand workspace sync for two Ubuntu laptops on the same network.

## What it does

`workspace_sync.py` treats Git repos and non-Git files differently:

- **Git repos**: records repo path, origin URL, branch, commit, and dirty state.
- **Non-Git files**: mirrors only the files from configured folders that are outside Git repos.

This avoids raw syncing of `.git` working trees.

## Requirements

Install these on both laptops:

```bash
sudo apt update
sudo apt install -y git rsync openssh-client
```

You also need Python 3.

## Files

- `workspace_sync.py`
- `workspace_sync_config.example.json`

## Setup

Copy the script somewhere in your PATH on both laptops, for example:

```bash
mkdir -p ~/.local/bin ~/.config/workspace_sync
cp workspace_sync.py ~/.local/bin/
chmod +x ~/.local/bin/workspace_sync.py
cp workspace_sync_config.example.json ~/.config/workspace_sync/config.json
```

Edit the config on **both** laptops.

Important:
- `roots` should contain the folders you want scanned recursively.
- `extra_paths` can contain individual files or directories.
- `target_home` should usually stay as `~/`.
- `snapshot_dir` is where the source creates the snapshot and where the target stores the pulled copy.

## SSH setup

The target laptop pulls from the source over SSH. Set up key-based SSH from target to source.

On target:

```bash
ssh-keygen -t ed25519
ssh-copy-id youruser@source-laptop
```

Test it:

```bash
ssh youruser@source-laptop hostname
```

## Usage

### 1. On the source laptop

Create the snapshot:

```bash
workspace_sync.py source
```

This writes:
- `manifest.json`
- `non_git_files.txt`
- `non_git_home/`

under `snapshot_dir`.

By default the script refuses to export if any tracked repo is dirty and `require_clean_git` is `true`.

### 2. On the target laptop

Pull and apply from the source laptop:

```bash
workspace_sync.py target --from youruser@source-laptop
```

This will:
- pull the snapshot over `rsync+ssh`
- mirror non-Git files into your home
- clone missing repos
- fetch/prune existing repos
- switch to the recorded branch
- fast-forward to the recorded commit when possible

## Safer default behavior

If a target repo has local changes, it is skipped.
If the target repo cannot be fast-forwarded cleanly to the recorded commit, it is skipped.

## Force mode

If you want the target to exactly match the source repo commit:

```bash
workspace_sync.py target --from youruser@source-laptop --force-hard-reset
```

That can discard local changes on the target.

## Local-only mode

If you already copied the snapshot manually and only want to apply it:

```bash
workspace_sync.py target --local-only
```

## Recommended workflow

1. Commit or stash work on the source laptop.
2. Run `workspace_sync.py source`.
3. On the other laptop, run `workspace_sync.py target --from user@host`.

## Notes

- Non-Git files are mirrored from source to target with deletion propagation.
- Git repos are not file-mirrored; they are reproduced through Git operations.
- Avoid putting huge datasets, VM images, caches, or secrets in the synced roots unless you really want them mirrored.
- The script expects all managed paths to live under your home directory.
