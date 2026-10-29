"""Loopback integration tests using the real SSH and SFTP protocols."""

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import paramiko

from file_compare import SSH_LOG, main


class PublicKeyServer(paramiko.ServerInterface):
    def __init__(self, public_key):
        self.public_key = public_key

    def check_auth_publickey(self, username, key):
        if username == "test-user" and key == self.public_key:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "publickey"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class LocalSFTP(paramiko.SFTPServerInterface):
    def __init__(self, server, *, root):
        super().__init__(server)
        self.root = root

    def local(self, path):
        parts = PurePosixPath(path).parts
        if ".." in parts:
            raise PermissionError("Traversal is not allowed")
        return self.root.joinpath(*(part for part in parts if part != "/"))

    def list_folder(self, path):
        try:
            return [
                paramiko.SFTPAttributes.from_stat(item.lstat(), filename=item.name)
                for item in self.local(path).iterdir()
            ]
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.local(path).lstat())
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def stat(self, path):
        return self.lstat(path)

    def remove(self, path):
        try:
            self.local(path).unlink()
            return paramiko.SFTP_OK
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def open(self, path, flags, attr):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            return paramiko.SFTP_PERMISSION_DENIED
        try:
            handle = paramiko.SFTPHandle(flags)
            handle.readfile = self.local(path).open("rb")
            return handle
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)


class SFTPIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.host_key = paramiko.RSAKey.generate(2048)
        cls.user_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.remote = self.root / "remote"
        self.local = self.root / "local"
        (self.remote / "files" / "nested").mkdir(parents=True)
        (self.local / "other").mkdir(parents=True)
        (self.remote / "files" / "nested" / "match.jpg").write_bytes(b"abc")
        (self.remote / "files" / "different-size.txt").write_bytes(b"remote")
        (self.remote / "files" / "different-extension.jpg").write_bytes(b"r")
        (self.remote / "files" / "missing #.txt").write_bytes(b"missing")
        (self.remote / "files" / "duplicate").mkdir()
        (self.remote / "files" / "duplicate" / "match.jpg").write_bytes(b"abc")
        (self.remote / "files" / "cycle").symlink_to(self.remote, target_is_directory=True)
        (self.local / "other" / "match.jpg").write_bytes(b"abc")
        (self.local / "different-size.txt").write_bytes(b"local")
        (self.local / "different-extension.png").write_bytes(b"longer local")
        (self.local / "local-only.txt").write_bytes(b"not on server")
        self.output = self.root / "report.txt"
        self.key_path = self.root / "id_rsa"
        self.user_key.write_private_key_file(str(self.key_path))
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(10)
        self.port = self.listener.getsockname()[1]
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text(
            f"[127.0.0.1]:{self.port} {self.host_key.get_name()} {self.host_key.get_base64()}\n"
        )
        self.errors = []
        self.allow_handshake_disconnect = False
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "host": "127.0.0.1", "port": self.port, "username": "test-user",
            "key_filename": str(self.key_path), "known_hosts": str(self.known_hosts),
            "local_path": str(self.local), "remote_path": "/files",
            "output": str(self.output), "timeout": 5,
        }))

    def serve(self):
        try:
            connection, _ = self.listener.accept()
            with connection, paramiko.Transport(connection) as transport:
                transport.add_server_key(self.host_key)
                transport.set_subsystem_handler(
                    "sftp", paramiko.SFTPServer, LocalSFTP, root=self.remote,
                )
                transport.start_server(server=PublicKeyServer(self.user_key))
                self.done.wait(10)
        except (EOFError, ConnectionResetError) as error:
            if not self.allow_handshake_disconnect:
                self.errors.append(error)
        except (OSError, paramiko.SSHException) as error:
            self.errors.append(error)

    def stop(self):
        self.done.set()
        self.listener.close()
        self.thread.join(timeout=12)
        self.assertFalse(self.thread.is_alive(), "SFTP test server did not stop")
        self.assertEqual(self.errors, [])

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--config", str(self.config), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cleanup_deletes_only_marked_unchanged_files_over_real_sftp(self):
        from cleanup import main as cleanup_main
        from file_compare import remote_url

        target = self.remote / "files" / "different-extension.jpg"
        changed = self.remote / "files" / "nested" / "match.jpg"
        entries = [
            dict(path="/files/different-extension.jpg", size=target.stat().st_size,
                 mtime=int(target.stat().st_mtime)),
            dict(path="/files/nested/match.jpg", size=999,
                 mtime=int(changed.stat().st_mtime)),
        ]
        for entry in entries:
            entry["url"] = remote_url("127.0.0.1", self.port, entry["path"])
        manifest = self.root / "deletion.json"
        manifest.write_text(json.dumps(dict(
            version=1, action="marked_for_deletion_only", requires_remote_revalidation=True,
            count=2, total_bytes=sum(entry["size"] for entry in entries), files=entries,
        )))
        audit = self.root / "cleanup.jsonl"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = cleanup_main([
                "--config", str(self.config), "--manifest", str(manifest),
                "--delete", "--confirm-count", "2", "--audit", str(audit),
            ])
        self.assertEqual(result, 1, "Changed files must produce a nonzero result")
        self.assertFalse(target.exists())
        self.assertTrue(changed.exists())
        self.assertTrue((self.remote / "files" / "missing #.txt").exists())
        self.assertTrue((self.local / "different-extension.png").exists())
        events = [json.loads(line) for line in audit.read_text().splitlines()]
        self.assertEqual(events[-1]["deleted"], 1)
        self.assertEqual(events[-1]["skipped"], 1)

    def test_backup_scan_and_cleanup_over_real_sftp(self):
        from cleanup import cleanup, load_manifest, scan_backed_up
        from file_compare import Options, open_remote

        options = Options(
            host="127.0.0.1", port=self.port, username="test-user",
            remote_path="/files", local_path=self.local,
            key_filename=self.key_path, known_hosts=self.known_hosts,
        )
        manifest = self.root / "backups.json"
        with open_remote(options) as sftp:
            @contextlib.contextmanager
            def reuse_connection(options):
                yield sftp

            with patch("cleanup.open_remote", side_effect=reuse_connection):
                with contextlib.redirect_stdout(io.StringIO()):
                    scan_backed_up(options, manifest)
                    items = load_manifest(manifest, options)
                    self.assertEqual({item.path for item in items}, {
                        "/files/nested/match.jpg", "/files/duplicate/match.jpg",
                    })
                    with (self.root / "dry-audit.jsonl").open("w") as audit:
                        self.assertEqual(cleanup(options, items, False, audit), 0)
                    self.assertTrue((self.remote / "files/nested/match.jpg").exists())
                    with (self.root / "delete-audit.jsonl").open("w") as audit:
                        self.assertEqual(cleanup(options, items, True, audit), 0)
        self.assertFalse((self.remote / "files/nested/match.jpg").exists())
        self.assertFalse((self.remote / "files/duplicate/match.jpg").exists())
        self.assertTrue((self.local / "other/match.jpg").exists())
        self.assertTrue((self.remote / "files/different-extension.jpg").exists())

    def test_full_comparison_reports_only_missing_remote_files(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "file_compare.py"),
             "--config", str(self.config), "--debug-ssh"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        base = f"sftp://127.0.0.1:{self.port}/files/"
        self.assertEqual(self.output.read_text().splitlines(), [
            base + "different-extension.jpg",
            base + "different-size.txt",
            base + "missing%20%23.txt",
        ])
        self.assertIn("Scanned 5 remote files; 3 missing", result.stdout)
        for stage in (
            "Connecting to 127.0.0.1:",
            "Remote version/idstring:",
            "=== Key exchange agreements ===",
            "Authentication (publickey) successful!",
            "SSH handshake, host-key verification and authentication complete",
            "Opening SFTP session",
            "SFTP session ready",
        ):
            with self.subTest(stage=stage):
                self.assertIn(stage, result.stderr)
        self.assertRegex(result.stderr, r"\d{2}:\d{2}:\d{2} INFO SSH:")
        self.assertNotIn("SSH:", result.stdout)
        self.assertNotIn("PRIVATE KEY", result.stderr)

    def test_filename_only_ignores_extension_and_size(self):
        code, _, stderr = self.run_cli("--filename-only")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            self.output.read_text(),
            f"sftp://127.0.0.1:{self.port}/files/missing%20%23.txt\n",
        )

    def test_all_remote_files_present_produces_empty_report(self):
        (self.local / "missing #.txt").write_bytes(b"any size")
        self.output.write_text("previous\n")
        code, stdout, stderr = self.run_cli("--filename-only")
        self.assertEqual(code, 0, stderr)
        self.assertIn("0 missing", stdout)
        self.assertEqual(self.output.read_bytes(), b"")

    def test_authentication_failure_preserves_previous_report(self):
        self.output.write_text("previous\n")
        previous_handlers = SSH_LOG.handlers.copy()
        previous_level, previous_propagate = SSH_LOG.level, SSH_LOG.propagate
        code, _, stderr = self.run_cli("--username", "invalid-user")
        self.assertEqual(code, 1)
        self.assertIn("Authentication failed", stderr)
        self.assertIn("Authentication (publickey) failed", stderr)
        self.assertNotIn("Opening SFTP session", stderr)
        self.assertEqual(self.output.read_text(), "previous\n")
        self.assertEqual(SSH_LOG.handlers, previous_handlers)
        self.assertEqual(SSH_LOG.level, previous_level)
        self.assertEqual(SSH_LOG.propagate, previous_propagate)

    def test_unknown_host_key_is_rejected(self):
        self.allow_handshake_disconnect = True
        self.known_hosts.write_text("")
        self.output.write_text("previous\n")
        code, _, stderr = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("known_hosts", stderr)
        self.assertEqual(self.output.read_text(), "previous\n")

    def test_missing_remote_directory_preserves_previous_report(self):
        self.output.write_text("previous\n")
        code, _, stderr = self.run_cli("--remote-path", "/absent")
        self.assertEqual(code, 1)
        self.assertIn("Error:", stderr)
        self.assertEqual(self.output.read_text(), "previous\n")

    def test_review_import_over_real_sftp(self):
        from file_compare import Options, remote_url
        from review import Store, prepare

        report = self.root / "input.txt"
        paths = [
            "/files/nested/match.jpg",
            "/files/different-extension.jpg",
            "/files/missing #.txt",
        ]
        report.write_text("\n".join(remote_url("127.0.0.1", self.port, path) for path in paths))
        store = Store(self.root / "review")
        options = Options(
            host="127.0.0.1", port=self.port, username="test-user",
            remote_path="/files", local_path=self.local,
            key_filename=self.key_path, known_hosts=self.known_hosts,
        )
        prepare(options, report, store)
        self.assertEqual(len(store.items()), 1)
        self.assertEqual(store.items()[0]["path"], "/files/different-extension.jpg")
        self.assertEqual(store.items()[0]["category"], "name_absent")

    def test_review_thumbnail_download_over_real_sftp(self):
        from PIL import Image
        from file_compare import Options, remote_url
        from review import Media, Store

        source = self.remote / "files" / "preview.png"
        Image.new("RGB", (800, 600), "green").save(source)
        store = Store(self.root / "review")
        store.import_items([{
            "id": "preview", "url": remote_url("127.0.0.1", self.port, "/files/preview.png"),
            "path": "/files/preview.png", "size": source.stat().st_size,
            "mtime": int(source.stat().st_mtime), "category": "name_absent", "kind": "image",
        }])
        options = Options(
            host="127.0.0.1", port=self.port, username="test-user",
            remote_path="/files", local_path=self.local,
            key_filename=self.key_path, known_hosts=self.known_hosts,
        )
        media = Media(options, store)
        preview = media.get("preview", True)
        with Image.open(preview) as image:
            self.assertLessEqual(image.width, 480)
            self.assertLessEqual(image.height, 320)
        self.assertEqual(media.get("preview", True), preview)
        self.assertEqual(media.get("preview", False).read_bytes(), source.read_bytes())


class HandshakeTimeoutTests(unittest.TestCase):
    def test_silent_server_shows_progress_before_timeout(self):
        done = threading.Event()
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            root = Path(directory)
            output = root / "report.txt"
            output.write_text("previous\n")
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(5)
            port = listener.getsockname()[1]

            def serve():
                connection, _ = listener.accept()
                with connection:
                    done.wait(5)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            stderr = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr):
                    code = main([
                        "--host", "127.0.0.1", "--port", str(port),
                        "--remote-path", "/", "--local-path", str(root),
                        "--output", str(output), "--timeout", "0.2", "--debug-ssh",
                    ])
                self.assertEqual(code, 1)
                self.assertIn("Connecting to 127.0.0.1:", stderr.getvalue())
                self.assertIn("Local version/idstring:", stderr.getvalue())
                self.assertIn("Error:", stderr.getvalue())
                self.assertNotIn("authentication complete", stderr.getvalue())
                self.assertEqual(output.read_text(), "previous\n")
            finally:
                done.set()
                thread.join(timeout=6)
            self.assertFalse(thread.is_alive())
