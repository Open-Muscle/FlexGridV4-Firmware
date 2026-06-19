# FlexGrid V4 Firmware

MicroPython firmware for the OpenMuscle FlexGrid V4 wearable sensor bracelet. ESP32-S3-WROOM-1-N16R8 host, 60-sensor Velostat matrix on the flex PCB, plus new V4 additions: ICM-42688-P IMU, onboard microSD logging, and an RGB status LED.

> **Status: in development (June 2026).** Built against the OpenMuscle device-discovery + dual-transport protocol (see [`OpenMuscle-Connect/docs/DEVICE-DISCOVERY-SPEC.md`](https://github.com/Open-Muscle/OpenMuscle-Connect/blob/main/docs/DEVICE-DISCOVERY-SPEC.md) and `WIRE-FORMAT.md`). V4 boards are in fab; firmware will be flashed and brought up when boards arrive.

## What this firmware does

- Scans the 15 by 4 Velostat sensor matrix at the configured rate (default ~59 Hz).
- Reads the ICM-42688-P IMU for orientation.
- Announces itself on the local Wi-Fi network via mDNS (`_openmuscle._udp`) and a UDP broadcast beacon fallback. No hardcoded destination IP.
- Accepts hub subscriptions via a small command channel; unicasts each sensor frame to every subscribed hub (cap: 4 subscribers, 5 s heartbeat timeout).
- Logs sessions to microSD when triggered (untethered mode).
- Drives an RGB status LED to surface device state at a glance.
- Adds BLE GATT advertising and notification (phase 4, after Wi-Fi is bedded in).

## How V4 differs from V3 (firmware-relevant)

| Subsystem | V3 | V4 |
|---|---|---|
| Sensor matrix | 15 x 4 Velostat | 15 x 4 Velostat (unchanged) |
| Controller | ESP32-S3-WROOM-1-N16R8 | ESP32-S3-WROOM-1-N16R8 (unchanged) |
| Display | OLED SSD1306 128x32 | OLED SSD1306 128x32 (unchanged) |
| Storage | None | **microSD via SPI** (new) |
| Status indication | OLED only | **3-channel RGB LED on GPIO 40/41/42** (new) |
| IMU | Footprint only, never populated | **ICM-42688-P populated and active** (new) |
| Network model | Hardcoded destination IP in firmware | **Discovery + subscribe** (new protocol) |
| Bluetooth | None | **BLE GATT** (planned phase 4) |
| Haptic motor | Footprint only | Driver populated but **defaults OFF**, opt-in only |

## Repo layout

```
FlexGridV4-Firmware/
├── README.md
├── LICENSE                       (MIT, OpenMuscle)
├── boot.py                       (boot sequence, sys.path setup)
├── main.py                       (entry point)
├── flexgrid.py                   (main loop, wires the subsystems together)
└── lib/
    ├── pinmap.py                 (V4 GPIO assignments)
    ├── settings_manager.py       (NVS-backed settings + device UUID)
    ├── logger.py                 (persistent log to flash)
    ├── power_manager.py          (battery, charger, MAX16054 soft power)
    ├── sensor_matrix.py          (15x4 Velostat scan, mux driver)
    ├── display_manager.py        (OLED SSD1306)
    ├── menu_manager.py           (boot/select/menu buttons)
    ├── status_led.py             (NEW: RGB LED PWM control + state palette)
    ├── imu.py                    (NEW: ICM-42688-P read)
    ├── sd_logger.py              (NEW: microSD session recording)
    ├── discovery.py              (NEW: mDNS + UDP broadcast announce)
    ├── subscribers.py            (NEW: subscriber list + heartbeat)
    ├── commands.py               (NEW: command channel decode/dispatch)
    ├── network_manager.py        (REWRITTEN: Wi-Fi + new protocol send/receive)
    └── ble.py                    (PHASE 4: BLE GATT service + advertising)
```

## Protocol summary

Implements the OpenMuscle v1.0 protocol per the canonical specs:
- Discovery + transport spec: [`OpenMuscle-Connect/docs/DEVICE-DISCOVERY-SPEC.md`](https://github.com/Open-Muscle/OpenMuscle-Connect/blob/main/docs/DEVICE-DISCOVERY-SPEC.md)
- Wire format: [`OpenMuscle-Connect/docs/WIRE-FORMAT.md`](https://github.com/Open-Muscle/OpenMuscle-Connect/blob/main/docs/WIRE-FORMAT.md)

Announce payload (mDNS TXT + UDP broadcast JSON):
```json
{
  "v": "1.0",
  "type": "announce",
  "id": "flexgrid-<6hex>",
  "role": "source",
  "dev": "flexgrid",
  "fw": "v4.0.0",
  "transports": ["wifi"],
  "caps": ["sensor", "status", "cmd", "imu"],
  "matrix": [15, 4],
  "services": { "sensor": 3141, "cmd": 8001 }
}
```

In phase 4, `transports` becomes `["wifi","ble"]` and a BLE GATT service appears in advertisements.

## License

MIT. See `LICENSE`.
