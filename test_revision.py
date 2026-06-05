#!/usr/bin/env python3
"""
test_revision.py - find which protocol your 3.5" screen speaks, without configure.py.

Run it once per revision and watch the screen:
    python test_revision.py A      # Turing 3.5 / UsbPCMonitor 3.5
    python test_revision.py B      # XuanFang 3.5

Whichever one paints a GREEN screen with text is your revision. A wrong guess
just shows nothing / garbage and harms nothing - move on to the next letter.

Run this from INSIDE the turing-smart-screen folder so 'library/...' imports work.
"""

import sys
from PIL import Image, ImageDraw
from library.lcd.lcd_comm import Orientation

COM_PORT = "COM5"          # your port from Device Manager
REV = (sys.argv[1].upper() if len(sys.argv) > 1 else "A")

# the 3.5" panel is natively 320x480 (portrait); orientation rotates it to 480x320
NATIVE_W, NATIVE_H = 320, 480
LAND_W, LAND_H = 480, 320

if REV == "A":
    from library.lcd.lcd_comm_rev_a import LcdCommRevA as Lcd
elif REV == "B":
    from library.lcd.lcd_comm_rev_b import LcdCommRevB as Lcd
elif REV == "C":
    from library.lcd.lcd_comm_rev_c import LcdCommRevC as Lcd
else:
    sys.exit(f"Unknown revision '{REV}'. Use A, B, or C.")

print(f"Trying revision {REV} on {COM_PORT} ...")
lcd = Lcd(com_port=COM_PORT, display_width=NATIVE_W, display_height=NATIVE_H)
lcd.Reset()
lcd.InitializeComm()
lcd.SetBrightness(level=50)        # rev A can run hot; 50% is a safe test value
lcd.SetOrientation(orientation=Orientation.LANDSCAPE)

img = Image.new("RGB", (LAND_W, LAND_H), (24, 140, 78))
d = ImageDraw.Draw(img)
d.text((60, 120), f"REVISION {REV}", fill=(255, 255, 255))
d.text((60, 160), f"COM {COM_PORT} - it works!", fill=(255, 255, 255))
img.save("revtest.png")
lcd.DisplayBitmap("revtest.png")   # documented API; reads the PNG we just saved

print("Sent. GREEN screen with text => this is your revision. "
      "Blank/garbled => try the next letter.")
