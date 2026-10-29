"""Find remote SFTP files absent from a local directory tree."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import os
import posixpath
import stat
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import paramiko


SSH_LOG = logging.getLogger("file_compare.ssh")


@contextmanager
def ssh_progress(debug: bool = False) -> Iterator[None]:
    handler = logging.StreamHandler(sys.stderr)
    level = logging.DEBUG if debug else logging.INFO
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s SSH: %(message)s", datefmt="%H:%M:%S",
    ))
    previous_level, previous_propagate = SSH_LOG.level, SSH_LOG.propagate
    SSH_LOG.setLevel(level)
    SSH_LOG.propagate = False
    SSH_LOG.addHandler(handler)
    try:
        yield
    finally:
        SSH_LOG.removeHandler(handler)
        SSH_LOG.setLevel(previous_level)
        SSH_LOG.propagate = previous_propagate
        handler.close()


@dataclass(frozen=True)
class Options:
    host: str
    remote_path: str
    local_path: Path
    port: int = 22
    username: str | None = None
    key_filename: Path | None = None
    known_hosts: Path | None = None
    output: Path = Path("missing-files.txt")
    filename_only: bool = False
    timeout: float = 30.0
    debug_ssh: bool = False


def parse_options(argv: Sequence[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="JSON configuration file")
    parser.add_argument("--host", help="SSH hostname or IP address (not a URL)")
    parser.add_argument("--port", type=int, help="SSH port (default: 22)")
    parser.add_argument("--username", help="SSH username (default: current user)")
    parser.add_argument("--remote-path", help="Remote directory to scan recursively")
    parser.add_argument("--local-path", help="Local directory to scan recursively")
    parser.add_argument("--output", help="Output text file (default: missing-files.txt)")
    parser.add_argument("--key-filename", help="Private SSH key; otherwise use agent/default keys")
    parser.add_argument("--known-hosts", help="Additional OpenSSH known_hosts file")
    parser.add_argument("--timeout", type=float, help="Connection and I/O timeout in seconds")
    parser.add_argument(
        "--debug-ssh", action=argparse.BooleanOptionalAction, default=None,
        help="Enable DEBUG SSH output (default: disabled)",
    )
    parser.add_argument(
        "--filename-only",
        "--ignore-extension-and-size",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Match only the filename without its last extension; ignore size",
    )
    args = vars(parser.parse_args(argv))
    config_path = args.pop("config")
    values: dict[str, object] = {}
    if config_path is not None:
        with config_path.expanduser().open(encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict):
            raise ValueError("Configuration must be a JSON object")
        unknown = config.keys() - Options.__dataclass_fields__.keys()
        if unknown:
            raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
        values.update(config)
    values.update({key: value for key, value in args.items() if value is not None})
    for key in ("host", "remote_path", "local_path"):
        if key not in values:
            parser.error(f"--{key.replace('_', '-')} is required (or set {key} in config)")
    text: dict[str, str] = {}
    for key in (
        "host", "remote_path", "local_path", "output", "username",
        "key_filename", "known_hosts",
    ):
        if key in values:
            value = values[key]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be a non-empty string")
            text[key] = value
    host = text["host"]
    if any(char in host for char in "/@?#") or any(char.isspace() for char in host):
        raise ValueError("host must be a hostname or IP address, not a URL")
    if ":" in host:
        ipaddress.IPv6Address(host)
    port = values.get("port", 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    timeout = values.get("timeout", 30.0)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number")
    if "filename_only" in values and type(values["filename_only"]) is not bool:
        raise ValueError("filename_only must be a JSON boolean")
    if "debug_ssh" in values and type(values["debug_ssh"]) is not bool:
        raise ValueError("debug_ssh must be a JSON boolean")
    paths = {
        key: Path(text[key]).expanduser()
        for key in ("local_path", "output", "key_filename", "known_hosts")
        if key in text
    }
    return Options(
        host=host, remote_path=text["remote_path"], local_path=paths["local_path"],
        port=port, username=text.get("username"), key_filename=paths.get("key_filename"),
        known_hosts=paths.get("known_hosts"), output=paths.get("output", Path("missing-files.txt")),
        filename_only=values.get("filename_only") is True, timeout=float(timeout),
        debug_ssh=values.get("debug_ssh") is True,
    )


def match_key(name: str, size: int, filename_only: bool) -> tuple[str, int | None]:
    if filename_only:
        return PurePosixPath(name).stem, None
    return name, size


def local_index(
    root: Path, filename_only: bool, output: Path
) -> set[tuple[str, int | None]]:
    if not root.is_dir():
        raise ValueError(f"Local path is not a directory: {root}")
    excluded = output.resolve()
    keys: set[tuple[str, int | None]] = set()
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif (
                    entry.is_file(follow_symlinks=False)
                    and Path(entry.path).resolve() != excluded
                ):
                    keys.add(match_key(
                        entry.name, entry.stat(follow_symlinks=False).st_size,
                        filename_only,
                    ))
    return keys


def remote_files(sftp: paramiko.SFTPClient, root: str) -> Iterator[tuple[str, int]]:
    root = sftp.normalize(root)
    mode = sftp.lstat(root).st_mode
    if mode is None or not stat.S_ISDIR(mode):
        raise ValueError(f"Remote path is not a directory: {root}")
    pending = [root]
    while pending:
        directory = pending.pop()
        # Drain pipelined directory responses before callers issue other SFTP requests.
        for entry in list(sftp.listdir_iter(directory)):
            if entry.filename in (".", "..") or "/" in entry.filename:
                raise ValueError(f"Invalid SFTP directory entry: {entry.filename!r}")
            path = posixpath.join(directory, entry.filename)
            if entry.st_mode is None:
                raise ValueError(f"Server did not return a file type for {path}")
            if stat.S_ISDIR(entry.st_mode):
                pending.append(path)
            elif stat.S_ISREG(entry.st_mode):
                if entry.st_size is None or entry.st_size < 0:
                    raise ValueError(f"Server did not return a valid file size for {path}")
                yield path, entry.st_size


def remote_url(host: str, port: int, path: str) -> str:
    authority = f"[{host}]" if ":" in host else host
    if port != 22:
        authority += f":{port}"
    return f"sftp://{authority}/{quote(path.lstrip('/'), safe='/')}"


def write_report(output: Path, urls: list[str]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n",
            dir=output.parent, prefix=f".{output.name}.", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            for url in sorted(urls):
                handle.write(url + "\n")
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def open_remote(options: Options) -> Iterator[paramiko.SFTPClient]:
    with paramiko.SSHClient() as client:
        client.set_log_channel(SSH_LOG.name)
        SSH_LOG.info("Loading trusted SSH host keys")
        client.load_system_host_keys()
        if options.known_hosts is not None:
            client.load_host_keys(str(options.known_hosts))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        SSH_LOG.info(
            "Connecting to %s:%d (timeout: %g seconds); "
            "resolving host / establishing TCP, then SSH handshake and authentication",
            options.host, options.port, options.timeout,
        )
        client.connect(
            hostname=options.host, port=options.port, username=options.username,
            key_filename=str(options.key_filename) if options.key_filename else None,
            timeout=options.timeout, auth_timeout=options.timeout,
            banner_timeout=options.timeout, channel_timeout=options.timeout,
            allow_agent=True, look_for_keys=True,
        )
        SSH_LOG.info("SSH handshake, host-key verification and authentication complete")
        SSH_LOG.info("Opening SFTP session")
        with client.open_sftp() as sftp:
            channel = sftp.get_channel()
            if channel is None:
                raise paramiko.SSHException("Server did not open an SFTP channel")
            channel.settimeout(options.timeout)
            SSH_LOG.info("SFTP session ready")
            yield sftp


def compare(options: Options) -> tuple[int, int]:
    SSH_LOG.info("Scanning local directory before connecting: %s", options.local_path)
    local = local_index(options.local_path, options.filename_only, options.output)
    SSH_LOG.info("Local scan complete: %d comparison keys", len(local))
    missing: list[str] = []
    scanned = 0
    with open_remote(options) as sftp:
        SSH_LOG.info("Scanning remote directory: %s", options.remote_path)
        for path, size in remote_files(sftp, options.remote_path):
            scanned += 1
            key = match_key(posixpath.basename(path), size, options.filename_only)
            if key not in local:
                missing.append(remote_url(options.host, options.port, path))
    write_report(options.output, missing)
    return scanned, len(missing)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        options = parse_options(argv)
        with ssh_progress(debug=options.debug_ssh):
            scanned, missing = compare(options)
    except (OSError, ValueError, paramiko.SSHException, EOFError) as error:
        print(f"Error: {error or type(error).__name__}", file=sys.stderr)
        return 1
    print(f"Scanned {scanned} remote files; {missing} missing. Report: {options.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
