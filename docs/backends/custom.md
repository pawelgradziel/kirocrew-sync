# Custom Backends

A backend is a single bash file that moves one directory — the sync bundle —
between this machine and wherever you want it stored. Anything you can script
works: Dropbox, OneDrive, WebDAV, a USB drive, an encrypted archive.

## The contract

Create `backends/your-backend.sh` and implement four functions:

```bash
backend_push() {
    local bundle_dir="$1"
    # Upload bundle_dir contents to your storage
}

backend_pull() {
    local bundle_dir="$1"
    # Download from your storage into bundle_dir
}

backend_status() {
    # Show backend status and configuration
}

backend_list() {
    # Print a cheap, stable fingerprint of remote state; see below
}
```

Then use it:

```bash
SYNC_BACKEND=your-backend ./kirocrew-sync.sh push
```

The file is sourced by `kirocrew-sync.sh`, so `log_info`, `log_success`,
`log_warn`, and `log_error` are available to you, and any variable you define at
the top of the file becomes backend configuration.

## What you can rely on

- `bundle_dir` is a fresh `mktemp -d` directory, removed after the command
  finishes. Do not cache anything there between runs.
- On push it holds the prepared transport payload: one git bundle per machine
  under `bundles/<machine-id>.bundle` (and any other machines' bundles the
  script re-pulled so a delete-mirroring transport does not drop them). Send
  its contents verbatim; do not rewrite files.
- On pull, fill `bundle_dir` with whatever is on the remote (typically the
  `bundles/` tree). An empty remote is fine — the sync script treats a failed
  or empty pull as "nothing to merge" and still packs and publishes local
  state.
- `backend_status` should report reachability and list machine bundles under
  `bundles/*.bundle` when present (see `backends/local.sh`).
- Return non-zero on failure. Push stops there; pull failure only warns.
- Exclude `*.lock` and `*.tmp` if your transport can, matching the other
  backends.

### `backend_list`: the daemon's cheap change-detection fingerprint

`lib/daemon.sh`'s background daemon polls `backend_list` on every tick (as
often as every 30 seconds) instead of running a full sync, to decide
whether anything is even worth syncing. It compares your output byte-for-
byte against what it saved last time, so the contract is:

- **Print a stable listing of the remote's `bundles/*.bundle` files** --
  one line per bundle, e.g. `name size mtime`, **sorted deterministically**
  (`LC_ALL=C sort`, not raw directory order, which the filesystem/API does
  not guarantee is stable between calls).
- Use data that only changes when the *content* changes: the bundle's own
  size and modification time (as reported by the remote), never anything
  derived from the time of the current query -- a query timestamp in the
  fingerprint would make every single poll look like a change and defeat
  the entire point.
- **A reachable remote with nothing published yet is not the same as an
  unreachable one.** Print a fixed, non-empty sentinel (the bundled
  backends use the literal string `EMPTY`) for "reachable, zero bundles" --
  this is the normal state before any machine's first push. Conflating it
  with "unreachable" recreates, in a subtler form, the exact bug this
  contract exists to prevent: see `backends/local.sh`'s `backend_list` for
  the reasoning in full.
- **On an unreachable remote (offline, bad auth, missing tool), print
  nothing and return non-zero.** `check_remote_changed()` in
  `lib/daemon.sh` treats empty output as "cannot reach backend" and backs
  off, rather than treating a transient failure as "nothing changed".
- Must be cheap and must not mutate the remote -- no uploads, no deletes,
  ideally a single lightweight list/stat call. Do not call your
  `check_*_configured`-style helpers that print multi-line setup
  instructions and `exit`: that output would become part of the
  fingerprint, and the `exit` bypasses the non-zero-return contract this
  function needs. Use the same lightweight tool-presence check
  (`command -v your-tool`) those helpers use, but return, don't exit.

See `backends/local.sh`, `backends/gdrive.sh`, `backends/s3.sh`, and
`backends/rsync.sh` for four different transports implementing the same
contract, and `tests/test_backend_local_list.sh` for a test of it.

## Configuration

Follow the pattern the bundled backends use, so settings can come from
`config.sh` without editing the backend file:

```bash
MY_TARGET="${MY_TARGET:-some-default}"
```

Also add a reachability check that runs before push and pull, and fails with
instructions rather than a raw transport error — see `check_gdrive_configured`
in `backends/gdrive.sh` for the shape.

See `backends/gdrive.sh`, `backends/s3.sh`, `backends/rsync.sh`, and
`backends/local.sh` for complete working examples.
