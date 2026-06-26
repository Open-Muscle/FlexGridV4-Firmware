# lib/imu.py
# IMU driver supporting three chip variants the fleet has shipped with:
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
#   3. Bosch BMI160 6-axis (GY-BMI160 breakout, currently the LASK5 unit)
#        Added to V4's driver too for fleet symmetry (overseer #0218): if a
#        future V4 BOM lands with this chip the existing path handles it.
#        I2C address: 0x68 (SDO=GND) or 0x69 (SDO=VDDIO)
#        CHIP_ID at register 0x00, expected value 0xD1
#        Sensor data at: GYRO 0x0C-0x11, ACC 0x12-0x17 (LE signed 16-bit)
#        IMPORTANT: boots in SUSPEND; CMD register 0x7E wakes accel (~5 ms)
#        and gyro (~80 ms) before any read returns sensible data.
#
# The driver auto-probes all three on boot. If none responds, IMU stays
# disabled and the firmware degrades gracefully.
#
# Per-chip scale is exposed via scale_dict() so the hub can normalize
# data.imu raw-counts to a common unit across mixed-fleet sources
# (per overseer #0217 cross-cutting note + vrpc #0200/#0210 ask).
#
# Datasheet refs:
#   - TDK InvenSense DS-000347 (genuine ICM-42688-P)
#   - TOKMAS ICM-42688-P-TOKMAS PDF distributed by LCSC
#   - Bosch BST-BMI160-DS000-09 (BMI160)

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
# Sensitivity at the configured ranges above.
_INV_GYRO_DPS_PER_LSB = 1.0 / 32.8           # +/-1000 dps -> 32.8 LSB/dps
_INV_ACCEL_G_PER_LSB  = 1.0 / 8192.0         # +/-4g       -> 8192 LSB/g


# ---------------------------------------------------------------------------
# Variant: TOKMAS "ICM-42688-P"
# Different register layout entirely. Power-on defaults are usable so the
# driver doesn't write any config registers; if you need different ODR/FS,
# they live at 0x20-0x25 per the TOKMAS datasheet. Scale assumes the
# +/-2000 dps / +/-16g power-on defaults; flag if a sample shows different
# behavior.
# ---------------------------------------------------------------------------
_TKM_ADDRS         = (0x36, 0x37)
_TKM_REG_CHIP_ID   = 0x1F     # any non-zero read = chip present
_TKM_REG_ACC_DATA  = 0x00     # 6 bytes: ACC X/Y/Z (low, high)
_TKM_REG_GYRO_DATA = 0x06     # 6 bytes: GYRO X/Y/Z (low, high)
_TKM_REG_TEMP_DATA = 0x0C     # 2 bytes: TEMP (low, high)
_TKM_GYRO_DPS_PER_LSB = 1.0 / 16.4           # +/-2000 dps default
_TKM_ACCEL_G_PER_LSB  = 1.0 / 2048.0         # +/-16g default


# ---------------------------------------------------------------------------
# Variant: Bosch BMI160 (LASK5 unit; included in the V4 driver for fleet symmetry)
# Power-on defaults are 100 Hz, +/-2g accel, +/-2000 dps gyro; the driver
# keeps these and exposes raw counts. The hub normalizes via scale_dict().
# ---------------------------------------------------------------------------
_BMI_ADDRS         = (0x68, 0x69)
_BMI_REG_CHIP_ID   = 0x00
_BMI_CHIP_ID       = 0xD1
_BMI_REG_DATA      = 0x0C   # 12 bytes contiguous: GYRO(6) + ACC(6) LE
_BMI_REG_CMD       = 0x7E
_BMI_CMD_ACC_NORMAL  = 0x11
_BMI_CMD_GYRO_NORMAL = 0x15
_BMI_GYRO_DPS_PER_LSB  = 1.0 / 16.4
_BMI_ACCEL_G_PER_LSB   = 1.0 / 16384.0


