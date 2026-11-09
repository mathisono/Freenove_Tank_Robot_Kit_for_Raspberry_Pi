# Deploying Rover Field Control

## Preserve the running rover

Use the existing SSH target, not an IP guessed from another project. Record
model, OS, architecture, Python/Picamera2 versions, camera sensor, current
robot repo/commit/local changes, active service or autostart command, PWM
configuration, and listening ports. Useful read-only checks:

```bash
cat /proc/device-tree/model
cat /etc/os-release
uname -m
python3 --version
rpicam-hello --list-cameras
python3 -c 'import importlib.metadata as m; print(m.version("picamera2"))'
systemctl --type=service --state=running
ps -eo pid,user,args
ss -ltnp
```

Make a timestamped backup of the original source, uncommitted changes,
`params.json`, identified service/autostart entries, and camera/PWM config.
Record exact rollback commands. Do not reinstall the OS, reset the old Git
checkout, run Freenove's installer, or alter a working camera overlay based
on an assumed camera model. The detected sensor is authoritative.

The new adapter targets Pi 4/FNK0077 V2: motors BCM24/23 and BCM5/6, servo PWM
GPIO12/13, sonar trigger/echo GPIO27/22. Preserve existing calibrated polarity
and PWM-chip settings. No stock `Servo()` object is constructed on startup.

Verify the PWM pin routing before commissioning servos. Installing the Python
PWM package alone does not enable those pins. Freenove's bundled Pi 4 setup
script uses this boot-config overlay:

```ini
dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4
```

Treat that line as a reference when inspecting the existing boot configuration,
not a command to append it blindly. Confirm that the selected
`/sys/class/pwm/pwmchipN` exposes both channels and that they route to GPIO12/13;
set `hardware.pwm_chip` to that chip. Preserve an already working equivalent
setup. If routing is missing, back up the boot configuration, resolve any
existing PWM/audio pin conflicts, and make the minimum required change with S2
off. A boot-overlay change requires a supervised reboot before testing. Do not
stack duplicate overlays or run the upstream cleanup script over a working
installation.

## Restore and install the branch separately

The delivery ZIP includes an incremental `rover-web.bundle` with prerequisite
`008139eee37d99ffc15766c7f2a04a9d8fc32849`, plus source and a restore script:

```bash
bash restore_bundle.sh /absolute/path/rover-web.bundle /new/path/freenove-rover
```

The script clones upstream, verifies the bundle, imports `rover-web`, and
checks it out. It refuses existing destinations. For a published fork,
clone its `rover-web` branch into a new directory instead. `/opt/freenove-rover`
is the recommended final service location; use a stable path without spaces
or percent signs.

To publish, run `scripts/publish_fork.sh` on a host already authenticated to
GitHub as `mathisono`. It verifies the fork relationship, never force-pushes,
and does not overwrite an unrelated repository. Deployment can proceed from
the verified bundle even if GitHub authentication is unavailable.

Run `scripts/install_rover.sh` inside the new checkout. It installs only the
needed OS packages, creates `.venv` with `--system-site-packages`, installs the
new Python package, and runs config/tests. It uses `/usr/bin/python3` so the
environment matches apt's Python packages. Picamera2/OpenCV/NumPy come from the
OS to avoid pip replacing its camera stack. No OS distribution upgrade is
performed. No hardware is opened by the tests.

Run `sudo scripts/install_service.sh`. It preserves existing local config,
creates a root-readable key if absent, and registers but does not start or
enable the service. The unit runs as root for the V2 hardware-PWM sysfs
interface. It starts tracks stopped and issues no servo target.

```bash
sudo .venv/bin/python -m rover_web --config /etc/freenove-rover/config.json --check-config
```

For first startup leave S2 Load off. Stop only the exact original robot
process/service identified earlier; never broadly kill Python processes.
Then start the new service:

```bash
sudo systemctl start rover-web
sudo systemctl status rover-web --no-pager
sudo journalctl -u rover-web -n 80 --no-pager
```

Open `http://ROVER_ADDRESS:8080` on the same reachable LAN. Enter the key from
`/etc/freenove-rover/access.token` privately. The code does not expose the rover
to the internet. WebRTC needs a reachable UDP path; Wi-Fi client isolation or
firewall rules can prevent video even if the HTTP page opens. An SSH TCP
forward alone does not forward WebRTC UDP.

## Optional C12-adjacent status LED

On the V2 board the RGB LEDs are indexed front-right LED0, front-left LED1,
rear-left LED2, rear-right LED3. LED3 is beside C12. The status feature writes
only LED3 through SPI0; the other three pixels are kept off. `python3-spidev`
and an enabled SPI0 overlay are required.

Set `status_led.enabled=true` in `/etc/freenove-rover/config.json`, validate
the config, and restart the service. With no web client LED3 blinks green;
with a client and controls disabled it shows red; with controls enabled it
shows solid green. These colors report only the web/control state, **not** the
physical S2 switch position or motor power. The former yellow S2-off state is
not used. The LED is informative only; STOP, the motor watchdog, and the
physical S2 switch remain the safety mechanisms.

