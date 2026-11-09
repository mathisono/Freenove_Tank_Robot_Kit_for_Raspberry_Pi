# Rover Field Control

A local browser console for Mathison's **Freenove FNK0077 Tank Robot Kit,
V2.0 board and Raspberry Pi 4**. This development fork is based on
[Freenove's original project](https://github.com/Freenove/Freenove_Tank_Robot_Kit_for_Raspberry_Pi)
at commit `008139eee37d99ffc15766c7f2a04a9d8fc32849`.

The original `Code/Server`, `Code/Client`, tutorials, and license are retained.
The new application lives in `rover_web/`. The original README is preserved
in [docs/UPSTREAM_README.md](docs/UPSTREAM_README.md).

## Features

- **Live video:** one Picamera2 instance, Pi 4 hardware H264 encoding, WebRTC
  packet passthrough without software re-encoding. Defaults: 960×540 at
  30 fps and 2 Mbps; independent vision at 320×180, up to 15 fps.
- **Direct:** touch direction pad or WASD/arrows. Hold to move, release to
  stop. Space and the large STOP control stop motion. Speed slider plus a
  separate configured motor cap.
- **Grab:** select a visible red ball, prepare the arm, align, approach using
  camera and sonar, stop at a calibrated pickup position, close, lift, hold.
  The close command stays latched until Release. No possession sensor exists;
  “Grip held” does not prove a successful pickup.
- **Release:** stop, lower, open. Available with driving/video disabled when
  servo commissioning is complete and another browser does not own control.
- **Search Mode — Coming soon:** reserved in the UI and rejected by the
  server. Grab does not roam when no ball is visible.
- **Responsive controls:** separate WebSocket, 25 Hz commands, one driver,
  expiring command tickets, independent motor deadlines, bounded video queues.
- **Stops:** stale/missing sensors, lost target, lost connection, expired
  commands, or STOP halt travel. Stop freezes further arm/claw slew at the
  last commanded position without automatically opening the gripper.

Ball color, rotation, track polarity, servo endpoints, speed limits, and
pickup geometry are configurable. Red is the kit-ball default, not a general
ball detector. Servo movement and autonomous grabbing require measured
commissioning settings. Direct driving requires fresh camera and sonar data,
but does not require a ball.

## Desktop demo

Python 3.11 or newer:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[desktop,test]'
.venv/bin/python -m rover_web --demo
```

Open `http://127.0.0.1:8080`. The DEMO label identifies synthetic video and
simulated actuators. No Pi hardware libraries are opened. Uncalibrated Grab
remains disabled, as it does on the real rover.

## Raspberry Pi installation

Read [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), then use
[docs/OPENCLAW_DEPLOY_PROMPT.md](docs/OPENCLAW_DEPLOY_PROMPT.md) as the handoff.
Deploy to a separate directory and retain the existing installation.

```bash
./scripts/install_rover.sh
sudo ./scripts/install_service.sh
```

These install dependencies and register a service. They do not start it,
reboot, change camera overlays, or enable autonomous grabbing. Calibration
and the access key live under `/etc/freenove-rover/`, outside Git. Never run
both old and new camera/GPIO servers concurrently.

## Git fork and delivery

Branch: `rover-web`. Original repository remote: `upstream`.

```bash
./scripts/publish_fork.sh
```

Run that on a computer with GitHub CLI already authenticated as `mathisono`.
It creates/verifies the official-kit fork and pushes `rover-web` without force.
It does not modify upstream or replace an unrelated repository.

The delivery's `rover-web.bundle` is an incremental Git bundle requiring the
upstream commit above. `scripts/restore_bundle.sh BUNDLE_PATH NEW_DIRECTORY`
clones upstream, verifies prerequisites, imports the branch, and refuses to
overwrite an existing directory. GitHub publication is separate from a local
Git fork; consult the delivery manifest for publication status.

## Tests and implementation notes

```bash
.venv/bin/python -m pytest -q
npm ci --ignore-scripts
npm run test:ui
```

Node is needed only for UI tests; the rover serves its assets through Python.
See [docs/VALIDATION.md](docs/VALIDATION.md) for results and verification limits,
and [docs/WEB_PROTOCOL.md](docs/WEB_PROTOCOL.md) for messages and endpoints.

The Pi 4 encoder adapter explicitly requests constrained baseline/level 3.1
through standard V4L2 controls and validates actual SPS/PPS output. Incompatible
encoder APIs or output produce a camera error and block movement. The adapter
was checked against Picamera2 0.3.39; actual installed camera/OS compatibility
must be verified on the rover.

This first release targets a reachable LAN. It does not configure public
access, router ports, STUN/TURN, mapping, or Search. No measured end-to-end
video latency is claimed before physical acceptance testing.

## Attribution and license

Original software/hardware mapping: Freenove. This fork adds the browser UI,
streaming service, control adapter, pickup controller, tests, and deployment
tools. The original [CC BY-NC-SA 3.0 license](LICENSE.txt) is retained for this
derived project. Dependencies retain their own licenses. This is not an
official Freenove release.