# Variant tags returned by detect()
VARIANT_NONE   = None
VARIANT_INVENS = "invensense"
VARIANT_TOKMAS = "tokmas"
VARIANT_BMI160 = "bmi160"


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
        """Probe all three variants. Returns True if any chip was found and
        brought up. SAFETY: any exception during init is logged and
        swallowed; init() returns False and self.present stays False so
        an absent or bad IMU cannot prevent the device from booting."""
        try:
            return self._init_inner()
        except Exception as e:
            logger.warn("IMU init unexpected failure (continuing without IMU): {} ({})".format(
                type(e).__name__, e))
            self.variant = VARIANT_NONE
            self.present = False
            return False

    def _init_inner(self):
        # Try genuine InvenSense first (V4's shipped variant most of the time).
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

        # Then try BMI160 at the same 0x68/0x69 addresses. CHIP_ID at reg 0x00.
        # If the InvenSense WHO_AM_I read above returned something OTHER than
        # 0x47 (which on a BMI160 reads back as 0x9b because reg 0x75 is
        # different on that chip), we can still match the BMI160 here.
        for addr in _BMI_ADDRS:
            try:
                cid = self.i2c.readfrom_mem(addr, _BMI_REG_CHIP_ID, 1)[0]
                if cid == _BMI_CHIP_ID:
                    self.addr = addr
                    self.variant = VARIANT_BMI160
                    self._init_bmi160()
                    self.present = True
                    logger.info("IMU: BMI160 at 0x{:02X}, CHIP_ID=0x{:02X}".format(addr, cid))
                    return True
            except OSError:
                continue

        # Then try TOKMAS variant (different addresses + register map).
        for addr in _TKM_ADDRS:
            try:
                cid = self.i2c.readfrom_mem(addr, _TKM_REG_CHIP_ID, 1)[0]
                if cid != 0x00 and cid != 0xFF:
                    self.addr = addr
                    self.variant = VARIANT_TOKMAS
                    self.present = True
                    logger.info("IMU: TOKMAS ICM-42688-P at 0x{:02X}, CHIP_ID=0x{:02X}".format(addr, cid))
                    return True
            except OSError:
                continue

        logger.warn("IMU: no compatible device on I2C (tried InvenSense 0x68/0x69, BMI160 0x68/0x69, TOKMAS 0x36/0x37)")
        self.variant = VARIANT_NONE
        self.present = False
        return False

    def _init_bmi160(self):
        """Bring BMI160 out of SUSPEND. CMD register 0x7E accepts power-mode
        commands; accel needs ~5 ms, gyro ~80 ms to reach normal mode."""
        self.i2c.writeto_mem(self.addr, _BMI_REG_CMD, bytes([_BMI_CMD_ACC_NORMAL]))
        time.sleep_ms(10)
        self.i2c.writeto_mem(self.addr, _BMI_REG_CMD, bytes([_BMI_CMD_GYRO_NORMAL]))
        time.sleep_ms(90)

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
            if self.variant == VARIANT_BMI160:
                return self._read_bmi160()
        except Exception as e:
            logger.warn("IMU read failed: {}".format(e))
        return None

    def _read_bmi160(self):
        # 12 bytes from 0x0C: GYRO(6 LE) + ACC(6 LE). Order is gyro-first
        # on BMI160, opposite of InvenSense + TOKMAS layout.
        buf = self.i2c.readfrom_mem(self.addr, _BMI_REG_DATA, 12)
        self.last["gx"] = _sign16_le(buf[0],  buf[1])
        self.last["gy"] = _sign16_le(buf[2],  buf[3])
        self.last["gz"] = _sign16_le(buf[4],  buf[5])
        self.last["ax"] = _sign16_le(buf[6],  buf[7])
        self.last["ay"] = _sign16_le(buf[8],  buf[9])
        self.last["az"] = _sign16_le(buf[10], buf[11])
        self.last["temp_c"] = None
        return self.last

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

    # ---- per-chip scale (cross-cutting for hub fusion) ------------------------

    def scale_dict(self):
        """Per-chip scale the hub needs to normalize raw-count data.imu
        across mixed-fleet sources. Shape pinned per overseer #0217;
        closes vrpc #0200/#0210 gyro-counts-to-rad/s ask (now per-chip).
        None when no IMU is present."""
        if not self.present:
            return None
        if self.variant == VARIANT_INVENS:
            return {
                "chip": "icm42688",
                "gyro_dps_per_lsb":  _INV_GYRO_DPS_PER_LSB,
                "accel_g_per_lsb":   _INV_ACCEL_G_PER_LSB,
            }
        if self.variant == VARIANT_TOKMAS:
            return {
                "chip": "icm42688-tokmas",
                "gyro_dps_per_lsb":  _TKM_GYRO_DPS_PER_LSB,
                "accel_g_per_lsb":   _TKM_ACCEL_G_PER_LSB,
            }
        if self.variant == VARIANT_BMI160:
            return {
                "chip": "bmi160",
                "gyro_dps_per_lsb":  _BMI_GYRO_DPS_PER_LSB,
                "accel_g_per_lsb":   _BMI_ACCEL_G_PER_LSB,
            }
        return None

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
