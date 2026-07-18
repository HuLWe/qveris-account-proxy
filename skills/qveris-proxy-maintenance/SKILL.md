---
name: qveris-proxy-maintenance
description: >-
  Install, inspect, update, redeploy, validate, roll back, and clean QVeris
  Account Proxy Docker deployments while preserving accounts, proxy tokens,
  bind mounts, and named volumes. Use for QVeris Proxy requests such as
  更新容器, 部署最新版, 清理旧备份, 回滚, 检查运行状态, Agent 安装, or
  maintaining the existing iStoreOS deployment at 192.168.1.19.
---

# QVeris Proxy Maintenance

Maintain the proxy end to end and report only actions actually verified. Keep
credential values out of commands, logs, Git, and responses.

## Guardrails

- Do not enable shell tracing with `set -x`, print a complete `docker inspect`,
  or render a complete `docker compose config`. Use allowlisted inspect formats
  and `docker compose ... config --quiet`.
- Do not parse or display `.env`, account files, proxy tokens, OAuth/API keys,
  cookies, private keys, or secret mount contents. The account configuration
  may be streamed only into hash and byte-count tools for integrity checks;
  never send its bytes to the terminal or model output.
- Never run `down -v`, `docker volume prune`, `docker system prune -a`, or
  `git clean -fdx`. Treat `config`, `/data`, `/run/secrets`,
  `/run/account-secrets`, and their host sources as protected data.
- Use exact inspected paths for cleanup. Do not include unrelated OpenClash
  backups or other applications in QVeris Proxy cleanup.

## Choose The Deployment Path

- For a new user installation, follow the repository README and use
  `start.cmd` or `start.sh`. Let the user enter the first upstream API key in
  the script's hidden terminal prompt.
- For an existing installation, inspect its Compose project, mounts,
  environment names, restart policy, and health before changing anything.
- For the known iStoreOS host, read
  [references/istoreos-deployment.md](references/istoreos-deployment.md) before
  issuing remote commands. Its bind-mount deployment must not use the
  quick-start Compose overlay.

## Maintenance Workflow

1. Read applicable `AGENTS.md` files and task-related repository files.
2. Run `git status --short --branch` and inspect the deployment without
   modifying it. Resolve the requested release to an exact commit; use the
   current `origin/main` only when the user asked for the latest version.
   Separate observed facts, reasonable inferences, and items not yet verified.
3. Record the running image ID, container health, ports, restart policy, and
   exact mount sources. Capture the account configuration file's size and
   SHA-256 into a mode-0600 transaction file for a post-deploy equality check;
   do not print or parse its contents. Never read credential files or print
   secret-bearing environment values.
4. Make the smallest repository change and add tests proportional to its
   impact. Run JavaScript syntax checks for admin UI changes plus Ruff and the
   relevant pytest suite; run the full suite before release.
5. Package the exact tested Git tree with `git archive`. Compare its SHA-256 on
   the local and remote hosts before extracting it.
6. Before replacement, tag the running image with a unique rollback tag and
   create a source/Compose backup that excludes `config`, `secrets`,
   `proxy-account-secrets`, state volumes, and older backup directories. Record
   its exact path in the transaction manifest.
7. Extract into a staging directory first. Verify expected files and a release
   marker specific to the change, then update only application source and
   Compose files.
8. Validate the merged Compose configuration without printing it. Build the
   new image while the old container remains running, then recreate only the
   proxy service after the build succeeds.
9. Verify the new image ID, `healthy` state, restart policy, unchanged mounts,
   unchanged account-configuration hash, clean startup logs, `/health/live`,
   `/health/ready`, and a static UI marker. Do not open `/admin/` during
   unattended validation when first-browser claim may still be unused.
10. On failure, restore the backed-up source and Compose files first, retag the
    rollback image as `local`, recreate the same service with the same mounts
    and environment, and repeat configuration-hash and health checks. Do not
    use `down -v`, volume prune, or secret-file inspection.
11. After success, remove only update archives and staging directories created
    by this run. For a standalone cleanup request, first identify and confirm
    the host, active Compose working directory, and project from inspected
    container labels; do not infer a deletion target from a directory name
    alone. Enumerate exact paths, sizes, image IDs, tags, and container
    references, then retain the newest valid rollback transaction by default.
    Delete only specifically identified older items and rerun health checks.
12. If Git delivery was requested, review the final diff, rerun required tests,
    commit only task files, push the intended branch, and report the commit ID.

## New User Agent Install

Use the prompt in the repository README under `交给 Agent 安装`. Confirm the OS,
installation directory, localhost/LAN mode, port, Docker Engine, and Compose v2
first. If a Compose project with the same name already exists, ask whether to
reuse its volumes or choose a different `QVP_PROJECT_NAME`. On PowerShell,
include the current-directory prefix: `.\start.cmd` or `.\start.cmd -Lan`. On
macOS/Linux, use `bash ./start.sh` or `bash ./start.sh --lan`.

Treat the launcher's hidden API-key prompt, bootstrap ticket, and any proxy-key
retrieval command as user interactions. Do not capture their output. Verify the
container, health endpoints, and static `admin.js` afterward without opening
`/admin/` in an Agent-controlled browser; the user's browser performs first
claim.

## Completion Report

State what changed, how it was tested, deployed image/commit identity, retained
data resources, cleanup results, rollback availability, and any remaining
unverified item. Provide the management URL and API base URL without including
the proxy key.
