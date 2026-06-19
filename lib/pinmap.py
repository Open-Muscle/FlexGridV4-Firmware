# lib/pinmap.py
# Central GPIO assignments for the FlexGrid V4 rigid PCB.
# Extracted from the V4 KiCad schematic (OM-FlexGrid-Rigid-PCB V4).
# Update here, not in individual modules.
#
# V4 deltas from V3:
#   - RGB status LED on GPIO 40, 41, 42 (was AUX_40, AUX_41 on V3; added 42)
#   - microSD socket on SPI bus (V3 reserved the SPI pins; V4 populates the socket)
#   - ICM-42688-P IMU populated on I2C (shared bus with the OLED)
#   - Haptic motor driver present but disabled at boot until opt-in
#
# Anything tagged PIN_TODO needs to be cross-checked against the V4 KiCad schematic
# before the firmware is bench-tested. They are best-guess assignments based on the
# V3 footprint plus what the V4 README specifies.

# ---------------------------------------------------------------------------
# Sensor matrix multiplexer (CD74HC4067)
# Unchanged from V3.
# ---------------------------------------------------------------------------
MUX_S0 = 5
MUX_S1 = 6
MUX_S2 = 7
MUX_S3 = 15
MUX_EN = 16  # active LOW

# ---------------------------------------------------------------------------
# Sensor matrix ADC row inputs (10k pulldowns on board)
# 100 pF caps on the sense lines (V4 fix vs V3's 2.2 uF column-bleed issue).
# Unchanged from V3.
# ---------------------------------------------------------------------------
ADC_ROW_0 = 1
ADC_ROW_1 = 2
ADC_ROW_2 = 3
ADC_ROW_3 = 4

# ---------------------------------------------------------------------------
# I2C bus (OLED SSD1306 128x32, ICM-42688-P IMU)
# Shared bus; the OLED is at 0x3C and the IMU at 0x68 (AD0 low) or 0x69 (AD0 high).
# Unchanged from V3.
# ---------------------------------------------------------------------------
I2C_SDA = 8
I2C_SCL = 9
IMU_I2C_ADDR = 0x68  # PIN_TODO: confirm AD0 strap on V4 schematic; could be 0x69

# ---------------------------------------------------------------------------
# SPI bus (microSD card socket J9 in V4)
# 1-bit SPI mode per V4 README ("DATA0 = MISO, CMD = MOSI, CLK = SCK, DATA3 = CS").
# ---------------------------------------------------------------------------
SPI_MISO = 11
SPI_SCK  = 12
SPI_MOSI = 13
SD_CS    = 14   # PIN_TODO: confirm against V4 schematic (V3 reserved 14 as a generic SPI CS)

# ---------------------------------------------------------------------------
# User input buttons (active LOW, internal pull-up)
# Unchanged from V3.
# ---------------------------------------------------------------------------
BTN_BOOT   = 0     # strap pin - also enters download mode at reset if held
BTN_SELECT = 10
BTN_MENU   = 21
# RESET button is hardwired to chip EN, not GPIO

# ---------------------------------------------------------------------------
# Power management
# Unchanged from V3.
# ---------------------------------------------------------------------------
PWR_OFF  = 45      # drive HIGH momentarily to tell MAX16054 to drop power
ADC_BAT  = 18      # battery voltage divider

# ---------------------------------------------------------------------------
# Haptic motor (NEW: driver populated on V4 but defaults OFF)
# Untested in this configuration; firmware must NOT drive this without an
# explicit opt-in setting. See settings_manager.SettingsManager.DEFAULTS
# "haptic_enabled": False.
# ---------------------------------------------------------------------------
MOT_SIG  = 17

# ---------------------------------------------------------------------------
# RGB status LED (NEW IN V4)
# Common-cathode RGB LED through 330 ohm series resistors.
# Each channel driven by a PWM-capable GPIO; expect to use machine.PWM().
# Per V4 README "RGB status LED" section.
# ---------------------------------------------------------------------------
RGB_R = 40
RGB_G = 41
RGB_B = 42

# ---------------------------------------------------------------------------
# Sensor matrix dimensions
# Unchanged from V3 hardware (15 columns x 4 rows = 60 cells).
# ---------------------------------------------------------------------------
NUM_COLS = 15      # 15 active columns on the mux (channel 15 unused)
NUM_ROWS = 4
