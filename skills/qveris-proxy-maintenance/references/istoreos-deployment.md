# iStoreOS Deployment Profile

Use this profile only after confirming the live deployment still matches it.

## Known Topology

- SSH alias: `private-blog` (`root@192.168.1.19`)
- Repository working directory:
  `/mnt/sata7-4/docker/codex-pulls/qveris-proxy`
- Compose project/service: `qveris-proxy` / `proxy`
- Container: `qveris-proxy-proxy-1`
- Image: `qveris-account-proxy:local`
- Published address: `0.0.0.0:18081 -> 8080`
- Compose files: `compose.yaml`, `compose.lite.yaml`, `compose.ui.yaml`
- Health URLs:
  `http://192.168.1.19:18081/health/live` and
  `http://192.168.1.19:18081/health/ready`
- The remote host has Docker Compose v2, `wget`, `curl`, and `tar`, but no Git.

Required persistent mounts:

| Host source | Container destination | Mode |
| --- | --- | --- |
| `<base>/config` | `/config` | read-write |
| `qveris-proxy_qveris_state` | `/data` | read-write |
| `<base>/proxy-account-secrets` | `/run/account-secrets` | read-only |
| `<base>/secrets` | `/run/secrets` | read-only |

Never add `compose.quickstart.yaml` to this deployment. It replaces the three
bind mounts with named volumes and makes the existing accounts appear missing.

## Read-Only Inspection

```powershell
ssh private-blog "docker inspect qveris-proxy-proxy-1 --format='{{.Image}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} {{.HostConfig.RestartPolicy.Name}}'"
ssh private-blog "docker inspect qveris-proxy-proxy-1 --format='{{range .Mounts}}{{println .Destination .Name .Source .RW}}{{end}}'"
ssh private-blog "docker compose version; df -h /mnt/sata7-4"
```

Use the iStoreOS Docker inspection tools when available. Inspect environment
variable names selectively; redact any name containing `KEY`, `TOKEN`,
`SECRET`, or `PASSWORD` before displaying output.

Create a private transaction manifest and record the account configuration
identity without reading it:

```sh
umask 077
mkdir -m 700 .release-backup-TRANSACTION
sha256sum config/accounts.json > .release-backup-TRANSACTION/accounts.sha256
wc -c < config/accounts.json > .release-backup-TRANSACTION/accounts.size
```

After deployment, use `sha256sum -c` and a numeric size comparison. Report only
pass/fail. SQLite state contains managed proxy-key hashes, limits, usage, and
browser claims, and can change during normal requests. Verify its unchanged
mount, existence, and `PRAGMA quick_check` result rather than hashing or dumping
application tables.

## Tested-Tree Transfer

Create a unique archive from the tested local commit and upload it. Do not
include the working tree, `.git`, local caches, or credentials.

```powershell
git status --short --branch
git archive --format=tar --output="$env:TEMP\qveris-update-COMMIT.tar" COMMIT
Get-FileHash -Algorithm SHA256 "$env:TEMP\qveris-update-COMMIT.tar"
scp "$env:TEMP\qveris-update-COMMIT.tar" private-blog:/tmp/qveris-update-COMMIT.tar
ssh private-blog "sha256sum /tmp/qveris-update-COMMIT.tar"
```

Extract first into a unique `.update-COMMIT-TIMESTAMP` directory. Verify
`compose.yaml`, `src/qveris_proxy/admin_assets/admin.js`, and the feature marker
for the release before changing the working directory.

## Compose Environment

Use the three-file overlay and preserve current non-secret behavior. The
standard environment for this host is:

```sh
QVP_SECRET_DIR=/mnt/sata7-4/docker/codex-pulls/qveris-proxy/secrets
QVP_ACCOUNT_SECRETS_DIR=/mnt/sata7-4/docker/codex-pulls/qveris-proxy/proxy-account-secrets
QVP_CONFIG_DIR=/mnt/sata7-4/docker/codex-pulls/qveris-proxy/config
QVP_BIND_ADDRESS=0.0.0.0
QVP_HOST_PORT=18081
QVP_ADMIN_FIRST_OPEN_CLAIM=true
QVP_ALLOW_API_KEY_FOR_OAUTH_ROUTES=true
QVP_DEFAULT_ACCOUNT=
```

Leave `QVP_DEFAULT_ACCOUNT` empty for dynamic round-robin default selection and
normal account deletion. Preserve an explicit value only when inspection shows
that the user intentionally relies on a locked default account.

Run `docker compose ... config --quiet`, then build before recreation:

```sh
docker compose -f compose.yaml -f compose.lite.yaml -f compose.ui.yaml build proxy
docker compose -f compose.yaml -f compose.lite.yaml -f compose.ui.yaml up -d --no-build --force-recreate proxy
```

Prefix both commands with the environment above or export it in the same SSH
session. Recheck image ID, health, mounts, logs, and restart policy afterward.

## Validation And Rollback

Validate without visiting `/admin/`:

```powershell
curl.exe -fsS http://192.168.1.19:18081/health/live
curl.exe -fsS http://192.168.1.19:18081/health/ready
curl.exe -fsS http://192.168.1.19:18081/admin/assets/admin.js
```

For rollback, extract `source-before-update.tgz` back into the active source
directory before pointing `qveris-account-proxy:local` at the rollback tag.
Recreate `proxy` with `--no-build --force-recreate` using the restored Compose
files and original environment. Confirm the account configuration hash, mounts,
and health before cleaning the failed image.

## Cleanup Allowlist

After successful validation, cleanup may include only:

- `/tmp/qveris-update-COMMIT.tar`
- the matching `.update-COMMIT-TIMESTAMP` staging directory
- enumerated `.release-backup-*` directories the user asked to remove
- enumerated obsolete `qveris-account-proxy` rollback tags not used by a
  container

Keep the active working directory, `config`, `secrets`,
`proxy-account-secrets`, `qveris-proxy_qveris_state`, and all other Docker
volumes. Do not remove OpenClash backups as part of proxy-source cleanup.

For standalone cleanup, list candidates with `ls -ld`, `du -sh`, and
`docker image ls qveris-account-proxy`. Before removing a tag, confirm no
container references its image ID with `docker ps -a --no-trunc`. Keep the
newest healthy rollback directory and image tag unless the user explicitly
requests zero retained rollback points. Re-list candidates and call both health
endpoints after deletion.
