import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from repo_fleet import core


def g(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout


def make_repo(path: Path, files=("a.txt",)):
    path.mkdir(parents=True)
    g(path, "init", "-q", "-b", "main")
    for f in files:
        (path / f).write_text("x\n")
    g(path, "add", "-A")
    g(path, "commit", "-qm", "init")
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.cfg = core.Config(
            roots=[core.Root("work", self.tmp / "work"), core.Root("vendor", self.tmp / "vendor", pull_only=True)],
            backup_dir=self.tmp / "bk", state_dir=self.tmp / "state", min_copies=2)
        self.out = []

    def say(self, s):
        self.out.append(s)


class TestConfig(Base):
    def test_load(self):
        p = self.tmp / "c.toml"
        p.write_text('[[root]]\nclass="a"\npath="/x"\n[backup]\ndir="/b"\nmin_copies=3\nprivate=["s"]\n'
                     '[[backup.destination]]\ntarget="h:p"\n')
        c = core.load_config(p)
        self.assertEqual(c.min_copies, 3)
        self.assertTrue(c.destinations[0].is_remote)
        self.assertIn("s", c.private)

    def test_requires_a_root(self):
        p = self.tmp / "c.toml"
        p.write_text("[backup]\n")
        with self.assertRaises(ValueError):
            core.load_config(p)


class TestDiscoverStatus(Base):
    def test_discover_counts_symlink_once(self):
        make_repo(self.tmp / "work" / "one")
        (self.tmp / "work" / "alias").symlink_to(self.tmp / "work" / "one")
        (self.tmp / "work" / "notrepo").mkdir()
        self.assertEqual(len(core.discover(self.cfg)), 1)

    def test_status_flags_dirty_and_no_remote(self):
        r = make_repo(self.tmp / "work" / "r")
        (r / "new.txt").write_text("y")
        bad = core.cmd_status(self.cfg, out=self.say)
        self.assertEqual(bad, 1)
        self.assertIn("DIRTY(1)", self.out[0])
        self.assertIn("no-remote", self.out[0])


class TestPull(Base):
    def setUp(self):
        super().setUp()
        self.origin = make_repo(self.tmp / "origin")
        g(self.tmp, "clone", "-q", str(self.origin), str(self.tmp / "work" / "c"))
        g(self.tmp / "work" / "c", "config", "user.email", "t@example.com")
        g(self.tmp / "work" / "c", "config", "user.name", "t")

    def advance_origin(self):
        (self.origin / "n.txt").write_text("n")
        g(self.origin, "add", "-A")
        g(self.origin, "commit", "-qm", "more")

    def test_fast_forwards_clean_repo(self):
        self.advance_origin()
        self.assertEqual(core.cmd_pull(self.cfg, out=self.say), 0)
        self.assertTrue((self.tmp / "work" / "c" / "n.txt").exists())

    def test_leaves_dirty_repo_alone(self):
        self.advance_origin()
        (self.tmp / "work" / "c" / "a.txt").write_text("local edit")
        core.cmd_pull(self.cfg, out=self.say)
        self.assertFalse((self.tmp / "work" / "c" / "n.txt").exists())
        self.assertTrue(any("left alone" in o for o in self.out))

    def test_leaves_other_branch_alone(self):
        self.advance_origin()
        g(self.tmp / "work" / "c", "switch", "-q", "-c", "feature")
        core.cmd_pull(self.cfg, out=self.say)
        self.assertFalse((self.tmp / "work" / "c" / "n.txt").exists())

    def test_pull_only_class_gets_push_disabled(self):
        g(self.tmp, "clone", "-q", str(self.origin), str(self.tmp / "vendor" / "v"))
        core.cmd_pull(self.cfg, out=self.say)
        url = g(self.tmp / "vendor" / "v", "config", "--local", "remote.origin.pushurl").strip()
        self.assertEqual(url, core.DISABLED_PUSH)
        pushed = subprocess.run(["git", "-C", str(self.tmp / "vendor" / "v"), "push", "origin", "HEAD"],
                                capture_output=True, text=True)
        self.assertNotEqual(pushed.returncode, 0)

    def test_dead_secondary_remote_does_not_fail_run(self):
        g(self.tmp / "work" / "c", "remote", "add", "old", str(self.tmp / "gone"))
        self.assertEqual(core.cmd_pull(self.cfg, out=self.say), 0)
        self.assertTrue(any("secondary remote 'old'" in o for o in self.out))


class TestBundle(Base):
    def setUp(self):
        super().setUp()
        make_repo(self.tmp / "work" / "a")
        make_repo(self.tmp / "work" / "secret")
        self.cfg.private = {"secret"}

    def test_creates_verified_bundles_and_checksums(self):
        self.cfg.min_copies = 1
        self.assertEqual(core.cmd_bundle(self.cfg, out=self.say), 0)
        self.assertTrue((self.cfg.backup_dir / "work" / "a.bundle").exists())
        self.assertTrue((self.cfg.backup_dir / "private" / "secret.bundle").exists())
        self.assertEqual(core.verify_checksums(self.cfg.backup_dir / "work"), [])

    def test_restore_from_bundle(self):
        self.cfg.min_copies = 1
        core.cmd_bundle(self.cfg, out=self.say)
        dest = self.tmp / "restored"
        g(self.tmp, "clone", "-q", str(self.cfg.backup_dir / "work" / "a.bundle"), str(dest))
        self.assertTrue((dest / "a.txt").exists())

    def test_unchanged_repo_not_rebundled_changed_repo_keeps_prev(self):
        self.cfg.min_copies = 1
        core.cmd_bundle(self.cfg, out=self.say)
        self.out.clear()
        core.cmd_bundle(self.cfg, out=self.say)
        self.assertIn("0 new", self.out[-1])
        (self.tmp / "work" / "a" / "z.txt").write_text("z")
        g(self.tmp / "work" / "a", "add", "-A")
        g(self.tmp / "work" / "a", "commit", "-qm", "z")
        core.cmd_bundle(self.cfg, out=self.say)
        self.assertTrue((self.cfg.backup_dir / "work" / "a.bundle.prev").exists())

    def test_corruption_is_detected(self):
        self.cfg.min_copies = 1
        core.cmd_bundle(self.cfg, out=self.say)
        with open(self.cfg.backup_dir / "work" / "a.bundle", "ab") as f:
            f.write(b"junk")
        self.assertEqual(core.verify_checksums(self.cfg.backup_dir / "work"), ["a.bundle"])

    def test_private_repo_excluded_from_shared_destination(self):
        shared = self.tmp / "shared"
        self.cfg.destinations = [core.Destination(str(shared), skip_classes=["private"])]
        self.assertEqual(core.cmd_bundle(self.cfg, out=self.say), 0)
        self.assertTrue((shared / "work" / "a.bundle").exists())
        self.assertFalse((shared / "private").exists())
        self.assertFalse(list(shared.rglob("*.prev")))

    def test_fails_when_too_few_copies_land(self):
        self.cfg.destinations = [core.Destination(str(self.tmp / "nope"), require_mount=str(self.tmp / "not-a-mount"))]
        self.assertEqual(core.cmd_bundle(self.cfg, out=self.say), 1)
        self.assertTrue(any(o.startswith("SKIP") for o in self.out))


if __name__ == "__main__":
    unittest.main()
