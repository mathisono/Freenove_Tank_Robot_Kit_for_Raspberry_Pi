# OpenClaw: deploy the Freenove rover web fork

Copy the prompt below into OpenClaw on a machine that can SSH into the rover.
Provide the downloaded `Freenove_Rover_Web.zip` with the prompt. If the SSH
target is not already in OpenClaw's established context, provide the actual
`user@hostname` or SSH-config alias. No robot host is assumed in this delivery.

---

You are setting up my existing Freenove FNK0077 tracked robot with its V2.0
shield and Raspberry Pi 4. The original Freenove code already works. Use the
attached `Freenove_Rover_Web.zip` to install the new `rover-web` development
branch in a separate directory. I authorize the repository setup, backup,
dependency installation, service configuration, and deployment described
here. Continue through all nonmoving setup steps without repeated permission
questions. Coordinate locally supervised actuator checks with me before
energizing the tracks or moving the claw.

## Objective and supplied code

The new application is a local web console with Pi 4 hardware H264 → WebRTC
video, separate low-latency WebSocket driving, a Grab/Release latch, and two
navigation slots: Direct works; Search Mode is explicitly Coming soon.
Grab tracks a visible red ball with OpenCV, uses fresh ultrasonic distance
and calibrated image geometry to approach, then closes/lifts/holds until
Release. There is no possession sensor. Do not replace this with an LLM
driving loop or invent Search behavior during deployment.

The archive contains `DELIVERY_MANIFEST.json`, `SHA256SUMS`, an incremental
`rover-web.bundle`, a review patch, a standalone `restore_bundle.sh`, and
`source/` with the application, scripts, tests, and documentation. The bundle
preserves the fork's Git ancestry. It requires upstream commit
`008139eee37d99ffc15766c7f2a04a9d8fc32849` from:

https://github.com/Freenove/Freenove_Tank_Robot_Kit_for_Raspberry_Pi

Read `source/README.md`, `source/docs/DEPLOYMENT.md`,
`source/docs/VALIDATION.md`, and `source/docs/WEB_PROTOCOL.md` before changing
the rover. Verify `sha256sum -c SHA256SUMS` in the extracted archive and
compare the restored branch commit with `DELIVERY_MANIFEST.json`. Checksums
detect a damaged or altered download; they are not a digital signature.

## Repository setup

Restore into a new working directory using:

```bash
bash restore_bundle.sh /absolute/path/rover-web.bundle /new/path/freenove-rover
```

Never overwrite the existing robot checkout. Verify the imported commit,
branch, and `upstream` remote. If GitHub CLI is already authenticated as
`mathisono`, run `scripts/publish_fork.sh` from the new checkout. I authorize
creating my fork of the original Freenove repository and pushing `rover-web`.
The script checks the account and fork parent and does not force-push. If
GitHub login is absent or belongs to another account, report publication as
pending and continue deployment from the verified bundle. Do not create a
similarly named repository under an unrelated account or claim a hosted fork
exists before verifying it. Keep credentials and rover keys out of Git.

## Inspect and preserve the working rover

Use my established SSH target. If it is missing, ask only for the SSH target
or required access information; do not guess an IP from another project.
Inspect model, OS/version, architecture, Python version, actual camera sensor,
Picamera2 version, motor/servo/sonar wiring, PWM chip/overlay, original Git
commit and local changes, `params.json`, active services/autostart, process
commands, and occupied ports. Use read-only checks first. The expected pins
are left motor BCM24/23, right BCM5/6, servo PWM GPIO12/13, sonar 27/22.

Create a timestamped backup of the old source and local configuration,
service/autostart files, and camera/PWM settings. Record exact rollback
commands and the prior service enablement state. Preserve the working camera
sensor and configuration. Do not force an IMX708 overlay merely because that
camera was discussed; the installed sensor may differ. Do not reinstall the
OS, run the stock Freenove setup script, reset the old checkout, replace the
working camera stack with pip packages, or kill unrelated Python processes.

## Install and start without actuator movement

Use a stable new directory, preferably `/opt/freenove-rover`, with no spaces
or percent signs. Restore the bundle there or transfer the verified restored
checkout without its development `.venv` or `node_modules`. Build a fresh
virtual environment on the Pi; do not transfer the workstation environment.

The current package targets Python 3.11+ and Raspberry Pi OS Bookworm/Trixie
on Pi 4. If the installed OS is older, finish the inventory and backup and
report that concrete compatibility blocker; do not silently upgrade the OS.
Run `scripts/install_rover.sh` from the new checkout. It uses OS Picamera2,
OpenCV, NumPy, and GPIO packages through a system-site-packages virtualenv,
installs the new service dependencies, validates configuration, and runs
hardware-independent tests. Diagnose installation or test errors before
continuing. Node is optional and is only used for the browser protocol tests.

