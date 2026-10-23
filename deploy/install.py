#!/usr/bin/env python3
"""Install only Pi App Hub, its narrow service helper and one private Serve route."""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import time
import urllib.request

SOURCE = Path(__file__).resolve().parent.parent
DEST = Path('/opt/pi-app-hub')
STATE = Path('/var/lib/pi-app-hub')
UNIT = Path('/etc/systemd/system/pi-app-hub.service')
HELPER = Path('/usr/local/sbin/pi-app-hub-service')
SUDOERS = Path('/etc/sudoers.d/pi-app-hub')

def run(*args, **kwargs):
    return subprocess.run(list(args), check=True, text=True, **kwargs)

def read_command(*args):
    return run(*args, capture_output=True).stdout

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', default=os.environ.get('SUDO_USER'))
    parser.add_argument('--no-tailscale',action='store_true',help='Install locally without configuring remote access')
    args=parser.parse_args()
    if os.geteuid()!=0:
        parser.error('Run with sudo after building and testing the frontend')
    if not args.user or args.user=='root' or not re.fullmatch(r'[a-z_][a-z0-9_-]*', args.user):
        parser.error('--user must name an existing non-root account with Docker access')
    account=pwd.getpwnam(args.user)
    docker_group=grp.getgrnam('docker')
    if docker_group.gr_gid not in os.getgrouplist(args.user,account.pw_gid):
        parser.error('The selected account must already belong to the Docker group')
    for file in ['index.html','manifest.webmanifest','icon-192.png','icon-512.png','sw.js']:
        if not (SOURCE/'frontend/dist'/file).is_file():
            parser.error('Frontend build or PWA icons missing. Run npm ci and npm run build first.')
    for target in [DEST,STATE,UNIT,HELPER,SUDOERS]:
        if target.is_symlink() or (target.exists() and target.stat().st_uid not in {0, account.pw_uid}):
            parser.error(f'Unexpected ownership or symlink: {target}')
    before=json.loads(read_command('tailscale','serve','status','--json')) if not args.no_tailscale else {}
    origin='http://127.0.0.1:8840'
    host=None
    if not args.no_tailscale:
        status=json.loads(read_command('tailscale','status','--json'))
        host=status.get('Self',{}).get('DNSName','').rstrip('.')
        if status.get('BackendState')!='Running' or not re.fullmatch(r'[a-zA-Z0-9.-]+\.ts\.net',host):
            parser.error('Connect Tailscale before installing remote access')
        origin=f'https://{host}:8840'
        current=before.get('Web',{}).get(f'{host}:8840')
        desired={'Handlers':{'/':{'Proxy':'http://127.0.0.1:8840'}}}
        if current and current!=desired:
            parser.error('Tailscale port 8840 is already used by a different route')
        if before.get('AllowFunnel',{}).get(f'{host}:8840'):
            parser.error('Port 8840 has Funnel enabled. Refusing to expose Pi App Hub publicly.')
    DEST.mkdir(parents=True,exist_ok=True,mode=0o755)
    STATE.mkdir(parents=True,exist_ok=True,mode=0o755)
    os.chown(DEST,0,0);os.chown(STATE,0,0);STATE.chmod(0o755)
    data=STATE/'data'
    if data.is_symlink():
        parser.error('Data directory cannot be a symlink')
    data.mkdir(exist_ok=True,mode=0o700)
    os.chown(data,account.pw_uid,account.pw_gid)
    backups=STATE/'install-backups';backups.mkdir(exist_ok=True,mode=0o700)
    stamp=str(time.time_ns())
    (backups/f'{stamp}-tailscale-serve.json').write_text(json.dumps(before,indent=2))
    for target in [UNIT,HELPER,SUDOERS]:
        if target.exists():
            shutil.copy2(target,backups/f'{stamp}-{target.name}')
    for folder in ['hub','frontend/dist']:
        shutil.copytree(SOURCE/folder,DEST/folder,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(SOURCE/'requirements.lock',DEST/'requirements.lock')
    run('python3','-m','venv',str(DEST/'.venv'))
    run(str(DEST/'.venv/bin/python'),'-m','pip','install','-r',str(DEST/'requirements.lock'))
    shutil.copy2(SOURCE/'deploy/service-helper.py',HELPER)
    os.chown(HELPER,0,0);HELPER.chmod(0o755)
    sudoers=f'{args.user} ALL=(root) NOPASSWD: {HELPER}\n'
    temp=Path('/etc/sudoers.d/.pi-app-hub-new')
    if temp.exists() or temp.is_symlink():
        parser.error('Unexpected pending sudoers file; inspect it before continuing')
    with temp.open('x') as file:file.write(sudoers)
    temp.chmod(0o440)
    run('/usr/sbin/visudo','-cf',str(temp))
    temp.replace(SUDOERS)
    service=f'''[Unit]
Description=Pi App Hub private application dashboard
After=network-online.target docker.service tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User={args.user}
Group={account.pw_gid}
SupplementaryGroups=docker systemd-journal
WorkingDirectory=/opt/pi-app-hub
Environment=PI_HUB_DATA=/var/lib/pi-app-hub/data
Environment=PI_HUB_ORIGIN={origin}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/pi-app-hub/.venv/bin/python -m hub
Restart=on-failure
RestartSec=4
UMask=0077
TimeoutStopSec=15
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/pi-app-hub /etc/systemd/system
# Storage page: only the allowlisted download caches, plus the Claude Code login it uses for advice.
ReadWritePaths=-{account.pw_dir}/.cache/pip -{account.pw_dir}/.npm/_cacache -{account.pw_dir}/.claude -{account.pw_dir}/.claude.json
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
LockPersonality=true

[Install]
WantedBy=multi-user.target
'''
    UNIT.write_text(service);UNIT.chmod(0o644)
    run('systemctl','daemon-reload')
    run('systemctl','enable','pi-app-hub.service')
    run('systemctl','restart','pi-app-hub.service')
    for attempt in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8840/api/apps',timeout=3) as response:
                if response.status==200:break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError('Local health check failed; inspect journalctl -u pi-app-hub')
    if not args.no_tailscale:
        run('tailscale','serve','--bg','--https=8840','http://127.0.0.1:8840')
        after=json.loads(read_command('tailscale','serve','status','--json'))
        for key,route in before.get('Web',{}).items():
            if after.get('Web',{}).get(key)!=route:
                raise RuntimeError('An existing Tailscale route changed; inspect the saved configuration')
        if after.get('AllowFunnel',{}).get(f'{host}:8840'):
            run('tailscale','serve','--https=8840','off')
            raise RuntimeError('Unexpected public exposure; Pi App Hub route was disabled')
    print(f'Pi App Hub ready: {origin}\nBackend: 127.0.0.1:8840\nData: {data}')

if __name__=='__main__':main()
