# Auvi NVR for Home Assistant

A custom integration that links Home Assistant to an **Auvi Video Recorder**
on your network:

- **Cameras.** Every stream the recorder shares over RTSP passthrough becomes a
  Home Assistant camera (live stream over RTSP, still image from the recorder's
  latest keyframe). Cameras that are not shared still get status sensors.
- **Recorder status.** Health, free storage, retention runway, uptime, cloud
  link, per-camera connected/recording state.
- **Real-time events.** The recorder's own rules engine posts every
  detection (person, vehicle, animal, face, license plate, motion, line
  cross, loiter, intrusion, tamper) to Home Assistant the moment it is
  recorded. They arrive as device triggers for the automation editor,
  per-camera detection sensors, a "last event" / "last license plate"
  sensor per camera, and an `auvi_nvr_event` on the event bus.
- **The recorder's own viewer in the sidebar.** An **Auvi NVR** entry opens the
  recorder's local web UI (live tiles, timeline, playback, clip export)
  **served through Home Assistant's own address**. The integration logs in to
  the recorder for you and pins the recorder's certificate itself, so no
  device has to trust a certificate and no login page appears, in a browser or
  in the Home Assistant apps.

## Requirements

- Home Assistant 2025.8 or newer.
- A recorder with the HTTPS listener on (the default, port 8443) and an admin
  password set (the recorder's web UI asks for one on first use).
- For camera entities: the RTSP passthrough listener enabled on the recorder's
  RTSP tab, with its login, and the cameras you want ticked for sharing.

## Install

### With HACS (custom repository)

1. HACS → Integrations → ⋮ → **Custom repositories**.
2. Add `https://github.com/ssleffler/auvi-ha-int`, category **Integration**.
3. Install **Auvi NVR**, then restart Home Assistant.

### By hand

Copy `custom_components/auvi_nvr` into your Home Assistant `config/` folder
so you have `config/custom_components/auvi_nvr/manifest.json`, then restart
Home Assistant.

## Set up

Settings → Devices & services → **Add integration** → *Auvi NVR*.

| Field | Value |
|---|---|
| Recorder address | the recorder's LAN address, e.g. `10.0.3.2` |
| HTTPS port | `8443` unless you changed it |
| Username / Admin password | the recorder's `admin` login |
| RTSP passthrough password | the password from the recorder's RTSP tab (optional) |
| Preferred stream | `sub` for a light stream, `main` for full resolution |

The next step shows the SHA-256 fingerprint of the recorder's root
certificate. Compare it with the **Access** tab of the recorder's console and
submit. From then on the integration trusts that root only.

Options (⚙ on the integration): polling interval, RTSP password, preferred
stream, whether only administrators may open the sidebar panel, and the
event feed (on/off, detection hold, the URL the recorder posts to, local
network only, certificate checks).

## What you get

- Device **Auvi NVR &lt;hostname&gt;** with sensors: status, storage free,
  retention runway, uptime, agent version, cameras; binary sensors: cloud
  connected, RTSP passthrough.
- One device per camera (make, model, firmware from the recorder) with
  `camera.*` (shared cameras only), `binary_sensor.*_connected`,
  `*_recording`, `*_shared_over_rtsp`, and status/frame-rate/bitrate sensors.
- Per camera, fed by the event feed: `binary_sensor.*_activity` (any object
  or analytic event), `*_person`, `*_vehicle`, `*_animal`,
  `*_license_plate`, `*_motion` (face, line crossed, loitering, intrusion
  and tamper exist too, disabled by default). Each switches on when the
  recorder posts a matching event and off after the hold set in the
  options (30 s by default); its attributes carry the event id, time,
  model label, zones, plate reading and watch-list match.
  `sensor.*_last_event` holds the last event type,
  `sensor.*_last_license_plate` the last plate read.
- Device triggers on every camera device ("Person detected", "Vehicle
  detected", "License plate read", "Activity", ...), so an automation can be
  built in the editor without templates.
- Sidebar entry **Auvi NVR**.

## Real-time events: how it works

The recorder has a rules engine (its console's **Rules** tab) whose
*webhook* action sends one HTTP request from the recorder itself. On
setup the integration installs one rule per camera there, named
`Home Assistant: <camera>` (plus one shared rule that covers the last minute
of the day, because the recorder's time windows are clock based), pointed at
a webhook on Home Assistant and authenticated with a secret the integration
stores in the recorder's keystore. The rules follow the camera list, are
rewritten if someone edits them, and are removed when the feed is switched
off in the options or the integration is deleted.

Each event is an `auvi_nvr_event` with `device_id`, `camera_id`,
`camera_name`, `type`, `activity` (true for object and analytic events),
`event_id`, `time`, `plate` and the recorder's full `metadata` document
(zones, plate reads, watch-list match, clothing colours, ...). For a
template condition:

```yaml
trigger:
  - platform: event
    event_type: auvi_nvr_event
    event_data:
      type: license_plate
condition: "{{ trigger.event.data.plate == 'ABC123' }}"
```

The recorder must be able to reach Home Assistant. By default the webhook
URL is Home Assistant's internal URL (Settings → System → Network); set the
*Home Assistant URL the recorder should post to* option when that is not
reachable from the recorder, e.g. `http://10.0.1.5:8123`. Posts are accepted
from the local network only unless that option is switched off. A webhook
rule fires at most once per second per camera.

## How the sidebar page works, and its limits

The page is proxied: `https://<your HA>/api/auvi_nvr/<entry>/local/` →
`https://<recorder>:8443/local/`. The integration sends its admin session
with each request and rewrites the recorder's links to the proxy path.

- **Live tiles use WebRTC.** The signalling goes through Home Assistant, but
  the video itself flows straight from the recorder to your browser, so live
  tiles play when the browser is on the recorder's LAN (or a VPN into it).
  Timeline playback and clip export go through Home Assistant and work from
  anywhere Home Assistant does.
- **Talk-down** is bridged through Home Assistant too, so it works wherever the page does.
- **"Log out"** inside the page ends only your panel session; the recorder's
  admin session the integration uses stays. Changing the admin password is
  refused from the frame: change it on the recorder, then update the
  integration (it will ask to sign in again).
- The recorder's pages run on Home Assistant's origin, exactly like Supervisor
  add-on ingress. Limit the panel to administrators in the options if that
  matters to you.

## Troubleshooting

- *Cannot connect*: check that `https://<recorder>:8443/healthz` answers from
  the Home Assistant host, and the Windows firewall rule for 8443 (the
  recorder's Access tab prints the exact command).
- *No camera entities*: the camera must be ticked on the recorder's RTSP tab,
  the RTSP listener must be on with a password, and the same password entered
  in the integration options.
- *Sidebar page says "Reconnecting"* for more than a moment: the Home
  Assistant log (`custom_components.auvi_nvr`) says why the recorder refused.
- *No events arrive*: open the recorder's Rules tab and check that the
  `Home Assistant: ...` rules exist and that a test fire reaches Home
  Assistant; the rule's last result shows the HTTP status. A 401 means the
  secret differs (the integration pushes it again on the next post); a
  connection error means the URL in the rule is not reachable from the
  recorder, so set the event URL option. The integration's diagnostics
  (⋮ → Download diagnostics) list received/rejected counts and the last
  rule error.

## License

MIT.
