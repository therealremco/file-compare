"""Plan verified media-backup cleanup or remove files from a cleanup manifest."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import posixpath
import stat
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, TextIO

import paramiko

from file_compare import Options, open_remote, parse_options, remote_files, remote_url, ssh_progress
from review import IMAGES, VIDEOS


@dataclass(frozen=True)
class MarkedFile:
    path: str
    url: str
    size: int
    mtime: int
    sha256: str | None = None
    local_path: str | None = None


def stream_hash(handle: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := handle.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def local_hash(path: Path, root: Path) -> tuple[str, int]:
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError(f"Local backup is not a canonical, non-symlink path: {path}")
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f"Local backup is outside configured local directory: {path}")
    current = path
    while current != root:
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Local backup path contains a symlink: {current}")
        if current == path and not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Local backup is not a regular file: {path}")
        current = current.parent
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Local backup is not a regular file: {path}")
        result = stream_hash(handle)
        after = os.fstat(handle.fileno())
        latest = path.stat()
    def signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    if signature(before) != signature(after) or signature(after) != signature(latest):
        raise ValueError(f"Local backup changed while hashing: {path}")
    return result


def backup_check(sftp: paramiko.SFTPClient, root: str, item: MarkedFile, local_root: Path) -> str | None:
    if item.sha256 is None or item.local_path is None:
        return "backup manifest is missing verification data"
    try:
        if local_hash(Path(item.local_path), local_root) != (item.sha256, item.size):
            return "local backup content no longer matches"
    except (ValueError, FileNotFoundError) as error:
        return str(error)
    with sftp.open(item.path, "rb") as handle:
        if stream_hash(handle) != (item.sha256, item.size):
            return "remote content no longer matches verified local backup"
    return check_file(sftp, root, item)


def scan_backed_up(options: Options, output: Path) -> None:
    if output.exists():
        raise ValueError(f"Backup manifest already exists: {output}")
    root = options.local_path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Configured local path must be a directory")
    candidates: dict[tuple[str, int], list[Path]] = {}

    def walk_error(error: OSError) -> None:
        raise error

    for directory, _, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        for name in filenames:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and path.suffix.lower() in IMAGES | VIDEOS:
                candidates.setdefault((name, info.st_size), []).append(path)
    items = []
    with open_remote(options) as sftp:
        remote_root = sftp.normalize(options.remote_path)
        for path, size in remote_files(sftp, remote_root):
            matches = candidates.get((posixpath.basename(path), size), [])
            if not matches or Path(path).suffix.lower() not in IMAGES | VIDEOS:
                continue
            info = sftp.lstat(path)
            if type(info.st_mtime) is not int:
                raise ValueError(f"Missing remote modification time: {path}")
            item = MarkedFile(path, remote_url(options.host, options.port, path), size, info.st_mtime)
            reason = check_file(sftp, remote_root, item)
            if reason:
                raise ValueError(f"Cannot verify {path!r}: {reason}")
            print(f"HASH remote {path!r}", flush=True)
            with sftp.open(path, "rb") as handle:
                digest, count = stream_hash(handle)
            reason = check_file(sftp, remote_root, item)
            if reason or count != size:
                raise ValueError(f"Remote file changed while hashing: {path!r}")
            for local in sorted(matches):
                if local_hash(local, root) == (digest, size):
                    items.append(dict(path=path, url=item.url, size=size, mtime=item.mtime,
                                      sha256=digest, local_path=str(local)))
                    print(f"VERIFIED {path!r} against {str(local)!r}", flush=True)
                    break
            else:
                print(f"NOT IDENTICAL {path!r}: no local candidate has matching contents", flush=True)
    data = dict(
        version=2, action="verified_local_backup", requires_remote_revalidation=True,
        generated_at=datetime.now(timezone.utc).isoformat(), count=len(items),
        total_bytes=sum(item["size"] for item in items), files=items,
    )
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    print(f"Verified backup manifest: {output}; {len(items)} files, {data['total_bytes']} bytes. "
          "Nothing deleted.", flush=True)


def load_manifest(path: Path, options: Options) -> list[MarkedFile]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if (
        not isinstance(data, dict) or type(data.get("version")) is not int
        or (data["version"], data.get("action")) not in (
            (1, "marked_for_deletion_only"), (2, "verified_local_backup"),
        )
        or data.get("requires_remote_revalidation") is not True
        or not isinstance(data.get("files"), list)
    ):
        raise ValueError("Expected a gallery deletion manifest or verified local backup manifest")
    items = []
    seen = set()
    for index, entry in enumerate(data["files"], 1):
        if not isinstance(entry, dict):
            raise ValueError(f"Manifest entry {index} must be an object")
        remote = entry.get("path")
        if (
            not isinstance(remote, str) or not remote.startswith("/")
            or remote.startswith("//") or "\0" in remote
            or posixpath.normpath(remote) != remote or remote == "/"
        ):
            raise ValueError(f"Manifest entry {index} has an invalid absolute path")
        if entry.get("url") != remote_url(options.host, options.port, remote):
            raise ValueError(f"Manifest entry {index} URL does not match configured host, port and path")
        if remote in seen:
            raise ValueError(f"Duplicate manifest path: {remote!r}")
        for key in ("size", "mtime"):
            if type(entry.get(key)) is not int or entry[key] < 0:
                raise ValueError(f"Manifest entry {index} {key} must be a nonnegative integer")
        digest = local = None
        if data["version"] == 2:
            digest, local = entry.get("sha256"), entry.get("local_path")
            if not isinstance(digest, str) or len(digest) != 64 or any(
                char not in "0123456789abcdef" for char in digest
            ):
                raise ValueError(f"Manifest entry {index} has an invalid SHA-256 digest")
            if (
                not isinstance(local, str) or "\0" in local or not Path(local).is_absolute()
                or Path(local).name != posixpath.basename(remote)
                or Path(remote).suffix.lower() not in IMAGES | VIDEOS
            ):
                raise ValueError(f"Manifest entry {index} has an invalid local media backup path")
        elif "sha256" in entry or "local_path" in entry:
            raise ValueError("Backup verification fields require a version 2 backup manifest")
        seen.add(remote)
        items.append(MarkedFile(remote, entry["url"], entry["size"], entry["mtime"], digest, local))
    if type(data.get("count")) is not int or data["count"] != len(items):
        raise ValueError("Manifest count does not match file list")
    if type(data.get("total_bytes")) is not int or data["total_bytes"] != sum(item.size for item in items):
        raise ValueError("Manifest total_bytes does not match file list")
    return items


def check_file(sftp: paramiko.SFTPClient, root: str, item: MarkedFile) -> str | None:
    if posixpath.commonpath([root, item.path]) != root or item.path == root:
        return "outside configured remote directory"
    # Reject symlinks in every ancestor as well as the final path.
    current = "/"
    parts = item.path.lstrip("/").split("/")
    try:
        for index, part in enumerate(parts):
            current = posixpath.join(current, part)
            info = sftp.lstat(current)
            if info.st_mode is None:
                return "missing file type metadata"
            if index < len(parts) - 1:
                if not stat.S_ISDIR(info.st_mode):
                    return "ancestor is not a real directory (possibly a symlink)"
            else:
                if not stat.S_ISREG(info.st_mode):
                    return "not a regular file (possibly a symlink)"
                if (info.st_size, info.st_mtime) != (item.size, item.mtime):
                    return "size or modification time changed since review"
        if sftp.normalize(item.path) != item.path:
            return "canonical remote path changed"
    except OSError as error:
        if error.errno == errno.ENOENT:
            return "file or ancestor no longer exists"
        raise
    return None


def event(audit: TextIO, action: str, **fields: object) -> None:
    audit.write(json.dumps({
        "time": datetime.now(timezone.utc).isoformat(), "action": action, **fields,
    }) + "\n")
    audit.flush()
    os.fsync(audit.fileno())


def cleanup(options: Options, items: list[MarkedFile], delete: bool, audit: TextIO) -> int:
    mode = "DELETE" if delete else "DRY RUN"
    event(audit, "start", mode=mode, host=options.host, port=options.port,
          remote_path=options.remote_path, count=len(items),
          total_bytes=sum(item.size for item in items))
    removed = skipped = eligible = 0
    if items:
        with open_remote(options) as sftp:
            root = sftp.normalize(options.remote_path)
            if not root.startswith("/") or root.startswith("//") or posixpath.normpath(root) != root:
                raise ValueError("Server returned an invalid remote root")
            for item in items:
                reason = check_file(sftp, root, item)
                if reason is None and item.sha256 is not None:
                    reason = backup_check(sftp, root, item, options.local_path.resolve(strict=True))
                if reason:
                    skipped += 1
                    event(audit, "skipped", path=item.path, reason=reason)
                    print(f"SKIP {item.path!r}: {reason}", flush=True)
                    continue
                eligible += 1
                if delete:
                    event(audit, "delete_requested", path=item.path, url=item.url,
                          size=item.size, mtime=item.mtime, sha256=item.sha256,
                          local_path=item.local_path)
                    try:
                        sftp.remove(item.path)
                    except (OSError, paramiko.SSHException, EOFError) as error:
                        event(audit, "delete_failed_or_uncertain", path=item.path, error=str(error))
                        raise
                    removed += 1
                    event(audit, "deleted", path=item.path)
                    print(f"DELETED {item.path!r}", flush=True)
                else:
                    event(audit, "would_delete", path=item.path, size=item.size, mtime=item.mtime,
                          sha256=item.sha256, local_path=item.local_path)
                    print(f"WOULD DELETE {item.path!r} ({item.size} bytes)", flush=True)
    event(audit, "complete", deleted=removed, eligible=eligible, skipped=skipped)
    print(f"{mode}: {eligible} eligible, {removed} deleted, {skipped} skipped.", flush=True)
    return 1 if skipped else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False, allow_abbrev=False)
    parser.add_argument("--cleanup-help", action="help")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--scan-backed-up", type=Path, metavar="NEW_MANIFEST",
                        help="Verify matching local/remote media by SHA-256 and write a separate manifest")
    parser.add_argument("--delete", action="store_true", help="Permanently delete; otherwise dry run")
    parser.add_argument("--confirm-count", type=int, help="Required with --delete: exact manifest file count")
    parser.add_argument("--audit", type=Path, help="New audit JSONL file; must not already exist")
    args, connection_args = parser.parse_known_args(argv)
    audit = None
    try:
        options = parse_options(connection_args)
        if args.scan_backed_up is not None:
            if args.delete or args.confirm_count is not None or args.audit is not None:
                raise ValueError("--scan-backed-up cannot be combined with --delete, --confirm-count or --audit")
            with ssh_progress(debug=options.debug_ssh):
                scan_backed_up(options, args.scan_backed_up.expanduser())
            return 0
        items = load_manifest(args.manifest.expanduser(), options)
        if args.delete and args.confirm_count != len(items):
            raise ValueError(f"Permanent deletion requires --confirm-count {len(items)}")
        if not args.delete and args.confirm_count is not None:
            raise ValueError("--confirm-count is only valid with --delete")
        audit_path = args.audit or Path(f"cleanup-audit-{uuid.uuid4().hex}.jsonl")
        descriptor = os.open(audit_path.expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        audit = os.fdopen(descriptor, "w", encoding="utf-8")
        print(f"Audit: {audit_path}", flush=True)
        print(f"{len(items)} marked files, {sum(item.size for item in items)} bytes. "
              + ("PERMANENT deletion enabled." if args.delete else "Dry run; nothing will be removed."),
              flush=True)
        with ssh_progress(debug=options.debug_ssh):
            return cleanup(options, items, args.delete, audit)
    except (OSError, ValueError, paramiko.SSHException, EOFError, KeyboardInterrupt) as error:
        if audit is not None:
            try:
                event(audit, "aborted", error=str(error) or type(error).__name__)
            except OSError as audit_error:
                print(f"Error writing audit: {audit_error}", file=sys.stderr)
        print(f"Error: {error or type(error).__name__}. Cleanup stopped; check the audit for partial results.",
              file=sys.stderr)
        return 1
    finally:
        if audit is not None:
            audit.close()


if __name__ == "__main__":
    sys.exit(main())