## Camera and Direct controls

Verify orientation/focus, moving video, decoded FPS, camera age, and control
RTT. Default `camera.rotation180=true` matches the original code's two flips;
change it if the real view is upside down. Ensure the overlay follows the red
ball and picture-left corresponds to rover-left.

Video uses the real sensor timestamp when available, bounded frame queues,
short keyframe intervals, and hardware H264. The encoder requests level 3.1
and validates output. If compatibility checks fail, diagnose the logged error;
do not disable validation to make a connection appear successful.

For first motor checks lift the tracks clear, supervise locally, enable S2,
and use low speed. Verify forward/reverse/left/right; adjust only the required
`left_inverted`/`right_inverted` setting. Direct drive needs fresh camera and
valid sonar even without a ball. During a raised-track test point the sonar
at an object in range. No echo is missing data, never an invented clear path.
Check release-to-stop, Space, STOP, tab hiding, Wi-Fi loss, and camera stalls.
Reconnection must not resume motion automatically.

Software deadlines cannot guarantee stopping after a frozen OS/kernel or
power fault; keep S2 accessible. There is no rear/cliff sensor here, so reverse
and turning require supervised clear space.

## Commission the claw and pickup geometry

Default values are starting limits, not measurements of this rover:

| Setting | Initial value / required observation |
| --- | --- |
| `hardware.servo_calibrated` | false until travel and endpoints are checked |
| Gripper open / closed | 90° / 145°, within 90–150° bounds |
| Arm pickup / carry | 95° / 135°, within 90–150° bounds |
| `control.autonomy_calibrated` | false until complete pickup is commissioned |
| `control.grasp_stop_cm` | sonar reading with ball correctly inside open claw |
| `control.ball_diameter_cm` | measured diameter of the actual ball |
| `control.camera_focal_length_norm` | calibrated focal length / image width |
| `control.grasp_x`, `grasp_y` | normalized ball center at the capture point |
| `control.camera_to_sonar_cm` | signed axial offset: positive when sonar is in front of the camera toward the ball; negative when behind |

Use the original working endpoints as evidence and verify them with the
tracks lifted. Nominal angles do not prove linkage clearance. The first
explicit servo command can move immediately: there is no position feedback.
Later targets slew at the configured rate. Stop freezes further slew while
retaining the last pulse; it does not open the claw.

Never use autonomous driving to discover servo endpoints. After safe endpoints
are known, set `servo_calibrated=true` to permit explicit Release/manipulator
commands. Keep `autonomy_calibrated=false` while measuring geometry. Do not
change calibration flags merely to unlock buttons.

For a centered ball at known camera-to-center distance Z, image diameter d
pixels and physical diameter D, approximate normalized focal length is
`(d / image_width) * Z / D`. Repeat at several distances with the actual lens
and crop. Wide-angle distortion makes off-center estimates less reliable.
The controller estimates front-surface range and applies the camera-to-sonar
offset. Its comparison can reject obvious wall echoes behind a ball, but is
not a depth map or proof of sonar/ball correspondence. Both sensors must look
through the same pickup corridor.

Measure `grasp_stop_cm` with the ball in the open claw's capture area and leave
margin beyond `min_clearance_cm`. Set image center/radius bounds accordingly.
Check the minimum PWM duty needed to move both tracks on the actual floor;
low approach/creep defaults may need tuning for friction and battery state.
Do not add timed blind movement to bypass disagreeing sensors.

Only after measurements, set `autonomy_calibrated=true`, validate JSON, and
restart with clear space. Start with a nearby visible ball. Verify acquire →
align → approach → stop → close → lift → hold, then explicit Release. Check
ball loss, invalid sonar, an obstructing object, STOP, and a Direct override.
Search stays unavailable.

There is no possession/force/position sensor. Holding is a close-command state;
Stop during closing can hold a partially closed position. Shutdown stops PWM
without commanding open, so torque is not guaranteed after restart or power
loss. Startup never resumes an interrupted pickup.

## Measure, enable startup, and roll back

WebSocket RTT is not video delay. Measure camera-to-display delay by filming
both a changing timer/LED and the browser view together. Record video settings,
browser, Wi-Fi signal, CPU load, temperature, and undervoltage/throttling status.
Check steering response separately. Tune bitrate/resolution before weakening
freshness checks. The receiver's minimum-buffer hint is not a latency promise.

After successful supervised acceptance:

```bash
sudo systemctl enable rover-web
```

Record commit/config backup, URL, old service state, measured results, and
remaining limitations. For rollback, stop/disable `rover-web`, restore the
exact previous service/autostart state, and start the original command after
camera/GPIO are released. No reset or OS reinstall should be necessary. Keep
both the backup and new checkout until any failure is understood.
