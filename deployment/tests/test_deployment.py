import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy import make_config, mqtt_login, service

RUN = Path(__file__).resolve().parents[1] / 'run'


class DeploymentTests(unittest.TestCase):
    def test_config_uses_external_broker_and_unique_network(self):
        credentials = dict(host='192.0.2.1', port=1883, username='test', password='private')
        first, second = make_config(credentials), make_config(credentials)
        self.assertEqual(first['mqtt']['server'], 'mqtt://192.0.2.1:1883')
        self.assertEqual(first['serial']['adapter'], 'ember')
        self.assertTrue(first['serial']['rtscts'])
        self.assertEqual(len(first['advanced']['network_key']), 16)
        self.assertNotEqual(first['advanced']['network_key'], second['advanced']['network_key'])
        self.assertNotEqual(first['frontend']['auth_token'], second['frontend']['auth_token'])
        self.assertNotIn('permit_join', first)

    def test_no_operation_can_target_sonoff_broker(self):
        for name in ['mosquitto', 'sshd', '../cube', 'cube;false']:
            with self.assertRaises(ValueError):
                service('down', name)

    def mqtt_server(self, reply):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        def serve():
            try:
                with listener.accept()[0] as conn:
                    conn.recv(4096)
                    # Fragment the CONNACK to test stream reassembly.
                    conn.sendall(reply[:2])
                    time.sleep(.01)
                    conn.sendall(reply[2:])
            finally:
                listener.close()
        worker = threading.Thread(target=serve)
        worker.start()
        return port, worker

    def test_mqtt_login_and_rejection_do_not_expose_password(self):
        for reply in [b'\x20\x02\x00\x00', b'\x20\x02\x00\x05']:
            port, worker = self.mqtt_server(reply)
            credentials = dict(host='127.0.0.1', port=port, username='test', password='do-not-print')
            if reply[-1] == 0:
                mqtt_login(credentials)
            else:
                with self.assertRaises(RuntimeError) as caught:
                    mqtt_login(credentials)
                self.assertNotIn('do-not-print', str(caught.exception))
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

    def test_short_failures_back_off_to_sixty_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'z2m').mkdir()
            fake_node = root / 'node'
            fake_node.write_text('#!/bin/sh\nexit 7\n')
            fake_node.chmod(0o700)
            fake_sleep = root / 'sleep'
            fake_sleep.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$NSP_ROOT/delays"\n')
            fake_sleep.chmod(0o700)
            environment = os.environ | {'NSP_ROOT': str(root), 'NSP_NODE': str(fake_node), 'NSP_SLEEP': str(fake_sleep)}
            for _ in range(6):
                result = subprocess.run(['sh', str(RUN)], env=environment, capture_output=True, timeout=3)
                self.assertEqual(result.returncode, 7)
            self.assertEqual((root / 'delays').read_text().splitlines(), ['5', '10', '20', '40', '60', '60'])

    def test_stop_terminates_only_owned_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'z2m').mkdir()
            fake_node = root / 'node'
            fake_node.write_text('#!/bin/sh\necho $$ > "$NSP_ROOT/child"\nexec sleep 60\n')
            fake_node.chmod(0o700)
            unrelated = subprocess.Popen(['sleep', '60'])
            process = subprocess.Popen(['sh', str(RUN)], env=os.environ | {'NSP_ROOT': str(root), 'NSP_NODE': str(fake_node)},
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 3
                while not (root / 'child').exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((root / 'child').exists())
                child = int((root / 'child').read_text())
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0)
                self.assertIsNone(unrelated.poll())
                with self.assertRaises(ProcessLookupError):
                    os.kill(child, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
                unrelated.terminate()
                unrelated.wait()


if __name__ == '__main__':
    unittest.main()
