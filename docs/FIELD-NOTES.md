# Field notes: how repo-fleet came about

These are real notes from consolidating my own source code into one managed, backed-up system. Names of
employers, hosts and customers are left out; the problems and the decisions are real.

## The starting point

My code had spread across four kinds of place:

| Kind | Example | Backup situation before |
|---|---|---|
| Active personal projects | a dozen working repos in one projects folder | pushed to GitHub, mostly |
| Personal archive | ~65 older repos | "it's on GitHub" |
| Work repositories | ~30 clones of an employer's repos | **must never be pushed anywhere else** |
| Local-only code | helper scripts, workstation config, tools with no remote at all | nothing |

I asked myself a simple question: *if this disk died tonight, what would I lose?* The honest answer was
"I don't know", which is the problem. The goal became **one command that tells me, and one that fixes it.**

## What the first inventory found

111 repositories in total. Of those:

* **Some had never been pushed.** A handful of projects, plus all the local-only tooling, existed in exactly one place.
* **Some upstreams were no longer cloneable.** Several work repos could not be re-cloned from their remote; the only
  copies were older local bundles and clones. They became *bundle-only* members of the fleet, which is why
  `repo-fleet` backs up from local state and never assumes a remote is the source of truth.
* **One repo had a dead secondary remote.** An old fork remote that no longer existed made `git fetch --all`
  fail, which failed the whole nightly run. Rule added: only `origin` is required; other remotes are best-effort
  and reported as a note.
* **Five repos were empty.** There is nothing to bundle in a repo with no commits, so those are skipped
  explicitly rather than failing `git bundle create`.

## Problems that shaped the design

**A false alarm that looked like corruption.** After the first backup I verified 27 bundles and the tool
reported every one of them BAD. They were fine: `git bundle verify` has to run *inside a repository* to check
prerequisites, and I had run it from a plain directory. Lesson: a verifier that can fail for environmental reasons
needs to be tested against known-good input. The bundle step now verifies in the source repo, and
`test_restore_from_bundle` proves a bundle actually clones.

**Status noise.** Nine repositories were *intentionally* held as bundles only, and the status command flagged
all nine as "needs attention" every day, which trains you to ignore the output. Hence `local_only` roots: a
property of the *class*, not a per-repo exception.

**The removable disk.** One backup destination was an external drive in a USB dock. It dropped off the bus once
during a write (the filesystem logged a journal error), came back under a new device name, and did **not**
remount. The share directory looked empty, and a naive `rsync --delete` into an unmounted mount point would have
written to the *root filesystem* underneath it. That is why a destination can declare `require_mount`: the copy
is **skipped, not attempted**, and the shortfall shows up in `min_copies` instead of as a silent success.

**What "backed up" should mean.** The run now ends with a line like
`copies landed: 3/3` and exits non-zero if fewer than `min_copies` destinations received the data. Before that,
a run where two of three copies failed still printed "done".

## Safety rules for code that must not leak

Work repositories live in a `pull_only` class. Every run re-applies a disabled push URL, because a rule you
set once is a rule that silently disappears when someone re-clones. A repo that must stay on one machine is
listed under `private` and is excluded from every shared destination.

## Result

One daily timer runs `repo-fleet all`: fast-forward what is safe to update, bundle what changed, copy to three
places, verify checksums, and report anything that needs a human. I restore-tested a sample of the bundles with a
plain `git clone`, which is the whole point of choosing bundles over a bespoke archive format.
