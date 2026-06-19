# lib/imu.py
# ICM-42688-P IMU driver.
#
# V4 hardware change: the IMU footprint is populated. Connected via I2C on
# the same bus as the OLED (pinmap.I2C_SDA / pinmap.I2C_SCL). Default I2C
# address is 0x68 (AD0 strap low); confirm against the V4 schematic and
# adjust pinmap.IMU_I2C_ADDR if needed.
#
# This is a minimal driver: WHO_AM_I check, set the ODR/range, periodic read
# of accel and gyro into a status snapshot. We intentionally do not stream
# IMU data at the same rate as the sensor matrix; the main loop reads it at
# whatever cadence the status loop runs (default ~5 s) and attaches the
# latest values to the device status `meta` block.
#
# Future work (not in v1): higher-rate IMU read for orientation fusion
# during recording, with the IMU samples interleaved with sensor frames at
# the source. That requires a deliberate frame format change and is out of
# scope for the initial bring-up.
#
# Datasheet references: TDK InvenSense ICM-42688-P, document DS-000347 r1.8.

from machine import I2C, Pin
import time
import pinmap
import logger


# Register addresses (Bank 0). The chip has user-bank switching for advanced
# config; we stay in bank 0 for everything the firmware needs.
_REG_WHO_AM_I       = 0x75
_REG_PWR_MGMT0      = 0x4E
_REG_GYRO_CONFIG0   = 0x4F
_REG_ACCEL_CONFIG0  = 0x50
_REG_TEMP_DATA1     = 0x1D  # temp high byte; subsequent registers are accel + gyro
_REG_ACCEL_DATA_X1  = 0x1F  # 6 bytes: accel X/Y/Z big-endian
_REG_GYRO_DATA_X1   = 0x25  # 6 bytes: gyro X/Y/Z big-endian

# WHO_AM_I expected value for the ICM-42688-P. The -PC variant returns the
# same; treat both as compatible.
_WHO_AM_I_EXPECTED = 0x47

# Power management: enable accel + gyro low-noise mode. Bits per datasheet
# section 14.36 (PWR_MGMT0):
#   bit 5: TEMP_DIS    0 = enabled
#   bit 4: IDLE        0 = on
#   bits 3:2: GYRO_MODE   00=off, 01=standby, 10=reserved, 11=low_noise
#   bits 1:0: ACCEL_MODE  00=off, 01=low_power, 10=low_noise, 11=reserved
# We want gyro + accel both in low-noise mode: 0b00001111 = 0x0F.
_PWR_MGMT0_LN = 0x0F

# Default range/ODR: +/-4g, 1000 dps, 100 Hz. Easy on the bus and plenty
# accurate for orientation tracking during a wearable session.
# ACCEL_CONFIG0: bits 7:5 = full scale (000=16g, 001=8g, 010=4g, 011=2g),
#                bits 3:0 = ODR (0110 = 1 kHz, 1000 = 100 Hz)
_ACCEL_CONFIG0 = (0b010 << 5) | 0b1000   # +/-4g, 100 Hz
# GYRO_CONFIG0:  bits 7:5 = full scale (000=2000dps, 001=1000dps, ...)
#                bits 3:0 = ODR
_GYRO_CONFIG0  = (0b001 << 5) | 0b1000   # +/-1000 dps, 100 Hz


class IMU:
    def __init__(self, i2c=None, addr=None):
        self.addr = addr if addr is not None else pinmap.IMU_I2C_ADDR
        if i2c is None:
            i2c = I2C(0, sda=Pin(pinmap.I2C_SDA), scl=Pin(pinmap.I2C_SCL), freq=400_000)
        self.i2c = i2c
        self.present = False
        # Latest sample, refreshed by read(). Units: accel = raw counts at +/-4g,
        # gyro = raw counts at +/-1000 dps, temp = raw counts.
        self.last = {
            "ax": 0, "ay": 0, "az": 0,
            "gx": 0, "gy": 0, "gz": 0,
            "temp_c": None,
        }

    def init(self):
        """Probe + configure the IMU. Returns True on success. If the chip
        is absent or unresponsive, returns False and self.present stays
        False; the main loop should treat this as a non-fatal warning and
        skip IMU reads."""
        try:
            who = self._read_reg(_REG_WHO_AM_I)
            if who != _WHO_AM_I_EXPECTED:
                logger.warn("IMU WHO_AM_I unexpected: 0x{:02X} (want 0x{:02X})".format(
                    who, _WHO_AM_I_EXPECTED))
                return False
            # Bring power management up first; some samples need a small
            # delay before subsequent register writes take effect.
            self._write_reg(_REG_PWR_MGMT0, _PWR_MGMT0_LN)
            time.sleep_ms(2)
            self._write_reg(_REG_ACCEL_CONFIG0, _ACCEL_CONFIG0)
            self._write_reg(_REG_GYRO_CONFIG0, _GYRO_CONFIG0)
            time.sleep_ms(2)
            self.present = True
            logger.info("IMU present (WHO_AM_I=0x{:02X}); accel +/-4g 100Hz, gyro +/-1000dps 100Hz".format(who))
            return True
        except Exception as e:
            logger.warn("IMU init failed: {}".format(e))
            return False

    def read(self):
        """One-shot read of accel + gyro + temp. Updates self.last and
        returns it. Safe to call from a status loop at any rate; the chip
        is in low-noise mode and gives the latest sample each time.
        Returns None if the IMU is not present."""
        if not self.present:
            return None
        try:
            # Accel + gyro live contiguously after the temp data registers,
            # so a single 14-byte read covers everything: temp (2), accel (6),
            # gyro (6). Cuts the I2C transactions per cycle from 3 to 1.
            buf = self.i2c.readfrom_mem(self.addr, _REG_TEMP_DATA1, 14)
            self.last["temp_c"] = _sign16(buf[0], buf[1]) / 132.48 + 25.0
            self.last["ax"]     = _sign16(buf[2], buf[3])
            self.last["ay"]     = _sign16(buf[4], buf[5])
            self.last["az"]     = _sign16(buf[6], buf[7])
            self.last["gx"]     = _sign16(buf[8], buf[9])
            self.last["gy"]     = _sign16(buf[10], buf[11])
            self.last["gz"]     = _sign16(buf[12], buf[13])
            return self.last
        except Exception as e:
            logger.warn("IMU read failed: {}".format(e))
            return None

    def status_summary(self):
        """Compact snapshot suitable for inclusion in the device_status
        meta block. Includes raw counts only; the hub side can convert to
        physical units using the configured ranges (currently +/-4g and
        +/-1000 dps)."""
        if not self.present:
            return None
        return {
            "accel":  [self.last["ax"], self.last["ay"], self.last["az"]],
            "gyro":   [self.last["gx"], self.last["gy"], self.last["gz"]],
            "temp_c": (round(self.last["temp_c"], 2)
                       if self.last["temp_c"] is not None else None),
        }

    # ---- low-level helpers ----------------------------------------------------

    def _read_reg(self, reg):
        return self.i2c.readfrom_mem(self.addr, reg, 1)[0]

    def _write_reg(self, reg, value):
        self.i2c.writeto_mem(self.addr, reg, bytes([value & 0xFF]))


def _sign16(hi, lo):
    """Big-endian unsigned bytes -> signed 16-bit."""
    v = (hi << 8) | lo
    return v - 0x10000 if v & 0x8000 else v
