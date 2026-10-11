# Direct MQTT deployment on NSPanel Pro 4.6.0

This local integration uses the Android ARM64 runtime from release `z2m-v2.7.1.6`
(`c660c9dbb676e9e5f20b3a9e48bff4b3a8628db6`). It does **not** execute the upstream
package installer or gateway scripts. Those scripts kill all Mosquitto processes,
assume a local MQTT broker, and back up files on the nearly full vendor partition.

Instead, the existing Termux/runit supervisor owns an independent `nspanel-z2m`
service. Runtime, private network configuration and rotating logs live under
`/data/local/nspanel_tools_pkg/z2m`, owned by Termux and mode 0700. Nothing is
installed to `/vendor`. The existing broker on port 1884 is untouched. Pro Tools
continues to manage the screen and dashboard, with its Zigbee integration disabled.

## Preconditions and private files

Use the existing root-capable ADB connection, Termux boot/runit and firmware 4.6.0.
The serial adapter is Ember on `/dev/ttyS5`, 115200 baud with RTS/CTS; the original
SONOFF 4.6 configuration also uses Ember/hardware flow control. Actual coordinator
firmware and operation must be verified during commissioning, without flashing it.
Existing networks must be inventoried before activation. Do not migrate unknown
paired devices automatically.

The two `~/.config/nspanel/*.mqtt.json` files contain `host`, `port`, `username`,
and `password`. Optional `probe_host` is used only by workstation login checks when
the panel reaches the same broker through NAT. The panel uses `host` directly.
Never commit these files. The directory also contains the private
broker ACL/setup instructions and the app-specific Android ID. The broker owner
adds the accounts and rules without replacing existing listeners or users.

The generated `z2m-configuration.json` contains the network key and frontend token.
It is created once, not regenerated on retries. Joining is closed by default.
Z2M uses the central broker directly, with a dedicated base topic and client ID.
The frontend is on port 8080 and requires its private auth token.

## Build, backup, stage and activate

1. Run `python3 -m unittest discover -s deployment/tests -v` and `sh -n deployment/run`.
2. Commit the feature increment, then merge it into local `develop`. Never push
   the integration branch. Deployment requires clean `develop` and the pinned
   release ancestor.
3. `python3 deployment/panel.py backup <new-private-directory>` creates a
   checksum-verified archive outside the panel. It includes the modern Termux
   `.bridge-cube` data, legacy data, preferences and service/boot configuration.
   This is an online snapshot; device migration must not be running concurrently.
4. `python3 deployment/deploy.py stage --backup <new-private-directory>` stages
   the runtime, verifies transfer and Node execution, and publishes a **down**
   service. It refuses to overwrite existing runtime/network data and reserves
   128 MiB free space. Partial installation remains stopped for diagnosis.
5. `python3 deployment/deploy.py check-broker` performs login-only checks for
   both accounts, without publishing or printing secrets.
6. `python3 deployment/deploy.py activate` verifies broker login, sets only the
   competing `cube` service down, checks exclusive UART access, and starts Z2M.
   It restores cube if activation fails. A successful start is not acceptance.

The service receives TERM from runit, forwards it only to its own child, and waits
for shutdown. After an exit, retry intervals grow 5/10/20/40/60 seconds; they reset
after a runtime of at least one minute. MQTT outages never trigger global process
kills or firmware operations. Logs rotate at roughly 1 MB, retaining five files.

## Stop and rollback

`python3 deployment/deploy.py stop` persistently stops Z2M, keeping network data.
`python3 deployment/deploy.py rollback` also restores cube's original running/down
state from the backup manifest. These commands do not delete device data or touch
SONOFF Mosquitto. The archive is retained for restoring original files if required.
App MQTT rollback should remove only the added `flutter.mqtt` preference while
the app is stopped; do not overwrite unrelated subsequent preference changes.

After devices have moved to a different network, restoring files alone does not
restore their former pairing. A physical reset/re-pairing is a separate operation.
Remove only this deployment's retained HA discovery entries when decommissioning.

## Acceptance

Validate exclusive serial ownership, actual coordinator firmware, MQTT/HA discovery,
ACL denial, last will and reconnect without stale commands. Verify dark-screen
operation, app restart independence, panel boot and physical proximity wake.
Compare dashboard frames with baseline. Preserve 15-second display-off and disabled
touch feedback. Record a 24-hour observation; do not claim it from a short test.

Pair the modified IKEA tunable-white controller and Philips 324131137411 with the
user present. Read actual model/firmware and exposes. Bind remote endpoint 1 to the
controller's group for `genOnOff`/`genLevelCtrl`; verify operation with Z2M stopped.
Only this provided LED installation may be actuated. No device firmware updates.
HA and the existing develop light sheet provide brightness and color temperature.
