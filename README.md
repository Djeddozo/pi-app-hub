# Pi App Hub

## Overview for reviewers

**Problem.** My Raspberry Pi runs a dozen self-built apps as Docker Compose projects and systemd
services. Checking, restarting or removing one meant SSH and remembering unit names — and a careless
remove could delete data.

**What I built.** A private, installable dashboard (FastAPI + React/TypeScript PWA) that
*discovers* every app automatically (Compose projects, custom system and user systemd units, manual
JSON manifests), shows live CPU/RAM/uptime, and lets me start, stop, restart or uninstall from my
phone. A storage view explains disk use and asks Claude Code (headless, no tools) for cleanup advice.

**Engineering highlights.**
- Provider-based discovery with reconciliation: missing apps are marked, not deleted; manual
  overrides survive rescans; provider failures keep last-known state.
- A deliberately narrow privileged boundary: the backend runs unprivileged; one root-owned helper
  accepts only fixed verbs on validated, root-owned custom unit files and is the only sudoers entry.
- Destructive actions need a short-lived, single-use preview token plus the exact app name, and
  there is intentionally **no data-deletion path** (volumes, bind mounts and code always remain).
- Cross-origin protection (exact Host/Origin, custom header), secret masking in logs, protected
  core services (Tailscale, Docker, SSH, networking), audit log of every administrative action.

**Tests.** 54 backend tests (pytest) plus Playwright browser tests in `frontend/`.
**My role.** Requirements, safety model and deployment; implementation with AI coding agents.
**Status.** Running on my Pi as `pi-app-hub.service`.

---

A lightweight FastAPI + React/TypeScript dashboard for a Raspberry Pi. The backend is hard-bound to **127.0.0.1:8840**. Remote access uses a dedicated **Tailscale Serve HTTPS port 8840**, without Funnel or public reverse proxies.

## Installed on this Pi

- Private URL: `https://your-pi.your-tailnet.ts.net:8840`
- Service: `pi-app-hub.service`
- Installed code: `/opt/pi-app-hub`
- Editable source: `/home/djeddozo/pi-app-hub`
- Registry and manifests: `/var/lib/pi-app-hub/data/registry.sqlite3`, `/var/lib/pi-app-hub/data/manifests/`
- Archived service units: `/var/lib/pi-app-hub/unit-backups/`
- Installer configuration backups: `/var/lib/pi-app-hub/install-backups/`

Existing Tailscale routes on 443, 8443 and 8444 are preserved. Existing Docker applications, volumes, networks and services are not reconfigured by installation.

## Android installation

1. Connect Tailscale on your Android phone.
2. Open the private HTTPS URL in Chrome.
3. Tap **Install app**, or Chrome's **⋮ → Add to Home screen → Install**.

The installed app has its own icon and opens without browser chrome. Tailscale must stay connected to reach the Pi. Only icons and a generic offline page are cached; API responses, logs and administrative data are never cached by the service worker. Browser installation eligibility varies by browser; an Add to Home screen shortcut is also usable.

## Build and install

