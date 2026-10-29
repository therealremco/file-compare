import contextlib
import http.client
import json
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from file_compare import Options
from review import Media, ReviewError, Store, make_handler, metadata, prepare, report_paths


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.local = self.root / "local"
        self.local.mkdir()
        self.store = Store(self.root / "review")
        self.options = Options("server", "/remote", self.local)
        self.item = {
            "id": "abc", "url": "sftp://server/remote/photo.png",
            "path": "/remote/photo.png", "size": 20, "mtime": 100,
            "category": "name_absent", "kind": "image",
        }
        self.store.import_items([self.item])

    def test_decisions_persist_manifest_and_changed_files_reset(self):
        self.store.decide("abc", "delete")
        resumed = Store(self.root / "review")
        self.assertEqual(resumed.item("abc")["status"], "delete")
        manifest = resumed.manifest()
        self.assertEqual(manifest["count"], 1)
        self.assertEqual(manifest["total_bytes"], 20)
        self.assertTrue(manifest["requires_remote_revalidation"])
        self.assertEqual(manifest["files"][0]["mtime"], 100)
        resumed.import_items([self.item])
        self.assertEqual(resumed.item("abc")["status"], "delete")
        resumed.import_items([{**self.item, "mtime": 101}])
        self.assertEqual(resumed.item("abc")["status"], "unreviewed")
        resumed.import_items([])
        self.assertEqual(resumed.items(), [])
        self.assertEqual(resumed.manifest()["count"], 0)
        with self.assertRaises(ReviewError):
            resumed.decide("abc", "delete")

    def test_invalid_decisions_do_not_change_state(self):
        for value in ("erase", "", "DELETE"):
            with self.assertRaises(ReviewError):
                self.store.decide("abc", value)
        self.assertEqual(self.store.item("abc")["status"], "unreviewed")

    def test_report_host_and_path_validation(self):
        report = self.root / "report.txt"
        report.write_text("sftp://server/remote/a%20b.png\n\nsftp://server/remote/a%20b.png\n")
        self.assertEqual(report_paths(report, self.options), ["/remote/a b.png"])
        for url in (
            "https://server/remote/a.png", "sftp://other/remote/a.png",
            "sftp://server:2222/remote/a.png", "sftp://server:0/remote/a.png",
            "sftp://user@server/remote/a.png",
            "sftp://server/remote/../a.png", "sftp://server/remote/%00a.png",
        ):
            report.write_text(url)
            with self.subTest(url=url), self.assertRaises(ValueError):
                report_paths(report, self.options)

    def test_remote_root_escape_and_symlink_are_rejected(self):
        sftp = Mock()
        sftp.normalize.return_value = "/elsewhere/photo.png"
        with self.assertRaises(ReviewError):
            metadata(sftp, "/remote/photo.png", "/remote")
        sftp.normalize.return_value = "/remote/photo.png"
        sftp.lstat.return_value.st_mode = stat.S_IFLNK
        with self.assertRaises(ReviewError):
            metadata(sftp, "/remote/photo.png", "/remote")

    def test_import_categorizes_exact_basename_and_ignores_present_files(self):
        (self.local / "nested").mkdir()
        (self.local / "nested" / "same.png").write_bytes(b"xx")
        (self.local / "present.png").write_bytes(b"xxx")
        (self.local / "stem.jpg").write_bytes(b"xx")
        report = self.root / "report.txt"
        report.write_text("\n".join(f"sftp://server/remote/{name}" for name in (
            "same.png", "present.png", "absent.mp4", "stem.png", "notes.txt",
        )))
        sftp = Mock()
        sftp.normalize.side_effect = lambda path: path
        info = Mock(st_size=3, st_mtime=100, st_mode=stat.S_IFREG)
        sftp.lstat.return_value = info
        with patch("review.open_remote") as connection:
            connection.return_value.__enter__.return_value = sftp
            prepare(self.options, report, self.store)
        categories = {Path(item["path"]).name: item["category"] for item in self.store.items()}
        self.assertEqual(categories, {
            "same.png": "same_name_different_size", "absent.mp4": "name_absent",
            "stem.png": "name_absent",
        })

    def test_failed_import_preserves_previous_snapshot(self):
        report = self.root / "report.txt"
        report.write_text("sftp://server/remote/a.png\n")
        with patch("review.open_remote") as connection:
            connection.return_value.__enter__.return_value.normalize.side_effect = OSError("offline")
            with self.assertRaises(OSError):
                prepare(self.options, report, self.store)
        self.assertEqual(self.store.items()[0]["id"], "abc")

    def test_image_preview_download_cache_and_change_detection(self):
        source = self.root / "source.png"
        Image.new("RGB", (800, 600), "blue").save(source)
        item = {**self.item, "size": source.stat().st_size}
        self.store.import_items([item])
        sftp = Mock()
        sftp.normalize.side_effect = lambda path: path
        sftp.lstat.return_value = Mock(
            st_size=item["size"], st_mtime=100, st_mode=stat.S_IFREG,
        )
        sftp.get.side_effect = lambda remote, local: shutil.copyfile(source, local)
        media = Media(self.options, self.store)
        with patch("review.open_remote") as connection:
            connection.return_value.__enter__.return_value = sftp
            preview = media.get("abc", True)
            with Image.open(preview) as image:
                self.assertEqual(image.size, (427, 320))
            self.assertEqual(media.get("abc", True), preview)
            sftp.get.assert_called_once()
            self.assertEqual(media.get("abc", False).read_bytes(), source.read_bytes())
        self.store.import_items([{**item, "mtime": 101}])
        with patch("review.open_remote") as connection:
            connection.return_value.__enter__.return_value = sftp
            with self.assertRaisesRegex(ReviewError, "changed"):
                media.get("abc", False)
        self.assertEqual(list(media.cache.glob("*.part")), [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed")
    def test_video_preview_from_real_short_clip(self):
        source = self.root / "clip.mp4"
        subprocess.run([
            shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-f", "lavfi",
            "-i", "color=c=blue:s=160x120:d=0.2", "-pix_fmt", "yuv420p", str(source),
        ], check=True, timeout=20)
        item = {**self.item, "kind": "video", "path": "/remote/clip.mp4",
                "size": source.stat().st_size}
        self.store.import_items([item])
        media = Media(self.options, self.store)
        with patch.object(media, "original", return_value=source):
            preview = media.get("abc", True)
        with Image.open(preview) as image:
            self.assertEqual(image.format, "JPEG")

    def test_missing_ffmpeg_is_explicit_error(self):
        source = self.root / "clip.mp4"
        source.write_bytes(b"fixture")
        self.store.import_items([{**self.item, "kind": "video"}])
        media = Media(self.options, self.store)
        with patch.object(media, "original", return_value=source), patch("review.shutil.which", return_value=None):
            with self.assertRaisesRegex(ReviewError, "require local FFmpeg"):
                media.get("abc", True)

    def test_changed_during_download_is_not_cached(self):
        source = self.root / "image.png"
        Image.new("RGB", (10, 10)).save(source)
        self.store.import_items([{**self.item, "size": source.stat().st_size}])
        media = Media(self.options, self.store)
        sftp = Mock()
        sftp.get.side_effect = lambda remote, local: shutil.copyfile(source, local)
        with patch("review.open_remote") as connection, patch.object(
            media, "check", side_effect=[None, ReviewError("changed")],
        ):
            connection.return_value.__enter__.return_value = sftp
            with self.assertRaisesRegex(ReviewError, "changed"):
                media.get("abc", True)
        self.assertEqual(list(media.cache.iterdir()), [])

    def test_http_gallery_decisions_manifest_and_range(self):
        source = self.root / "image.png"
        Image.new("RGB", (20, 20)).save(source)
        media = Mock()
        media.get.return_value = source
        with ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, media, "token")) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                self.addCleanup(connection.close)
                connection.request("GET", "/token/")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn(b"Remote media review", response.read())
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                connection.request("GET", "/token/api/files")
                response = connection.getresponse()
                self.assertEqual(json.loads(response.read())[0]["status"], "unreviewed")
                connection.request("POST", "/token/api/decision",
                                   json.dumps({"id": "abc", "status": "delete"}),
                                   {"Origin": f"http://127.0.0.1:{server.server_port}"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.request("GET", "/token/manifest")
                response = connection.getresponse()
                self.assertEqual(json.loads(response.read())["count"], 1)
                connection.request("GET", "/token/media/abc", headers={"Range": "bytes=0-9"})
                response = connection.getresponse()
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), source.read_bytes()[:10])
                connection.request("GET", "/wrong/api/files")
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                connection.request("POST", "/token/api/decision",
                                   json.dumps({"id": "abc", "status": "keep"}),
                                   {"Origin": "https://untrusted.example"})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                self.assertEqual(self.store.item("abc")["status"], "delete")
            finally:
                server.shutdown()
                thread.join(timeout=5)
