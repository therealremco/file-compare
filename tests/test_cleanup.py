import contextlib
import errno
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cleanup import MarkedFile, check_file, cleanup, load_manifest, main
from file_compare import Options, SSH_LOG


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.options = Options("server", "/remote", self.root)
        self.item = MarkedFile("/remote/a.png", "sftp://server/remote/a.png", 30, 100)
        self.manifest = self.root / "manifest.json"
        self.data = {
            "version": 1, "action": "marked_for_deletion_only",
            "requires_remote_revalidation": True, "count": 1, "total_bytes": 30,
            "files": [dict(path=self.item.path, url=self.item.url, size=30, mtime=100)],
        }
        self.save()
        self.sftp = Mock()
        self.sftp.normalize.side_effect = lambda path: path
        self.sftp.lstat.side_effect = lambda path: Mock(
            st_mode=stat.S_IFREG if path.endswith(".png") else stat.S_IFDIR,
            st_size=30, st_mtime=100,
        )

    def save(self):
        self.manifest.write_text(json.dumps(self.data), encoding="utf-8")

    def cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main([
                "--host", "server", "--remote-path", "/remote",
                "--local-path", str(self.root), "--manifest", str(self.manifest), *args,
            ])

    def test_exported_manifest_is_accepted(self):
        from review import Store

        store = Store(self.root / "review")
        store.import_items([dict(
            id="file", path=self.item.path, url=self.item.url, size=30, mtime=100,
            category="name_absent", kind="image",
        )])
        store.decide("file", "delete")
        self.manifest.write_text(json.dumps(store.manifest()))
        self.assertEqual(load_manifest(self.manifest, self.options), [self.item])

    def test_cleanup_debug_output_default_and_overrides(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({
            "host": "server", "remote_path": "/remote", "local_path": str(self.root),
            "debug_ssh": True,
        }))

        def logged_cleanup(*args):
            SSH_LOG.debug("debug handshake detail")
            SSH_LOG.info("connection progress")
            SSH_LOG.warning("connection warning")
            SSH_LOG.error("connection error")
            return 0

        for index, (flags, debug) in enumerate([
            ([], False), (["--debug-ssh"], True),
            (["--config", str(config)], True),
            (["--config", str(config), "--no-debug-ssh"], False),
        ]):
            stderr = io.StringIO()
            previous_level = SSH_LOG.level
            previous_handlers = SSH_LOG.handlers.copy()
            with self.subTest(flags=flags), patch("cleanup.cleanup", side_effect=logged_cleanup):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                    code = main([
                        "--host", "server", "--remote-path", "/remote",
                        "--local-path", str(self.root), "--manifest", str(self.manifest),
                        "--audit", str(self.root / f"audit-{index}.jsonl"), *flags,
                    ])
                self.assertEqual(code, 0)
                self.assertEqual("DEBUG SSH: debug handshake detail" in stderr.getvalue(), debug)
                for message in ("connection progress", "connection warning", "connection error"):
                    self.assertIn(message, stderr.getvalue())
                self.assertEqual(SSH_LOG.level, previous_level)
                self.assertEqual(SSH_LOG.handlers, previous_handlers)

    def test_invalid_manifests_are_rejected_before_connection(self):
        original = json.loads(json.dumps(self.data))
        invalid = [
            {"count": 2}, {"total_bytes": 31}, {"version": True}, {"version": 2},
            {"files": "bad"}, {"action": "delete"}, {"requires_remote_revalidation": False},
            {"files": [original["files"][0], original["files"][0]], "count": 2, "total_bytes": 60},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.data = {**original, **changes}
                self.save()
                with patch("cleanup.open_remote") as connection:
                    self.assertEqual(self.cli(), 1)
                    connection.assert_not_called()
        for changes in [
            {"path": "/remote/../a.png"}, {"path": "//remote/a.png"},
            {"path": "relative.png"}, {"path": "/"}, {"size": True},
            {"size": -1}, {"mtime": None}, {"mtime": 1.5},
            {"url": "sftp://other/remote/a.png"}, {"url": "sftp://server:2222/remote/a.png"},
            {"url": "sftp://server/remote/b.png"},
        ]:
            with self.subTest(changes=changes):
                self.data = {**original, "files": [{**original["files"][0], **changes}]}
                self.save()
                with self.assertRaises(ValueError):
                    load_manifest(self.manifest, self.options)

    def test_confirmation_required_before_connecting(self):
        for args in [("--delete",), ("--delete", "--confirm-count", "2"),
                     ("--confirm-count", "1")]:
            with self.subTest(args=args), patch("cleanup.open_remote") as connection:
                self.assertEqual(self.cli(*args), 1)
                connection.assert_not_called()

    def test_dry_run_never_removes_and_writes_audit(self):
        audit_path = self.root / "audit.jsonl"
        with patch("cleanup.open_remote") as connection:
            connection.return_value.__enter__.return_value = self.sftp
            self.assertEqual(self.cli("--audit", str(audit_path)), 0)
        self.sftp.remove.assert_not_called()
        events = [json.loads(line) for line in audit_path.read_text().splitlines()]
        self.assertEqual([entry["action"] for entry in events], ["start", "would_delete", "complete"])
        self.assertEqual(events[0]["mode"], "DRY RUN")
        self.assertEqual(audit_path.stat().st_mode & 0o777, 0o600)

    def test_confirmed_delete_and_audit(self):
        audit_path = self.root / "audit.jsonl"

        def remove(path):
            events = [json.loads(line) for line in audit_path.read_text().splitlines()]
            self.assertEqual(events[-1]["action"], "delete_requested")
            self.assertEqual(path, self.item.path)

        self.sftp.remove.side_effect = remove
        with patch("cleanup.open_remote") as connection:
            connection.return_value.__enter__.return_value = self.sftp
            self.assertEqual(self.cli("--delete", "--confirm-count", "1", "--audit", str(audit_path)), 0)
        self.sftp.remove.assert_called_once_with(self.item.path)
        events = [json.loads(line) for line in audit_path.read_text().splitlines()]
        self.assertEqual(events[-2]["action"], "deleted")
        self.assertEqual(events[-1]["deleted"], 1)

    def test_changed_missing_outside_and_symlink_files_are_skipped(self):
        with self.assertRaises(PermissionError):
            self.sftp.lstat.side_effect = PermissionError(errno.EACCES, "denied")
            check_file(self.sftp, "/remote", self.item)
        self.sftp.lstat.side_effect = FileNotFoundError(errno.ENOENT, "absent")
        self.assertIn("no longer exists", check_file(self.sftp, "/remote", self.item))
        self.assertIn("outside", check_file(self.sftp, "/other", self.item))
        for mode, size, mtime in [
            (stat.S_IFLNK, 30, 100), (stat.S_IFDIR, 30, 100),
            (stat.S_IFREG, 31, 100), (stat.S_IFREG, 30, 101),
        ]:
            with self.subTest(mode=mode, size=size, mtime=mtime):
                self.sftp.lstat.side_effect = lambda path: Mock(
                    st_mode=mode if path.endswith(".png") else stat.S_IFDIR,
                    st_size=size, st_mtime=mtime,
                )
                with (self.root / "audit.jsonl").open("w") as audit, patch("cleanup.open_remote") as connection:
                    connection.return_value.__enter__.return_value = self.sftp
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(cleanup(self.options, [self.item], True, audit), 1)
        self.sftp.remove.assert_not_called()
        self.sftp.lstat.side_effect = lambda path: Mock(
            st_mode=stat.S_IFLNK if path == "/remote" else stat.S_IFREG,
        )
        self.assertIn("ancestor", check_file(self.sftp, "/remote", self.item))

    def test_existing_audit_is_never_overwritten(self):
        audit = self.root / "audit.jsonl"
        audit.write_text("existing")
        with patch("cleanup.open_remote") as connection:
            self.assertEqual(self.cli("--audit", str(audit)), 1)
            connection.assert_not_called()
        self.assertEqual(audit.read_text(), "existing")

    def test_audit_failure_prevents_removal(self):
        audit = self.root / "audit.jsonl"
        with patch("cleanup.open_remote") as connection, patch(
            "cleanup.os.fsync", side_effect=OSError("disk full"),
        ):
            connection.return_value.__enter__.return_value = self.sftp
            self.assertEqual(self.cli("--delete", "--confirm-count", "1", "--audit", str(audit)), 1)
            connection.assert_not_called()
        self.sftp.remove.assert_not_called()

    def test_canonical_path_change_is_skipped(self):
        self.sftp.normalize.side_effect = None
        self.sftp.normalize.return_value = "/other/a.png"
        self.assertIn("canonical", check_file(self.sftp, "/remote", self.item))
        self.sftp.remove.assert_not_called()

    def test_removal_error_stops_before_next_file_and_records_uncertainty(self):
        self.data["files"].append(dict(
            path="/remote/b.png", url="sftp://server/remote/b.png", size=30, mtime=100,
        ))
        self.data.update(count=2, total_bytes=60)
        self.save()
        audit = self.root / "audit.jsonl"
        self.sftp.remove.side_effect = OSError("connection lost")
        with patch("cleanup.open_remote") as connection:
            connection.return_value.__enter__.return_value = self.sftp
            self.assertEqual(self.cli("--delete", "--confirm-count", "2", "--audit", str(audit)), 1)
        self.sftp.remove.assert_called_once_with("/remote/a.png")
        events = [json.loads(line) for line in audit.read_text().splitlines()]
        self.assertEqual(events[-2]["action"], "delete_failed_or_uncertain")
        self.assertEqual(events[-1]["action"], "aborted")

    def test_empty_manifest_no_connection(self):
        self.data.update(files=[], count=0, total_bytes=0)
        self.save()
        with patch("cleanup.open_remote") as connection:
            self.assertEqual(self.cli("--audit", str(self.root / "audit.jsonl")), 0)
            connection.assert_not_called()