Requires Python 3.11+, Node 20.19+, Docker, systemd, Tailscale and an existing non-root account in the Docker group. SQLite is bundled with Python. On Raspberry Pi OS, the account normally already has journal visibility; the service explicitly adds `systemd-journal` membership for its process.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
cd frontend
npm ci
npm run build
cd ..
.venv/bin/pytest -q
sudo python3 deploy/install.py --user djeddozo
```

PNG icons are included in source. To regenerate them from `frontend/public/icon.svg`, run `node render-icons.mjs` from `frontend/` with Chromium installed, then rebuild.

The installer copies code to `/opt/pi-app-hub`, installs pinned Python dependencies, creates its own service, installs a restricted systemd helper and adds only the private port 8840 route. It saves existing configuration and refuses a conflicting route or Funnel-enabled port. Rerun after rebuilding to deploy source updates. Use `--no-tailscale` for a local-only installation.

```bash
systemctl status pi-app-hub
journalctl -u pi-app-hub --no-pager -n 100
tailscale serve status
```

The application URL is not a public link. Tailnet access policy determines who can reach it; everyone allowed to reach this dashboard has administrative app permissions. No separate password is configured.

## Application discovery

- Docker containers are grouped by `com.docker.compose.project`. Standalone containers remain separate apps.
- Custom, regular service files directly under `/etc/systemd/system` and the current user's `~/.config/systemd/user` are discovered. Distribution services, symlink aliases, template units and ordinary processes are excluded. User units use the explicit `systemd-user` source (for example, the existing HelloAsso Ticket Watcher) and can be started, stopped or restarted; Pi App Hub does not remove user unit files from the dashboard.
- Manual JSON manifests are watched in the manifests directory. The UI can create these registrations.
- Docker events trigger rescans, as do manifest changes. Every 60 seconds discovery runs as a fallback. Resource samples and the UI refresh every 5 seconds; systemd app counters refresh on discovery.
- Successful discovery marks absent apps **Missing** without deleting metadata. Provider failures retain last-known state and show the source offline. A return from Missing restores the app automatically.
- SQLite holds discovered data separately from manual overrides. Scans preserve edits. Runtime identifiers are immutable through the API.
- The scan summary counts new apps, changed metadata/status/membership and newly missing apps, excluding normal uptime/resource changes.

Example manual manifest:

```json
{
  "id": "my-tool",
  "name": "My Tool",
  "icon": "code",
  "project_path": "/home/djeddozo/my-tool",
  "web_url": "https://your-pi.your-tailnet.ts.net:9000",
  "data_directories": ["/home/djeddozo/my-tool/data"],
  "protected": false
}
```

Optional `pid` and `process_started_at` (Unix seconds from `psutil.Process(pid).create_time()`) enable monitoring of a manual process. Both must match to avoid PID reuse. A URL alone does not prove an app is running. Manual apps are observation-only: lifecycle management requires a Docker container or custom systemd service, so no arbitrary command execution is introduced. A manual uninstall hides its registration and preserves the process, files and manifest.

Supported icon names: `box`, `wallet`, `activity`, `calendar`, `car`, `message`, `database`, `grid`, `globe`, `code`. Docker labels `pi-app-hub.name`, `pi-app-hub.url` and `pi-app-hub.protected=true` can supply metadata. Health check results from Docker are displayed; editable health-check text is descriptive and never executed.

## Management and removal

Start, stop and restart act only on freshly rediscovered identifiers. Compose member start order follows its dependency labels; stop order is reversed. The dashboard manages existing containers through the Docker API; it does not recreate removed containers or run `compose up`, builds or migrations.

Uninstall always requires a short-lived, single-use preview token and exact app name. Changed membership, metadata or unit content requires another preview. There is deliberately no data-deletion option:

- **Docker:** stop and remove exactly the listed containers, with volume removal disabled. Container writable layers are lost (explicitly warned in the preview). Bind mounts, volumes, images, networks, Compose files and project directories remain.
- **systemd:** stop/disable the listed unit and move its root-owned unit file into the backup directory. Dependent services and activation triggers block removal. Drop-ins, application code and data remain. An administrator can restore the archived unit file, reload systemd and enable it again.
- **Manual:** unregister from the dashboard only.

Removed entries remain as hidden registry tombstones so preserved manifests do not immediately re-register themselves. Register a manual app under a new ID to re-add it. To restore a deliberately uninstalled Docker project under the same project name, an administrator can clear its registry `removed` flag after redeploying it. There is no automatic filesystem cleanup.

## Storage

**Storage** (`/system/storage`, `hub/storage.py`) shows disk, RAM and swap separately, an expandable folder tree with sizes for `~`, `/var`, `/opt` and `/usr`, suggestions, and advice from Claude.

- Metadata only: sizes and paths are scanned with `du -x`; file contents are never read and symbolic links are never followed.
- Advice comes from the owner's existing Claude Code login (`claude -p`, no tools, no MCP servers, no API key, empty working folder). Claude cannot change anything. A scan runs every 6 hours; Claude is asked automatically at most once a day, or on demand with **Ask Claude**.
- Only allowlisted download caches (pip HTTP/wheels, npm `_cacache`) can be deleted, after a preview and typing `DELETE CACHES`. The approval expires after 5 minutes, works once, and is void if the cache changed after the preview. Deletion is fd-relative and never follows symlinks. Everything else is review-only.
- Cleanup and Claude requests appear in **Activity**; cleanup history and the last scan are kept in `storage.json` in the data directory.
- The service unit grants write access only to those caches and to `~/.claude`/`~/.claude.json` (for the Claude login); the rest of the home folder stays read-only.

## Security boundaries

- Loopback-only listener; fixed application entrypoint and a private Tailscale Serve route.
- Exact Host/Origin validation plus a required custom header on mutations prevents browser cross-origin management. No CORS grant, shell endpoint, custom command fields or environment dump.
- HTTP(S) URLs without embedded credentials, validated service names/full Docker IDs, validated absolute metadata paths. Metadata paths are never used for filesystem removal.
- Pi App Hub, Tailscale, Docker, SSH, systemd and networking components are protected. Discovered protection cannot be overridden through metadata edits. The independent root helper repeats service protection and filesystem checks.
- Backend runs as the existing non-root Docker account. Docker socket access is inherently powerful. The installed root-owned helper accepts only fixed verbs against root-owned custom service files; it rejects symlinks, writable units, protected names and unexpected arguments. Only this helper is added to sudoers.
- The service has a read-only system/home filesystem except the registry area and `/etc/systemd/system` (needed by the helper; ordinary UNIX permissions still prevent non-root writes).
- Administrative attempts and outcomes are recorded in SQLite, with the newest 10,000 events retained and the latest 200 shown. Failed partial operations are reported; rescan before retrying.
- Logs mask recognizable secret fields, bearer tokens, credentials in URLs and environment assignments. Arbitrary unlabeled secrets cannot be guaranteed detectable. Process view shows executable names, not command arguments or environments.
- No remote fonts, telemetry or third-party frontend requests.

CPU percentages can exceed 100% for multithreaded apps. On Pi kernels without memory cgroup accounting, container RAM is summed process RSS (shared pages may be counted more than once). The host RAM tile reports actual system memory. No boot/kernel settings are modified.

## Architecture and tests

`hub/system.py` samples host stats and private routes. `hub/discovery.py` defines independent providers. `hub/registry.py` reconciles snapshots and preserves metadata. `hub/manager.py` coordinates watchers, events, metrics and actions. `hub/main.py` supplies validated APIs and serves the built React app. `deploy/service-helper.py` is an independently validated privileged boundary. New discovery sources can implement `source` and `discover()` and be added to the provider list.

Backend tests cover grouping, missing/return semantics, persistent overrides, provider failure, path/ID/URL validation, critical protection, cross-origin requests, audit entries, dependency ordering and safe uninstall. Browser tests cover live discovery, navigation, scan results, Android layout, PWA assets and offline isolation.

```bash
.venv/bin/pytest -q
cd frontend
npm test
# Verify the deployed private URL:
HUB_URL=https://your-pi.your-tailnet.ts.net:8840 npm test
```

Official references: [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve), [FastAPI lifecycle](https://fastapi.tiangolo.com/advanced/events/), [Docker SDK containers](https://docker-py.readthedocs.io/en/stable/containers.html).
