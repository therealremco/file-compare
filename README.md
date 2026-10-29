# File Compare

A set of Python 3.10+ command-line tools for managing copies of files on different machines:

**`file_compare.py`**

- Recursively lists a remote SFTP directory and reports files not found anywhere in a local directory tree. It reads names and metadata only; it does not download, modify, or delete remote files.

**`review.py`**

- Runs an image browser with thumbnails, choose whether the file should be kept or deleted.

**`cleanup.py`**

- Dry run to determine which files are elligible for deletion, a second run to actually delete, optional runs to delete files from the server that are definitely present on the local host.

## Starting With the Why

I created this tool because I have been generatng images and videos with ComfyUI, the desktop app on my laptop and Comfy running as a server on my NVIDIA GB10 workstation. On my laptop I organise images and videos as projects, Comfy also stores all generated output in `/output`. Instea of just deleting everything there, I wanted to check if I had forgotten to download anything that I wanted to keep on my laptop (with cloud/NAS backup). Maybe it's overkill, but hopefully it will come in handy now and then and perhaps there are other uses I haven't thought of yet. Of course if you do find an orphaned file, don't forget to copy it from the server before deleting anything!

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Authentication uses your SSH agent, default SSH keys, or `--key-filename`.
For an encrypted key, first load it with `ssh-add`. Password authentication and
interactive prompts are not supported. The SSH username defaults to the current
local user.

Host keys **must already be trusted** in `~/.ssh/known_hosts`, or in an additional
file supplied using `--known-hosts`. Verify the server's fingerprint through a
trusted channel before adding it. Unknown or changed host keys cause an error;
the tool never automatically trusts them.

## Usage

```sh
python file_compare.py \
  --host files.example.com \
  --username alice \
  --remote-path /srv/files \
  --local-path /Users/alice/Downloads/files \
  --output missing-files.txt
```

`--host` accepts a hostname or IP address, not a URL. Use `--port` for a non-default
SSH port. SSH config aliases, ProxyJump, and settings from `~/.ssh/config` are not
read; supply connection settings explicitly.

By default, files match when their **complete filename (including extension) and
size in bytes** match, regardless of directory structure. This is a metadata
comparison, not a content/hash comparison. One local match satisfies every remote
file with the same matching key; duplicate counts are not compared. Local-only
files never appear in the report.

To ignore both extension and size:

```sh
python file_compare.py --config config.json --filename-only
```

`--ignore-extension-and-size` is an alias for `--filename-only`. Only the **last**
extension is removed: `photo.jpg` matches `photo.png`, while `archive.tar.gz`
has the comparison name `archive.tar`. Dotfiles such as `.env` retain their name.
Names are case-sensitive and no Unicode normalization is applied.

Both trees are scanned recursively, including hidden files. Symlinks and special
files are skipped to avoid cycles and escaping the selected directories. Use real
directories as scan roots. The output file itself is excluded from local matches.
Scans are not snapshots: run against trees that are not being modified.

## Configuration and precedence

Copy [config.example.json](config.example.json) to `config.json` and edit it:

```sh
cp config.example.json config.json
python file_compare.py --config config.json
python file_compare.py --config config.json --local-path /another/folder --port 2222
python file_compare.py --config config.json --no-filename-only
```

Precedence is **explicit CLI option > JSON configuration > built-in default**.
The negative flag `--no-filename-only` overrides a configured `true`.

Configuration keys correspond to CLI options, using underscores:

| Key | Default |
| --- | --- |
| `host` | Required |
| `remote_path` | Required |
| `local_path` | Required |
| `port` | `22` |
| `username` | Current local user |
| `key_filename` | SSH agent / default keys |
| `known_hosts` | Standard known_hosts only |
| `output` | `missing-files.txt` |
| `filename_only` | `false` |
| `timeout` | `30` seconds for connection and individual I/O operations |
| `debug_ssh` | `false`; enable DEBUG SSH logging in all commands |

Omit optional keys instead of setting them to `null`. Unknown keys and invalid
values are errors. All relative local paths (including those in the configuration)
are relative to the **current working directory**, not the configuration file.
`~` is expanded in local paths. Remote paths are interpreted by the SFTP server;
relative paths use the login directory, and `~` is not expanded.

## Report and errors

Timestamped connection progress is printed immediately to **stderr** on every run.
By default this includes INFO progress, warnings, and errors. Set `"debug_ssh": true`
or pass `--debug-ssh` for SSH client/server banners, key-exchange negotiation, and
detailed authentication attempts. Pass `--no-debug-ssh` to override a configured
`true`. This applies to comparison, review (including preview downloads), and
cleanup. A local-scan message
also distinguishes time spent indexing local files from time spent connecting.
If a connection times out, the last message helps identify the stage reached;
timeouts and host-key verification behavior are unchanged.

To save the diagnostics while keeping the report separate:

```sh
python file_compare.py --config config.json --debug-ssh 2>ssh-progress.log
```

