# ez-draw

Draws an image in a Roblox drawing game by controlling the mouse (macOS).

## Setup

```bash
python3 -m pip install -r requirements.txt
python3 drawer.py
```

Grant the app running the script (e.g. Terminal) these permissions in System Settings > Privacy & Security:

- **Accessibility** - to move and click the mouse
- **Screen Recording** - for Auto brush, Auto speed, Color check, screenshot correction and reading the round timer

## Use

1. **Calibrate tab**: hover each point for 3s (canvas corners, color wheel, brightness slider, brush slider, timer box).
2. **Auto brush**, **Auto speed** and **Color check** once per game (results are saved in `config.json`).
3. Copy an image, press **Preview**, then **Start drawing**. With the timer box calibrated it fits the drawing to the time left.

Press **Esc** (or move the mouse) to stop.
