# boot.py - runs first on every reset.
# Keep this minimal so REPL access stays reachable even if main.py crashes.
import sys
sys.path.append('/lib')

# A faint glow from the first moment MicroPython runs, so a powered band is
# never dark, even before main.py starts or if it crashes. Same pins and
# level as the status LED's floor (lib/status_led.py POWER_FLOOR); the
# StatusLed object takes the channels over later.
try:
    from machine import Pin, PWM
    import pinmap
    for _pin in (pinmap.RGB_R, pinmap.RGB_G, pinmap.RGB_B):
        PWM(Pin(_pin), freq=1000, duty_u16=6 << 8)
except Exception:
    pass
