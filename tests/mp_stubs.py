"""Desktop stand-ins for the MicroPython-only modules, so the firmware logic
can be unit-tested with plain CPython (no board needed).

    python -m unittest discover -s tests          (from the repo root)

Import this module first in every test file. It:
  * puts lib/ and the repo root on sys.path,
  * installs fake machine / network / uos / ujson / ssd1306 / framebuf /
    micropython modules,
  * adds the time.ticks_* helpers and asyncio.sleep_ms that MicroPython has
    and CPython lacks.
Nothing here touches real hardware, a real network or the real filesystem.
"""
import asyncio
import json
import os
import sys
import time
import types
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (os.path.join(ROOT, "lib"), ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)


class FakePin:
    """machine.Pin stand-in. `level` is what value() returns (1 = released
    for the active-low buttons)."""
    IN = 0
    OUT = 1
    PULL_UP = 2

    def __init__(self, *a, **k):
        self.level = 1

    def value(self, v=None):
        if v is not None:
            self.level = v
        return self.level


def _install():
    machine = types.ModuleType("machine")
    machine.Pin = FakePin
    for name in ("ADC", "PWM", "I2C", "SoftI2C", "SPI", "SDCard", "WDT"):
        setattr(machine, name, mock.MagicMock(name="machine." + name))
    machine.freq = mock.MagicMock(name="machine.freq")
    machine.reset = mock.MagicMock(name="machine.reset")
    machine.soft_reset = mock.MagicMock(name="machine.soft_reset")
    sys.modules["machine"] = machine

    net = types.ModuleType("network")
    net.STA_IF = 0
    net.AP_IF = 1
    net.STAT_IDLE = 1000
    net.STAT_CONNECTING = 1001
    net.STAT_GOT_IP = 1010
    net.STAT_NO_AP_FOUND = 201
    net.STAT_WRONG_PASSWORD = 202
    net.STAT_ASSOC_FAIL = 203
    net.STAT_HANDSHAKE_TIMEOUT = 204
    net.WLAN = mock.MagicMock(name="network.WLAN")
    sys.modules["network"] = net

    sys.modules["uos"] = os
    sys.modules["ujson"] = json
    for name in ("ssd1306", "framebuf"):
        sys.modules[name] = mock.MagicMock(name=name)
    mp = types.ModuleType("micropython")
    mp.const = lambda x: x
    sys.modules["micropython"] = mp

    # MicroPython time helpers.
    if not hasattr(time, "ticks_ms"):
        # Counts from "boot" (now) like the real clock, so the 30-bit wrap
        # is never near and ticks_diff(now, 0) is simply the uptime.
        t0 = time.monotonic()
        time.ticks_ms = lambda: int((time.monotonic() - t0) * 1000) + 1000
        time.ticks_us = lambda: int((time.monotonic() - t0) * 1000000) + 1000000
        time.ticks_add = lambda a, b: (a + b) & 0x3FFFFFFF
        time.ticks_diff = lambda a, b: ((a - b + 0x20000000) & 0x3FFFFFFF) - 0x20000000
    if not hasattr(asyncio, "sleep_ms"):
        asyncio.sleep_ms = lambda ms: asyncio.sleep(ms / 1000.0)


_install()

import logger  # noqa: E402  (real lib/logger.py; persistence stays off)

logger.DEBUG = False


def load_by_path(name, path):
    """Import a module from an explicit file path under a private name, so
    two copies of the same firmware file can be tested side by side."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
