# Quick Start Guide

Get KiroCrew syncing between your laptops in 5 minutes.

## Step 1: Install rclone

**macOS:**
```bash
brew install rclone
```

**Linux:**
```bash
curl https://rclone.org/install.sh | sudo bash
```

## Step 2: Clone and initialize

```bash
git clone https://github.com/pawelgradziel/kirocrew-sync.git
cd kirocrew-sync
./kirocrew-sync.sh init
```

## Step 3: Configure Google Drive

```bash
rclone config
```

- Choose: `n` (New remote)
- Name: `kirocrew-gdrive`
- Type: `drive` (Google Drive)
- Scope: `drive` (Full access)
- Client ID/Secret: Leave blank (or add your own - see README)
- Complete browser authorization

## Step 4: First sync

**On your current laptop:**
```bash
./kirocrew-sync.sh push
```

**On your other laptop:**
```bash
git clone https://github.com/pawelgradziel/kirocrew-sync.git
cd kirocrew-sync
./kirocrew-sync.sh init
rclone config  # Use the SAME settings as above
./kirocrew-sync.sh pull
```

## Done! 

Your KiroCrew data is now synced. Use `push` and `pull` commands to keep laptops in sync.

## Daily Usage

```bash
# Start of day on laptop B (get latest from laptop A)
./kirocrew-sync.sh pull

# End of day on laptop B (share with laptop A)  
./kirocrew-sync.sh push
```

## What's Synced

✅ Chat history  
✅ Knowledge base  
✅ Artifacts  
✅ Memory & search indexes  
✅ Learned lessons  
✅ Configuration  

## Troubleshooting

**"KiroCrew is running" error:**
```bash
pkill -f kirocrew
```

**Check what will sync:**
```bash
./kirocrew-sync.sh status
```

**Test Google Drive connection:**
```bash
rclone lsd kirocrew-gdrive:
```

See [README.md](README.md) for full documentation, other storage backends (S3, rsync), and advanced usage.
