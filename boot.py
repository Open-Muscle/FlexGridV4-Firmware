# boot.py - runs first on every reset.
# Keep this minimal so REPL access stays reachable even if main.py crashes.
import sys
sys.path.append('/lib')

# A faint glow from the first moment MicroPython runs, so a powered band is
# never dark, even before main.py starts or if it crashes. Same pins and
# level as the status LED's floor (lib/status_led.py POWER_FLOOR); the
# StatusLed object takes the channels over later.
# The channels are kept on pinmap.BOOT_PWM so StatusLed takes them over
# instead of opening new ones: a second PWM owner on the same pins left
# the firmware's patterns unable to reach the LED (seen on a kit band).
try:
    from machine import Pin, PWM
    import pinmap
    pinmap.BOOT_PWM = tuple(PWM(Pin(_pin), freq=1000, duty_u16=6 << 8)
                            for _pin in (pinmap.RGB_R, pinmap.RGB_G, pinmap.RGB_B))
except Exception:
    pass
