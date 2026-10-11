#!/usr/bin/env node
// Read-only 24-hour observer. Never logs MQTT payloads or credentials.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {spawnSync} = require('node:child_process');
const mqtt = require('../z2m/node_modules/mqtt');

const output = process.argv[2];
if (!output) throw new Error('Usage: node deployment/observe.js OUTPUT.jsonl');
process.umask(0o077);
const fd = fs.openSync(output, 'wx', 0o600);
const credentials = JSON.parse(fs.readFileSync(path.join(os.homedir(), '.config/nspanel/nspanel_og_essen_z2m.mqtt.json')));
const base = 'zigbee2mqtt/nspanel_og_essen';
let state = 'unknown';
let lastStateAt = null;
function record(event, data = {}) {
  fs.writeSync(fd, JSON.stringify({at: new Date().toISOString(), event, ...data}) + '\n');
}
const client = mqtt.connect(`mqtt://${credentials.probe_host || credentials.host}:${credentials.port}`, {
  username: credentials.username, password: credentials.password,
  clientId: `nsp-observer-${process.pid}`, reconnectPeriod: 5000,
});
client.on('connect', () => {
  record('observer_connected');
  client.subscribe(`${base}/bridge/state`, {qos: 0});
});
client.on('offline', () => record('observer_offline'));
client.on('error', error => record('observer_error', {code: error.code || 'unspecified'}));
client.on('message', (topic, payload) => {
  try {
    const value = JSON.parse(payload.toString()).state;
    if (!['online', 'offline'].includes(value)) return;
    state = value;
    lastStateAt = new Date().toISOString();
    record('bridge_state', {state});
  } catch (_) { record('invalid_state_message'); }
});
function sample() {
  const command = 'cat /proc/uptime; ps -A | grep -E "node|mosquitto|nspanel.develop|nspanelpro.tools"; '
    + 'dumpsys power | grep -E "mWakefulness=|Display Power"';
  const result = spawnSync('adb', ['-s', process.env.NSP_ADB || '192.168.188.84:5555', 'shell', 'su', '0', 'sh'], {
    input: command + '\n', encoding: 'utf8', timeout: 20000, maxBuffer: 1024 * 1024,
  });
  record('sample', {mqttConnected: client.connected, state, lastStateAt,
    adbOk: result.status === 0, panel: result.status === 0 ? result.stdout.trim() : null});
}
record('started', {durationHours: 24, intervalSeconds: 300, scope: 'Coordinator and panel processes; no paired-device or physical-proximity validation'});
sample();
const interval = setInterval(sample, 300000);
let stopping = false;
function stop(reason) {
  if (stopping) return;
  stopping = true;
  clearInterval(interval);
  record('finished', {reason});
  client.end(true);
  fs.closeSync(fd);
  process.exit(0);
}
setTimeout(() => stop('24h elapsed'), 24 * 3600000);
process.on('SIGTERM', () => stop('terminated early'));
process.on('SIGINT', () => stop('interrupted'));
