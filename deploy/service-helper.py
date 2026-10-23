#!/usr/bin/python3 -I
"""Root-owned, fixed operations only. No user commands or arbitrary file removal."""
import hashlib
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

UNIT_DIR = Path('/etc/systemd/system')
BACKUPS = Path('/var/lib/pi-app-hub/unit-backups')
CRITICAL = re.compile(r'(pi[-_ ]?app[-_ ]?hub|tailscale|docker|containerd|ssh|systemd|network|netplan|wifi|wpa|dhcp|connman|firewall|nftables|iptables|ufw|resolved|dbus|cloudflare|wireguard|openvpn|nm-|modem|avahi|polkit)', re.I)

def run(*args):
    return subprocess.run(['/usr/bin/systemctl', *args], check=True, capture_output=True, text=True, timeout=75,
                          env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin', 'LANG':'C.UTF-8'}).stdout

def validate(unit):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}\.service', unit) or CRITICAL.search(unit):
        raise ValueError('Invalid or protected unit')
    path = UNIT_DIR / unit
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or path.resolve() != path:
        raise ValueError('Unit must be a root-owned regular custom service, without writable or symlink paths')
    content = path.read_text()
    for line in content.splitlines():
        if line.startswith(('ExecStart=', 'ExecStartPre=', 'ExecStop=', 'Description=')) and CRITICAL.search(line):
            raise ValueError('Critical infrastructure is protected')
    fragment = run('show', unit, '--property=FragmentPath', '--value').strip()
    if fragment != str(path):
        raise ValueError('Service does not resolve to the expected custom unit')
    return path, hashlib.sha256(content.encode()).hexdigest()

def main(args):
    if len(args) not in {2,3} or args[0] not in {'start','stop','restart','check','uninstall'}:
        raise ValueError('Allowed operations: start, stop, restart, check, uninstall')
    action, unit = args[:2]
    path, digest = validate(unit)
    if action in {'check','uninstall'}:
        related = run('show', unit, '--property=TriggeredBy,RequiredBy,BoundBy,ConsistsOf').strip()
        if any(re.search(r'\.(service|socket|timer)\b', line.split('=',1)[-1]) for line in related.splitlines()):
            raise ValueError('Service has dependent units or activation triggers; remove it manually')
    if action == 'check':
        print(digest)
    elif action == 'uninstall':
        if len(args) != 3 or args[2] != digest:
            raise ValueError('Unit changed since preview')
        run('disable', '--now', unit)
        if validate(unit)[1] != digest:
            raise ValueError('Unit changed during removal; it was stopped but not archived')
        parent = BACKUPS.parent.lstat()
        if BACKUPS.parent.resolve() != BACKUPS.parent or parent.st_uid != 0 or parent.st_mode & 0o022:
            raise ValueError('Unsafe archive parent directory')
        BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = BACKUPS.lstat()
        if BACKUPS.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Unsafe archive directory')
        backup = BACKUPS / f'{time.time_ns()}-{unit}'
        path.rename(backup)
        try:
            run('daemon-reload')
        except Exception:
            backup.rename(path)
            run('daemon-reload')
            raise
        print(str(backup))
    else:
        if len(args) != 2:
            raise ValueError('Unexpected arguments')
        run(action, unit)

if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
