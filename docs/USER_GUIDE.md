# YandexDevices - User Guide

![YandexDevices Icon](../static/YandexDevices.png "YandexDevices plugin")

## Purpose

`YandexDevices` integrates osysHome with Yandex Quasar IoT and Yandex Stations.

After setup, the module lets you:

- authorize Yandex account access via QR flow;
- import and refresh station/device lists from Yandex cloud;
- monitor Yandex device capabilities and sensor-like properties;
- link capability values to osysHome object properties and methods;
- send control commands from osysHome back to Yandex devices;
- run cloud TTS on selected Yandex Stations.

> [!IMPORTANT]
> The integration is bidirectional: Yandex -> osysHome value updates and osysHome -> Yandex control commands.

---

## Interface Overview

Main admin page:

```text
/admin/YandexDevices
```

Top actions:

1. `Update` - refreshes stations and devices from Yandex cloud.
2. `Authorization` - opens QR-based login status and flow.
3. `Settings` - polling and update behavior.

Tabs:

- `Stations` - station-level settings and TTS behavior.
- `Devices` - discovered IoT devices and capability linking.

---

## Quick Start Checklist

- [ ] Open `/admin/YandexDevices`.
- [ ] Go to `Authorization` and complete QR login.
- [ ] Click `Update` to import stations and IoT devices.
- [ ] Open `Stations` and configure TTS mode for needed stations.
- [ ] For LAN: set station **IP**, **Generate token**, optional **osysHome object** (`glagol_linked_object`).
- [ ] Open `Devices`, choose a device, and configure capability links.
- [ ] Enable module polling in `Settings` if periodic updates are required.

---

## Authorization (QR Flow)

Open:

```text
YandexDevices?op=auth
```

Then:

1. Click `QR code`.
2. Scan the QR code using Yandex app.
3. Click `Continue` to confirm.
4. Verify status becomes `Authorized`.

If token/cookie becomes invalid, use `Reset` and re-run QR login.

> [!WARNING]
> Without successful authorization, refresh and control calls to Yandex APIs will fail.

---

## Stations Tab

The stations list shows:

- station title and icon;
- minimum SAY level (`Min level say`);
- online state (`Online`/`Offline`);
- last update timestamp.

### Station edit form

Fields:

| Field | Meaning |
| --- | --- |
| `Title` | Station name in module DB |
| `Platform` | Yandex platform identifier |
| `IOT id` | Quasar IoT identifier |
| `IP` | Station LAN address (required for Glagol) |
| `Token` | Device token (`Generate token` in station card) |
| `osysHome object (name)` | `glagol_linked_object` — object that receives Glagol snapshot properties |
| `TTS` | `No`, `Local (Glagol / LAN)`, `Cloud` |
| `Min level SAY` | Minimum message level required for `say()` |

The station edit page is two columns: **settings** on the left; **LAN Glagol status**, **player**, and property hints on the right. Status and track info update over Socket.IO when a background WebSocket to the station is active.

If token is missing, use `Generate token` inside station edit page.

---

## LAN Glagol (local control)

Glagol is Yandex’s LAN protocol (WebSocket, port **1961**). Use it for local TTS, player control, and pushing station state into an osysHome object without polling HTTP.

### Setup

1. Complete **Authorization** and **Update** so stations appear in the list.
2. Open the station card: set **IP** on the LAN, click **Generate token**.
3. Optionally set **osysHome object (name)** — the same name as the object in the object tree (not a DB id).
4. Create matching properties on that object (the station card lists suggested property names from the Glagol snapshot).

### Commands from object methods

See **[Commands.md](Commands.md)** for full `glagol_command` parameters and examples.

```python
from app.core.lib.common import callPluginFunction

r = callPluginFunction("YandexDevices", "glagol_command", {
    "object": self.name,
    "text": "Turn on the living room light",
})
```

`self.name` must match `glagol_linked_object` on the station.

### Live admin UI

When IP and token are set, the plugin keeps a background connection. The linked object receives: `state`, `volume`, `muted`, `alice_state`, `media_title`, `media_subtitle`, `media_duration`, `media_progress`, `media_cover_url`.

The station page subscribes via Socket.IO (`subscribeData` → `YandexDevices`):

- `glagol_snapshot` — player / track;
- `glagol_ws_status` — connection phase, RX/TX frame counters.

---

## Devices Tab

The devices list shows:

- title and icon;
- Yandex type;
- room;
- IoT ID;
- last update time.

Open a device to edit:

- `Update period` (per-device polling override);
- links for each discovered capability/property;
- read-only mode for link direction.

---

## How Capability Links Work

Each device has capabilities/properties like:

- `devices.capabilities.on_off`
- `devices.capabilities.range.temperature`
- `devices.properties.float.temperature`
- `devices.properties.event.open`

Each record may be linked to:

- `linked_object` + `linked_property`
- `linked_object` + `linked_method`

### Property link behavior

- incoming Yandex value is written to osysHome property;
- if link is writable (not read-only), reverse updates from osysHome can be sent back to Yandex device.

### Method link behavior

On value change, module calls linked object method with payload:

```json
{
  "NEW_VALUE": "...",
  "OLD_VALUE": "...",
  "DEVICE_STATE": 1,
  "UPDATED": "timestamp",
  "MODULE": "YandexDevices"
}
```

---

## SAY and TTS

Action:

```text
say(message, level=0, args=None)
```

Filtering logic:

- station must have `tts` enabled;
- station must have `min_level`;
- `level` must be >= `min_level`.

Modes:

- `tts = 1` (`Local (Glagol / LAN)`) — `sendText` over LAN; requires IP, `platform`, `iot_id`, and device token.
- `tts = 2` (`Cloud`) — Yandex scenario-based server action.

Long cloud messages are split into short sentences and sent sequentially.

---

## Settings

Global settings in modal:

| Setting | Meaning |
| --- | --- |
| `Enable get device data` | Enables periodic polling in cyclic task |
| `Default update period device data (seconds)` | Default interval for devices without per-device value |
| `Update only linked devices` | Poll only devices that have at least one linked capability |

---

## Widget

Module exposes `widget` action and template showing:

- number of stations;
- number of devices.

---

## Troubleshooting

### Authorization remains `Not authorized`

Check:

- QR flow was fully completed with `Continue`;
- cookie/token were not reset;
- server can reach Yandex endpoints.

### Devices are listed but values do not update

Check:

- `Enable get device data` is enabled;
- per-device `Update period` is valid;
- account still authorized.

### Property/method links do not react

Check:

- link points to existing object/property or object/method;
- device was polled after saving links;
- capability actually provides a changing value.

### Reverse control does not work

Check:

- capability is not marked `Readonly`;
- capability type supports outgoing action semantics in current implementation.

---

### LAN Glagol does not connect

Check:

- station **IP** is reachable from the osysHome host;
- **device token** is current (regenerate if needed);
- firewall allows outbound TCP to the station on port **1961**.

---

## See Also

- [Glagol commands (Commands.md)](Commands.md)
- [Technical Reference](TECHNICAL_REFERENCE.md)
- [Module index](index.md)
