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

1. **Calibrate tab**: press **Calibrate all** (or **Set** on one point) and click each spot in Roblox; Esc skips. The click isn't passed to the game. Set the optional **Safe spot in the Roblox window** to skip the 5s countdown: Start then focuses Roblox and clicks there itself.
2. **Tools tab**: **Auto brush**, **Auto speed** and **Color check** once per game (results are saved in `config.json`).
3. Copy an image, press **Paste image**, then **Start drawing**. With the timer box calibrated it fits the drawing to the time left; otherwise it fits the **Drawing time limit** (Settings). The progress panel shows planning progress, time left and how much time is to spare.
4. With the paint bucket calibrated, the background color is filled in one click (start on a blank canvas).
5. **Library tab > Pre-plan...**: plan the selected pictures ahead for a round length. Start then draws them with no planning wait, so the whole round goes to drawing. Plans are thrown out when brush, color, canvas or speed settings change. **Prep** does just the brush-independent part (scaling, colors, line art for every brush size the search can try) for the selected pictures in the background, which makes planning at Start 8-20% faster.
6. **Quality** (next to Start): **Fast** (fine brush 6pt+, 16 colors, no edge pass or line art: plans in seconds), **Balanced** (4.3pt+, 24 colors) or **Best** (smallest brushes, most colors: slowest). The live log under the progress bars (and `drawer_log.txt`) shows what planning and drawing are doing.
7. Tick **Auto-tune** (next to Start) to pick brush sizes, colors, smoothing and line art automatically for each picture.

Press **Esc** (or move the mouse) to stop.
