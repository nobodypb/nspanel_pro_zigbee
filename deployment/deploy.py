#!/usr/bin/env python3
"""Stage immutable Z2M payload and manage only its dedicated Termux/runit service."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import struct
import subprocess
import tarfile

from panel import ADB, PREFIX, ROOT, RUNTIME, SERVICE, shell, push_private

PRIVATE = Path.home() / '.config/nspanel'
BASE_COMMIT = 'c660c9dbb676e9e5f20b3a9e48bff4b3a8628db6'


def mqtt_login(credentials):
    """Read-only MQTT 3.1.1 login check. No subscriptions or application messages."""
    def utf8(value):
        b = value.encode()
        return struct.pack('!H', len(b)) + b
    payload = (utf8('nsp-setup-' + secrets.token_hex(5)) +
               utf8(credentials['username']) + utf8(credentials['password']))
    body = utf8('MQTT') + bytes([4, 0xC2, 0, 30]) + payload
    remaining, length = len(body), bytearray()
    while True:
        digit = remaining % 128
        remaining //= 128
        length.append(digit | (128 if remaining else 0))
        if not remaining:
            break
    with socket.create_connection((credentials.get('probe_host', credentials['host']), credentials['port']), timeout=5) as s:
        s.sendall(b'\x10' + length + body)
        response = b''
        while len(response) < 4:
            part = s.recv(4 - len(response))
            if not part:
                break
            response += part
        if response != b'\x20\x02\x00\x00':
            code = response[-1] if len(response) == 4 else 'no CONNACK'
            raise RuntimeError(f'MQTT login rejected ({code}); credentials suppressed')
        s.sendall(b'\xe0\x00')


def make_config(credentials):
    """Pinned firmware 4.6.0 uses Ember with hardware flow control, not old EZSP."""
    return {
        'version': 4,
        'mqtt': {'server': f"mqtt://{credentials['host']}:{credentials['port']}",
                 'user': credentials['username'], 'password': credentials['password'],
                 'base_topic': 'zigbee2mqtt/nspanel_og_essen',
                 'client_id': 'nspanel_og_essen_z2m', 'keepalive': 30},
        'serial': {'port': '/dev/ttyS5', 'adapter': 'ember', 'baudrate': 115200, 'rtscts': True},
        'homeassistant': {'enabled': True},
        'frontend': {'enabled': True, 'port': 8080, 'auth_token': secrets.token_urlsafe(32)},
        'availability': {'enabled': True},
        'advanced': {'channel': 15, 'pan_id': secrets.randbelow(0xFFFE) + 1,
                     'ext_pan_id': list(secrets.token_bytes(8)),
                     'network_key': list(secrets.token_bytes(16)),
                     'log_level': 'info', 'log_output': ['console']},
    }


def require_clean_develop():
    def git(*args):
        return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True).strip()
    if git('branch', '--show-current') != 'develop' or git('status', '--porcelain'):
        raise RuntimeError('Deploy only a clean integrated develop checkout')
    subprocess.run(['git', '-C', str(ROOT), 'merge-base', '--is-ancestor', BASE_COMMIT, 'HEAD'], check=True)
    return git('rev-parse', 'HEAD')


def stage(backup):
    os.umask(0o077)
    commit = require_clean_develop()
    backup = Path(backup)
    baseline = json.loads((backup / 'manifest.json').read_text())
    if not (backup / 'COMPLETE').exists() or hashlib.sha256((backup / 'before.tar.gz').read_bytes()).hexdigest() != baseline['archive_sha256']:
        raise RuntimeError('Verified private backup required')
    if shell('test -e ' + RUNTIME + ' && echo present || true'):
        raise RuntimeError('Runtime already exists; refuse to overwrite configuration or network')
    if shell('test -e ' + SERVICE + ' && echo present || true'):
        raise RuntimeError('Service already exists; refuse to overwrite')
    artifacts = PRIVATE / 'artifacts'
    artifacts.mkdir(exist_ok=True, mode=0o700)
    archive = artifacts / ('z2m-' + commit[:12] + '.tar.gz')
    with tarfile.open(archive, 'w:gz') as tar:
        for folder in ['nodejs', 'z2m']:
            tar.add(ROOT / folder, arcname=folder)
    size = archive.stat().st_size
    with tarfile.open(archive) as tar:
        expanded = sum(m.size for m in tar.getmembers())
    available = int(shell('df -k /data').splitlines()[-1].split()[3]) * 1024
    if available < size + expanded + 128 * 1024 * 1024:
        raise RuntimeError('Insufficient space for package and 128 MiB reserve')
    credentials = json.loads((PRIVATE / 'nspanel_og_essen_z2m.mqtt.json').read_text())
    config_path = PRIVATE / 'z2m-configuration.json'
    if not config_path.exists():
        config_path.write_text(json.dumps(make_config(credentials), indent=2) + '\n')
    uid = shell('stat -c %u /data/data/com.termux')
    stage_path = '/data/local/tmp/.nsp-z2m-payload.tar.gz'
    staging_service = '/data/local/tmp/.nsp-z2m-service'
    subprocess.run(ADB + ['push', str(archive), stage_path], check=True, stdout=subprocess.DEVNULL)
    try:
        if shell('sha256sum ' + stage_path).split()[0] != hashlib.sha256(archive.read_bytes()).hexdigest():
            raise RuntimeError('Payload transfer checksum mismatch')
        shell('set -e; umask 077; mkdir -p ' + RUNTIME + '; chmod 755 /data/local/nspanel_tools_pkg; tar -xzf ' + stage_path + ' -C ' + RUNTIME +
              '; mkdir -p ' + RUNTIME + '/data ' + RUNTIME + '/log; chmod 755 ' + RUNTIME + '/nodejs/bin/node', timeout=180)
        push_private(config_path, RUNTIME + '/data/configuration.yaml', uid)
        # A stopped service is assembled outside runsvdir, then published atomically.
        shell('set -e; mkdir -p ' + staging_service + '/log; touch ' + staging_service + '/down')
        push_private(ROOT / 'deployment/run', staging_service + '/run', uid, '700')
        push_private(ROOT / 'deployment/log-run', staging_service + '/log/run', uid, '700')
        shell("printf 's1000000\nn5\n' > " + RUNTIME + '/log/config; chown -R ' + uid + ':' + uid +
              ' ' + RUNTIME + ' ' + staging_service + '; chmod 700 ' + RUNTIME + ' ' + staging_service +
              '; mv ' + staging_service + ' ' + SERVICE)
        result = shell('su ' + uid + ' env LD_LIBRARY_PATH=' + RUNTIME + '/nodejs/lib ' + RUNTIME + '/nodejs/bin/node --version')
        if not result.startswith('v24.'):
            raise RuntimeError('Unexpected bundled Node runtime')
        manifest = {'commit': commit, 'base_commit': BASE_COMMIT, 'node_version': result,
                    'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                    'backup': str(backup), 'runtime': RUNTIME, 'service': SERVICE,
                    'status': 'staged-down', 'termux_uid': uid}
        (PRIVATE / 'z2m-deployment.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print('Staged and verified; Z2M remains stopped. Bundled Node:', result)
    except Exception:
        # Preserve partial files for diagnosis. Never remove pre-existing runtime data.
        shell('test ! -d ' + SERVICE + ' || touch ' + SERVICE + '/down')
        raise
    finally:
        shell('rm -f ' + stage_path)


def service(command, name):
    if name not in ['cube', 'nspanel-z2m'] or command not in ['up', 'down', 'status']:
        raise ValueError('Unsupported service operation')
    uid = shell('stat -c %u /data/data/com.termux')
    return shell('su ' + uid + ' ' + PREFIX + '/bin/sv -w 45 ' + command + ' ' + PREFIX + '/var/service/' + name, timeout=50)


def activate():
    require_clean_develop()
    credentials = json.loads((PRIVATE / 'nspanel_og_essen_z2m.mqtt.json').read_text())
    mqtt_login(credentials)
    manifest = json.loads((PRIVATE / 'z2m-deployment.json').read_text())
    if manifest['status'] not in ['staged-down', 'stopped']:
        raise RuntimeError('Activation state is not staged-down/stopped')
    cube = PREFIX + '/var/service/cube'
    shell('touch ' + cube + '/down')
    service('down', 'cube')
    try:
        # Refuse to start if another process still owns the serial radio.
        owners = shell('for f in /proc/[0-9]*/fd/*; do t=$(readlink "$f" 2>/dev/null); '
                       'if [ "$t" = /dev/ttyS5 ]; then echo "$f"; fi; done')
        if owners:
            raise RuntimeError('Serial radio remains open; activation stopped')
        shell('rm -f ' + SERVICE + '/down')
        service('up', 'nspanel-z2m')
        manifest['status'] = 'started-pending-validation'
        (PRIVATE / 'z2m-deployment.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print('Z2M started; runtime, MQTT, firmware and sleep validation still required.')
    except Exception:
        shell('touch ' + SERVICE + '/down')
        service('down', 'nspanel-z2m')
        baseline = json.loads((Path(manifest['backup']) / 'manifest.json').read_text())
        if not baseline['cube_was_down']:
            shell('rm -f ' + cube + '/down')
            service('up', 'cube')
        raise


def stop(rollback=False):
    manifest = json.loads((PRIVATE / 'z2m-deployment.json').read_text())
    shell('touch ' + SERVICE + '/down')
    service('down', 'nspanel-z2m')
    if rollback:
        baseline = json.loads((Path(manifest['backup']) / 'manifest.json').read_text())
        if not baseline['cube_was_down']:
            shell('rm -f ' + PREFIX + '/var/service/cube/down')
            service('up', 'cube')
    manifest['status'] = 'rolled-back' if rollback else 'stopped'
    (PRIVATE / 'z2m-deployment.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Z2M stopped; ' + ('original cube service restored.' if rollback else 'network data retained.'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['stage', 'activate', 'stop', 'rollback', 'check-broker'])
    parser.add_argument('--backup')
    args = parser.parse_args()
    if args.command == 'stage':
        if not args.backup:
            parser.error('--backup required')
        stage(args.backup)
    elif args.command == 'activate':
        activate()
    elif args.command in ['stop', 'rollback']:
        stop(args.command == 'rollback')
    else:
        for name in ['nspanel_og_essen_z2m', 'nspanel_og_essen']:
            mqtt_login(json.loads((PRIVATE / (name + '.mqtt.json')).read_text()))
            print(name + ': MQTT authentication passed')


if __name__ == '__main__':
    main()