Run `sudo scripts/install_service.sh`. It creates configuration and a private
access key under `/etc/freenove-rover/` and registers `rover-web.service`
without starting or enabling it. Keep `servo_calibrated=false` and
`autonomy_calibrated=false` until their respective checks are complete.
Transfer verified original endpoints/polarity into local config as appropriate;
do not treat the example endpoints as measurements of my robot.

Have me leave S2 Load off for first startup. Stop only the identified original
camera/GPIO service/process, preserving its rollback information. Do not run
both servers simultaneously. Start `rover-web`, inspect its status/journal,
and provide the actual LAN URL and a private way to retrieve the access key.
Do not paste the key into public logs, commits, URLs, or the final report.

## Verify video and direct control

With actuators still unpowered, confirm authentication, live decoded video,
correct camera orientation, red-ball overlay, decoded FPS, fresh frame age,
WebSocket RTT, and that Search Mode remains disabled. The default view rotates
180 degrees to match the original software. Resolve real hardware encoder
errors rather than bypassing the SPS/profile checks. Test a second browser
as an observer and verify only one browser can own driving.

The page is LAN-only by default. WebRTC requires a reachable UDP path, so an
HTTP page loading through an SSH TCP forward alone does not prove video can
connect. Do not open router ports or add public exposure to solve this.

Coordinate with me to lift the tracks clear, keep S2 accessible, enable the
load, and test low-speed forward/reverse/turns. Correct only necessary motor
polarity settings. Direct driving requires fresh camera and valid sonar;
during a bench test point the sonar at an object in its valid range. Verify
release-to-stop, STOP, Space, tab hiding, loss of Wi-Fi, stale video, expired
commands, and that reconnecting never restarts motion. No echo must remain
invalid data, not be converted into a clear path.

## Commission Grab and Release

Do not run autonomous motion unattended or use it to find servo limits.
With local supervision and tracks raised, use known working original
calibration and controlled endpoint checks to establish safe gripper open /
closed and arm pickup / carry positions. The first commanded servo position
can move immediately because the kit has no position feedback. Stop freezes
further slew; it does not automatically open the claw.

Only after endpoints and travel are verified, set
`hardware.servo_calibrated=true`. Keep autonomy disabled while measuring:

- The actual ball diameter and HSV color ranges if the ball is not red.
- Normalized camera focal length using the real lens/crop at several known
  centered-ball distances, as described in `docs/DEPLOYMENT.md`.
- Camera-to-sonar axial offset and whether both sensors see the pickup corridor.
- Sonar distance and image center/radius with the ball correctly inside the
  open claw, leaving margin beyond minimum obstacle clearance.
- Effective low-speed track duty on the actual floor and battery condition.

Fill the required local fields including `grasp_stop_cm`, `ball_diameter_cm`,
`camera_focal_length_norm`, and pickup alignment settings from those measured
values. Do not fabricate measurements, remove sensor checks, or set a
calibration flag merely to unlock Grab. If a physical measurement cannot be
made remotely, finish the working video/Direct deployment and tell me exactly
which local measurement remains.

After commissioning, set `control.autonomy_calibrated=true`, validate config,
and restart. In clear supervised space, test a nearby visible ball through
acquire → align → approach → stop → close → lift → hold. Confirm the grab
request remains latched until explicit Release, then lower/open. Test target
loss, invalid sonar, an obstruction, STOP, and a Direct override. Search stays
unimplemented. A “Grip held” state is commanded posture, not verified object
possession; interrupted closing can leave a partial grip. Shutdown/power loss
does not guarantee holding torque.

## Acceptance, startup, and report

Measure video latency separately from WebSocket RTT. Film a changing timer
or LED and its browser image together; record browser, resolution/FPS/bitrate,
Wi-Fi conditions, CPU load/temperature, and undervoltage/throttling. Report
measured values only. If needed, tune bitrate/resolution before considering
changes to freshness checks.

Enable the new service at boot only after supervised acceptance. Prevent the
identified old server from competing at boot, while keeping its exact previous
state documented. If the new service fails, stop/disable it, release camera
and GPIO, and restore the old startup command/state from the backup.

End with: hosted fork URL if verified (otherwise publication pending), exact
deployed commit/branch and path, SSH target, LAN URL, backup and rollback
commands, active/enabled service state, actual camera and OS, tests run, measured
latency/FPS, calibrated values, and any remaining physical checks. Distinguish
implemented code from behavior observed on the real rover. Do not report
autonomous pickup as commissioned unless its physical acceptance test passed.
