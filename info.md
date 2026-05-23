# OneMeter

Unofficial Home Assistant integration for the **OneMeter** BLE energy-meter
optical reader. Reads the device + (when a meter is attached) its OBIS
register values via the stock firmware. Routes through your existing
Bluetooth source (USB adapter or ESPHome `bluetooth_proxy`).

**Not affiliated with OneMeter sp. z o.o.** Community reverse-engineering
project; brand artwork served by HA's central brands repository.

## Setup is a one-time per-device

You need your OneMeter device's **AES key** and **IV** (16 bytes each)
to configure the integration. These are written into the device's
flash during cloud registration and aren't available anywhere outside
the device. Currently the only documented extraction path is **SWD**
(a programmer connected to the device's debug pads).

See [the README](https://github.com/grappeq/onemeter-ha-integration#extracting-credentials)
for details before installing.

## Quick links

- [Full README](https://github.com/grappeq/onemeter-ha-integration#readme)
- [Known limitations](https://github.com/grappeq/onemeter-ha-integration#known-limitations)
  (the OneMeter device has a post-session cooldown that constrains how
  often you can poll — 1 h is the recommended default)
- [Issues](https://github.com/grappeq/onemeter-ha-integration/issues)
