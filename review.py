"""Local, read-only SFTP media review gallery."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import posixpath
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import paramiko
from PIL import Image, ImageOps

from file_compare import Options, local_index, open_remote, parse_options, remote_url, ssh_progress


IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
VIDEOS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
STATUSES = {"unreviewed", "keep", "delete"}
ASSETS = Path(__file__).parent / "review_assets"


class ReviewError(ValueError):
    pass


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.directory / "review.sqlite3"
        with self.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, url TEXT NOT NULL, path TEXT NOT NULL,
                    size INTEGER NOT NULL, mtime INTEGER NOT NULL,
                    category TEXT NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unreviewed', active INTEGER NOT NULL DEFAULT 1
                )
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def items(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM files WHERE active=1 ORDER BY path"
            )]

    def item(self, identifier):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM files WHERE id=? AND active=1", (identifier,),
            ).fetchone()
        if row is None:
            raise ReviewError("Unknown or inactive file")
        return dict(row)

    def import_items(self, items):
        with self.connect() as db:
            db.execute("UPDATE files SET active=0")
            for item in items:
                db.execute("""
                    INSERT INTO files (id,url,path,size,mtime,category,kind)
                    VALUES (:id,:url,:path,:size,:mtime,:category,:kind)
                    ON CONFLICT(id) DO UPDATE SET
                        status=CASE WHEN files.size=excluded.size AND files.mtime=excluded.mtime
                            THEN files.status ELSE 'unreviewed' END,
                        size=excluded.size, mtime=excluded.mtime, category=excluded.category,
                        kind=excluded.kind, active=1
                """, item)

    def decide(self, identifier, status):
        if status not in STATUSES:
            raise ReviewError("Invalid review status")
        self.item(identifier)
        with self.connect() as db:
            db.execute("UPDATE files SET status=? WHERE id=?", (status, identifier))

    def manifest(self):
        items = [item for item in self.items() if item["status"] == "delete"]
        return {
            "version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "action": "marked_for_deletion_only", "requires_remote_revalidation": True,
            "count": len(items), "total_bytes": sum(item["size"] for item in items),
            "files": [
                {key: item[key] for key in ("url", "path", "size", "mtime", "category")}
                for item in items
            ],
        }


def report_paths(report: Path, options: Options):
    paths = set()
    for number, line in enumerate(report.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        url = urlsplit(line.strip())
        if (
            url.scheme != "sftp" or url.hostname != options.host.lower()
            or (22 if url.port is None else url.port) != options.port or url.username is not None
            or url.query or url.fragment
        ):
            raise ReviewError(f"Report line {number}: URL does not match configured SSH host")
        path = unquote(url.path, errors="strict")
        if not path.startswith("/") or "\0" in path or posixpath.normpath(path) != path:
            raise ReviewError(f"Report line {number}: invalid absolute remote path")
        paths.add(path)
    return sorted(paths)


def metadata(sftp, path, root):
    resolved = sftp.normalize(path)
    if posixpath.commonpath([root, resolved]) != root or resolved == root:
        raise ReviewError(f"Remote path escapes configured directory: {path}")
    info = sftp.lstat(path)
    if info.st_mode is None or not stat.S_ISREG(info.st_mode):
        raise ReviewError(f"Not a regular remote file: {path}")
    if info.st_size is None or info.st_mtime is None or info.st_size < 0:
        raise ReviewError(f"Missing remote size or modification time: {path}")
    return info


def prepare(options: Options, report: Path, store: Store):
    paths = report_paths(report, options)
    local = local_index(options.local_path, False, options.output)
    names = {name for name, _ in local}
    items = []
    skipped = 0
    with open_remote(options) as sftp:
        root = sftp.normalize(options.remote_path)
        for index, path in enumerate(paths, 1):
            suffix = Path(path).suffix.lower()
            if suffix not in IMAGES | VIDEOS:
                skipped += 1
                continue
            info = metadata(sftp, path, root)
            name = posixpath.basename(path)
            if (name, info.st_size) in local:
                skipped += 1
                continue
            url = remote_url(options.host, options.port, path)
            items.append({
                "id": hashlib.sha256(url.encode()).hexdigest(),
                "url": url, "path": path, "size": info.st_size, "mtime": info.st_mtime,
                "category": "same_name_different_size" if name in names else "name_absent",
                "kind": "video" if suffix in VIDEOS else "image",
            })
            print(f"\rIndexed {index}/{len(paths)} report entries", end="", file=sys.stderr, flush=True)
    store.import_items(items)
    print(f"\nReady: {len(items)} media files; {skipped} matching/non-media entries skipped.", file=sys.stderr)


class Media:
    def __init__(self, options: Options, store: Store):
        self.options = options
        self.store = store
        self.lock = threading.Lock()
        self.cache = store.directory / "cache"
        self.cache.mkdir(exist_ok=True, mode=0o700)

    def check(self, sftp, item):
        root = sftp.normalize(self.options.remote_path)
        info = metadata(sftp, item["path"], root)
        if (info.st_size, info.st_mtime) != (item["size"], item["mtime"]):
            raise ReviewError("Remote file changed since import. Restart review to refresh it.")

    def original(self, item):
        version = hashlib.sha256(
            f'{item["url"]}\0{item["size"]}\0{item["mtime"]}'.encode()
        ).hexdigest()
        target = self.cache / (version + Path(item["path"]).suffix.lower())
        if target.exists():
            return target
        with open_remote(self.options) as sftp:
            self.check(sftp, item)
            temporary = target.with_suffix(target.suffix + ".part")
            try:
                sftp.get(item["path"], str(temporary))
                self.check(sftp, item)
                if temporary.stat().st_size != item["size"]:
                    raise ReviewError("Incomplete media download")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return target

    def get(self, identifier, thumbnail):
        with self.lock:
            item = self.store.item(identifier)
            original = self.original(item)
            if not thumbnail:
                return original
            target = original.with_suffix(original.suffix + ".thumb.jpg")
            if target.exists():
                return target
            temporary = target.with_suffix(".part.jpg")
            try:
                if item["kind"] == "video":
                    executable = shutil.which("ffmpeg")
                    if executable is None:
                        raise ReviewError("Video thumbnails require local FFmpeg; install it and retry.")
                    result = subprocess.run([
                        executable, "-nostdin", "-v", "error", "-y", "-i", str(original),
                        "-frames:v", "1", "-vf", "scale=480:320:force_original_aspect_ratio=decrease",
                        str(temporary),
                    ], capture_output=True, timeout=60, check=False)
                    if result.returncode:
                        raise ReviewError("FFmpeg preview failed: " + result.stderr.decode(errors="replace")[-500:])
                else:
                    with Image.open(original) as image:
                        image = ImageOps.exif_transpose(image)
                        image.thumbnail((480, 320))
                        image.convert("RGB").save(temporary, "JPEG")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return target


def make_handler(store: Store, media: Media, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Request paths contain the browser access token.
            logging.getLogger("review.http").debug("HTTP request handled")

        def send_data(self, code, data, content_type, **headers):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self'; media-src 'self'; "
                             "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            for key, value in headers.items():
                self.send_header(key.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(data)

        def authorized_path(self):
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected_host:
                raise ReviewError("Invalid request host")
            path = urlsplit(self.path).path
            prefix = f"/{token}/"
            if not path.startswith(prefix):
                raise ReviewError("Invalid access token")
            return path[len(prefix):]

        def do_GET(self):
            try:
                path = self.authorized_path()
                if path == "api/files":
                    self.send_data(200, json.dumps(store.items()).encode(), "application/json")
                elif path == "manifest":
                    self.send_data(
                        200, json.dumps(store.manifest(), indent=2).encode(), "application/json",
                        Content_Disposition='attachment; filename="deletion-manifest.json"',
                    )
                elif path in ("", "app.js", "style.css"):
                    asset = ASSETS / (path or "index.html")
                    self.send_data(200, asset.read_bytes(),
                                   mimetypes.guess_type(asset.name)[0] or "application/octet-stream")
                elif path.startswith(("thumb/", "media/")):
                    mode, identifier = path.split("/", 1)
                    file = media.get(identifier, mode == "thumb")
                    self.send_file(file)
                else:
                    self.send_data(404, b"Not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                logging.getLogger("review.http").debug("Browser disconnected during media transfer")
            except (OSError, ValueError, paramiko.SSHException, EOFError, sqlite3.Error,
                    subprocess.SubprocessError, Image.DecompressionBombError) as error:
                self.send_data(400, str(error).encode(), "text/plain; charset=utf-8")

        def send_file(self, file):
            size = file.stat().st_size
            start, end = 0, size - 1
            partial = self.headers.get("Range")
            if partial:
                import re
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", partial)
                if not match:
                    self.send_data(416, b"Unsupported range", "text/plain",
                                   Content_Range=f"bytes */{size}")
                    return
                start = int(match[1])
                end = min(int(match[2]) if match[2] else end, end)
                if start > end:
                    self.send_data(416, b"Invalid range", "text/plain",
                                   Content_Range=f"bytes */{size}")
                    return
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("Referrer-Policy", "no-referrer")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with file.open("rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def do_POST(self):
            try:
                path = self.authorized_path()
                origin = self.headers.get("Origin")
                if origin != f"http://127.0.0.1:{self.server.server_port}":
                    raise ReviewError("Invalid request origin")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048:
                    raise ReviewError("Invalid request body size")
                if path != "api/decision":
                    raise ReviewError("Unknown action")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ReviewError("Expected a JSON object")
                identifier, status = data.get("id"), data.get("status")
                if not isinstance(identifier, str) or not isinstance(status, str):
                    raise ReviewError("id and status must be strings")
                store.decide(identifier, status)
                self.send_data(200, b'{"ok":true}', "application/json")
            except (OSError, ValueError, sqlite3.Error) as error:
                self.send_data(400, str(error).encode(), "text/plain; charset=utf-8")

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--review-help", action="help", help="Show review-specific options")
    parser.add_argument("--report", type=Path, help="Existing missing-files report (default: configured output)")
    parser.add_argument("--review-dir", type=Path, default=Path(".review"))
    parser.add_argument("--listen-port", type=int, default=0, help="Local HTTP port; default: choose a free port")
    args, compare_args = parser.parse_known_args(argv)
    try:
        if not 0 <= args.listen_port <= 65535:
            raise ReviewError("listen-port must be between 0 and 65535")
        options = parse_options(compare_args)
        store = Store(args.review_dir.expanduser())
        with ssh_progress(debug=options.debug_ssh):
            prepare(options, args.report or options.output, store)
            token = secrets.token_urlsafe(32)
            handler = make_handler(store, Media(options, store), token)
            with ThreadingHTTPServer(("127.0.0.1", args.listen_port), handler) as server:
                print(f"Review gallery: http://127.0.0.1:{server.server_port}/{token}/", flush=True)
                print("Keep this process running. Ctrl-C stops it. No remote files will be modified.", flush=True)
                server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, paramiko.SSHException, EOFError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
