# Web control DOM checks

These development-only tests load the real `rover_web/static/index.html` and
`app.js` into jsdom. Video frame delivery, WebRTC, WebSocket transport, and rover
state are explicit mocks. No camera, GPIO, network connection, or physical rover
is used.

From the repository root:

```sh
npm ci --ignore-scripts
npm run test:ui
```

The pinned jsdom version requires Node.js 22.22.2+, 24.15.0+, or 26+ according to
the version ranges in `package.json`. Node.js and these dependencies are only
needed on a development machine; the Raspberry Pi application serves the static
files directly from Python.

The checks cover control gating, input release, retrieval latching, stale-video
and focus-loss behavior, control ownership, disabled Search Mode, and access-key
handling. They do **not** verify actual browser rendering, real WebRTC decoding,
network latency, hardware behavior, or physical ball pickup.