Logs can include hostnames, usernames, key fingerprints, and local paths (not
private key contents). Review them before sharing. A longer timeout can be set
with `--timeout 60` or `"timeout": 60` in the configuration.

The UTF-8 report contains one sorted SFTP URL per missing remote file, using its
actual absolute remote path and original filename/extension. Non-default ports
are included. Spaces, non-ASCII characters, and URL-special characters are
percent-encoded so each URL occupies exactly one line:

```text
sftp://files.example.com/srv/files/photos/holiday.jpg
sftp://files.example.com/srv/files/some%20document.pdf
```

An empty result produces an empty file. The report is replaced only after both
scans complete successfully; a scan, authentication, or permission error exits
nonzero without replacing a previous report. The output's parent directory must
already exist. A successful comparison exits `0`, even when files are missing.
Runtime/configuration errors exit `1`; command-line syntax errors exit `2`.

## Visual review gallery

Review the image/video URLs from an existing report in a local browser. This is a
**mark-only workflow**: the gallery has no remote rename, quarantine, or delete
operation. Exported manifests are for a separate, explicitly authorized cleanup.

Install the requirements again if upgrading an existing installation (Pillow is
used for image previews). For video thumbnails, install FFmpeg locally; on macOS:

```sh
brew install ffmpeg
```

Generate a report as usual, then start the gallery:

```sh
python file_compare.py --config config.json
python review.py --config config.json
```

Open the `http://127.0.0.1:...` URL printed by the review command. Keep that process
running while reviewing. Stop with Ctrl-C. It listens only on IPv4 loopback and
uses a randomly generated browser access token; do not share that URL.

All existing connection/configuration options work, including `--host`,
`--username`, and `--timeout`. Additional review CLI options:

| Option | Default |
| --- | --- |
| `--report` | Configured comparison `output` |
| `--review-dir` | `.review` in the current directory |
| `--listen-port` | An automatically selected free local port |
| `--review-help` | Show these review-specific options |

Review-specific options are CLI-only, not JSON configuration keys. Use different
review directories for separate review collections; importing a report replaces
the active collection, while retaining past decisions for unchanged URLs. Run
only one gallery process per review directory at a time.

### Categories and review controls

Before opening the gallery, the tool checks report entries against current remote
metadata and a fresh recursive local index:

- **Name absent locally:** no local file has the same exact basename, including
  extension.
- **Same name / different size:** a local file has the exact basename, but no file
  with that name has the remote file's size.
- Files now matching by name and size are excluded, as are non-media entries.

These categories always use exact, case-sensitive basenames, regardless of the
`filename_only` comparison option. To review all size differences, generate the
input report **without** filename-only matching:

```sh
python file_compare.py --config config.json --no-filename-only
```

Filter the gallery by category, decision, media type, or a filename/folder substring.
Click a thumbnail to open the full image or video player. Use the buttons or
keyboard shortcuts: left/right arrows to navigate, **K** to keep, **D** to mark for
deletion, **U** to reset to unreviewed, and Esc to close. Marking never changes the
remote file. The header shows category counts and total bytes marked for deletion.
The grid initially shows 60 entries; use Show more for the next batch.

Decisions are saved immediately in `.review/review.sqlite3` and survive restarts.
Reimporting a file whose recorded size or modification time changed resets its
decision to unreviewed. A failed import leaves the prior collection intact. A
missing/stale report entry is an explicit import error: regenerate the report.

### Previews and downloads

Thumbnails are generated on demand as cards approach the visible part of the page.
This implementation uses **SFTP only**: it downloads the original to the local
cache before generating a thumbnail. It does not execute FFmpeg or other commands
on the server. In particular, even a video thumbnail requires downloading the
whole clip once. Downloads are serialized to limit SSH connections. Cache hits
avoid reconnecting; opening full media reuses the downloaded file.

Images use Pillow; video thumbnails use local FFmpeg. Supported input extensions
are JPG/JPEG, PNG, WebP, GIF, BMP, TIFF, AVIF, MP4, MOV, M4V, WebM, MKV, and AVI.
Preview decoding depends on installed Pillow/FFmpeg capabilities, and video
playback depends on the browser's codec support. Unsupported or corrupt media
show an error; they are not silently omitted.

The cache lives under `.review/cache` and includes original private media, so
ensure sufficient disk space and protect that directory. Delete that specific
cache directory while the gallery is stopped to reclaim space; keep the SQLite
database to preserve decisions. File size and mtime are checked before and after
each new download. Cached media reflects the imported snapshot; restart review
to refresh metadata. Same-size/same-mtime content changes cannot be detected
without hashing. No remote files are written.

### Exporting marks safely

Click **Export deletion manifest** to download JSON containing all active marked
files (not just the filtered selection), exact SFTP URLs/paths, sizes, modification
times, categories, total count, and total bytes. No files are deleted by export.

### Permanently removing marked files

The gallery remains read-only. Use the separate cleanup command with the exported
manifest. Stop the gallery first and export fresh marks before cleanup. The
manifest is a snapshot: changing decisions in the gallery does not change an
already exported manifest.

First run a **dry run** (the default):

