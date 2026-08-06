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
- On push it already holds the prepared bundle, with knowledge paths rewritten
  into portable form. Send its contents verbatim; do not rewrite files.
- On pull you must fill it. `manifest.json` is what the sync script checks for:
  if it is missing after `backend_pull`, the pull is treated as failed and
  nothing is applied locally.
- Return non-zero on failure. Push and pull both stop there.
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

See `backends/gdrive.sh`, `backends/s3.sh`, and `backends/rsync.sh` for complete
working examples.
