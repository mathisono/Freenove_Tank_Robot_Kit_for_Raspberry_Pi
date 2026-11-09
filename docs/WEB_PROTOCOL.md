# Rover web protocol, v1

The browser loads `/` and `/static/app.{css,js}` from the rover. Nothing is
loaded from a CDN. Real hardware mode requires a local access key; demo mode
with no key is loopback-only by default and never opens GPIO.

## HTTP

- `GET /api/status`: public authentication/demo/version status. Includes live
  `state` only after authentication (or in keyless demo mode).
- `POST /api/login`, JSON `{"token":"..."}`: creates a random, server-side
  session and a HttpOnly, SameSite=Strict cookie. No key in query strings.
- `POST /api/offer`, JSON `{"type":"offer","sdp":"..."}`: authenticated
  WebRTC offer; returns `{"type":"answer","sdp":"..."}`. H264 only.
- `GET /api/snapshot.jpg`: authenticated diagnostic image from the shared
  vision stream, not a second camera instance.
- `/ws`: authenticated same-origin WebSocket. It carries controls and
  telemetry, never video frames. One browser may own controls at a time.

## WebSocket

Server sends `{"type":"state","ticket":"opaque","state":{...}}` at 10 Hz.
Echo the latest ticket in drive, heartbeat, and action messages. Tickets expire
after 500 ms: old motion waiting in a TCP queue must not become fresh merely
because it has finally arrived. Movement also expires after 300 ms without a
new drive command. A fresh heartbeat does not refresh a drive command.

Client messages:

```json
{"type":"heartbeat","ticket":"LATEST","video_ok":true,"seq":1}
{"type":"action","ticket":"LATEST","action":"enable"}
{"type":"drive","ticket":"LATEST","seq":2,"throttle":0.3,"steering":0.1}
{"type":"action","ticket":"LATEST","action":"grab"}
{"type":"action","ticket":"LATEST","action":"release"}
{"type":"action","action":"stop"}
{"type":"action","ticket":"LATEST","action":"disable"}
{"type":"ping","sent_at":12345.67}
```

Drive values are normalized -1..1; positive steering turns right. Motor output
is scaled by the configured speed cap. Drive sequence numbers must increase
for that WebSocket. A nonzero Direct command overrides a Grab sequence. A
zero drive command stops tracks and cancels approach; do not send idle zeros
continuously while Grab is active.

Heartbeat interval is 150 ms. `video_ok` means actual displayed video frames
are still arriving, not just that the WebRTC connection says “connected”.
Lost video, focus loss, page hiding, controller disconnect, an old command,
or Stop requires deliberate re-enabling. Stop needs no ticket and is allowed
from any authenticated viewer. Release is allowed by the current driver, or
when nobody owns controls, even with driving disabled. It never advances the
tracks. Search Mode is not an accepted action.

Server replies `{"type":"pong","sent_at":...}` for RTT measurement and
`{"type":"error","message":"..."}` for rejected operations. The UI follows
server state for the claw; it never claims a successful grab from a click.

Relevant state fields: `enabled`, `control_owned`, `owner`, `mode`, `phase`,
`grip_closed`, `possession_confirmed` (always false without a possession
sensor), `autonomy_calibrated`, `reason`, `motor.left/right`, `distance_cm`,
`range_age_s`, `vision_age_s`, `target`, and `camera`. Target x/y coordinates
are fractions of image width/height; radius is a fraction of image width.

WebRTC uses local ICE candidates with no cloud/STUN/TURN service. This first
release targets browsers and rover on the same reachable LAN. HTTP/WebSocket
authentication does not encrypt a plain-HTTP LAN connection. Use a trusted
LAN or a separately configured trusted HTTPS/VPN path; this service does not
open router ports or configure public access.
