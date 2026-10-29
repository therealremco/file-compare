import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import paramiko

from file_compare import local_index, match_key, remote_files, remote_url, write_report


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_nested_local_index_and_ignored_output_and_symlinks(self):
        nested = self.root / "different" / "structure"
        nested.mkdir(parents=True)
        (nested / "photo.jpg").write_bytes(b"abc")
        (self.root / "local-only.txt").write_bytes(b"x")
        output = self.root / "missing-files.txt"
        output.write_text("previous report", encoding="utf-8")
        (self.root / "cycle").symlink_to(self.root, target_is_directory=True)
        (self.root / "link.jpg").symlink_to(nested / "photo.jpg")
        keys = local_index(self.root, False, output)
        self.assertEqual(keys, {("photo.jpg", 3), ("local-only.txt", 1)})
        keys = local_index(self.root, True, output)
        self.assertEqual(keys, {("photo", None), ("local-only", None)})

    def test_matching_rules(self):
        self.assertNotEqual(match_key("a.jpg", 1, False), match_key("a.png", 1, False))
        self.assertNotEqual(match_key("a.jpg", 1, False), match_key("a.jpg", 2, False))
        self.assertEqual(match_key("a.jpg", 1, True), match_key("a.png", 99, True))
        self.assertNotEqual(match_key("A.jpg", 1, True), match_key("a.jpg", 1, True))
        self.assertEqual(match_key("archive.tar.gz", 4, True), ("archive.tar", None))
        self.assertEqual(match_key(".env", 0, True), (".env", None))
        self.assertEqual(match_key("no-extension", 0, True), ("no-extension", None))

    def test_url_format_and_escaping(self):
        self.assertEqual(
            remote_url("example.com", 2222, "/files/a #?\n\u00e9.txt"),
            "sftp://example.com:2222/files/a%20%23%3F%0A%C3%A9.txt",
        )
        self.assertEqual(remote_url("::1", 22, "/a"), "sftp://[::1]/a")

    def test_report_sorted_empty_and_atomic_on_failure(self):
        output = self.root / "result.txt"
        write_report(output, ["sftp://host/z", "sftp://host/a"])
        self.assertEqual(output.read_text(), "sftp://host/a\nsftp://host/z\n")
        with patch("file_compare.os.replace", side_effect=PermissionError("blocked")):
            with self.assertRaises(PermissionError):
                write_report(output, ["new"])
        self.assertEqual(output.read_text(), "sftp://host/a\nsftp://host/z\n")
        self.assertEqual(list(self.root.iterdir()), [output])
        write_report(output, [])
        self.assertEqual(output.read_bytes(), b"")

    def test_missing_local_root_is_error(self):
        with self.assertRaises(ValueError):
            local_index(self.root / "absent", False, self.root / "out")

    def test_remote_metadata_errors_propagate(self):
        sftp = Mock()
        sftp.normalize.return_value = "/remote"
        sftp.lstat.return_value = paramiko.SFTPAttributes()
        sftp.lstat.return_value.st_mode = stat.S_IFDIR
        entry = paramiko.SFTPAttributes()
        entry.filename = "file.txt"
        sftp.listdir_iter.return_value = [entry]
        with self.assertRaisesRegex(ValueError, "file type"):
            list(remote_files(sftp, "/remote"))
        entry.st_mode = stat.S_IFREG
        with self.assertRaisesRegex(ValueError, "file size"):
            list(remote_files(sftp, "/remote"))
