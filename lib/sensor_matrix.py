# lib/sensor_matrix.py
# 15-column x 4-row Velostat sensor matrix on the V3 flex PCB.
#
# v0.1.7: discharge bumped 30us -> 100us.
#   Empirical: with 30us discharge + discard-first-read, the
#   right-direction carryover was still ~60% per col (1460 -> 934 -> 557 ->
#   ...). 100us appears to clear it. Per-cell time goes up but scan is
#   still well above 50 Hz target.
#
# v0.1.6: discard-first-read trick + 30us discharge.
#   Pin.init(IN) -> ADC.read() can return a stale sample latched into
#   the SAH cap before the Pin transition. Discard the first read so
#   the second sees the post-transition voltage. Known MicroPython
#   ESP32 ADC behavior.
# v0.1.5: mux ENABLE gated address writes + per-cell row discharge.
# v0.1.2: row-outer, col-inner scan with ground-other-rows.

from machine import Pin, ADC
import time
import pinmap


class SensorMatrix:
    def __init__(self, attenuation=ADC.ATTN_11DB, settle_us=5,
                 discharge_us=100, addr_settle_us=2, avg_samples=1):
        # 4-bit MUX address lines (CD74HC4067)
        self.S = [
            Pin(pinmap.MUX_S0, Pin.OUT),
            Pin(pinmap.MUX_S1, Pin.OUT),
            Pin(pinmap.MUX_S2, Pin.OUT),
            Pin(pinmap.MUX_S3, Pin.OUT),
        ]
        # MUX enable: active LOW. Hold HIGH (= disabled) while changing address
        # to prevent the mux from briefly routing intermediate channel numbers.
        self.mux_en = Pin(pinmap.MUX_EN, Pin.OUT, value=0)

        self._row_nums = (pinmap.ADC_ROW_0, pinmap.ADC_ROW_1,
                          pinmap.ADC_ROW_2, pinmap.ADC_ROW_3)
        self.row_pins = [Pin(n, Pin.IN) for n in self._row_nums]
        self.adc = []
        for n in self._row_nums:
            a = ADC(Pin(n))
            a.atten(attenuation)
            self.adc.append(a)

        self.num_cols = pinmap.NUM_COLS
        self.num_rows = pinmap.NUM_ROWS
        self.settle_us = settle_us
        self.discharge_us = discharge_us
        self.addr_settle_us = addr_settle_us
        self.avg_samples = avg_samples

        # Pre-allocated matrix; reused across scans to avoid GC pressure.
        self.matrix = [[0] * self.num_rows for _ in range(self.num_cols)]

    def _select_column(self, channel):
        # Gate the address change behind mux-disable so intermediate states
        # never get routed to a real channel (esp. accidentally back to col 0).
        self.mux_en.value(1)              # disable mux
        self.S[0].value(channel & 0x1)
        self.S[1].value((channel >> 1) & 0x1)
        self.S[2].value((channel >> 2) & 0x1)
        self.S[3].value((channel >> 3) & 0x1)
        # Tiny pause so the address pins are stable before re-enabling.
        if self.addr_settle_us:
            time.sleep_us(self.addr_settle_us)
        self.mux_en.value(0)              # re-enable at new (clean) address

    def _set_row_mode(self, target_row):
        """target_row = INPUT (ADC), all others = OUTPUT LOW (sneak-path shunt)."""
        for i, p in enumerate(self.row_pins):
            if i == target_row:
                p.init(Pin.IN)
            else:
                p.init(Pin.OUT, value=0)

    def _discharge_and_read(self, row):
        """Drain residual parasitic charge from the row trace, then read.

        Two-read trick: the first ADC.read() after a Pin mode change can
        return a stale sample (sample-and-hold latched the pre-transition
        voltage). Discard it. Subsequent reads are fresh.
        """
        p = self.row_pins[row]
        # Hard-drive the row pin low to drain trace + SAH cap.
        p.init(Pin.OUT, value=0)
        if self.discharge_us:
            time.sleep_us(self.discharge_us)
        # Back to INPUT (ADC) for sampling.
        p.init(Pin.IN)
        if self.settle_us:
            time.sleep_us(self.settle_us)
        # Discard first read (may be stale), then take real sample(s).
        self.adc[row].read()
        if self.avg_samples <= 1:
            return self.adc[row].read()
        s = 0
        for _ in range(self.avg_samples):
            s += self.adc[row].read()
        return s // self.avg_samples

    def scan_matrix(self):
        """
        Row-outer, col-inner scan with mux-gated address changes and
        per-cell row discharge. Returns the pre-allocated matrix.
        """
        m = self.matrix
        for row in range(self.num_rows):
            self._set_row_mode(row)
            for col in range(self.num_cols):
                self._select_column(col)
                m[col][row] = self._discharge_and_read(row)
        # Park rows as inputs so external code can read ADCs freely.
        for p in self.row_pins:
            p.init(Pin.IN)
        return m
