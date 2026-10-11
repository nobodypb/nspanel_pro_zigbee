#!/usr/bin/env python3
"""Private, reproducible NSPanel deployment helpers. Never print credentials."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import secrets

ADB = ['adb', '-s', os.environ.get('NSP_ADB', '192.168.188.84:5555')]
APP = 'nl.mennovanhout.nspanel.develop'
PREFIX = '/data/data/com.termux/files/usr'
RUNTIME = '/data/local/nspanel_tools_pkg/z2m'
SERVICE = PREFIX + '/var/service/nspanel-z2m'
ROOT = Path(__file__).resolve().parents[1]


def shell(command, *, timeout=60):
    r = subprocess.run(ADB + ['shell', 'su', '0', 'sh'], input=(command + '\n').encode(),
                       capture_output=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError('Panel command failed; inspect privately (output suppressed)')
    return r.stdout.decode().strip()


def push_private(local, remote, uid='0', mode='600'):
    # Transport through the shell-readable staging directory, then restrict access.
    directory = '/data/local/tmp/.nsp-transfer-' + secrets.token_hex(8)
    stage = directory + '/payload'
    shell('set -e; umask 077; mkdir ' + directory + '; chown 2000:2000 ' + directory)
    try:
        subprocess.run(ADB + ['push', str(local), stage], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shell(f'set -e; mkdir -p {shlex.quote(str(Path(remote).parent))}; '
              f'cp {stage} {shlex.quote(remote)}; chmod {mode} {shlex.quote(remote)}; '
              f'chown {uid}:{uid} {shlex.quote(remote)}')
    finally:
        shell('rm -rf ' + directory)


def backup(destination):
    os.umask(0o077)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    paths = [
        '/vendor/bin/siliconlabs_host', '/data/vendor/siliconlabs_host',
        '/data/data/com.termux/files/home/data/zgateway',
        '/data/data/com.termux/files/home/data/.bridge-cube',
        PREFIX + '/etc/pm2', PREFIX + '/etc/mosquitto',
        PREFIX + '/var/service/cube/run', PREFIX + '/var/service/cube/down',
        '/data/data/com.termux/files/home/.termux/boot',
        '/data/data/com.seaky.nspanelpro.tools/shared_prefs',
        '/data/data/com.eWeLinkControlPanel/shared_prefs',
        '/data/data/' + APP + '/shared_prefs',
        '/data/data/' + APP + '/app_flutter',
    ]
    present = [p for p in paths if shell('test -e ' + shlex.quote(p) + ' && echo yes || true') == 'yes']
    archive = destination / 'before.tar.gz'
    # This firmware's su uses a PTY and corrupts binary stdout; use adb pull instead.
    remote = '/data/local/tmp/.nsp-backup-' + secrets.token_hex(8)
    shell('umask 077; mkdir ' + remote + '; chown 2000:2000 ' + remote)
    try:
        shell('set -e; tar -czf ' + remote + '/before.tar.gz -C / ' +
              ' '.join(shlex.quote(p.lstrip('/')) for p in present) +
              '; chown 2000:2000 ' + remote + '/before.tar.gz; chmod 600 ' +
              remote + '/before.tar.gz', timeout=180)
        expected = shell('sha256sum ' + remote + '/before.tar.gz').split()[0]
        subprocess.run(ADB + ['pull', remote + '/before.tar.gz', str(archive)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=180)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Backup transfer checksum mismatch')
    finally:
        shell('rm -rf ' + remote)
    with tarfile.open(archive) as tar:
        members = tar.getnames()
        if not members:
            raise RuntimeError('Empty backup')
    # Separate exact app preferences for a narrowly scoped MQTT configuration rollback.
    prefs = '/data/data/' + APP + '/shared_prefs/FlutterSharedPreferences.xml'
    (destination / 'app-preferences.xml').write_text(shell('cat ' + prefs) + '\n')
    snapshot = {
        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'members': len(members), 'paths': present,
        'cube_was_down': PREFIX + '/var/service/cube/down' in present,
        'app_uid': shell('stat -c %u /data/data/' + APP),
        'termux_uid': shell('stat -c %u /data/data/com.termux'),
        'vendor_host_hashes': shell('find /vendor/bin/siliconlabs_host -type f -exec sha256sum {} \\;'),
        'firmware': shell('getprop ro.build.display.id'),
    }
    (destination / 'manifest.json').write_text(json.dumps(snapshot, indent=2) + '\n')
    (destination / 'COMPLETE').write_text(snapshot['created_at'] + '\n')
    print('Private backup verified:', destination)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['backup'])
    p.add_argument('destination')
    args = p.parse_args()
    backup(args.destination)


if __name__ == '__main__':
    main()
