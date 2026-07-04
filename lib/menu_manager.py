# lib/menu_manager.py
# 2-button OLED menu for V4: MENU cycles menus, SELECT cycles items.
# The silkscreen "BOOT" button is wired to EN (hardware reset), NOT a
# GPIO, so it carries no menu action (schematic-verified 2026-07-02).

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

    def get_state(self):
        return {
            "mode": f"Menu {self.current_menu + 1}/{len(self.menus)}",
            "menu_items": self.menus[self.current_menu],
            "current_selection": self.current_selection,
        }
