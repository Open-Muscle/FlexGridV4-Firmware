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


# 8-bit color tuples (0-255 per channel) per named state.
#
# Palette discipline (board #0220 alignment with Tory's locked palette,
# 2026-06-30; CHARGE recolored purple per Tory 2026-07-02): no two
# distinct modes share an identical hue+pattern. Pattern carries the
# disambiguator where hues are reused:
#   PURPLE pulse vs PURPLE solid = charging vs full
#   RED pulse    vs RED blink    = right-role idle vs fault
# The idle hue ENCODES THE ROLE (blue=left, red=right, green=labeler,
# white=unassigned); active modes use mode-coded hues (orange=streaming,
# cyan=USB, purple=standby, magenta=provisioning, white-dim=sleep).
# The state names below are stable; flexgrid.py picks the right one
# from the current (power_mode, has_subscribers, vbus, role, fault,
# sleep) tuple every animator tick.
PALETTE_DEFAULT = {
    # --- Boot / system overlays ---
    "boot":             (0,   0,   200, "breathe"),     # blue slow breathe while booting / Wi-Fi connecting
    "fault":            (255, 0,   0,   "blink_fast"),  # red fast blink, e.g. recent WDT-reset
    "ap_provisioning":  (200, 0,   200, "blink_slow"),  # magenta 1 Hz blink during AP-mode provisioning
    "sleep":            (40,  40,  40,  "breathe_slow"), # very-dim white slow breathe (gyro stillness)
    # --- STREAM mode, idle (no subscribers): hue encodes role ---
    "idle_left":        (0,   0,   200, "pulse_3s"),    # brief blue pulse every 3s
    "idle_right":       (200, 0,   0,   "pulse_3s"),    # brief red pulse every 3s
    "idle_labeler":     (0,   200, 0,   "pulse_3s"),    # brief green pulse every 3s
    "idle_unassigned":  (100, 100, 100, "pulse_3s"),    # brief white pulse every 3s ("tag me")
    # --- STREAM mode, has subscribers ---
    "streaming":        (200, 80,  0,   "solid"),       # soft solid orange
    "streaming_on_usb": (200, 80,  0,   "stream_usb"),  # orange solid + brief cyan flash every 5s
    # --- STANDBY ---
    "standby":          (0,   140, 200, "breathe_slow"), # slow cyan breath, 3 s period
    # --- RECORD (SD logging, unsupervised; Wi-Fi off) ---
    "record":           (0,   200, 0,   "breathe"),      # green 1 Hz breath = capturing to SD
    # --- CHARGE mode (PURPLE per Tory 2026-07-02: "band turn purple") ---
    "charging":         (120, 0,   180, "pulse_1500"),  # slow purple pulse, 1.5 s period
    "charge_full":      (120, 0,   180, "solid"),       # solid purple = battery full
    # --- Legacy palette entries kept so existing callers do not break.
    # Slowly migrate them to the new names above and then drop these.
    "idle":             (32,  32,  32,  "solid"),
    "wifi_lost":        (200, 80,  0,   "blink_fast"),
    "error":            (255, 0,   0,   "blink_fast"),
    "calibrating":      (0,   200, 200, "breathe"),
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

    def set_rgb(self, r, g, b):
        """Public direct RGB write (0..255 per channel). For callers that
        need a fixed color outside the named-state palette, e.g. the
        provisioning AP-mode indicator. Bypasses the animator; the next
        set_state() call will overwrite it."""
        self._set_rgb(r, g, b)

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
        if mode == "breathe_slow":
            # 0.33 Hz triangle wave: 0 -> 1 -> 0 over 3000 ms. Used for
            # standby (purple) and sleep (very-dim white).
            phase = (t_ms % 3000) / 1500.0
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
        if mode == "pulse_3s":
            # Brief 200 ms peak at the start of every 3000 ms window;
            # dark for the remaining 2800 ms. Used for the role-color
            # idle indicator (BLUE/RED/GREEN/WHITE) so an unattended
            # device sips power and still flags its role at a glance.
            in_window = t_ms % 3000
            if in_window < 200:
                # Quick rise-fall over the 200 ms pulse for a softer look.
                k = in_window / 100.0
                if k > 1.0:
                    k = 2.0 - k
                self._set_rgb(int(r0 * k), int(g0 * k), int(b0 * k))
            else:
                self._set_rgb(0, 0, 0)
            return
        if mode == "pulse_1500":
            # 1.5 s period triangle pulse for CHARGE-mode "charging".
            # Always visible (not zero-floor) so the user sees the
            # device is actively charging even at a glance.
            phase = (t_ms % 1500) / 750.0
            if phase > 1.0:
                phase = 2.0 - phase
            k = 0.2 + 0.8 * phase   # 20%-100% range, never fully off
            self._set_rgb(int(r0 * k), int(g0 * k), int(b0 * k))
            return
        if mode == "stream_usb":
            # The "STREAM mode + USB plugged + has subscribers" composite:
            # solid orange base with a brief cyan flash every 5000 ms.
            # The flash is short (150 ms) so the streaming-orange identity
            # is dominant; the cyan blip says "you are streaming on USB
            # (charging slowly) -- press SELECT to switch to CHARGE".
            t_in = t_ms % 5000
            if t_in < 150:
                k = t_in / 75.0
                if k > 1.0:
                    k = 2.0 - k
                # Cyan overlay color
                self._set_rgb(int(0 * k), int(140 * k), int(200 * k))
            else:
                self._set_rgb(r0, g0, b0)
            return
