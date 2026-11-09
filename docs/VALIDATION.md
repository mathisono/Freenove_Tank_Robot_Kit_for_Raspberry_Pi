# Validation record

## Development verification

Verified on 2026-11-09 in the development workspace, using Python 3.12.
No SSH connection to the physical rover was available in this task.

| Check | Result | What it establishes |
| --- | --- | --- |
| Python suite: `python -m pytest -q` | **74 passed** | Controller state transitions, motor watchdog behavior with fake GPIO, vision cases, camera queue/profile handling, authenticated API/WebSocket behavior, and a real local WebRTC transport test |
| UI suite: `npm run test:ui` | **13 passed** | The actual HTML/JavaScript handles input, ownership, rendered-frame freshness, Grab/Release latching, disabled Search, and credential clearing with explicit DOM/transport mocks |
| `node --check rover_web/static/app.js` | Passed | JavaScript syntax |
| `bash -n scripts/*.sh` (explicit script arguments) | Passed | Shell syntax; not execution on Pi hardware |
| `git diff --check` | Passed | No whitespace errors in the changes |

The Python test result above comprises 31 controller, 7 hardware-adapter,
7 vision, 16 server, and 13 camera/video cases. Development dependencies were
installed in a local virtualenv. The package installs in editable mode and
loads configuration without opening hardware. The shipped example config
keeps servo and autonomous pickup commissioning disabled.

### Real WebRTC transport test

`tests/test_video_integration.py` runs the application's HTTP offer endpoint
and an aiortc receiver in one process. It negotiates H264, completes actual
ICE/DTLS/RTP transport, and receives at least three decoded video frames with
increasing timestamps. Source frames and the source encoder are synthetic;
the test does not use a Pi camera or V4L2 encoder. ICE discovery is explicitly
restricted to loopback in the test because the development sandbox forbids
network-interface enumeration. The real transport and decoder remain active.

This verifies the service's signaling/transport path and that viewing video
does not enable motion. It does not establish browser compatibility, Wi-Fi
latency, hardware encoder output, or camera-to-display delay on the rover.

### Control and API cases

Tests exercise a fresh valid approach through close/lift/hold and Release;
rejected uncalibrated pickup; target loss/change; stale vision and range;
geometry mismatch; timeouts; collision clearance; repeated latch commands;
manual override; Stop during manipulator slew; and preservation of the last
commanded grip. Fake hardware tests check an independent expired motor lease
while the controller is absent and while sonar sampling holds its lock,
plus a partial motor-output failure.

Server tests cover access control, same-origin requirements, one controller
at a time, expired command tickets, invalid/non-finite drive values, command
ordering, video loss, observer STOP, Release while driving is disabled,
uncalibrated Grab rejection, and expiration of a live WebSocket's login session.

UI tests load the shipped page and script in jsdom. Browser media, WebRTC,
WebSocket, and rover state are mocked explicitly. Tests distinguish an actual
rendered-frame callback from merely having a connected WebRTC peer; a stalled
picture stops driving and requires deliberate re-enablement. No full browser
visual inspection was completed: this workspace's browser environment could
not reach the local server, and its Chromium sandbox could not create the
required socket. Responsive layout and real browser behavior still require
acceptance on the operator's browser.

### Camera compatibility work

The Pi 4 H264 adapter was checked against the official Picamera2 0.3.39
implementation. It requests constrained-baseline profile and level 3.1 using
standard V4L2 controls, then validates actual SPS/PPS before forwarding packets.
It does not relabel an incompatible encoded stream by editing SDP or SPS.
Queue tests verify that a slow client drops dependent frames and waits for a
keyframe, instead of accumulating a delayed video backlog. Packet tests verify
that encoded packets are packed without instantiating a software encoder.

The real camera driver's acceptance of those controls remains an on-rover
check. An unsupported API or invalid profile produces a camera error and
prevents driving. Preserve the detected working sensor rather than forcing a
camera model from a previous conversation.

## Not yet physically verified

- Raspberry Pi OS, architecture, installed Picamera2/V4L2 compatibility, actual
  camera sensor/orientation, and the configured PWM-chip/overlay routing.
- Motor polarity, effective minimum duty, stopping distance, sonar alignment,
  servo travel/endpoints, battery/thermal behavior, and cable clearance.
- Ball diameter, focal scale, camera/sonar offset, capture distance and image
  geometry, and an actual approach/pickup/release cycle.
- End-to-end video and control delay over the rover's LAN, actual browser
  rendering/decoding, multi-client performance, and service startup/rollback.

Those checks are part of `DEPLOYMENT.md` and the OpenClaw handoff. A passing
simulation does not prove a physical pickup. No measured latency target is
claimed. Search Mode is deliberately unavailable.

## Repository and delivery verification

The original `Code/` tree and upstream license remain unchanged. The delivery
manifest records the committed branch, upstream prerequisite, file checksums,
and GitHub publication status. The incremental bundle must be verified and
imported into a repository containing its prerequisite, then its branch SHA
compared with the manifest. The source snapshot is included for inspection;
the bundle restores the full Git-derived checkout with the original files.
