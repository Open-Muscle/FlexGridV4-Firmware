# lib/imu.py
# IMU driver supporting two chip variants the V4 BOM has shipped with:
#
#   1. Genuine InvenSense ICM-42688-P  (LCSC C1850418)
#        I2C address: 0x68 (AD0=GND) or 0x69 (AD0=VDDIO)
#        WHO_AM_I at register 0x75, expected value 0x47
#        Sensor data at: ACC 0x1F-0x24, GYRO 0x25-0x2A, TEMP 0x1D-0x1E
#
#   2. TOKMAS "ICM-42688-P" rebrand    (LCSC C54308212)
#        Same package + general spec, but DIFFERENT I2C address and register map.
#        I2C address: 0x36 (SDO=GND) or 0x37 (SDO=VDDIO)
#        CHIP_ID at register 0x1F (any non-zero value confirms presence)
#        Sensor data at: ACC 0x00-0x05, GYRO 0x06-0x0B, TEMP 0x0C-0x0D
#
# The driver auto-probes both variants on boot. If neither responds, IMU stays
# disabled and the firmware degrades gracefully.
#
# Datasheet refs:
#   - TDK InvenSense DS-000347 (genuine ICM-42688-P)
#   - TOKMAS ICM-42688-P-TOKMAS PDF distributed by LCSC

from machine import I2C, Pin
import time
import pinmap
import logger


# ---------------------------------------------------------------------------
# Variant: genuine InvenSense ICM-42688-P
# ---------------------------------------------------------------------------
_INV_ADDRS         = (0x68, 0x69)
_INV_REG_WHO_AM_I  = 0x75
_INV_WHO_AM_I      = 0x47
_INV_REG_PWR_MGMT0 = 0x4E
_INV_REG_GYRO_CFG0 = 0x4F
_INV_REG_ACC_CFG0  = 0x50
_INV_REG_TEMP_DATA = 0x1D    # 14 bytes contiguous: TEMP(2) + ACC(6) + GYRO(6)
_INV_PWR_MGMT0_LN  = 0x0F    # accel + gyro both in low-noise mode
_INV_ACC_CFG0      = (0b010 << 5) | 0b1000   # +/-4g, 100 Hz
_INV_GYRO_CFG0     = (0b001 << 5) | 0b1000   # +/-1000 dps, 100 Hz


# ---------------------------------------------------------------------------
# Variant: TOKMAS "ICM-42688-P"
# Different register layout entirely. Power-on defaults are usable so the
# driver doesn't write any config registers; if you need different ODR/FS,
# they live at 0x20-0x25 per the TOKMAS datasheet.
# ---------------------------------------------------------------------------
_TKM_ADDRS         = (0x36, 0x37)
_TKM_REG_CHIP_ID   = 0x1F     # any non-zero read = chip present
_TKM_REG_ACC_DATA  = 0x00     # 6 bytes: ACC X/Y/Z (low, high)
_TKM_REG_GYRO_DATA = 0x06     # 6 bytes: GYRO X/Y/Z (low, high)
_TKM_REG_TEMP_DATA = 0x0C     # 2 bytes: TEMP (low, high)


# Variant tags returned by detect()
VARIANT_NONE   = None
VARIANT_INVENS = "invensense"
VARIANT_TOKMAS = "tokmas"


