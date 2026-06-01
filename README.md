# Vizo

Vizo is a local multi-agent workflow system for coordinating coding tasks, model routing, task recovery, Web Console control, and optional WeCom notifications.

This public repository is generated from a private working repository through a whitelist sync process. Runtime state, local memories, private project checkouts, tool caches, real configuration files, logs, screenshots, and historical planning archives are intentionally excluded.

## What Is Included

- Core Python runtime and CLI entry points.
- Built-in hooks, role templates, and built-in agent definitions.
- Docker and example configuration files.
- Focused user and deployment documentation.
- Regression tests that are safe to publish.
- `PUBLIC_SYNC_MANIFEST.txt` and `scripts/sync_public_repo.py`, which define and maintain the public repository boundary.

## Quick Start

1. Create a Python environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy the example configuration and fill in your own values:

   ```bash
   cp config.json.example config.json
   cp .env.example .env
   ```

4. Start from the user manual:

   ```bash
   less USER-MANUAL.md
   ```

## Syncing From The Private Repo

From the private working repository, run:

```bash
python3 scripts/sync_public_repo.py --target "$HOME/vizo-public" --init-git
```

Use `--dry-run` first if you want to preview the changes:

```bash
python3 scripts/sync_public_repo.py --target "$HOME/vizo-public" --dry-run
```

The sync is whitelist-based. Files not listed in `PUBLIC_SYNC_MANIFEST.txt` are removed from the public repo, except for `.git`.

## Before Publishing

Choose and add a license before publishing on GitHub. Without a license, other people can view the code but do not receive clear reuse rights.
