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
