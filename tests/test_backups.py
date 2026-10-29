import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cleanup import MarkedFile, backup_check, cleanup, load_manifest, local_hash, main, scan_backed_up
from file_compare import Options


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.local = self.root / "local"
        self.local.mkdir()
        self.copy = self.local / "photo.png"
        self.copy.write_bytes(b"abc")
        self.digest = hashlib.sha256(b"abc").hexdigest()
        self.options = Options("server", "/remote", self.local, filename_only=True)
        self.item = MarkedFile("/remote/photo.png", "sftp://server/remote/photo.png",
                               3, 100, self.digest, str(self.copy))
        self.sftp = Mock()
        self.sftp.normalize.side_effect = lambda path: path
        self.sftp.lstat.side_effect = lambda path: Mock(
            st_mode=stat.S_IFREG if path.endswith(".png") else stat.S_IFDIR,
            st_size=3, st_mtime=100,
        )
        self.sftp.open.side_effect = lambda *args: io.BytesIO(b"abc")
        self.manifest = self.root / "backups.json"

    def scan(self, files=None):
        with patch("cleanup.open_remote") as connection, patch(
            "cleanup.remote_files", return_value=files or [("/remote/photo.png", 3)],
        ), contextlib.redirect_stdout(io.StringIO()):
            connection.return_value.__enter__.return_value = self.sftp
            scan_backed_up(self.options, self.manifest)

    def test_scan_manifest_round_trip_and_media_only(self):
        (self.local / "notes.txt").write_bytes(b"abc")
        (self.local / "photo.jpg").write_bytes(b"abc")
        self.scan([("/remote/photo.png", 3), ("/remote/notes.txt", 3),
                   ("/remote/photo.webp", 3), ("/remote/photo.png", 4)])
        self.assertEqual(load_manifest(self.manifest, self.options), [self.item])
        self.sftp.remove.assert_not_called()
        data = json.loads(self.manifest.read_text())
        self.assertEqual(data["action"], "verified_local_backup")
        self.assertEqual(data["count"], 1)

    def test_same_name_and_size_but_different_content_is_not_backed_up(self):
        self.sftp.open.side_effect = lambda *args: io.BytesIO(b"xyz")
        self.scan()
        self.assertEqual(load_manifest(self.manifest, self.options), [])

    def test_duplicate_local_names_one_identical_copy_is_sufficient(self):
        self.copy.write_bytes(b"xyz")
        nested = self.local / "nested"
        nested.mkdir()
        (nested / "photo.png").write_bytes(b"abc")
        self.scan()
        items = load_manifest(self.manifest, self.options)
        self.assertEqual(items[0].local_path, str(nested / "photo.png"))

    def test_local_symlinks_and_outside_paths_are_rejected(self):
        outside = self.root / "outside.png"
        outside.write_bytes(b"abc")
        link = self.local / "link.png"
        link.symlink_to(outside)
        for path in (link, outside):
            with self.subTest(path=path), self.assertRaises(ValueError):
                local_hash(path, self.local)
        (self.local / "linked-directory").symlink_to(self.root, target_is_directory=True)
        self.copy.unlink()
        self.copy.symlink_to(outside)
        self.scan()
        self.assertEqual(load_manifest(self.manifest, self.options), [])

    def test_revalidation_rejects_missing_changed_local_and_remote_contents(self):
        self.assertIsNone(backup_check(self.sftp, "/remote", self.item, self.local))
        self.copy.write_bytes(b"xyz")
        self.assertIn("local backup content", backup_check(self.sftp, "/remote", self.item, self.local))
        self.copy.unlink()
        self.assertIn("No such file", backup_check(self.sftp, "/remote", self.item, self.local))
        self.copy.write_bytes(b"abc")
        self.sftp.open.side_effect = lambda *args: io.BytesIO(b"xyz")
        self.assertIn("remote content", backup_check(self.sftp, "/remote", self.item, self.local))

    def test_cleanup_rehashes_before_removal_and_skips_changed_backups(self):
        for delete in (False, True):
            with self.subTest(delete=delete), patch("cleanup.open_remote") as connection:
                connection.return_value.__enter__.return_value = self.sftp
                with (self.root / "audit.jsonl").open("w") as audit, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cleanup(self.options, [self.item], delete, audit), 0)
        self.sftp.remove.assert_called_once_with("/remote/photo.png")
        self.sftp.remove.reset_mock()
        self.copy.write_bytes(b"xyz")
        with patch("cleanup.open_remote") as connection:
            connection.return_value.__enter__.return_value = self.sftp
            with (self.root / "audit.jsonl").open("w") as audit, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cleanup(self.options, [self.item], True, audit), 1)
        self.sftp.remove.assert_not_called()

    def test_scan_failure_does_not_publish_manifest(self):
        self.sftp.open.side_effect = OSError("read failed")
        with self.assertRaises(OSError):
            self.scan()
        self.assertFalse(self.manifest.exists())

    def test_invalid_backup_metadata_rejected(self):
        self.scan()
        original = json.loads(self.manifest.read_text())
        for key, value in [("sha256", None), ("sha256", "invalid"),
                           ("local_path", "relative.png"), ("local_path", "/backup/other.png")]:
            with self.subTest(key=key):
                data = json.loads(json.dumps(original))
                data["files"][0][key] = value
                self.manifest.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    load_manifest(self.manifest, self.options)
        data = json.loads(json.dumps(original))
        data.update(version=1, action="marked_for_deletion_only")
        self.manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            load_manifest(self.manifest, self.options)

    def test_scan_existing_output_and_delete_flags_refused(self):
        self.manifest.write_text("existing")
        with self.assertRaises(ValueError):
            self.scan()
        self.assertEqual(self.manifest.read_text(), "existing")
        with patch("cleanup.open_remote") as connection:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([
                    "--host", "server", "--remote-path", "/remote", "--local-path", str(self.local),
                    "--scan-backed-up", str(self.manifest), "--delete",
                ]), 1)
            connection.assert_not_called()

    def test_nonregular_local_backup_is_rejected_before_opening(self):
        self.copy.unlink()
        os.mkfifo(self.copy)
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            local_hash(self.copy, self.local)