class IMU:
    def __init__(self, i2c=None):
        if i2c is None:
            i2c = I2C(0, sda=Pin(pinmap.I2C_SDA), scl=Pin(pinmap.I2C_SCL), freq=400_000)
        self.i2c = i2c
        self.variant = VARIANT_NONE
        self.addr = None
        self.present = False
        # Most recent sample. ACC/GYRO are raw counts in their respective ranges.
        # temp_c is computed for InvenSense; for TOKMAS it's raw counts (the
        # ROOM_TEMP offset register exists but the conversion isn't documented
        # in our hands; raw is exposed and the hub can convert if it has the
        # offset).
        self.last = {
            "ax": 0, "ay": 0, "az": 0,
            "gx": 0, "gy": 0, "gz": 0,
            "temp_c": None,
        }

    # ---- detection + init -----------------------------------------------------

    def init(self):
        """Probe both variants. Returns True if either was found and brought up."""
        # Try genuine InvenSense first
        for addr in _INV_ADDRS:
            try:
                who = self.i2c.readfrom_mem(addr, _INV_REG_WHO_AM_I, 1)[0]
                if who == _INV_WHO_AM_I:
                    self.addr = addr
                    self.variant = VARIANT_INVENS
                    self._init_invens()
                    self.present = True
                    logger.info("IMU: InvenSense ICM-42688-P at 0x{:02X}, WHO_AM_I=0x{:02X}".format(addr, who))
                    return True
            except OSError:
                continue

        # Then try TOKMAS variant
        for addr in _TKM_ADDRS:
            try:
                cid = self.i2c.readfrom_mem(addr, _TKM_REG_CHIP_ID, 1)[0]
                if cid != 0x00 and cid != 0xFF:
                    self.addr = addr
                    self.variant = VARIANT_TOKMAS
                    # TOKMAS power-on defaults give usable sensor data already.
                    self.present = True
                    logger.info("IMU: TOKMAS ICM-42688-P at 0x{:02X}, CHIP_ID=0x{:02X}".format(addr, cid))
                    return True
            except OSError:
                continue

        logger.warn("IMU: no compatible device found on I2C (tried InvenSense 0x68/0x69 and TOKMAS 0x36/0x37)")
        self.variant = VARIANT_NONE
        self.present = False
        return False

    def _init_invens(self):
        """Configure genuine InvenSense ICM-42688-P for low-noise mode at 100 Hz."""
        self.i2c.writeto_mem(self.addr, _INV_REG_PWR_MGMT0, bytes([_INV_PWR_MGMT0_LN]))
        time.sleep_ms(2)
        self.i2c.writeto_mem(self.addr, _INV_REG_ACC_CFG0, bytes([_INV_ACC_CFG0]))
        self.i2c.writeto_mem(self.addr, _INV_REG_GYRO_CFG0, bytes([_INV_GYRO_CFG0]))
        time.sleep_ms(2)

    # ---- read -----------------------------------------------------------------

    def read(self):
        """One-shot read of accel + gyro + temp. Updates self.last and returns it.
        Returns None if the chip isn't present or the read fails."""
        if not self.present:
            return None
        try:
            if self.variant == VARIANT_INVENS:
                return self._read_invens()
            if self.variant == VARIANT_TOKMAS:
                return self._read_tokmas()
        except Exception as e:
            logger.warn("IMU read failed: {}".format(e))
        return None

    def _read_invens(self):
        # 14 bytes starting at 0x1D: TEMP(2 big-endian) + ACC(6 big-endian) + GYRO(6 big-endian)
        buf = self.i2c.readfrom_mem(self.addr, _INV_REG_TEMP_DATA, 14)
        self.last["temp_c"] = _sign16_be(buf[0], buf[1]) / 132.48 + 25.0
        self.last["ax"]     = _sign16_be(buf[2], buf[3])
        self.last["ay"]     = _sign16_be(buf[4], buf[5])
        self.last["az"]     = _sign16_be(buf[6], buf[7])
        self.last["gx"]     = _sign16_be(buf[8], buf[9])
        self.last["gy"]     = _sign16_be(buf[10], buf[11])
        self.last["gz"]     = _sign16_be(buf[12], buf[13])
        return self.last

    def _read_tokmas(self):
        # 6 bytes accel + 6 bytes gyro + 2 bytes temp, all little-endian (LO byte
        # at the lower address per the TOKMAS datasheet's "ACC DATA XL @ 0x00,
        # ACC DATA XH @ 0x01" pattern).
        # Single 14-byte read at 0x00 is contiguous: ACC(6) + GYRO(6) + TEMP(2).
        buf = self.i2c.readfrom_mem(self.addr, _TKM_REG_ACC_DATA, 14)
        self.last["ax"]     = _sign16_le(buf[0], buf[1])
        self.last["ay"]     = _sign16_le(buf[2], buf[3])
        self.last["az"]     = _sign16_le(buf[4], buf[5])
        self.last["gx"]     = _sign16_le(buf[6], buf[7])
        self.last["gy"]     = _sign16_le(buf[8], buf[9])
        self.last["gz"]     = _sign16_le(buf[10], buf[11])
        # TOKMAS temp conversion is not documented in the LCSC datasheet in a
        # way we can ship: the formula T_c = (TEMP_DATA - ROOM_TEMP)/14 + 25
        # needs the per-chip ROOM_TEMP offset from registers 0x29-0x2A AND a
        # confirmed slope, neither verified on hardware. Exposing the raw
        # signed-16 count as Celsius is wrong (phone saw -1894 C live;
        # board #0156). Until the formula is verified end-to-end on a TOKMAS
        # part, surface temp_c as None so downstream consumers can hide it
        # rather than display garbage. Phone is already guarding.
        # TODO: read ROOM_TEMP once at init() and apply the formula here.
        self.last["temp_c"] = None
        return self.last

    # ---- summary for status meta ----------------------------------------------

    def status_summary(self):
        """Compact snapshot for the status meta block."""
        if not self.present:
            return None
        return {
            "variant": self.variant,
            "addr":    self.addr,
            "accel":   [self.last["ax"], self.last["ay"], self.last["az"]],
            "gyro":    [self.last["gx"], self.last["gy"], self.last["gz"]],
            "temp_c":  (round(self.last["temp_c"], 2)
                        if self.last["temp_c"] is not None else None),
        }


def _sign16_be(hi, lo):
    """Big-endian unsigned bytes -> signed 16-bit (genuine InvenSense format)."""
    v = (hi << 8) | lo
    return v - 0x10000 if v & 0x8000 else v


def _sign16_le(lo, hi):
    """Little-endian unsigned bytes -> signed 16-bit (TOKMAS format)."""
    v = (hi << 8) | lo
    return v - 0x10000 if v & 0x8000 else v
