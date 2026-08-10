# Custom Backends

A backend is a single bash file that moves one directory — the sync bundle —
between this machine and wherever you want it stored. Anything you can script
works: Dropbox, OneDrive, WebDAV, a USB drive, an encrypted archive.

## The contract

Create `backends/your-backend.sh` and implement three functions:

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
