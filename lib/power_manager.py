# lib/power_manager.py
# Power management for V3: battery voltage monitoring + MAX16054 soft power-off.
#
# Voltage path: VBAT --R7(100k)--> ADC_BAT (GPIO18) --R8(100k)--> GND
# That's a 2:1 divider, so Vbat = 2 * V(GPIO18).
#
# Accuracy note: the ESP32-S3 SAR ADC is markedly nonlinear at ATTN_11DB --
# a naive linear (raw / 4095) * 3.3 V mapping under-reports by ~100-200 mV
# (which doubles to 200-400 mV on the battery side). We use the chip's
# factory-calibrated `read_uv()` whenever it's available (MicroPython 1.20+
# on ESP32-S3) and fall back to a piecewise-linear correction only if the
# port lacks read_uv. In practice on the boards we've measured, read_uv
# agrees with a multimeter at the battery terminal to within ~20 mV.

from machine import Pin, ADC
import time
import pinmap
import logger


class PowerManager:
    VBAT_DIVIDER = 2.0          # R7 == R8 -> Vbat = 2 * V(pin)
    ADC_FULL_SCALE = 4095
    ADC_VREF_FALLBACK = 3.3     # nominal full-scale for ATTN_11DB; used only
                                #   when read_uv() isn't available
    AVG_SAMPLES = 64            # ~30 ms of sampling at typical ADC clock

    # 1S LiPo voltage curve (rough -- LiPo discharge is nonlinear, this is
    # 'good enough for a status icon'). Below 3.5 V the cell falls off a
    # cliff fast, so we clip to 0% there.
    LIPO_FULL = 4.20
    LIPO_EMPTY = 3.50

    # VBUS-present heuristic (board #0220): V4 does not route the LTC4054
    # CHRG status pin to a GPIO, so we infer USB-plugged from battery
    # voltage. An unloaded 1S LiPo sitting on a discharge curve does not
    # cross 4.05 V; if we see VBAT above that under live load, USB is in
    # and the charger is feeding both the rail and the cell. Hysteresis
    # gap (4.05 enter / 3.95 exit) keeps the bit from chattering during
    # CV-phase ripple.
    VBUS_PRESENT_ENTER_V = 4.05
    VBUS_PRESENT_EXIT_V  = 3.95
    # CV-phase steady reading: anything at-or-above 4.18 V for several
    # samples means the charger is in trickle / done. We surface this as
    # "battery full" so the LED can switch from charging-pulse to solid.
    BATTERY_FULL_V = 4.18

    def __init__(self):
        self.bat_adc = ADC(Pin(pinmap.ADC_BAT))
        self.bat_adc.atten(ADC.ATTN_11DB)
        # WIDTH_12BIT is the only width ESP32-S3 MicroPython actually
        # supports; setting it explicitly is harmless on builds that ignore
        # the call.
        try:
            self.bat_adc.width(ADC.WIDTH_12BIT)
        except (AttributeError, ValueError):
            pass
        self.pwr_off = Pin(pinmap.PWR_OFF, Pin.OUT, value=0)

        # Probe for read_uv() once at init -- avoids per-call exception cost.
        self._has_read_uv = hasattr(self.bat_adc, "read_uv")

        # VBUS presence. Preferred source: the GPIO39 divider (R19/R20
        # off USB VBUS, found in the V4 netlist 2026-07-02) - a real
        # digital signal with clean edges. Fallback when the pin init
        # fails (or on hardware without the divider): the hysteretic
        # VBAT-threshold heuristic below, which is known to miss the
        # unplug edge on a nearly-full battery.
        self._vbus_pin = None
        try:
            self._vbus_pin = Pin(pinmap.VBUS_STAT, Pin.IN)
        except Exception:
            pass
        v0 = self.battery_voltage()
        self._vbus_present = (self._vbus_pin.value() == 1) if self._vbus_pin \
            else (v0 >= self.VBUS_PRESENT_ENTER_V)
        self._full_streak = 0

    def battery_raw(self):
        """Average of AVG_SAMPLES raw samples to reduce ADC noise (~12 bits LSB)."""
        s = 0
        for _ in range(self.AVG_SAMPLES):
            s += self.bat_adc.read()
        return s // self.AVG_SAMPLES

    def battery_uv(self):
        """Calibrated microvolts at the ADC pin, or None if read_uv missing."""
        if not self._has_read_uv:
            return None
        s = 0
        for _ in range(self.AVG_SAMPLES):
            s += self.bat_adc.read_uv()
        return s // self.AVG_SAMPLES

    def battery_voltage(self):
        """Battery voltage (V) at the JST connector, after the 2:1 divider."""
        uv = self.battery_uv()
        if uv is not None:
            return (uv / 1_000_000.0) * self.VBAT_DIVIDER
        # Fallback: linear approximation. Known to under-read by 100-200 mV
        # at high LiPo voltages on ESP32-S3 -- prefer read_uv when available.
        raw = self.battery_raw()
        return (raw / self.ADC_FULL_SCALE) * self.ADC_VREF_FALLBACK * self.VBAT_DIVIDER

    def battery_percent(self):
        """0..100 estimate for a 1S LiPo. Clipped at both ends."""
        v = self.battery_voltage()
        if v <= self.LIPO_EMPTY:
            return 0
        if v >= self.LIPO_FULL:
            return 100
        return int((v - self.LIPO_EMPTY) / (self.LIPO_FULL - self.LIPO_EMPTY) * 100)

    def update_vbus_state(self):
        """Re-evaluate the VBUS-present + battery-full flags. Prefers the
        GPIO39 hardware detect (exact, edge-clean); falls back to the
        hysteretic VBAT threshold when the pin is unavailable. Call from
        a periodic power-mode loop (1 Hz is fine). Returns the current
        (vbus_present, battery_full) tuple."""
        v = self.battery_voltage()
        if self._vbus_pin is not None:
            try:
                self._vbus_present = self._vbus_pin.value() == 1
                if not self._vbus_present:
                    self._full_streak = 0
            except Exception:
                self._vbus_pin = None   # fall back permanently this boot
        if self._vbus_pin is None:
            if self._vbus_present:
                # Sticky: only drop to False below the lower threshold
                if v < self.VBUS_PRESENT_EXIT_V:
                    self._vbus_present = False
                    self._full_streak = 0
            else:
                if v >= self.VBUS_PRESENT_ENTER_V:
                    self._vbus_present = True

        # battery_full requires VBUS present AND several consecutive
        # samples at/above the CV-trickle threshold. Without the streak
        # gate, a single voltage spike on the rail (e.g. a USB hub
        # negotiating) would flip the LED to solid-cyan briefly.
        if self._vbus_present and v >= self.BATTERY_FULL_V:
            self._full_streak += 1
        else:
            self._full_streak = 0
        return self._vbus_present, self.is_battery_full()

    def is_vbus_present(self):
        """Latest VBUS-present bit. Set by update_vbus_state(); cheap to read."""
        return self._vbus_present

    def is_battery_full(self):
        """True once the streak counter says we have been at/above the
        CV-trickle threshold for at least 5 consecutive update ticks."""
        return self._full_streak >= 5

    def power_off(self):
        """Toggle the MAX16054 to drop the main rail. Goodbye, world."""
        logger.info("Power-off requested")
        # MAX16054 latch is released by a pulse on its KILL input.
        self.pwr_off.value(1)
        time.sleep_ms(200)
        self.pwr_off.value(0)
        # If we're still here after a few seconds, USB is keeping us alive.
        time.sleep(3)
        logger.warn("Still alive -- likely USB-powered, latch can't drop rail")
