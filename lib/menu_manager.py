# lib/menu_manager.py
# 3-button menu for V4 (BOOT, MENU, SELECT; RESET is hardwired reset).
# BOOT is also the GPIO0 strap pin, usable as a normal input after boot.

from machine import Pin
import time
import pinmap


class MenuManager:
    def __init__(self, display, network, power, debounce_ms=200):
        self.display = display
        self.network = network
        self.power = power
        self.select_btn = Pin(pinmap.BTN_SELECT, Pin.IN, Pin.PULL_UP)
        self.menu_btn   = Pin(pinmap.BTN_MENU,   Pin.IN, Pin.PULL_UP)
        self.boot_btn   = Pin(pinmap.BTN_BOOT,   Pin.IN, Pin.PULL_UP)
        self.debounce = debounce_ms

        self.menus = [
            ["Start Session", "Settings", "About"],
            ["Wi-Fi", "UDP Target", "Back"],
            ["Battery", "Version", "Power Off"],
        ]
        self.current_menu = 0
        self.current_selection = 0

    def _debounced(self, pin):
        if pin.value() == 0:
            time.sleep_ms(self.debounce)
            while pin.value() == 0:
                pass
            return True
        return False

    def check_buttons(self):
        if self._debounced(self.menu_btn):
            self.current_menu = (self.current_menu + 1) % len(self.menus)
            self.current_selection = 0

        if self._debounced(self.select_btn):
            self.current_selection = (
                self.current_selection + 1
            ) % len(self.menus[self.current_menu])

        # BOOT long-press → power off (only when in the 'About' menu)
        if self.boot_btn.value() == 0 and self.current_menu == 2:
            t0 = time.ticks_ms()
            while self.boot_btn.value() == 0:
                if time.ticks_diff(time.ticks_ms(), t0) > 1500:
                    self.power.power_off()
                    return

    def get_state(self):
        return {
            "mode": f"Menu {self.current_menu + 1}/{len(self.menus)}",
            "menu_items": self.menus[self.current_menu],
            "current_selection": self.current_selection,
        }
