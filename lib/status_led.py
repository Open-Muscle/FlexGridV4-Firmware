# lib/status_led.py
# 3-channel PWM-driven common-cathode RGB status LED.
#
# V4 hardware: discrete RGB LED (Avago ASMB-UTF2-0E20B), common cathode,
# 330 ohm series resistors on each anode. Channels:
#   R = pinmap.RGB_R (GPIO 40)
#   G = pinmap.RGB_G (GPIO 41)
#   B = pinmap.RGB_B (GPIO 42)
#
# Zero quiescent current when off: duty = 0 on all channels, the LED draws
# no current. This matters because the 500 mAh battery has to last hours
# of standby and an always-on indicator would eat budget.
#
# The state palette maps named device states to RGB tuples + animation hints.
# The main loop calls status_led.set_state(name) and the LED reflects it.
# Animations (breathe, blink) are driven by a coroutine in flexgrid.py.

from machine import Pin, PWM
import pinmap


# 8-bit color tuples (0-255 per channel) for each named state.
# The exact palette is firmware-controlled, not user-facing; tune to taste.
PALETTE_DEFAULT = {
    "boot":         (0,   0,   255, "breathe"),    # blue slow breathe while booting / Wi-Fi connecting
    "idle":         (32,  32,  32,  "solid"),      # dim white at idle (no peer subscribed)
    "streaming":    (0,   200, 0,   "solid"),      # green solid while at least one hub is subscribed and receiving
    "recording":    (200, 200, 0,   "breathe"),    # yellow breathe during a session recording
    "predicting":   (180, 0,   200, "solid"),      # purple solid when pushing predictions to an actuator
    "sleeping":     (0,   0,   0,   "solid"),      # off
    "wifi_lost":    (200, 80,  0,   "blink_fast"), # orange fast blink when Wi-Fi dropped
    "error":        (255, 0,   0,   "blink_fast"), # red fast blink on sensor/IMU/SD fault
    "calibrating":  (0,   200, 200, "breathe"),    # cyan breathe in calibration menu
}


# PWM frequency. ~1 kHz is the sweet spot: visibly flicker-free, well within
# the ESP32-S3's LEDC capabilities, no audible whine from the components.
_PWM_FREQ_HZ = 1000


class StatusLed:
    def __init__(self, palette=None):
        self._r = PWM(Pin(pinmap.RGB_R), freq=_PWM_FREQ_HZ, duty_u16=0)
        self._g = PWM(Pin(pinmap.RGB_G), freq=_PWM_FREQ_HZ, duty_u16=0)
        self._b = PWM(Pin(pinmap.RGB_B), freq=_PWM_FREQ_HZ, duty_u16=0)
        self.palette = palette or PALETTE_DEFAULT
        self.state = "boot"
        self._target = self.palette[self.state]
        # Phase advances 0..1 for animations; the animator coroutine updates it.
        self._phase = 0.0
        self._set_rgb(self._target[0], self._target[1], self._target[2])

    def _set_rgb(self, r, g, b):
        """Write raw 0-255 RGB to the PWM channels."""
        # 8-bit -> 16-bit duty, common cathode so duty 0 = off.
        self._r.duty_u16(int(r) << 8)
        self._g.duty_u16(int(g) << 8)
        self._b.duty_u16(int(b) << 8)

    def set_state(self, state):
        """Switch to a named state from the palette. Unknown states fall back
        to 'idle' silently (we never want a typo to brick the LED)."""
        if state not in self.palette:
            state = "idle"
        self.state = state
        self._target = self.palette[state]
        # For solid states snap to the target instantly; animated states will
        # be driven by the animator on its next tick.
        if self._target[3] == "solid":
            self._set_rgb(self._target[0], self._target[1], self._target[2])

    def off(self):
        """Force off regardless of state. Used at deep sleep / shutdown."""
        self._set_rgb(0, 0, 0)

    def animate(self, t_ms):
        """Advance animation by the wall-clock time delta. Called by the
        animator coroutine in flexgrid.py at ~30 Hz. t_ms is the current
        millisecond clock; we use it modulo the period for the chosen mode.
        """
        r0, g0, b0, mode = self._target
        if mode == "solid":
            return
        if mode == "breathe":
            # 1 Hz triangle wave: 0 -> 1 -> 0 over 1000 ms.
            phase = (t_ms % 1000) / 500.0
            if phase > 1.0:
                phase = 2.0 - phase
            k = phase
            self._set_rgb(int(r0 * k), int(g0 * k), int(b0 * k))
            return
        if mode == "blink_fast":
            # ~3 Hz square wave, ~165 ms on / ~165 ms off.
            on = (t_ms // 165) % 2 == 0
            if on:
                self._set_rgb(r0, g0, b0)
            else:
                self._set_rgb(0, 0, 0)
            return
        if mode == "blink_slow":
            on = (t_ms // 500) % 2 == 0
            if on:
                self._set_rgb(r0, g0, b0)
            else:
                self._set_rgb(0, 0, 0)
            return
