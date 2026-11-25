# repo-fleet

A small, dependency-free CLI that keeps a **fleet of git repositories** healthy: one command shows which
repos are dirty, ahead, behind or have no remote; another updates them safely; a third makes **verified,
checksummed backups** that are copied to several places.

It exists because "I pushed it to GitHub" is not a backup strategy: unpushed branches, repos with no
remote and repos whose upstream disappeared are exactly the ones you lose.

```
$ repo-fleet status
work       billing-service                  main                 ok
work       experiments                      feature/x            DIRTY(2) ahead 1
oss        parser                           main                 behind 3
vendor     partner-sdk                      main                 ok
== 4 repos, 2 need attention

$ repo-fleet all
updated   oss/parser +3
bundles: 1 new, 3 unchanged, 0 failed; copies landed: 3/3
```

## Where this came from

I consolidated about 110 repositories (personal, an archive, work repos that must never be pushed elsewhere, and
local-only tooling) into one backed-up system, and this is the tool that did it. The inventory found projects
that had never been pushed, repos whose upstream was gone, and a backup drive that dropped off the bus and did not
remount. Each of those shaped a rule below. The full story is in [docs/FIELD-NOTES.md](docs/FIELD-NOTES.md).

## Design decisions

| Rule | Why | Test |
|---|---|---|
| Pull is **fast-forward only**, on a clean tree, on the default branch. It never merges, rebases, pushes or switches branch. | A sync tool that can surprise you gets switched off. | `TestPull.test_leaves_*` |
| A `pull_only` class has its **push URL disabled** on every run. | Code you must not publish can't be published by accident, even by another tool in the same checkout. | `test_pull_only_class_gets_push_disabled` |
| Dead *secondary* remotes are noted, not fatal. | One abandoned fork shouldn't fail the whole nightly run. | `test_dead_secondary_remote_does_not_fail_run` |
| Backups are `git bundle`s, accepted only if `git bundle verify` passes; the last good one is kept as `.prev`. | A bundle is a single file that restores with a plain `git clone`. No special tooling at 3 a.m. | `test_restore_from_bundle` |
| Bundles are rebuilt only when `for-each-ref` changes (hash kept in state). | Fast daily runs; no churn on the backup disk. | `test_unchanged_repo_not_rebundled...` |
| `SHA256SUMS` per class directory, plus `repo-fleet verify`. | Detects bit-rot on a cold disk. | `test_corruption_is_detected` |
| Destinations can be a directory or `host:path` over ssh (rsync, key auth only). A destination can require a mount point and is **skipped, not failed**, while the disk is absent. | Removable disks come and go. | `test_fails_when_too_few_copies_land` |
| `min_copies` makes "backup succeeded" mean something: the exit code is non-zero if fewer copies landed. | A silent single copy is the failure mode this tool exists to prevent. | same |
| `private` repos are excluded from shared destinations. | Some repos must stay on one machine. | `test_private_repo_excluded_from_shared_destination` |

## Install and use

```sh
python3 -m pip install .          # Python >= 3.11, no third-party packages; needs git (+ rsync for remote copies)
mkdir -p ~/.config/repo-fleet && cp examples/config.toml ~/.config/repo-fleet/
repo-fleet status --fetch
repo-fleet all                     # pull + bundle + status; run from cron or a systemd timer
repo-fleet verify
```

Restore any repo from its bundle:

```sh
git clone ~/backups/repo-fleet/work/billing-service.bundle billing-service
```

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests     # 16 tests, creates throwaway repos in a temp dir
```

See [`examples/config.toml`](examples/config.toml) for every option.

## Layout

```
src/repo_fleet/core.py   config, discovery, status, pull, bundle, checksums (library)
src/repo_fleet/cli.py    argparse front end
tests/test_core.py       behaviour tests against real temporary git repositories
```
