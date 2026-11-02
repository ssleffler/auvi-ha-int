# Auvi NVR for Home Assistant

A custom integration that links Home Assistant to an **Auvi Video Recorder**
on your network:

- **Cameras.** Every stream the recorder shares over RTSP passthrough becomes a
  Home Assistant camera (live stream over RTSP, still image from the recorder's
  latest keyframe). Cameras that are not shared still get status sensors.
- **Recorder status.** Health, free storage, retention runway, uptime, cloud
  link, per-camera connected/recording state.
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
stream, and whether only administrators may open the sidebar panel.

## What you get

- Device **Auvi NVR &lt;hostname&gt;** with sensors: status, storage free,
  retention runway, uptime, agent version, cameras; binary sensors: cloud
  connected, RTSP passthrough.
- One device per camera (make, model, firmware from the recorder) with
  `camera.*` (shared cameras only), `binary_sensor.*_connected`,
  `*_recording`, `*_shared_over_rtsp`, and status/frame-rate/bitrate sensors.
- Sidebar entry **Auvi NVR**.

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

## License

MIT.
