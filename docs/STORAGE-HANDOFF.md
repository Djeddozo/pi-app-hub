# Storage page work log (formerly "Pi Space")

Updated 2026-10-13. Built in source, NOT yet deployed to /opt/pi-app-hub.

## Background
- Started by Codex as `disk_manager/` inside hq-system; Djeddi clarified it belongs in Pi App Hub. Ported here; the HQ copy was removed.
- Requirements: folder tree with sizes, automatic suggestions, storage/RAM advice through the existing Claude login,
  cleanup only after explicit user approval (never automatic deletion from AI advice).
- Earlier emergency cleanup (Codex): apt/npm/uv caches cleaned; pip cache moved to `/tmp/hq-repair-pip-cache`
  (temporary, gone after reboot; it is only a cache). `~/.codex/packages` (~3.9 GB) and `~/.local/share/claude` were
  NOT removed; check the active versions before touching them.

## Done
- `hub/storage.py`: `du -x` metadata scan, tree, allowlisted cache cleanup (preview, single-use 5-minute token,
  fingerprint, typed `DELETE CACHES`, fd-relative symlink-safe deletion), Claude advice via `claude -p` with no tools,
  no MCP and no API key. Scan every 6 h; automatic advice at most once a day.
- `hub/main.py`: `/api/storage`, `/api/storage/tree`, `/api/storage/{scan,advice}`, `/api/storage/cleanup/preview`,
  `/api/storage/cleanup`; mutations are audited in Activity.
- `frontend/src/main.tsx` + `style.css`: Storage page at `/system/storage` in the sidebar. `npm run build` OK.
- `tests/test_storage.py` (11 tests); full hub suite: 54 passed.
- `deploy/install.py`: unit gets `ReadWritePaths` for the pip/npm caches and `~/.claude`, `~/.claude.json`.
- README section "Storage".
- Verified with a temporary hub on 127.0.0.1:8851 + Playwright (desktop and 393 px mobile: no page errors,
  no horizontal overflow); real scan and real Claude advice worked.

## Open
1. Deploy with the installer from the source folder as root: `python3 deploy/install.py --user user`.
2. After deploy, check that "Ask Claude" works inside the hardened unit (it may need one more writable path).