```sh
python cleanup.py --config config.json --manifest ~/Downloads/deletion-manifest.json
```

Inspect the `WOULD DELETE` paths and count, then explicitly enable permanent
deletion. Replace `42` with the exact total count in the manifest:

```sh
python cleanup.py --config config.json \
  --manifest ~/Downloads/deletion-manifest.json --delete --confirm-count 42
```

**This permanently removes remote files. There is no trash, quarantine, or undo.**
Keep backups if needed. No cleanup is performed simply by marking or exporting.
The cleanup command never recursively removes directories or touches local media.

It validates the entire manifest structure, count, byte total, duplicate paths,
and matching configured host/port before connecting. For each file it checks that
the path is beneath the configured remote directory, every path component is
non-symlink, the target is a regular file, and size/mtime still match the review.
Changed, missing, out-of-scope, and symlinked files are explicitly skipped.
Permission/network errors stop the run; earlier deletions cannot be rolled back.
Both dry runs and deletion return nonzero if any files are skipped or errors occur.

A new `cleanup-audit-<unique-id>.jsonl` file is written in the current directory
for every run, recording the mode, per-file results, and summary. Use `--audit PATH`
to choose a different **new** file; existing files are never overwritten.
Deletion intent is flushed to disk before each removal. If the connection fails
during removal, the outcome may be uncertain: inspect the server and audit before
retrying. Reruns safely skip files that are already missing.

Size/mtime checks cannot detect content replaced with identical metadata.
SFTP has no atomic "delete only if metadata still matches" operation: stop any
processes modifying these directories during cleanup to avoid a check/delete race.
The same limitation applies to symlink changes during a run. Do not use cleanup
against a concurrently modified or untrusted tree.

Connection settings and CLI-over-config precedence match the comparison tool.
All commands suppress DEBUG SSH messages by default, while retaining INFO progress,
warnings, and errors. Set `"debug_ssh": true` in the configuration or pass
`--debug-ssh` to enable detailed SSH diagnostics. `--no-debug-ssh` overrides a
configured `true`. Review uses the setting during both import and media downloads.

Use `--cleanup-help` to see cleanup-specific flags; these are CLI-only, not JSON
configuration keys. As with review, the existing configuration must contain
`local_path`, although cleanup does not scan or modify it.
After cleanup, regenerate the comparison report and restart the gallery to remove
deleted files from the active review list. Cached downloads and historical review
decisions are not removed automatically.

### Removing media already backed up locally

This is a separate, opt-in workflow, independent of gallery deletion marks.
First generate a **new** verified-backup manifest:

```sh
python cleanup.py --config config.json --scan-backed-up backed-up-media.json
```

The scan considers only the image/video extensions supported by the gallery,
matches **full filename including extension and byte size** anywhere in the
local tree, and then compares **SHA-256 of the entire local and remote contents**.
`filename_only` is deliberately ignored. Same name/size but different content is
not eligible. If several local files share a name and size, any identical copy
can satisfy the match. Local and remote symlinks are not eligible. The manifest
records the specific local copy, remote metadata, and hash. No files are removed
by scanning, and existing manifest files are never overwritten.

Hashing remote media streams all candidate contents over SFTP; this can take
significant time and network bandwidth. No originals are cached by this scan.
Read errors or changes detected while hashing abort manifest generation.

Inspect the manifest and run a dry run:

```sh
python cleanup.py --config config.json --manifest backed-up-media.json
```

To delete, use the same explicit confirmation as for gallery manifests, replacing
`42` with the exact manifest count:

```sh
python cleanup.py --config config.json --manifest backed-up-media.json \
  --delete --confirm-count 42
```

Both dry runs and deletion **rehash the recorded local copy and remote file**.
A disappeared, relocated, or changed local backup, or changed remote contents,
prevents that file's deletion. The recorded local path must still be a regular,
non-symlink file under the configured local directory. Cleanup never removes the
local copy. A relocated backup requires a new scan. Existing gallery manifests
retain their original mark-based behavior; they do not imply a verified backup.

Stop writers on **both trees** during scanning and cleanup. As with ordinary
cleanup, checks and deletion are not atomic; SFTP cannot eliminate a race with
concurrent writers. A readable local copy is not a guarantee of durable backup:
ensure the local storage is reliable before permanently deleting the remote copy.
An inaccessible backup mount or permission/network error stops cleanup. Review
the per-file audit for partial results.

## Tests

```sh
python -m unittest discover -s tests -v
```

## Acknowledgements

- The code was hammered out by GPT-6.1 Sol in Microsoft Visual Studio Code/Copilot.
- Thanks to Comfy and all those souls in the OSS community making many amazing things possible.

## License

Copyright (c) 2026 Remco van Schoonderwoerd den Bezemer, Whisker Software.

This project is licensed under the [MIT License](LICENSE). You may use, modify,
and redistribute it, including commercially, provided you retain the copyright
and permission notices as required by the license. The software is provided
without warranty; see the license for the full terms.

If you use or adapt this project, a link back to this repository is appreciated!