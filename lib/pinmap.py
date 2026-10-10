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
# Kit bands ship WITHOUT the OLED (its header is left unplugged); the firmware
# logs "No screen" and carries on. Unchanged from V3.
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
# Verified against the V4 schematic 2026-07-02 (SELECT1 -> net SELECT ->
# IO10; MENU1 -> net MENU -> IO21). MENU + SELECT are the ONLY readable
# button GPIOs.
#
# IMPORTANT: the silkscreen "BOOT" button (BOOT1) is wired to EN (chip
# reset), NOT to GPIO0. Pressing it reboots the chip; it cannot be read
# as an input.
#
# The switch printed SW_RST1 (bottom left) IS on GPIO0: its schematic net is
# labelled "100", which is the ESP32-S3 IO0 pin. Holding SW_RST1 while
# pressing BOOT1 puts the chip in download mode (reflash a stuck board);
# otherwise it does nothing in normal use. GPIO0 is a boot-strap pin (low
# at reset selects download mode), so the firmware must NOT read it or hang
# any action on it: a press while the chip resets would change how it boots.
#
# The power button SW_PWR1 (top edge) goes to the MAX16054 soft-power
# controller, not to a GPIO; see PWR_OFF below.
# ---------------------------------------------------------------------------
BTN_SELECT = 10    # SELECT1 -> IO10
BTN_MENU   = 21    # MENU1   -> IO21
# BTN_BOOT (GPIO0) intentionally absent: GPIO0 is the boot-strap pin that
# SW_RST1 pulls low (see above), so firmware leaves it alone. The BOOT1
# button pulls EN (reset) and cannot be read at all.

# ---------------------------------------------------------------------------
# Power management
# ---------------------------------------------------------------------------
PWR_OFF  = 45      # drive HIGH momentarily to tell MAX16054 to drop power
                   # (the power button SW_PWR1 reaches the same MAX16054: a
                   # short press switches on, a short press again switches off)
ADC_BAT  = 18      # battery voltage divider
# VBUS presence detect - V4.2 SCHEMATIC ONLY, NOT WIRED ON AS-BUILT V4
# BOARDS (Tory 2026-07-02: R19/R20 divider into GPIO39 was added to the
# schematic after fab, alongside the ESD fix). On the physical V4 bands
# GPIO39 floats, so firmware must NOT read it unless the per-device
# settings.json opts in with "vbus_stat_pin": 39 (set that on V4.2+
# hardware only). Default behavior everywhere else: VBAT-threshold
# heuristic in power_manager.
VBUS_STAT = 39   # V4.2+; informational, gated by settings["vbus_stat_pin"]

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
