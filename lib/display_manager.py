# lib/display_manager.py
# SSD1306 128x64 OLED over I2C on the V3 rigid PCB.

import ssd1306
from machine import Pin, I2C
import pinmap
import logger


class DisplayManager:
    def __init__(self, width=128, height=64, i2c_freq=400000, i2c_addr=0x3C):
        self.oled = None
        self.width = width
        self.height = height
        try:
            self.i2c = I2C(0, scl=Pin(pinmap.I2C_SCL), sda=Pin(pinmap.I2C_SDA),
                           freq=i2c_freq)
            devices = self.i2c.scan()
            logger.debug(f"I2C devices found: {[hex(d) for d in devices]}")
            if i2c_addr not in devices:
                raise OSError(f"SSD1306 not at 0x{i2c_addr:02X}")
            self.oled = ssd1306.SSD1306_I2C(width, height, self.i2c, addr=i2c_addr)
            self.clear()
            logger.info(f"SSD1306 {width}x{height} initialized")
        except Exception as e:
            logger.error(f"SSD1306 init failed: {e}. Display disabled.")

    def clear(self):
        if not self.oled:
            return
        self.oled.fill(0)
        self.oled.show()

    def draw_sensor_matrix(self, matrix):
        """Render the 15x4 sensor matrix as a heatmap. Uses 8 px cells."""
        if not self.oled:
            return
        self.oled.fill(0)
        cols = len(matrix)
        rows = len(matrix[0]) if cols else 0
        cell = 8                                 # 15 * 8 = 120 px wide, 4 * 8 = 32 px tall
        x_off = max(0, (self.width - cols * cell) // 2)
        y_off = max(0, (self.height - rows * cell) // 2)
        for c in range(cols):
            for r in range(rows):
                v = matrix[c][r]
                x = x_off + c * cell
                y = y_off + r * cell
                if v < 200:
                    continue
                elif v < 1000:
                    self.oled.pixel(x + 3, y + 3, 1)
                elif v < 2000:
                    self.oled.fill_rect(x + 2, y + 2, 3, 3, 1)
                elif v < 3000:
                    self.oled.fill_rect(x + 1, y + 1, 5, 5, 1)
                else:
                    self.oled.fill_rect(x, y, cell - 1, cell - 1, 1)
        self.oled.show()

    def update(self, state):
        """Render menu / status. 128x64 = 8 lines of 8px text, 16 chars each."""
        if not self.oled:
            return
        self.oled.fill(0)
        line = 0
        mode = state.get('mode', '')
        if mode:
            self.oled.text(f"{mode}"[:16], 0, line * 8)
            line += 1
        for idx, item in enumerate(state.get('menu_items', [])):
            if line >= 8:
                break
            prefix = '>' if idx == state.get('current_selection', 0) else ' '
            self.oled.text(f"{prefix}{item}"[:16], 0, line * 8)
            line += 1
        self.oled.show()

    def text_screen(self, lines):
        """Show up to 8 lines of plain text (debug/status helper)."""
        if not self.oled:
            return
        self.oled.fill(0)
        for i, ln in enumerate(lines[:8]):
            self.oled.text(str(ln)[:16], 0, i * 8)
        self.oled.show()
