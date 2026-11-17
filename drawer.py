#!/usr/bin/env python3
"""Auto-draw an image in a Roblox drawing game by controlling the mouse (macOS)."""
import colorsys
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time

import cv2
import numpy as np
import Quartz
from PIL import Image, ImageFilter, ImageOps
from PySide6.QtCore import QFile, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
                               QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListView, QListWidget, QListWidgetItem, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget)

sys.setswitchinterval(0.001)  # keep the drawing thread on time while planning runs alongside

_NO_NAP = []


def boost_thread():
    """Run the calling thread at the highest priority (user-interactive). macOS demotes
    background apps' threads, and at background priority a 4ms sleep really takes ~100ms."""
    try:
        import ctypes
        ctypes.CDLL("/usr/lib/libSystem.B.dylib").pthread_set_qos_class_self_np(0x21, 0)
    except Exception:
        pass


def precise_sleep(dt):
    """Sleep dt seconds accurately: OS sleep for most of it, then watch the clock for the
    last few ms (immune to the OS waking low-priority threads late)."""
    if dt <= 0:
        return
    end = time.perf_counter() + dt
    if dt > 0.0025:
        time.sleep(dt - 0.0015)
    while time.perf_counter() < end:
        pass


def keep_awake():
    """Stop macOS App Nap from throttling us while Roblox is in front. Without this, a
    background app's short sleeps get stretched many times over, so drawing crawls (a 4ms
    pause between mouse moves can turn into hundreds of ms)."""
    if _NO_NAP:
        return
    try:
        from Foundation import NSProcessInfo
        user_initiated, latency_critical = 0x00FFFFFF, 0xFF00000000
        _NO_NAP.append(NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            user_initiated | latency_critical, "Drawing in Roblox with precise mouse timing"))
    except Exception:
        pass
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drawer_log.txt")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "library")  # local only (gitignored)
LIB_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff")

DEFAULTS = {
    "canvas_tl": None,       # [x, y] screen points
    "canvas_br": None,
    "picker": "wheel",       # "wheel" (color wheel + brightness slider) or "hex" (text box)
    "hex_box": None,
    "ring_center": None,     # hue-ring picker: ring center, a point mid-band, square corners
    "ring_point": None,
    "square_tl": None,
    "square_br": None,
    "wheel_center": None,
    "wheel_edge": None,      # any point on the wheel's outer rim
    "value_top": None,       # brightness slider, white end
    "value_bottom": None,    # brightness slider, black end
    "timer_tl": None,        # box around the in-game round timer (read by OCR)
    "timer_br": None,
    "brush_min": None,       # brush size slider, left end
    "brush_max": None,       # brush size slider, right end
    "brush_pct": 10,         # fine brush slider position (0-100)
    "bucket_tool": None,     # optional paint bucket tool button (fills the background in one click)
    "brush_tool": None,      # brush/pen tool button (to switch back after the bucket)
    "use_bucket": True,
    "brush_dec": None,       # optional brush size arrows (exact +-1 steps, for small brushes)
    "brush_inc": None,
    "arrow_curve": None,     # measured [(clicks above minimum, diameter pt)]
    "brush_clicks": None,    # fine / big / tiny brush set by arrow clicks instead of the slider
    "big_brush_clicks": None,
    "tiny_brush_clicks": None,
    "big_brush_pct": 0,      # big brush slider position (0 = no big brush)
    "big_brush_pt": None,    # measured big brush diameter (screen points)
    "tiny_brush_pct": 0,     # tiny (edge) brush slider position (0 = no edge pass)
    "tiny_brush_pt": None,   # measured tiny brush diameter (screen points)
    "edge_contrast": 15,
    "merge_de": 8,           # merge colors closer than this (Lab distance)
    "smooth": 1.5,           # simplify color shapes over this many cells (0 = off)
    "time_factor": 1.0,      # actual / predicted drawing time, learned from real runs (fallback)
    "timing_model": None,    # learned per part: {"mouse": x, "color": x, "brush": x}
    "game_rate": 1.0,        # game-timer seconds per real second (learned from timer readings)
    "picker_ms": 30,         # pause between clicks on the color wheel / sliders (kept safe)
    "value_curve": None,     # Color check: measured brightness slider response [(asked, got)]
    "line_fill": True,       # follow thin shapes with lines instead of row-by-row
    "line_art": True,        # draw the picture's thin curves (outlines, strands) as lines
    "line_contrast": 35,     # how strongly a thin line must stand out to count as line art
    "correct_passes": 2,     # screenshot check-and-fix passes after drawing (0 = off)
    "time_limit": 150,       # auto brush: pick sizes so drawing takes at most this many seconds     # only sharpen edges between colors at least this different
    "corner_frames": 2,      # frames to hold each corner (prevents diagonal streaks)
    "min_frame_ms": 9,       # fastest event delay used when running late
    "spacing": 4.0,          # screen points between scanlines / grid cells
    "colors": 12,
    "skip_white": True,
    "hash_prefix": True,
    "frame_ms": 17,          # delay between mouse events (~1 Roblox frame)
    "segment": 60.0,         # max drag distance per mouse event (points)
    "color_wait_ms": 250,    # wait after setting a color
    "min_blob": 6,           # merge same-color specks smaller than this (cells)
    "denoise": 3,            # median filter size before color reduction (0 = off)
    "key_ms": 10,            # key hold / gap when typing hex
    "brush_pt": None,        # measured fine brush diameter (screen points)
}


class Aborted(Exception):
    pass


# ---------- low-level mouse (Quartz) ----------

def mouse_pos():
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return loc.x, loc.y


ESC_KEYCODE = 53


def stop_key_down():
    """True while Esc is physically held (polled; no event tap needed)."""
    return bool(Quartz.CGEventSourceKeyState(Quartz.kCGEventSourceStateHIDSystemState, ESC_KEYCODE))


_LAST_POST = [0.0]  # when the last mouse event went out (perf_counter)


def _post(kind, x, y):
    ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft)
    if kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, 1)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    _LAST_POST[0] = time.perf_counter()


def focus_roblox(spot):
    """Bring the Roblox window to the front and click a safe spot in it (so the game takes
    the following mouse input) - replaces the 5s countdown."""
    try:
        from AppKit import NSWorkspace, NSApplicationActivateIgnoringOtherApps
        apps = [a for a in NSWorkspace.sharedWorkspace().runningApplications()
                if (a.localizedName() or "") == "Roblox"
                or (a.bundleIdentifier() or "").startswith("com.roblox.RobloxPlayer")]
        if apps:
            apps[0].activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
            time.sleep(0.2)
    except Exception:
        pass
    x, y = spot
    _post(Quartz.kCGEventMouseMoved, x, y)
    time.sleep(0.05)
    _post(Quartz.kCGEventLeftMouseDown, x, y)
    time.sleep(0.05)
    _post(Quartz.kCGEventLeftMouseUp, x, y)
    time.sleep(0.25)


# ---------- image -> drawing plan ----------

def _to_lab(rgb):
    c = rgb / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    xyz = c @ np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]).T
    xyz /= np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], 1)


def quantize_lab(img, k, iters=15, seed=0):
    """k-means++ in Lab space. Returns (index bytes, flat RGB palette list)."""
    rgb = np.asarray(img.convert("RGB"), dtype=np.float64).reshape(-1, 3)
    lab = _to_lab(rgb)
    uniq, inv = np.unique(np.round(lab, 1), axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    wts = np.bincount(inv).astype(np.float64)
    k = min(k, len(uniq))
    rng = np.random.default_rng(seed)
    # k-means++ seeding (distance-weighted, NOT area-weighted beyond counts) keeps small vivid colors
    cent = [uniq[np.argmax(wts)]]
    d2 = ((uniq - cent[0]) ** 2).sum(1)
    for _ in range(1, k):
        p = d2 * np.sqrt(wts)
        cent.append(uniq[rng.choice(len(uniq), p=p / p.sum())] if p.sum() > 0 else uniq[0])
        d2 = np.minimum(d2, ((uniq - cent[-1]) ** 2).sum(1))
    cent = np.array(cent)
    for _ in range(iters):
        lab_d = ((uniq[:, None, :] - cent[None]) ** 2).sum(2)
        lbl = lab_d.argmin(1)
        new = np.array([np.average(uniq[lbl == j], axis=0, weights=wts[lbl == j]) if (lbl == j).any() else cent[j]
                        for j in range(k)])
        if np.allclose(new, cent, atol=0.05):
            break
        cent = new
    lbl = ((uniq[:, None, :] - cent[None]) ** 2).sum(2).argmin(1)
    px_lbl = lbl[inv]
    pal = np.zeros((k, 3))
    for j in range(k):
        m = px_lbl == j
        if m.any():
            pal[j] = rgb[m].mean(0)
    return bytes(px_lbl.astype(np.uint8)), [int(round(v)) for v in pal.reshape(-1)]


HS = 8  # hi-res target pixels per cell (edge pass precision)


def _assign_lab(rgb, pal):
    """Nearest palette index (Lab distance) for an (N, 3) uint8 array."""
    P = _to_lab(np.array(pal, dtype=np.float64).reshape(-1, 3))
    a = rgb.reshape(-1, 3).astype(np.int32)
    key = (a[:, 0] << 16) | (a[:, 1] << 8) | a[:, 2]  # 1-D keys: much faster unique than rows
    uk, inv = np.unique(key, return_inverse=True)
    uniq = np.stack([(uk >> 16) & 255, (uk >> 8) & 255, uk & 255], 1).astype(np.float64)
    lbl = ((_to_lab(uniq)[:, None, :] - P[None]) ** 2).sum(2).argmin(1)
    return lbl[inv.reshape(-1)].astype(np.uint8)


def _merge_colors(L, pal, white, merge_de, min_frac):
    """Fold near-identical colors (Lab distance < merge_de) and colors covering less than
    min_frac of the drawing into their nearest color. Each color costs a color change and its
    own strokes, so near-duplicates mostly waste time. Returns the remapped label image."""
    lab = _to_lab(np.array(pal, dtype=np.float64).reshape(-1, 3))
    n = len(lab)
    remap = np.arange(256)
    counts = np.bincount(L.reshape(-1), minlength=256)[:n].astype(np.int64)
    alive = [c for c in range(n) if counts[c] > 0]
    total = max(1, sum(counts[c] for c in alive if not white[c]))
    # near-black, barely tinted colors all read as black in the game (its darks come out even
    # darker): fold them into the darkest one instead of drawing black on black
    darks = [c for c in alive if not white[c] and lab[c][0] < 15 and np.hypot(lab[c][1], lab[c][2]) < 12]
    if len(darks) > 1:
        base = min(darks, key=lambda c: lab[c][0])
        for c in darks:
            if c != base:
                remap[remap == c] = base
                counts[base] += counts[c]
                counts[c] = 0
                alive.remove(c)
    while len(alive) > 1:
        A = np.array(alive)
        d = np.sqrt(((lab[A][:, None] - lab[A][None]) ** 2).sum(2))
        np.fill_diagonal(d, np.inf)
        small = [c for c in alive if not white[c] and counts[c] < min_frac * total]
        if small:  # tiny color -> nearest color
            c = min(small, key=lambda c: counts[c])
            i = alive.index(c)
            j = int(np.argmin(d[i]))
            src, dst = c, alive[j]
        else:
            i, j = np.unravel_index(np.argmin(d), d.shape)
            if d[i, j] >= merge_de:
                break
            a, b = alive[i], alive[j]
            if white[a] != white[b]:  # merging into white = not drawn; only if it's near-white
                src, dst = (b, a) if white[a] else (a, b)
            else:
                src, dst = (a, b) if counts[a] < counts[b] else (b, a)
        remap[remap == src] = dst
        counts[dst] += counts[src]
        counts[src] = 0
        alive.remove(src)
    return remap[L].astype(np.uint8)


def _majority(L, k):
    """Mode filter: each pixel takes the most common color in a k x k window. Smooths
    blotchy shading regions into solid shapes (far fewer strokes), keeps real edges."""
    if k < 3:
        return L
    cols = np.unique(L)
    best = np.zeros(L.shape, np.float32) - 1
    out = L.copy()
    for c in cols:
        votes = cv2.boxFilter((L == c).astype(np.float32), -1, (k, k), normalize=False)
        win = votes > best
        out[win] = c
        best[win] = votes[win]
    return out


def _despeckle_cv(L, min_area):
    """Recolor same-color blobs smaller than min_area pixels to their most common
    neighboring color (removes grain specks that would each cost strokes)."""
    L = L.copy()
    for _ in range(2):
        changed = False
        for c in np.unique(L):
            n, comp, stats, _ = cv2.connectedComponentsWithStats((L == c).astype(np.uint8), connectivity=4)
            for i in range(1, n):
                x, y, w, h, area = stats[i]
                if area >= min_area:
                    continue
                x0, y0 = max(0, x - 1), max(0, y - 1)
                x1, y1 = min(L.shape[1], x + w + 1), min(L.shape[0], y + h + 1)
                blob = comp[y0:y1, x0:x1] == i
                ring = cv2.dilate(blob.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool) & ~blob
                nb = L[y0:y1, x0:x1][ring]
                nb = nb[nb != c]
                if len(nb):
                    L[y0:y1, x0:x1][blob] = np.bincount(nb).argmax()
                    changed = True
        if not changed:
            break
    return L


def _thin(m):
    """Zhang-Suen thinning: 1-px centerlines of a bool mask (run per component crop)."""
    out = np.zeros_like(m)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8), connectivity=8)
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        out[y:y + h, x:x + w] |= _thin_core(lab[y:y + h, x:x + w] == i)
    return out


def _thin_core(m):
    img = m.copy()
    while True:
        changed = False
        for step in (0, 1):
            p = np.pad(img, 1)
            P2, P3, P4 = p[:-2, 1:-1], p[:-2, 2:], p[1:-1, 2:]
            P5, P6, P7 = p[2:, 2:], p[2:, 1:-1], p[2:, :-2]
            P8, P9 = p[1:-1, :-2], p[:-2, :-2]
            nb = [P2, P3, P4, P5, P6, P7, P8, P9]
            B = np.zeros(img.shape, np.uint8)
            A = np.zeros(img.shape, np.uint8)
            for i in range(8):
                B += nb[i]
                A += (~nb[i]) & nb[(i + 1) % 8]
            if step == 0:
                c = ~(P2 & P4 & P6) & ~(P4 & P6 & P8)
            else:
                c = ~(P2 & P4 & P8) & ~(P2 & P6 & P8)
            rm = img & (B >= 2) & (B <= 6) & (A == 1) & c
            if rm.any():
                img &= ~rm
                changed = True
        if not changed:
            return img


def _trace(mask, eps, keep=None):
    """Polylines along the outline of a binary mask. With `keep` (bool mask), only the parts
    of each outline lying on keep are returned (as open polylines)."""
    cnts, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    out = []
    for cnt in cnts:
        cnt = cnt.reshape(-1, 2)
        if keep is None or keep[cnt[:, 1], cnt[:, 0]].all():
            pieces, closed = [cnt], True
        else:
            on = keep[cnt[:, 1], cnt[:, 0]]
            if not on.any():
                continue
            # rotate so the loop starts at an "off" point, then split into "on" runs
            start = int(np.flatnonzero(~on)[0])
            cnt, on = np.roll(cnt, -start, 0), np.roll(on, -start)
            edges = np.flatnonzero(np.diff(np.concatenate([[0], on.astype(np.int8), [0]])))
            pieces = [cnt[a:b] for a, b in zip(edges[::2], edges[1::2])]
            closed = False
        for pc in pieces:
            ap = cv2.approxPolyDP(pc.reshape(-1, 1, 2), eps, closed).reshape(-1, 2)
            pts = [(float(x), float(y)) for x, y in ap]
            if closed and len(pts) > 2:
                pts.append(pts[0])
            out.append(pts)
    return out


def _skeleton_paths(sk, eps):
    """Open polylines along a 1-px skeleton (each pixel visited once: lines are drawn once,
    not out-and-back like a contour). Junctions split paths; loops become closed paths."""
    ys, xs = np.nonzero(sk)
    pix = set(zip(xs.tolist(), ys.tolist()))
    if not pix:
        return []
    nbrs = {}
    for x, y in pix:
        # a diagonal neighbor that's also reachable through a shared side neighbor is the same
        # path (staircase), not a branch
        nbrs[(x, y)] = [(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                        if (dx or dy) and (x + dx, y + dy) in pix
                        and not (dx and dy and ((x + dx, y) in pix or (x, y + dy) in pix))]
    seen = set()  # visited edges

    def walk(a, b):
        path = [a, b]
        seen.add((a, b)); seen.add((b, a))
        while len(nbrs[b]) == 2:  # continue through simple chain pixels
            nxt = [n for n in nbrs[b] if (b, n) not in seen]
            if not nxt:
                break
            a, b = b, nxt[0]
            seen.add((a, b)); seen.add((b, a))
            path.append(b)
        return path

    paths = []
    starts = [p for p in pix if len(nbrs[p]) != 2]  # endpoints, junctions and lone pixels
    for p in sorted(starts):
        if not nbrs[p]:
            paths.append([p, p])
        for n in nbrs[p]:
            if (p, n) not in seen:
                paths.append(walk(p, n))
    for p in sorted(pix):  # remaining pure loops
        for n in nbrs[p]:
            if (p, n) not in seen:
                paths.append(walk(p, n))
    out = []
    for path in paths:
        arr = np.array(path, np.int32).reshape(-1, 1, 2)
        ap = cv2.approxPolyDP(arr, eps, False).reshape(-1, 2) if len(path) > 2 else arr.reshape(-1, 2)
        out.append([(float(x), float(y)) for x, y in ap])
    return out


def _line_fill(R, rf, thin_max, cell=8):
    """Shape-following fill for the thin parts of region R (hi-res px): components whose
    widest point is at most 2*thin_max are filled with rings that follow the outline inward
    (brush radius rf), and anything narrower than the brush gets one stroke along its center
    line. Far fewer strokes than row-by-row filling for curvy/vertical lines and strands.
    Returns (strokes, covered mask)."""
    Ru = R.astype(np.uint8)
    if not Ru.any():
        return [], np.zeros_like(R)
    dist = cv2.distanceTransform(Ru, cv2.DIST_L2, 5)
    # thin = parts of R no disk of radius thin_max fits into (strands, lines, narrow bands),
    # even when attached to a wide area; tiny slivers (e.g. corners of wide shapes) are left
    # to the row fill
    d = int(2 * thin_max + 1)
    thick = cv2.morphologyEx(Ru, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d, d)))
    Rt = (Ru & (1 - thick)).astype(np.uint8)
    n, comp, stats, _ = cv2.connectedComponentsWithStats(Rt, connectivity=8)
    big_enough = stats[:, cv2.CC_STAT_AREA] >= 6 * rf * rf
    big_enough[0] = False
    Rt = big_enough[comp]
    if not Rt.any():
        return [], np.zeros_like(R)
    eps = 0.35 * rf  # well below the brush radius: the line still hugs the shape
    step = 1.7 * rf
    strokes = []
    k = 0
    while True:  # rings just inside the outline, then further in
        m = dist * Rt >= rf + k * step + 0.5
        if not m.any():
            break
        strokes += _trace(m, eps)
        k += 1
    # parts too narrow for even the first ring: one stroke along their center line
    first = (dist * Rt >= rf + 0.5).astype(np.uint8)
    r = int(math.ceil(rf + 1.5))
    reach = cv2.dilate(first, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))).astype(bool)
    left = Rt & ~reach
    n2, comp2, st2, _ = cv2.connectedComponentsWithStats(left.astype(np.uint8), connectivity=8)
    keep2 = st2[:, cv2.CC_STAT_AREA] >= rf * rf  # ignore crumbs
    keep2[0] = False
    left = keep2[comp2]
    if left.any():
        strokes += _skeleton_paths(_thin(left), eps)

    # per thin piece: keep the lines only where they beat row-by-row filling clearly
    n, comp, stats, _ = cv2.connectedComponentsWithStats(Rt.astype(np.uint8), connectivity=8)
    line_cost = np.zeros(n)
    owner = []
    for pts in strokes:
        x, y = int(round(pts[0][0])), int(round(pts[0][1]))
        i = comp[min(max(y, 0), comp.shape[0] - 1), min(max(x, 0), comp.shape[1] - 1)]
        if i == 0:  # nudge to a labelled pixel along the stroke
            i = next((comp[int(round(py)), int(round(px))] for px, py in pts
                      if comp[int(round(py)), int(round(px))]), 0)
        owner.append(i)
        length = sum(math.hypot(x1 - x0, y1 - y0) for (x0, y0), (x1, y1) in zip(pts, pts[1:])) / cell
        line_cost[i] += 3 + 2 * len(pts) + length / 6
    rows = comp[cell // 2::cell]  # what row-by-row filling would see (one sample row per brush row)
    starts = (rows[:, 1:] != rows[:, :-1]) & (rows[:, 1:] > 0)
    runs = np.bincount(rows[:, 1:][starts], minlength=n) + np.bincount(rows[:, 0], minlength=n)
    scan_cost = 4 * runs + stats[:, cv2.CC_STAT_AREA] / (cell * cell) / 6
    use = (line_cost < 0.7 * scan_cost) & (line_cost > 0)
    use[0] = False
    strokes = [p for p, i in zip(strokes, owner) if use[i]]
    return strokes, use[comp]


def _detect_lines(hi, bw, contrast, max_colors=3):
    """Thin line art in the (unsimplified) image: dark or light curves narrower than about
    one brush width bw (px), clearly contrasting with their surroundings and at least two
    brush widths long. Returns (paths, rgb per path, mask of line pixels)."""
    g = cv2.GaussianBlur(cv2.cvtColor(np.ascontiguousarray(hi), cv2.COLOR_RGB2GRAY), (3, 3), 0).astype(np.float32)
    k = int(2 * bw) | 1
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    dark = cv2.morphologyEx(g, cv2.MORPH_BLACKHAT, ker)  # thin dark lines
    light = cv2.morphologyEx(g, cv2.MORPH_TOPHAT, ker)   # thin light lines
    mask = ((dark > contrast) | (light > contrast)).astype(np.uint8)
    # bridge small breaks along a line (faded/antialiased spots) so curves trace in one piece
    j = max(3, int(bw * 0.4) | 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (j, j)))
    n, comp, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    ok = st[:, cv2.CC_STAT_AREA] >= bw * bw * 0.5
    ok[0] = False
    mask = ok[comp]
    if not mask.any():
        return [], [], mask
    # keep only genuinely thin parts (wider blobs are left to the fill)
    dist = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 3)
    sk = _thin(mask) & (dist <= 1.2 * bw)
    paths = [_smooth_path(p, LINE_SMOOTH * bw) for p in _skeleton_paths(sk, 0.7)
             if len(p) > 1 and np.hypot(*np.diff(np.array(p), axis=0).T).sum() >= 3 * bw]
    if not paths:
        return [], [], np.zeros_like(mask)
    # color of each line: the darkest/lightest-ish middle of the pixels along it
    cols, weights = [], []
    for p in paths:
        P = np.array(p)
        seg = np.concatenate([np.linspace(a, b, max(2, int(np.hypot(*(b - a))) + 1)) for a, b in zip(P, P[1:])])
        xy = np.clip(np.round(seg).astype(int), 0, [hi.shape[1] - 1, hi.shape[0] - 1])
        cols.append(np.median(hi[xy[:, 1], xy[:, 0]].astype(np.float64), axis=0))
        weights.append(len(xy))
    cols = np.array(cols)
    # few line colors = few color switches: cluster in Lab, weighted by length
    lab = _to_lab(cols)
    k = min(max_colors, len(cols))
    cent = lab[np.argsort(-np.array(weights))[:k]].copy()
    for _ in range(10):
        lbl = ((lab[:, None] - cent[None]) ** 2).sum(2).argmin(1)
        for j in range(k):
            if (lbl == j).any():
                w = np.array(weights)[lbl == j]
                cent[j] = (lab[lbl == j] * w[:, None]).sum(0) / w.sum()
    rgb = np.array([np.average(cols[lbl == j], axis=0, weights=np.array(weights)[lbl == j])
                    if (lbl == j).any() else cols[0] for j in range(k)])
    line_mask = np.zeros(mask.shape, np.uint8)
    for p in paths:
        cv2.polylines(line_mask, [np.round(np.array(p) * 16).astype(np.int32)], False, 1,
                      max(1, int(bw)), cv2.LINE_8, 4)
    return paths, [rgb[j] for j in lbl], line_mask.astype(bool) & mask | sk


LINE_SMOOTH = 0.35  # line art point simplification, in brush widths


def _smooth_path(p, tol):
    """Adobe-style 'simplify': light smoothing, then as few points as stay within tol (px).
    Fewer, gentler points = fewer game frames and no corner holds."""
    P = np.array(p, np.float64)
    if len(P) < 3:
        return [tuple(map(float, q)) for q in P]
    # densify, smooth with a small moving average (keeps the ends fixed), then simplify
    seg = [np.linspace(a, b, max(2, int(np.hypot(*(b - a))) + 1))[:-1] for a, b in zip(P, P[1:])]
    D = np.vstack(seg + [P[-1:]])
    if len(D) > 5:
        k = max(3, int(tol * 2) | 1)
        pad = np.vstack([np.repeat(D[:1], k // 2, 0), D, np.repeat(D[-1:], k // 2, 0)])
        ker = np.ones(k) / k
        Ds = np.stack([np.convolve(pad[:, 0], ker, "valid"), np.convolve(pad[:, 1], ker, "valid")], 1)
        Ds[0], Ds[-1] = D[0], D[-1]
        D = Ds
    ap = cv2.approxPolyDP(np.round(D * 16).astype(np.int32).reshape(-1, 1, 2), tol * 16, False).reshape(-1, 2) / 16
    return [tuple(map(float, q)) for q in ap]


def _order_paths(paths):
    """Greedy nearest-neighbor order (and direction) to cut travel between line strokes."""
    left, out, cur = list(paths), [], (0.0, 0.0)
    while left:
        best = min(range(len(left)), key=lambda i: min(math.dist(cur, left[i][0]), math.dist(cur, left[i][-1])))
        p = left.pop(best)
        if math.dist(cur, p[-1]) < math.dist(cur, p[0]):
            p = p[::-1]
        out.append(p)
        cur = p[-1]
    return out


def _edge_strokes(R, rt, band, keep=None, keep_deep=None, band_deep=None):
    """Tiny-brush strokes (hi-res px) that paint the band of width `band` just inside the
    region R's outline, so every color edge is redrawn sharply at the end. Brush centers run
    on contours of R eroded by rt, rt + step, ...; regions thinner than the brush get their
    centerline instead."""
    dist = cv2.distanceTransform(R.astype(np.uint8), cv2.DIST_L2, 5)
    step = 1.7 * rt
    band_deep = band if band_deep is None else band_deep
    rings = min(5, 1 + max(0, math.ceil((band_deep - 2 * rt) / step)))
    eps = max(1.3, 0.6 * rt)
    strokes = []
    first = None
    for k in range(rings):
        m = dist >= rt + k * step + 0.5
        if not m.any():
            break
        if first is None:
            first = m
        # rings up to `band` deep go along every strong edge; deeper ones only near white
        strokes += _trace(m, eps, keep if k * step < band - 2 * rt + 1e-6 or k == 0 else keep_deep)
    # parts of R the first ring can't reach (thinner than the brush): draw their centerline
    reach = first if first is not None else np.zeros_like(R)
    if first is not None:
        r = int(math.ceil(rt + 1))
        reach = cv2.dilate(first.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))).astype(bool)
    thin = R & ~reach
    if keep is not None:
        thin &= keep
    if thin.any():
        sk = _thin(thin)
        for pts in _trace(sk, eps):
            strokes.append(pts if len(pts) > 1 else pts * 2)
    return strokes


SUB = 4  # horizontal sub-cells per cell: strokes can start/stop at 1/4-cell precision


def _disk(r, sx=1):
    """Offsets (dy, dx) of an ellipse: radius r rows, r * sx columns."""
    ry, rx = int(math.floor(r)), int(math.floor(r * sx))
    return [(dy, dx) for dy in range(-ry, ry + 1) for dx in range(-rx, rx + 1)
            if (dx / sx) ** 2 + dy * dy <= r * r]


def _shift(m, dy, dx):
    """out[y, x] = m[y + dy, x + dx] (False outside)."""
    h, w = m.shape
    out = np.zeros_like(m)
    y0, y1 = max(0, -dy), min(h, h - dy)
    x0, x1 = max(0, -dx), min(w, w - dx)
    if y0 < y1 and x0 < x1:
        out[y0:y1, x0:x1] = m[y0 + dy:y1 + dy, x0 + dx:x1 + dx]
    return out


def _morph(m, offs, erode):
    """out[y, x] = AND (erode) / OR (dilate) of m[y + dy, x + dx] over offs (False outside).
    Offsets are grouped into horizontal runs per row; each run is one compare against a
    run-length map (erode: True cells ending here; dilate: cells since the last True)."""
    rows = {}
    for dy, dx in offs:
        rows.setdefault(dy, set()).add(dx)
    h, w = m.shape
    P = max(abs(dx) for _, dx in offs) + 1
    mp = np.zeros((h, w + 2 * P), bool)
    mp[:, P:P + w] = m
    idx = np.arange(w + 2 * P, dtype=np.int32)
    if erode:  # L = length of the True run ending at x
        L = idx - np.maximum.accumulate(np.where(mp, -1, idx), axis=1)
    else:      # D = distance back to the last True (huge if none)
        D = idx - np.maximum.accumulate(np.where(mp, idx, -10 ** 6), axis=1)
    out = np.ones((h, w), bool) if erode else np.zeros((h, w), bool)
    for dy, dxs in rows.items():
        dxs = sorted(dxs)
        a = dxs[0]
        for p, q in zip(dxs, dxs[1:] + [None]):
            if q == p + 1:
                continue
            b = p  # run a..b: window [x+a, x+b], judged at its right end x+b
            if erode:
                r = L[:, P + b:P + b + w] >= b - a + 1
                out &= _shift(r, dy, 0)
            else:
                r = D[:, P + b:P + b + w] <= b - a
                out |= _shift(r, dy, 0)
            a = q
    return out


def _erode(m, offs):
    return _morph(m, offs, True)


def _dilate(m, offs):
    return _morph(m, offs, False)


def _segments(row):
    """(A, B) index ranges of consecutive True values."""
    out, w, x = [], len(row), 0
    while x < w:
        if not row[x]:
            x += 1
            continue
        A = x
        while x + 1 < w and row[x + 1]:
            x += 1
        out.append((A, x))
        x += 1
    return out


def _row_runs(allowed_row, target_row):
    """Runs [a, b, A, B, used]: target span a..b inside allowed segment A..B."""
    out = []
    for A, B in _segments(allowed_row):
        hits = np.flatnonzero(target_row[A:B + 1])
        if len(hits):
            out.append([A + int(hits[0]), A + int(hits[-1]), A, B, False])
    return out


def _fine_runs(allowed_row, target_row, k, open_l=False, open_r=False):
    """Runs in brush-center space for a brush covering center +-k sub-cells: centers are
    inset by k so the round brush ends exactly at the color edge instead of spilling over
    (not at an open canvas edge, where overhang is clipped by the game)."""
    out = []
    n = len(allowed_row)
    for A, B in _segments(allowed_row):
        hits = np.flatnonzero(target_row[A:B + 1])
        if not len(hits):
            continue
        a, b = A + int(hits[0]), A + int(hits[-1])
        CA = A if open_l and A == 0 else A + k
        CB = B if open_r and B == n - 1 else B - k
        if CA > CB:  # segment narrower than the brush: paint its middle
            CA = CB = (A + B) // 2
        ca = min(max(a if CA == A else a + k, CA), CB)
        cb = min(max(b if CB == B else b - k, CA), CB)
        if ca > cb:
            ca = cb = min(max((a + b) // 2, CA), CB)
        out.append([ca, cb, CA, CB, False])
    return out


PRUNE_GAP = 10.0   # cells: a redundant stretch shorter than this is drawn through (lifting costs more)
PRUNE_NEED = 0.12  # share of a brush footprint that must change for that part of a stroke to stay


def _paint_hi(canvas, col, strokes, r):
    """Paint strokes (hi-res px polylines) with a round brush of radius r px onto canvas."""
    th, rr = max(1, int(round(2 * r))), max(1, int(r * 16))
    for P in strokes:
        P = np.round(np.asarray(P, np.float64) * 16).astype(np.int32)
        if len(P) > 1:
            cv2.polylines(canvas, [P], False, col, th, cv2.LINE_8, 4)
        for q in (P[[0, -1]] if len(P) > 1 else P):
            cv2.circle(canvas, (int(q[0]), int(q[1])), rr, col, -1, cv2.LINE_8, 4)


def _cut_polyline(P, cum, a, b):
    """Part of polyline P (n x 2, cumulative lengths cum) between arc lengths a and b."""
    def at(s):
        i = min(max(int(np.searchsorted(cum, s, side="right")) - 1, 0), len(P) - 2)
        seg = cum[i + 1] - cum[i]
        t = 0.0 if seg <= 0 else (s - cum[i]) / seg
        return P[i] + (P[i + 1] - P[i]) * min(max(t, 0.0), 1.0)
    inner = [P[i] for i in range(len(P)) if a < cum[i] < b]
    return np.array([at(a)] + inner + [at(b)])


def _prune_strokes(canvas, tgt, col, strokes, r, cell):
    """Drop the parts of strokes (hi-res px) that only paint pixels already showing col, or
    pixels that shouldn't be col. Ends are trimmed; a redundant stretch in the middle is cut
    out only if it is longer than PRUNE_GAP cells. Returns the kept strokes."""
    U = (canvas != col).any(2) & (tgt == col).all(2)
    if not U.any():
        return []
    H, W = U.shape
    I = np.zeros((H + 1, W + 1), np.int32)
    I[1:, 1:] = U.cumsum(0).cumsum(1)
    k = max(1, int(round(r * 0.8)))      # box inside the brush footprint
    need = max(3, int(PRUNE_NEED * (2 * k + 1) ** 2))
    step = max(1.0, cell / 2)
    gap = PRUNE_GAP * cell
    out = []
    for P in strokes:
        P = np.asarray(P, np.float64)
        if len(P) == 1:
            P = np.vstack([P, P])
        cum = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(P, axis=0).T))])
        s = np.append(np.arange(0.0, cum[-1], step), cum[-1])
        idx = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(P) - 2)
        seg = np.maximum(cum[idx + 1] - cum[idx], 1e-9)
        q = P[idx] + (P[idx + 1] - P[idx]) * np.clip((s - cum[idx]) / seg, 0, 1)[:, None]
        x, y = np.round(q[:, 0]).astype(int), np.round(q[:, 1]).astype(int)
        x0, x1 = np.clip(x - k, 0, W), np.clip(x + k + 1, 0, W)
        y0, y1 = np.clip(y - k, 0, H), np.clip(y + k + 1, 0, H)
        good = (I[y1, x1] - I[y0, x1] - I[y1, x0] + I[y0, x0]) >= need
        if good.all():
            out.append(P)
            continue
        gi = np.flatnonzero(good)
        if not len(gi):
            continue
        a = s[gi[0]]
        for i, j in zip(gi[:-1], gi[1:]):
            if s[j] - s[i] > gap:
                out.append(_cut_polyline(P, cum, a, s[i]))
                a = s[j]
        out.append(_cut_polyline(P, cum, a, s[gi[-1]]))
    return out


def _rasterize(strokes, shape):
    """Cells crossed by polylines (horizontal, vertical, or one-cell-per-row diagonals)."""
    P = np.zeros(shape, dtype=bool)
    for pts in strokes:
        for (x0, y0), (x1, y1) in zip(pts, pts[1:] or pts):
            n = max(abs(x1 - x0), abs(y1 - y0))
            for i in range(n + 1):
                t = i / n if n else 0
                P[round(y0 + (y1 - y0) * t), round(x0 + (x1 - x0) * t)] = True
    return P


def _stroke_ticks(strokes):
    """Rough mouse-tick cost of strokes (per stroke + per corner + length)."""
    n = 0
    for pts in strokes:
        n += 3 + 2 * len(pts)
        n += sum(abs(x1 - x0) / SUB + abs(y1 - y0) for (x0, y0), (x1, y1) in zip(pts, pts[1:])) / 6
    return n


def _fill_ticks(M, T):
    """Rough mouse-tick cost of filling T with the fine brush (~2 corners per row run)."""
    if not T.any():
        return 0.0
    runs = sum(len(_segments(row)) for row in (M & T)) if T.sum() < 4000 else \
        int(np.count_nonzero(np.diff(T.astype(np.int8), axis=1) == 1) + np.count_nonzero(T[:, 0]))
    return 4 * runs + T.sum() / SUB / 6


def _big_pass(M, T, rb, sx, open_sides=(False, False, False, False)):
    """Big round brush (radius rb rows = rb * sx sub-cells) fills the parts of T it fits
    into without touching cells outside M. open_sides (left, right, top, bottom): grid edges
    that are the canvas edge - the brush may hang over them (the game clips it).
    Returns (strokes, covered_mask)."""
    py, px_ = int(math.ceil(rb + 1)), int(math.ceil((rb + 1) * sx))
    l, r, t, b = open_sides
    Mp = np.pad(M, ((py, py), (px_, px_)), constant_values=False)
    if l:
        Mp[:, :px_] = np.pad(M[:, :1], ((py, py), (0, 0)), mode="edge")
    if r:
        Mp[:, -px_:] = np.pad(M[:, -1:], ((py, py), (0, 0)), mode="edge")
    if t:
        Mp[:py] = Mp[py:py + 1]
    if b:
        Mp[-py:] = Mp[-py - 1:-py]
    E = _erode(Mp, _disk(rb + 0.5, sx))[py:-py, px_:-px_]  # centers whose footprint stays allowed
    C = E & _dilate(T, _disk(rb, sx))       # ...and that actually paint target cells
    if not C.any():
        return [], np.zeros_like(T)
    h = M.shape[0]
    step = max(1, int(2 * rb * 0.85))
    rows = list(range(min(step // 2, h - 1), h, step))
    runs = [_row_runs(E[y], C[y]) for y in rows]

    def can_drop(x, ya, yb):
        return bool(E[min(ya, yb):max(ya, yb) + 1, x].all())

    strokes = []
    for i in range(len(rows)):
        for run in runs[i]:
            if not run[4]:
                strokes.append(_chain(runs, rows, i, run, can_drop=can_drop))
    covered = _dilate(_rasterize(strokes, M.shape), _disk(max(0.0, rb - 0.6), sx))
    return strokes, covered


# ---- picture prep: the part of planning that doesn't depend on the brushes ----
# (scaled to the grid, line art found, colors reduced and cleaned up). Cached in memory and on
# disk per picture + grid size + image settings, so the brush search and later rounds reuse it.

PREP_DIR = os.path.join(LIB_DIR, ".prep")
PREP_MAX_MB = 400
_PREP_MEM = {}


def _prep_key(img, *args):
    import hashlib
    return picture_id(img) + "_" + hashlib.md5(repr(args).encode()).hexdigest()[:16]


def picture_prep(img, grid_w, grid_h, ncolors, skip_white, min_blob, denoise, merge_de, min_color_frac,
                 smooth, lines, line_contrast):
    """(w, h, labels at HS px per cell, palette, line paths, line colors). Cached."""
    import pickle
    import zlib
    args = (grid_w, grid_h, ncolors, bool(skip_white), min_blob, denoise, float(merge_de), min_color_frac,
            float(smooth), bool(lines), float(line_contrast) if lines else 0.0, HS, 3)
    key = _prep_key(img, *args)
    hit = _PREP_MEM.get(key)
    path = os.path.join(PREP_DIR, key + ".bin")
    if hit is None:
        try:
            with open(path, "rb") as f:
                w, h, Lz, shape, pal, lp, lc = pickle.loads(f.read())
            hit = (w, h, np.frombuffer(zlib.decompress(Lz), np.uint8).reshape(shape), pal, lp, lc)
            os.utime(path)  # recently used: kept when the cache is trimmed
        except (OSError, EOFError, ValueError, pickle.UnpicklingError, zlib.error):
            hit = None
    if hit is None:
        hit = _picture_prep(img, grid_w, grid_h, ncolors, skip_white, min_blob, denoise, merge_de,
                            min_color_frac, smooth, lines, line_contrast)
        try:
            os.makedirs(PREP_DIR, exist_ok=True)
            w, h, L, pal, lp, lc = hit
            tmp = path + f".{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(pickle.dumps((w, h, zlib.compress(np.ascontiguousarray(L, np.uint8).tobytes(), 3),
                                      L.shape, pal, lp, lc)))
            os.replace(tmp, path)
            _trim_prep()
        except OSError:
            pass
    if len(_PREP_MEM) >= 24:
        _PREP_MEM.pop(next(iter(_PREP_MEM)))
    _PREP_MEM[key] = hit
    w, h, L, pal, lp, lc = hit
    return w, h, L.copy(), list(pal), lp, lc


def _trim_prep():
    """Keep the disk cache under PREP_MAX_MB (least recently used go first)."""
    try:
        fs = [(e.stat().st_mtime, e.stat().st_size, e.path) for e in os.scandir(PREP_DIR) if e.name.endswith(".bin")]
    except OSError:
        return
    total = sum(sz for _, sz, _ in fs)
    for _, sz, p in sorted(fs):
        if total <= PREP_MAX_MB * 1e6:
            break
        try:
            os.remove(p)
            total -= sz
        except OSError:
            pass


def _small_features(hi, contrast=60, max_frac=0.006):
    """Mask of small, compact, clearly darker spots (eyes, pupils, nostrils, mouths),
    from ~half a cell to ~4 cells across: smoothing and despeckling would erase them, and faces
    look wrong without them. Long bands and shading are left alone; a bigger contrasting area
    is searched again at higher contrast for the spots inside it. Grain/texture (lots of such
    spots) is left to the denoising: the most contrasting spots are kept up to max_frac of the
    picture. None if there are none."""
    g = cv2.cvtColor(np.ascontiguousarray(hi), cv2.COLOR_RGB2GRAY).astype(np.float32)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(3 * HS) | 1, int(3 * HS) | 1))
    # dark spots only: eyes, pupils, nostrils, mouths. (Light specks are mostly glare, foam
    # and texture: keeping them costs a lot of strokes for little.)
    score = cv2.morphologyEx(g, cv2.MORPH_BLACKHAT, k)
    min_area, max_side = (0.7 * HS) ** 2, 4 * HS
    taken = np.zeros(g.shape, bool)
    spots = []  # (peak contrast, area, slices, mask)
    for thr in (contrast, 1.5 * contrast, 2.2 * contrast):
        m = ((score > thr) & ~taken).astype(np.uint8)
        n, comp, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        for i in range(1, n):
            x, y, bw, bh, area = stats[i]
            if area < min_area or bw > max_side or bh > max_side or area < 0.25 * bw * bh:
                continue  # too small to draw, a long band, or a thin curve (line art's job)
            sl = (slice(y, y + bh), slice(x, x + bw))
            sm = comp[sl] == i
            spots.append((float(score[sl][sm].max()), int(area), sl, sm))
            taken[sl] |= sm
    if not spots:
        return None
    keep = np.zeros(g.shape, bool)
    budget, used = max_frac * g.size, 0
    for peak, area, sl, sm in sorted(spots, key=lambda t: -t[0]):
        if used + area > budget:
            break
        used += area
        keep[sl] |= sm
    return keep if used else None


def _picture_prep(img, grid_w, grid_h, ncolors, skip_white, min_blob, denoise, merge_de, min_color_frac,
                  smooth, lines, line_contrast):
    img = ImageOps.exif_transpose(img).convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    bg.alpha_composite(img)
    img = bg.convert("RGB")

    scale = min(grid_w / img.width, grid_h / img.height)
    w = max(1, min(grid_w, round(img.width * scale)))
    h = max(1, min(grid_h, round(img.height * scale)))
    hi = np.asarray(img.resize((w * HS, h * HS), Image.LANCZOS))
    line_paths, line_cols = [], []
    if lines:  # thin curves: drawn as lines at the end, removed from what gets filled
        line_paths, line_cols, lmask = _detect_lines(hi, HS / 0.9, line_contrast)
        if line_paths:  # fill the lines with their surroundings (closing removes thin dark lines,
            # opening thin light ones) - much faster than inpainting, same effect for thin lines
            hole = cv2.dilate(lmask.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
            k = int(2 * HS / 0.9) | 1
            ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
            src = np.ascontiguousarray(hi)
            closed = cv2.morphologyEx(src, cv2.MORPH_CLOSE, ker)
            opened = cv2.morphologyEx(src, cv2.MORPH_OPEN, ker)
            g = src.mean(2)
            fill = np.where((closed.mean(2) - g > g - opened.mean(2))[..., None], closed, opened)
            hi = np.where(hole[..., None], fill, src).astype(np.uint8)
    spots = _small_features(hi)  # eyes, pupils, nostrils: put back after smoothing/despeckling
    raw = hi
    if denoise >= 3:  # median filter: removes grain/texture specks, keeps edges and lines
        hi = cv2.medianBlur(np.ascontiguousarray(hi), min(15, (denoise * HS // 2) | 1))
    # palette from the denoised picture; the kept spots then take their nearest palette color
    _, pal = quantize_lab(Image.fromarray(cv2.resize(hi, (w * 2, h * 2), interpolation=cv2.INTER_AREA)), ncolors)
    L = _assign_lab(hi, pal).reshape(h * HS, w * HS)
    npal = len(pal) // 3
    white = np.array([skip_white and min(pal[3 * i:3 * i + 3]) >= 245 for i in range(npal)] + [False] * (256 - npal))
    L = _merge_colors(L, pal, white, merge_de, min_color_frac)
    L = _majority(L, int(smooth * HS) | 1 if smooth > 0 else 0)
    if min_blob > 1:
        L = _despeckle_cv(L, min_blob * HS * HS // 2)
    if spots is not None:  # each spot as one solid blob of its own (median) color
        used = np.unique(L)
        P = np.array(pal, np.uint8).reshape(-1, 3)[used]
        n, comp = cv2.connectedComponents(spots.astype(np.uint8), connectivity=8)
        for i in range(1, n):
            sm = comp == i
            px_ = raw[sm].reshape(-1, 3)
            gray = px_.mean(1)
            col = np.median(px_[gray <= np.median(gray)], axis=0)  # the spot's dark core
            j = used[_assign_lab(col[None, None].astype(np.uint8), P.reshape(-1).tolist())[0]]
            if j != np.bincount(L[sm]).argmax():  # visibly different from what's there now
                L[sm] = j
    return w, h, L.astype(np.uint8), pal, line_paths, line_cols


def build_plan(img, grid_w, grid_h, ncolors, skip_white, min_blob=3, big_r=0.0, denoise=3, tiny_r=0.0,
               edge_contrast=15.0, merge_de=8.0, min_color_frac=0.002, smooth=0, sink=None,
               target_only=False, edges_only=False, line_fill=True, line_art_r=0.0, line_contrast=35.0,
               bucket=False, predrawn=None):
    """img: PIL Image. Grid cell = fine brush spacing; fill rows are cells, fill columns are
    SUB sub-cells per cell. big_r / tiny_r = big / tiny brush radius in cells (0 = off).
    Returns (preview_image, steps); step = (hex, "big"|"fine"|"tiny", strokes),
    stroke = [(gx, gy), ...] polyline in cell units (may be fractional).

    Colors are drawn largest-area first. A color may paint over cells of colors drawn later
    (they get overwritten), which merges runs. Per color, the big brush fills areas it fits
    inside, then the fine brush does the rest. Fills never spill onto white (background).
    Finally the tiny brush redraws a band just inside every color's outline, traced as smooth
    curves from a high-res target, which fixes the row-stepped edges.
    """
    w, h, L, pal, line_paths, line_cols = picture_prep(
        img, grid_w, grid_h, ncolors, skip_white, min_blob, denoise, merge_de, min_color_frac, smooth,
        line_art_r > 0, line_contrast)
    W = w * SUB
    npal = len(pal) // 3
    white = np.array([skip_white and min(pal[3 * i:3 * i + 3]) >= 245 for i in range(npal)] + [False] * (256 - npal))
    bucket_c = None
    if bucket:  # paint bucket: the largest color is filled over the whole canvas in one click,
        # so white is no longer free (it gets painted like any color). Not worth it if the
        # largest color is white already (the canvas is white).
        big_c = int(np.argmax(np.bincount(L.reshape(-1), minlength=256)[:npal]))
        if not (skip_white and white[big_c]) and min(pal[3 * big_c:3 * big_c + 3]) < 245:
            bucket_c = big_c
            white = np.zeros_like(white)
    ox, oy = (grid_w - w) // 2, (grid_h - h) // 2
    if target_only:  # just the color target (for screenshot correction)
        rgb = np.array(pal, dtype=np.uint8).reshape(-1, 3)[L]
        rgb[white[L]] = 255
        for p_, c_ in zip(line_paths, line_cols):  # line art on top
            cv2.polylines(rgb, [np.round(np.array(p_) * 16).astype(np.int32)], False,
                          tuple(int(v) for v in c_), max(1, int(round(2 * line_art_r * HS))), cv2.LINE_8, 4)
        return Image.fromarray(rgb), []
    counts = np.bincount(L.reshape(-1), minlength=256)
    order = sorted((c for c in range(npal) if counts[c] and not white[c]), key=lambda c: -counts[c])
    rank_of = np.full(256, -1, dtype=np.int32)  # -1 = never paint over (white / background)
    for r, c in enumerate(order):
        rank_of[c] = r

    # fill grid: one sample per (row, sub-cell); a cell whose brush band touches white is
    # treated as white so fills never spill onto the background (the edge pass fills the gap)
    px = HS // SUB
    D = L[HS // 2::HS, px // 2::px][:h, :W]
    band_white = white[L].reshape(h, HS, W, px).any(axis=(1, 3))
    RANK = rank_of[D]
    RANK[band_white] = -1
    rows = list(range(h))
    k = SUB // 2  # fine brush half-width in sub-cells (brush ~1.1 cells wide)
    center_offs = [(0, dx) for dx in range(-k, k + 1)]

    def to_cells(pts):
        return [((x + 0.5) / SUB - 0.5 + ox, y + oy) for x, y in pts]

    def hi_to_cells(pts):
        return [((x + 0.5) / HS - 0.5 + ox, (y + 0.5) / HS - 0.5 + oy) for x, y in pts]

    # each color's edge strokes are planned just before its fill, so the fill can leave out
    # what the edge pass will paint anyway
    edges, edge_cells = {}, {}

    def edge_for(c):
        return

    if tiny_r > 0:
        rt = tiny_r * HS
        band = 0.6 * HS       # fills can be off by ~half a row at edges between colors
        band_deep = 0.8 * HS  # ...and stop up to ~0.8 row short of white / the background
        lab = _to_lab(np.array(pal, dtype=np.float64).reshape(-1, 3))
        lab_w = _to_lab(np.array([[255.0, 255.0, 255.0]]))[0]
        ker3 = np.ones((3, 3), np.uint8)
        rk = int(math.ceil(band_deep + rt + 2))
        # strong_vs[a][b]: colors a and b differ enough that their shared edge gets sharpened
        lab_all = np.array([lab_w if white[i] else lab[i] for i in range(npal)] + [lab_w] * (256 - npal))
        dist = np.sqrt(((lab_all[:, None] - lab_all[None]) ** 2).sum(2))
        strong_vs = (dist >= edge_contrast) | white[None, :]
        bgish = white.copy()  # white canvas + the background color (largest area, drawn first)
        if order:
            bgish[order[0]] = True
        ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * rk + 1, 2 * rk + 1))
        def edge_for(c):  # noqa: F811
            R = L == c
            # The background (largest color, drawn first) and white are never re-traced: fills
            # never spill onto them (see the fill grid), so their edges are traced only from the
            # other side, deep enough (band_deep) to close the gap the no-spill fill leaves.
            # Edges between two other colors get one cheap ring from each side.
            # Soft shading steps (below edge_contrast) aren't traced at all.
            outer = cv2.dilate(R.astype(np.uint8), ker3).astype(bool) & ~R
            nb = L[outer]
            if bgish[c]:
                hit_bg = strong_vs[c][nb] & white[nb]  # background: only its edge with white
                hit = np.zeros_like(hit_bg)
            else:
                hit_bg = strong_vs[c][nb] & bgish[nb]
                hit = strong_vs[c][nb] & ~bgish[nb]
            strong, deep = np.zeros_like(R), np.zeros_like(R)
            strong[outer] = hit | hit_bg
            deep[outer] = hit_bg
            if not strong.any():
                return
            keep = cv2.dilate(strong.astype(np.uint8), ker).astype(bool)
            keep_deep = cv2.dilate(deep.astype(np.uint8), ker).astype(bool) & keep
            strokes = _edge_strokes(R, rt, band, keep, keep_deep, band_deep)
            if strokes:
                edges[c] = strokes
                # fill cells this edge pass fully repaints anyway: the fill can skip them
                cov = np.zeros(R.shape, np.uint8)
                th = max(1, int(2 * rt - 1))
                for pts in strokes:
                    P = np.round(np.array(pts) * 16).astype(np.int32)
                    cv2.polylines(cov, [P], False, 1, th, cv2.LINE_8, 4)
                    for q in P:
                        cv2.circle(cov, (int(q[0]), int(q[1])), max(1, int((rt - 0.5) * 16)), 1, -1, cv2.LINE_8, 4)
                edge_cells[c] = (cov.astype(bool) & R).reshape(h, HS, W, px).all(axis=(1, 3))

    if edges_only:  # just the edge pass (used to swap in a more detailed one mid-drawing)
        for c in order:
            edge_for(c)
        return None, [("{:02x}{:02x}{:02x}".format(*pal[3 * c:3 * c + 3]), "tiny",
                       [hi_to_cells(p) for p in edges[c]]) for c in order if c in edges]

    # area brushes, largest first: big_r may be one radius or [(tag, radius_cells), ...]
    tiers = [("big", big_r)] if isinstance(big_r, (int, float)) else list(big_r)
    tiers = [(t, r) for t, r in sorted(tiers, key=lambda tr: -tr[1]) if r >= 1.2]
    open_sides = (ox == 0, ox + w >= grid_w, oy == 0, oy + h >= grid_h)  # picture touches canvas edge

    # background already drawn (while this plan was being made): (hex, radius cells or 0 for a
    # bucket fill, strokes in cells). Cells it surely covered are left out of the background fill.
    pre_cov, pre_bucket = None, False
    if predrawn and order:
        bg_lab = _to_lab(np.array([pal[3 * order[0]:3 * order[0] + 3]], np.float64))[0]
        cov = np.zeros((h * HS, w * HS), np.uint8)
        for hx_, rc, strokes in predrawn:
            lab_ = _to_lab(np.array([[int(hx_[i:i + 2], 16) for i in (0, 2, 4)]], np.float64))[0]
            if np.sqrt(((lab_ - bg_lab) ** 2).sum()) > 10:
                continue  # a different color: doesn't count as background
            if rc <= 0:
                pre_bucket = True
                cov[:] = 1
                continue
            rad = rc * HS
            for pts in strokes:
                P = np.round(np.array([((x - ox + 0.5) * HS, (y - oy + 0.5) * HS) for x, y in pts]) * 16).astype(np.int32)
                if len(P) > 1:
                    cv2.polylines(cov, [P], False, 1, max(1, int(2 * rad)), cv2.LINE_8, 4)
                for q in P:
                    cv2.circle(cov, (int(q[0]), int(q[1])), int(rad * 16), 1, -1, cv2.LINE_8, 4)
        if not pre_bucket:  # keep half a row of margin inside what the strokes covered
            k = HS | 1
            cov = cv2.erode(cov, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)),
                            borderType=cv2.BORDER_CONSTANT, borderValue=1)
        pre_cov = cov.astype(bool).reshape(h, HS, W, px).all(axis=(1, 3))

    plan = []
    # what the canvas shows so far (hi-res, picture area): strokes are trimmed where they would
    # only repaint pixels that already have their color
    tgt = np.array(pal + [255] * (768 - len(pal)), dtype=np.uint8).reshape(-1, 3)[L]
    tgt[white[L]] = 255
    canvas = np.full_like(tgt, 255)
    rad_hi = {t: rr * HS for t, rr in tiers}
    rad_hi.update(fine=HS / 0.9 / 2, tiny=tiny_r * HS, line=line_art_r * HS)

    def cells_to_hi(p):
        return [((x - ox + 0.5) * HS - 0.5, (y - oy + 0.5) * HS - 0.5) for x, y in p]

    def hi_to_c(P):
        return [(float(x + 0.5) / HS - 0.5 + ox, float(y + 0.5) / HS - 0.5 + oy) for x, y in P]

    for hx_, rc, strokes in predrawn or []:
        col = tuple(int(hx_[i:i + 2], 16) for i in (0, 2, 4))
        if rc <= 0:
            canvas[:] = col
        else:
            _paint_hi(canvas, col, [cells_to_hi(p) for p in strokes], rc * HS)

    def add(step):
        hx_, tag, strokes = step
        col = tuple(int(hx_[i:i + 2], 16) for i in (0, 2, 4))
        if tag == "bucket":
            canvas[:] = col
        else:
            r = rad_hi.get(tag) or HS / 0.9 / 2
            P = [cells_to_hi(p) for p in strokes]
            if tag != "line":
                P = _prune_strokes(canvas, tgt, col, P, r, HS)
                if not P:
                    return
                step = (hx_, tag, [hi_to_c(p) for p in P])
            _paint_hi(canvas, col, P, r)
        plan.append(step)
        if sink is not None:
            sink(step)  # stream: drawing can start before planning finishes

    for r, c in enumerate(order):
        hx = "{:02x}{:02x}{:02x}".format(*pal[3 * c:3 * c + 3])
        if c == bucket_c:  # one bucket click at the canvas center fills it all
            if not pre_bucket:
                add((hx, "bucket", [[(grid_w / 2 - 0.5, grid_h / 2 - 0.5)]]))
            continue
        edge_for(c)
        M = RANK >= r
        if c in edges and r > 0:
            # never spill onto the background: skip cells whose brush band touches it (this
            # color's deep edge band fills that gap precisely with the tiny brush)
            # (only the middle of the band counts: a sliver of overlap at the band's top/bottom
            # edge is tolerated, which keeps the gap - and the edge band - shallow)
            hard = (bgish[L] & strong_vs[c][L]).reshape(h, HS, W, px)
            m = HS // 5
            M &= ~hard[:, m:HS - m].any(axis=(1, 3))
        T = (D == c) & M
        if r == 0 and pre_cov is not None:
            T &= ~pre_cov
        if c in edge_cells:
            T &= ~edge_cells[c]
        lines = []
        if line_fill:  # thin strands/lines/bands: follow their shape instead of row by row
            rf = HS / 0.9 / 2  # fine brush radius (hi-res px)
            lines, lcov = _line_fill((L == c) & (rank_of[L] >= 0), rf, 3 * rf, HS)
            if lines:  # rows skip cells that are mostly line-drawn pieces
                T &= ~(lcov.reshape(h, HS, W, px).mean(axis=(1, 3)) >= 0.5)
        for tag, rad in tiers:  # largest brush first, each fills what it fits into...
            big, covered = _big_pass(M, T, rad, SUB, open_sides)
            if not big:
                continue
            rest = T & ~covered
            # ...but only if that's quicker than leaving those cells to the smaller brushes
            if _stroke_ticks(big) + 12 + _fill_ticks(M, rest) < 0.95 * _fill_ticks(M, T):
                add((hx, tag, [to_cells(p) for p in big]))
                T = rest
        if lines:
            add((hx, "fine", [hi_to_cells(p) for p in lines]))
        if not T.any():
            continue

        add((hx, "fine", [to_cells(p) for p in _fill_strokes(M, T, open_sides[:2])]))

    # edges last (they fix the fills' row-stepped borders); start with the last fill color
    # so that switch is free
    edge_order = [c for c in order if c in edges]
    last = next((st for st in reversed(plan)), None)
    if last is not None:
        lc = next((c for c in edge_order if "{:02x}{:02x}{:02x}".format(*pal[3 * c:3 * c + 3]) == last[0]), None)
        if lc is not None:
            edge_order.remove(lc)
            edge_order.insert(0, lc)
    for c in edge_order:
        hx = "{:02x}{:02x}{:02x}".format(*pal[3 * c:3 * c + 3])
        add((hx, "tiny", [hi_to_cells(p) for p in edges[c]]))

    # line art last, on top of everything; one step per line color
    groups = {}
    for p_, c_ in zip(line_paths, line_cols):
        groups.setdefault("{:02x}{:02x}{:02x}".format(*[int(round(v)) for v in c_]), []).append(p_)
    for hx_, ps in groups.items():
        add((hx_, "line", [hi_to_cells(p_) for p_ in _order_paths(ps)]))

    rgb = np.array(pal, dtype=np.uint8).reshape(-1, 3)[L]
    rgb[white[L]] = 255
    for p_, c_ in zip(line_paths, line_cols):  # line art on top
        cv2.polylines(rgb, [np.round(np.array(p_) * 16).astype(np.int32)], False,
                      tuple(int(v) for v in c_), max(1, int(round(2 * line_art_r * HS))), cv2.LINE_8, 4)
    return Image.fromarray(rgb), plan  # target at HS px per cell


def _fill_strokes(M, T, open_lr=(False, False)):
    """Fine-brush strokes (sub-cell x, row y) painting target cells T, never letting the
    brush (+-SUB//2 sub-cells) leave the allowed cells M (except over open canvas edges)."""
    h, W = M.shape
    rows = list(range(h))
    k = SUB // 2
    runs = [_fine_runs(M[y], T[y], k, *open_lr) for y in rows]
    allowed = _erode(M, [(0, dx) for dx in range(-k, k + 1)]).tolist()  # where the brush center may go
    cell_run = [dict() for _ in rows]  # x -> run whose center segment covers x
    for y in rows:
        for run in runs[y]:
            for x in range(run[2], run[3] + 1):
                cell_run[y][x] = run
    strokes = []
    for y0 in rows:
        for run in runs[y0]:
            if run[4]:
                continue
            pts = _chain(runs, rows, y0, run)
            # keep the pen down while a short path over allowed cells reaches more work
            while True:
                hop = _hop(pts[-1], allowed, cell_run, W, h)
                if hop is None:
                    break
                path, nrun = hop
                pts = _simplify(pts + path[1:-1] + _chain(runs, rows, path[-1][1], nrun, path[-1][0]))
            strokes.append(pts)
    return strokes


def _simplify(pts):
    """Drop duplicate points and merge consecutive segments going the same direction."""
    out = [pts[0]]
    for p in pts[1:]:
        if p == out[-1]:
            continue
        if len(out) >= 2:
            (x0, y0), (x1, y1) = out[-2], out[-1]
            dx0, dy0, dx1, dy1 = x1 - x0, y1 - y0, p[0] - x1, p[1] - y1
            if dx0 * dy1 == dy0 * dx1 and dx0 * dx1 + dy0 * dy1 > 0:
                out[-1] = p
                continue
        out.append(p)
    return out


HOP_RADIUS = 24   # max BFS steps (sub-cells) to travel with the pen down
HOP_MAX_SEGS = 3  # travel only if it costs fewer mouse events than lifting


def _hop(start, allowed, cell_run, w, h):
    """BFS over allowed cells (8-connected; diagonals need both side cells allowed) to the
    nearest cell belonging to an unfinished run. Returns (path, run) or None."""
    from collections import deque
    sx, sy = start
    prev = {start: None}
    dq = deque([(start, 0)])
    while dq:
        (x, y), dist = dq.popleft()
        run = cell_run[y].get(x)
        if run is not None and not run[4] and (x, y) != start:
            path = [(x, y)]
            while prev[path[-1]] is not None:
                path.append(prev[path[-1]])
            path.reverse()
            path = _simplify(path)
            return (path, run) if len(path) - 1 <= HOP_MAX_SEGS else None
        if dist >= HOP_RADIUS:
            continue
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nx, ny = x + dx, y + dy
            if not (0 <= nx < w and 0 <= ny < h) or (nx, ny) in prev or not allowed[ny][nx]:
                continue
            if dx and dy and not (allowed[y][nx] and allowed[ny][x]):
                continue
            prev[(nx, ny)] = (x, y)
            dq.append(((nx, ny), dist + 1))
    return None


def _chain(runs, rows, i, r, s=None, can_drop=None):
    """Zigzag down through connected runs. runs[i] = runs on row rows[i];
    run = [a, b, A, B, used]: a..b must be painted, A..B may be painted.
    Looks ahead so each row ends exactly where the next row starts.
    can_drop(x, y0, y1): whether the vertical move between rows is allowed."""
    r[4] = True
    y = rows[i]
    s = r[0] if s is None else s
    pts = [(s, y)]
    ok_drop = can_drop or (lambda x, a, b: True)
    while True:
        a, b, A, B, _ = r
        lo, hi = min(a, s), max(b, s)
        if lo < s < hi:  # entered mid-run: go to nearer end first
            s = lo if s - lo <= hi - s else hi
            pts.append((s, y))
        dirs = ["R", "L"] if lo == hi else (["R"] if s == lo else ["L"])
        best = None
        if i + 1 < len(rows):
            y2 = rows[i + 1]
            nxt = [n for n in runs[i + 1] if not n[4]]
            for n in nxt:
                for d in dirs:
                    if d == "R":
                        E = min(max(hi, n[1]), B, n[3])
                        ok = E >= hi and E >= n[2]
                    else:
                        E = max(min(lo, n[0]), A, n[2])
                        ok = E <= lo and E <= n[3]
                    if ok and ok_drop(E, y, y2):
                        best = (n, E)
                        break
                if best:
                    break
            if best is None:
                end = hi if dirs[0] == "R" else lo
                best = next(((n, end) for n in nxt if n[2] <= end <= n[3] and ok_drop(end, y, y2)), None)
        if best is None:
            pts.append((hi if s == lo else lo, y))
            break
        n, E = best
        pts.append((E, y))
        pts.append((E, y2))
        n[4] = True
        r, i, y, s = n, i + 1, y2, E
    return _simplify(pts)


def simulate(plan, grid_w, grid_h, spacing, sizes, size=420):
    """Render the plan with round brushes (sizes = {"fine"|"big"|"tiny": diameter pt}),
    as the game would draw it. Rendered at 3x then downsampled for smooth edges."""
    ss = 3
    k = size * ss / (max(grid_w, grid_h) * spacing)  # px per point
    W, H = max(1, round(grid_w * spacing * k)), max(1, round(grid_h * spacing * k))
    cv = np.full((H, W, 3), 255, np.uint8)
    for hx, tag, strokes in plan:
        col = tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4))
        if tag == "bucket":
            cv[:] = col
            continue
        r = max(0.5, sizes.get(tag, spacing) * k / 2)
        for pts in strokes:
            P = np.array([[(x + 0.5) * spacing * k * 16, (y + 0.5) * spacing * k * 16] for x, y in pts], np.int32)
            if len(P) > 1:
                cv2.polylines(cv, [P], False, col, max(1, int(round(2 * r))), cv2.LINE_8, 4)
            for p in (P[[0, -1]] if len(P) > 1 else P):
                cv2.circle(cv, (int(p[0]), int(p[1])), int(r * 16), col, -1, cv2.LINE_8, 4)
            for p in P[1:-1]:  # round joins
                cv2.circle(cv, (int(p[0]), int(p[1])), int(r * 16), col, -1, cv2.LINE_8, 4)
    return Image.fromarray(cv2.resize(cv, (max(1, W // ss), max(1, H // ss)), interpolation=cv2.INTER_AREA))


TICK_OVERHEAD = 0.0009  # measured: each paced mouse step takes ~1ms longer than the delay


def step_parts(cfg, st, prev):
    """(mouse ticks, corner-hold ticks, fixed seconds) for one plan step: mouse ticks scale with
    mouse speed, corner holds always last their normal time, color/brush switches are fixed
    (they click the game's picker and brush slider at a safe, slower pace)."""
    if st[1] == "bucket":  # color + 3 clicks (bucket tool, canvas, brush tool) + fill render
        ui = max(cfg["frame_ms"], cfg.get("picker_ms", 30)) / 1000
        fixed = 10 * ui if cfg["picker"] in ("wheel", "ring") else 0.15 + 55 * cfg["key_ms"] / 1000
        return 0, 0, fixed + cfg["color_wait_ms"] / 1000 + 9 * ui + 0.3
    holds = (cfg["corner_frames"] - 1) * sum(len(held_corners(p)) for p in st[2])
    ticks = count_events([st], cfg["spacing"], cfg["segment"], cfg["corner_frames"]) - holds
    ui = max(cfg["frame_ms"], cfg.get("picker_ms", 30)) / 1000
    fixed = 0.0
    if prev is None or st[0] != prev[0]:  # color change
        fixed += (10 * ui if cfg["picker"] in ("wheel", "ring") else 0.15 + 55 * cfg["key_ms"] / 1000)
        fixed += cfg["color_wait_ms"] / 1000
    if (prev is None or st[1] != prev[1]) and cfg.get("brush_min") and cfg.get("brush_max"):
        fixed += 5 * ui + 0.1  # brush size change
    return ticks, holds, fixed


def step_components(cfg, st, prev):
    """Raw predicted seconds of one step split into (mouse steps, color change, brush change)."""
    if st[1] == "bucket":
        return step_parts(cfg, st, prev)[2], 0.0, 0.0
    ticks, holds, _ = step_parts(cfg, st, prev)
    ui = max(cfg["frame_ms"], cfg.get("picker_ms", 30)) / 1000
    color = brush = 0.0
    if prev is None or st[0] != prev[0]:
        color = (10 * ui if cfg["picker"] in ("wheel", "ring") else 0.15 + 55 * cfg["key_ms"] / 1000) + cfg["color_wait_ms"] / 1000
    if (prev is None or st[1] != prev[1]) and cfg.get("brush_min") and cfg.get("brush_max"):
        brush = 5 * ui + 0.1
    return (ticks + holds) * (cfg["frame_ms"] / 1000 + TICK_OVERHEAD), color, brush


def step_value(st, cfg):
    """How much a step adds to the picture: the area it paints (stroke length x brush width)."""
    if st[1] == "bucket":
        return float("inf")  # the whole background
    d = brush_sizes(cfg).get(st[1], cfg["spacing"] / 0.9) / cfg["spacing"]
    length = sum(math.hypot(x1 - x0, y1 - y0) for pts in st[2] for (x0, y0), (x1, y1) in zip(pts, pts[1:]))
    return (length + len(st[2])) * d


def step_seconds(cfg, st, prev, calibrated=True):
    """Predicted seconds for one step. Calibrated: each part scaled by what real runs measured
    (learned per part: mouse steps, color changes, brush changes)."""
    mouse, color, brush = step_components(cfg, st, prev)
    if not calibrated:
        return mouse + color + brush
    tm = cfg.get("timing_model")
    if tm:
        return mouse * tm.get("mouse", 1.0) + color * tm.get("color", 1.0) + brush * tm.get("brush", 1.0)
    return (mouse + color + brush) * float(cfg.get("time_factor") or 1.0)


def fit_timing(rows, old=None):
    """Learn per-part multipliers from a run: rows = [(mouse, color, brush, actual seconds)].
    Least squares, kept within sane bounds and blended with what was learned before."""
    A = np.array([[m, c, b] for m, c, b, _ in rows], float)
    y = np.array([a for *_, a in rows], float)
    use = A.sum(0) > 0.5  # only parts this run actually exercised
    if not use.any() or len(rows) < 6:
        return old
    coef = np.ones(3)
    sol, *_ = np.linalg.lstsq(A[:, use], y, rcond=None)
    coef[use] = sol
    coef = np.clip(coef, [0.6, 0.3, 0.3], [10.0, 10.0, 10.0])
    prev = old or {"mouse": 1.0, "color": 1.0, "brush": 1.0}
    keys = ["mouse", "color", "brush"]
    return {k: round(0.3 * prev.get(k, 1.0) + 0.7 * (coef[i] if use[i] else prev.get(k, 1.0)), 3)
            for i, k in enumerate(keys)}


def estimate_seconds(plan, cfg, spacing=None):
    """Predicted drawing time for a plan with the given timing settings."""
    c = cfg if spacing is None else dict(cfg, spacing=spacing)
    return 0.3 + sum(step_seconds(c, st, plan[i - 1] if i else None) for i, st in enumerate(plan))


def sharp_turn(a, b, c, min_deg=50):
    """True if the path a->b->c turns by more than min_deg at b (needs a held corner)."""
    v1, v2 = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return True
    cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    return cos < math.cos(math.radians(min_deg))


def held_corners(pts):
    """Vertices (by index) that get extra hold frames: start, end, and sharp turns."""
    idx = {0, len(pts) - 1}
    for i in range(1, len(pts) - 1):
        if sharp_turn(pts[i - 1], pts[i], pts[i + 1]):
            idx.add(i)
    return idx


def count_events(plan, spacing, segment, corner_frames=1):
    """Paced mouse events per stroke: move + down + drags (+ corner holds) + up."""
    n = 0
    for _, _, strokes in plan:
        for pts in strokes:
            n += 3 + (corner_frames - 1) * len(held_corners(pts))
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                d = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 * spacing
                n += max(1, int(d / segment) + (d % segment > 0))
            if len(pts) == 1:
                n += 1
    return n


def inverse_curve(curve, target):
    """curve: [(requested, observed)] measured pairs. Returns the request that should produce
    `target` (linear interpolation; clamped to the measured range)."""
    pts = sorted(curve)
    req = [p for p, _ in pts]
    obs = list(np.maximum.accumulate([o for _, o in pts]))  # observed must not go down
    if target <= obs[0]:
        return req[0] * target / obs[0] if obs[0] > 0 else req[0]
    for (r0, o0), (r1, o1) in zip(zip(req, obs), zip(req[1:], obs[1:])):
        if o0 <= target <= o1:
            return r0 if o1 == o0 else r0 + (r1 - r0) * (target - o0) / (o1 - o0)
    return req[-1]


# ---------- screen measurement ----------

def capture(x, y, w, h):
    """Screenshot a screen region (points). Returns (rgb int32 array, px per point) or None
    if capture failed (e.g. no Screen Recording permission)."""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        res = subprocess.run(["screencapture", "-x", "-R", f"{int(x)},{int(y)},{int(w)},{int(h)}", path],
                             capture_output=True, timeout=10)
        if res.returncode != 0 or os.path.getsize(path) == 0:
            return None
        img = np.asarray(Image.open(path).convert("RGB"), dtype=np.int32)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return img, img.shape[1] / int(w)


def dot_diameter(img, scale, x0, y0, cx, cy, half):
    """Diameter (pt) of the dark dot centered at screen point (cx, cy) inside a capture whose
    top-left is screen point (x0, y0). None if no dot there."""
    px, py, hp = int(round((cx - x0) * scale)), int(round((cy - y0) * scale)), int(half * scale)
    crop = img[max(0, py - hp):py + hp, max(0, px - hp):px + hp]
    if crop.size == 0:
        return None
    lum = crop.mean(2)
    c_y, c_x = min(hp, py), min(hp, px)  # dot center inside the crop
    # compare with the canvas around it (small dots are anti-aliased: gray, not black)
    border = np.concatenate([lum[0], lum[-1], lum[:, 0], lum[:, -1]])
    bg = float(np.median(border))
    core = float(lum[max(0, c_y - 3):c_y + 4, max(0, c_x - 3):c_x + 4].min())
    if bg - core < 40:
        return None  # nothing clearly darker than the canvas here
    dark = lum < (bg + core) / 2  # half-way edge: measures the dot's real width
    if not dark[max(0, c_y - 3):c_y + 4, max(0, c_x - 3):c_x + 4].any():
        return None
    if not dark[c_y, c_x]:  # re-center on the darkest nearby pixel
        win = lum[max(0, c_y - 3):c_y + 4, max(0, c_x - 3):c_x + 4]
        dy, dx = np.unravel_index(np.argmin(win), win.shape)
        c_y, c_x = max(0, c_y - 3) + dy, max(0, c_x - 3) + dx

    def extent(line, mid):
        if not line[mid]:
            return 0
        lo = hi = mid
        while lo - 1 >= 0 and line[lo - 1]:
            lo -= 1
        while hi + 1 < len(line) and line[hi + 1]:
            hi += 1
        return hi - lo + 1

    row = dark[c_y] | dark[max(0, c_y - 1)]
    col = dark[:, c_x] | dark[:, max(0, c_x - 1)]
    d_px = max(extent(row, c_x), extent(col, c_y))
    return round(d_px / scale, 1) if d_px >= 1 else None


def ocr_text(path):
    """Text lines in an image file, via macOS Vision (accurate mode; ~0.05s once warm)."""
    import Vision
    from Foundation import NSURL
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(path), None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)
    handler.performRequests_error_([req], None)
    out = []
    for obs in (req.results() or []):
        cand = obs.topCandidates_(1)
        if cand:
            out.append(str(cand[0].string()))
    return out


def parse_timer(texts):
    """Seconds from timer text like '02:40', 'O 2:41', '1.05' or '95'; None if unreadable."""
    import re
    joined = " ".join(texts)
    m = re.findall(r"(\d{1,2})\s*[:.;]\s*(\d{2})", joined)
    if m:
        mins, secs = int(m[-1][0]), int(m[-1][1])
        return mins * 60 + secs if secs < 60 else None
    m = re.findall(r"\b(\d{1,3})\b", joined)
    return int(m[-1]) if m and int(m[-1]) <= 600 else None


def read_timer(tl, br):
    """Seconds left on the in-game timer inside the calibrated box, or None."""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        x, y = min(tl[0], br[0]), min(tl[1], br[1])
        w, h = max(4, abs(br[0] - tl[0])), max(4, abs(br[1] - tl[1]))
        res = subprocess.run(["screencapture", "-x", "-R", f"{int(x)},{int(y)},{int(w)},{int(h)}", path],
                             capture_output=True, timeout=10)
        if res.returncode != 0 or os.path.getsize(path) == 0:
            return None
        return parse_timer(ocr_text(path))
    except Exception:
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def warm_up_ocr():
    """First Vision call in a process is slow; do it in the background at startup."""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        Image.new("RGB", (60, 20), "white").save(path)
        ocr_text(path)
    except Exception:
        pass
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def measure_dot(cx, cy, half=55):
    """Screenshot around (cx, cy) and return the dark dot's diameter in points, or None."""
    cap = capture(cx - half, cy - half, 2 * half, 2 * half)
    if cap is None:
        return None
    img, scale = cap
    return dot_diameter(img, scale, int(cx - half), int(cy - half), cx, cy, half)


# ---------- drawing ----------

# macOS ANSI virtual keycodes
KEYCODES = {
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3,
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28, "9": 25,
    "#": 20, "right": 124, "backspace": 51, "enter": 36, "command": 55, "shift": 56,
}
# Private source: our events don't inherit real/leftover modifier state (e.g. the Fn flag
# macOS attaches to arrow keys, which turned "f" into Fn+F = toggle fullscreen).
_KEY_SRC = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)


def _key(name, down, flags=0, char=None):
    ev = Quartz.CGEventCreateKeyboardEvent(_KEY_SRC, KEYCODES[name], down)
    Quartz.CGEventSetFlags(ev, flags)  # explicit: never Fn/Ctrl/etc. unless asked
    if char is not None:
        Quartz.CGEventKeyboardSetUnicodeString(ev, len(char), char)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


KEY_DELAY = 0.01  # seconds; set from config before drawing


def tap(name, flags=0, char=None):
    if stop_key_down():
        raise Aborted("Esc pressed")
    _key(name, True, flags, char)
    time.sleep(KEY_DELAY)
    _key(name, False, flags, char)
    time.sleep(KEY_DELAY)


def type_char(ch):
    if ch == "#":
        sh = Quartz.kCGEventFlagMaskShift
        _key("shift", True, sh)
        time.sleep(KEY_DELAY)
        tap("#", sh, "#")
        _key("shift", False, 0)
        time.sleep(KEY_DELAY)
    else:
        tap(ch, 0, ch)


def select_all():
    cmd = Quartz.kCGEventFlagMaskCommand
    _key("command", True, cmd)
    time.sleep(KEY_DELAY)
    tap("a", cmd)
    _key("command", False, 0)
    time.sleep(KEY_DELAY)


class DotPacker:
    """Places square test slots left-to-right in rows inside the canvas (any shape/size)."""

    def __init__(self, tl, br, margin=4, gap=6):
        self.x0, self.y0 = tl[0] + margin, tl[1] + margin
        self.x1, self.y1 = br[0] - margin, br[1] - margin
        self.gap = gap
        self.cx, self.ry, self.rh = self.x0, self.y0, 0.0

    def max_side(self):
        return min(self.x1 - self.x0, self.y1 - self.y0)

    def place(self, side):
        """Center of a free square of this side, or None when the canvas is full."""
        side = min(side, self.max_side())
        if self.cx + side > self.x1:  # next row
            self.cx, self.ry, self.rh = self.x0, self.ry + self.rh + self.gap, 0.0
        if self.ry + side > self.y1 or side <= 0:
            return None
        c = (self.cx + side / 2, self.ry + side / 2)
        self.cx += side + self.gap
        self.rh = max(self.rh, side)
        return c


STAGE_OF = {"bucket": "fill", "huge": "fill", "big": "fill", "medium": "fill", "fine": "fill", "tiny": "edges", "line": "lines"}


class Drawer:
    def __init__(self, cfg, stop_event, status, progress=None):
        self.cfg = cfg
        self.stop = stop_event
        self.status = status
        raw_progress = progress or (lambda stage, frac, text="", hx="": None)
        raw_status = status
        last = {"p": {}, "s": 0.0}

        def progress_throttled(stage, frac, text="", hx=""):
            now = time.perf_counter()  # at most ~5 updates/s per bar (repaints compete with drawing)
            if frac < 0 or frac >= 1.0 or now - last["p"].get(stage, 0.0) > 0.2:
                last["p"][stage] = now
                raw_progress(stage, frac, text, hx)

        def status_throttled(msg):
            now = time.perf_counter()
            if now - last["s"] > 0.15 or not msg.startswith("Step "):
                last["s"] = now
                raw_status(msg)
        self.progress = progress_throttled
        self.status = status_throttled
        self.stage_span = (0.0, 1.0)  # correction passes share one bar
        self.last = None
        self.frame = cfg["frame_ms"] / 1000.0
        self.frame0 = self.frame  # normal speed; self.frame drops when running late
        self.min_frame = min(self.frame0, cfg.get("min_frame_ms", 9) / 1000.0)
        self.t = time.perf_counter()
        global KEY_DELAY
        KEY_DELAY = cfg.get("key_ms", 10) / 1000.0

    def tick(self):
        """Wait until one frame after the previous tick (no drift from loop overhead)."""
        if stop_key_down():
            raise Aborted("Esc pressed")
        # at least one step after the previous event - also right after a pause (a color
        # change), or the move to a stroke's start and the press land in the same game frame
        target = max(self.t, _LAST_POST[0]) + self.frame
        now = time.perf_counter()
        if target > now:
            precise_sleep(target - now)
        self.t = max(target, now)

    def check(self):
        if self.stop.is_set():
            raise Aborted("stopped")
        if stop_key_down():
            raise Aborted("Esc pressed")
        if self.last is not None:
            x, y = mouse_pos()
            if abs(x - self.last[0]) > 40 or abs(y - self.last[1]) > 40:
                raise Aborted("mouse moved by user")

    def move(self, x, y):
        _post(Quartz.kCGEventMouseMoved, x, y)
        self.last = (x, y)

    def park(self):
        """Move the cursor off the canvas (bottom-right corner) for a screenshot. Waits and
        lifts the pen again first: a busy game can still have it down, and the move would
        then draw a line across the canvas."""
        if self.last is not None:
            time.sleep(0.12)
            _post(Quartz.kCGEventLeftMouseUp, *self.last)
            time.sleep(0.08)
        br = self.cfg["canvas_br"]
        self.move(br[0] - 3, br[1] - 3)

    def click(self, x, y):
        self.move(x, y)
        self.ui_wait()
        _post(Quartz.kCGEventLeftMouseDown, x, y)
        self.ui_wait()
        _post(Quartz.kCGEventLeftMouseUp, x, y)
        self.ui_wait()

    def stroke(self, pts):
        """pts: screen-space polyline. Each vertex is held for one frame so the game
        sees every corner. Cursor hovers the start for a frame before pressing, or Roblox
        doesn't register the press as a drag."""
        self.check()
        x0, y0 = pts[0]
        self.move(x0, y0)
        self.tick()
        _post(Quartz.kCGEventLeftMouseDown, x0, y0)
        self.tick()
        seg = max(1.0, self.cfg["segment"])
        hold = max(1, int(self.cfg.get("corner_frames", 2)))
        x, y = x0, y0
        try:
            for _ in range(hold - 1):  # make sure the game registers the start point
                _post(Quartz.kCGEventLeftMouseDragged, x0, y0)
                self.tick()
            held = held_corners(pts)
            cut = getattr(self, "_cut_strokes", False) and self.deadline
            for vi, (x1, y1) in enumerate(pts[1:] or [pts[0]], 1):
                if cut and time.time() > self.deadline:
                    break  # time's up: lift the pen now (run() stops after this stroke)
                px, py = x, y
                dist = ((x1 - px) ** 2 + (y1 - py) ** 2) ** 0.5
                steps = max(1, -(-int(dist) // int(seg)))
                for i in range(1, steps + 1):
                    t = i / steps
                    x, y = px + (x1 - px) * t, py + (y1 - py) * t
                    _post(Quartz.kCGEventLeftMouseDragged, x, y)
                    self.last = (x, y)
                    self.tick()
                # hold sharp corners extra frames: a missed corner = diagonal streak. When the
                # mouse is sped up, hold for the same real time (more, shorter ticks)
                n_hold = math.ceil((hold - 1) * self.frame0 / self.frame - 1e-9) if vi in held else 0
                for _ in range(n_hold):
                    _post(Quartz.kCGEventLeftMouseDragged, x, y)
                    self.tick()
        finally:
            _post(Quartz.kCGEventLeftMouseUp, x, y)
            self.tick()

    def bucket_fill(self, x, y):
        """Paint bucket at (x, y) with the current color, then back to the brush."""
        self.click(*self.cfg["bucket_tool"])
        self.click(x, y)
        time.sleep(0.25)  # let the game flood the canvas
        self.click(*self.cfg["brush_tool"])
        self.t = time.perf_counter()

    def ui_wait(self):
        """Pause between events on the game's UI (color wheel, sliders). Kept at a safe
        length however fast the drawing runs: a too-quick click on a slider doesn't move it,
        and every color after that keeps the old brightness (e.g. everything turns black)."""
        if stop_key_down():
            raise Aborted("Esc pressed")
        precise_sleep(max(self.frame0, self.cfg.get("picker_ms", 30) / 1000.0))
        self.t = time.perf_counter()

    def press_at(self, x, y):
        """Click that also works on sliders/wheels that only react to drags."""
        self.check()
        self.move(x, y)
        self.ui_wait()
        _post(Quartz.kCGEventLeftMouseDown, x, y)
        self.ui_wait()
        _post(Quartz.kCGEventLeftMouseDragged, x + 0.5, y)
        self.ui_wait()
        _post(Quartz.kCGEventLeftMouseDragged, x, y)
        self.ui_wait()
        _post(Quartz.kCGEventLeftMouseUp, x, y)
        self.ui_wait()

    def set_color(self, hexstr):
        if self.cfg["picker"] == "wheel":
            self.set_color_wheel(hexstr)
        elif self.cfg["picker"] == "ring":
            self.set_color_ring(hexstr)
        else:
            self.set_color_hex(hexstr)

    def set_color_ring(self, hexstr):
        """Hue ring around a color square: ring has red at the top with hue increasing
        clockwise; square has full color on the left, white on the right, black at the bottom."""
        r, g, b = (int(hexstr[i:i + 2], 16) / 255 for i in (0, 2, 4))
        h, sat, v = colorsys.rgb_to_hsv(r, g, b)
        if self.cfg.get("value_curve") and v > 0:
            v = min(1.0, max(0.0, inverse_curve(self.cfg["value_curve"], v)))
        if sat > 0.02:  # grays don't need a hue
            (cx, cy), (ex, ey) = self.cfg["ring_center"], self.cfg["ring_point"]
            rr = math.hypot(ex - cx, ey - cy)
            ang = math.radians(90 - h * 360)  # clockwise from the top
            self.press_at(cx + rr * math.cos(ang), cy - rr * math.sin(ang))
        (x0, y0), (x1, y1) = self.cfg["square_tl"], self.cfg["square_br"]
        inset = 0.04  # stay inside the square (its corners are rounded)
        fx = inset + (1 - 2 * inset) * (1 - sat)
        fy = inset + (1 - 2 * inset) * (1 - v)
        self.press_at(x0 + (x1 - x0) * fx, y0 + (y1 - y0) * fy)
        precise_sleep(self.cfg["color_wait_ms"] / 1000.0)

    def set_color_wheel(self, hexstr):
        r, g, b = (int(hexstr[i:i + 2], 16) / 255 for i in (0, 2, 4))
        h, sat, v = colorsys.rgb_to_hsv(r, g, b)
        (cx, cy), (ex, ey) = self.cfg["wheel_center"], self.cfg["wheel_edge"]
        R = math.hypot(ex - cx, ey - cy) * 0.97  # stay just inside the rim
        # wheel: red on the left, hue increases counter-clockwise
        theta = math.radians(h * 360 + 180)
        self.press_at(cx + sat * R * math.cos(theta), cy - sat * R * math.sin(theta))
        (tx, ty), (bx, by) = self.cfg["value_top"], self.cfg["value_bottom"]
        if self.cfg.get("value_curve") and v > 0:  # measured by Color check: fix the slider's response
            v = min(1.0, max(0.0, inverse_curve(self.cfg["value_curve"], v)))
        f = 1 - v
        self.press_at(tx + (bx - tx) * f, ty + (by - ty) * f)
        precise_sleep(self.cfg["color_wait_ms"] / 1000.0)

    def set_brush(self, pct):
        """pct: slider position (0-100), or ("clicks", n): n arrow steps above the minimum."""
        if isinstance(pct, (tuple, list)) and pct[0] == "clicks":
            return self._brush_by_clicks(int(pct[1]))
        self._clicks_at = None  # the slider moved: arrow position unknown
        (ax, ay), (bx, by) = self.cfg["brush_min"], self.cfg["brush_max"]
        f = max(0.0, min(1.0, pct / 100))
        self.press_at(ax + (bx - ax) * f, ay + (by - ay) * f)
        time.sleep(0.1)

    def _brush_by_clicks(self, n):
        """Exact small brush sizes with the arrows: go to the minimum once (slider far left,
        then a few - clicks), then click + / - only the difference."""
        if getattr(self, "_clicks_at", None) is None:
            if self.cfg.get("brush_min") and self.cfg.get("brush_max"):
                # jump near the minimum with the slider: the lowest slider position known to
                # work (clicks at the very end of a slider are sometimes ignored), then - clicks
                curve = self.cfg.get("brush_curve") or []
                low = min((p for p, _ in curve), default=0.0)
                (ax, ay), (bx, by) = self.cfg["brush_min"], self.cfg["brush_max"]
                f = max(0.0, min(1.0, low / 100))
                self.press_at(ax + (bx - ax) * f, ay + (by - ay) * f)
            for _ in range(int(self.cfg.get("arrow_reset_clicks", 25))):  # extra clicks at the minimum do nothing
                self.click(*self.cfg["brush_dec"])
            self._clicks_at = 0
        diff = n - self._clicks_at
        btn = self.cfg["brush_inc"] if diff > 0 else self.cfg["brush_dec"]
        for _ in range(abs(diff)):
            self.click(*btn)
        self._clicks_at = n
        time.sleep(0.05)

    def activate(self):
        """First click on the game (Roblox may swallow it to focus the window)."""
        pt = {"hex": self.cfg["hex_box"], "ring": self.cfg.get("square_tl")}.get(self.cfg["picker"], self.cfg["wheel_center"])
        self.click(*pt)
        time.sleep(0.3)
        self.t = time.perf_counter()

    def set_color_hex(self, hexstr):
        self.check()
        hx, hy = self.cfg["hex_box"]
        self.click(hx, hy)
        time.sleep(0.08)
        select_all()
        # cursor to end, then delete everything (works even if Cmd+A is ignored).
        # Box holds at most 7 chars ("#rrggbb").
        for _ in range(8):
            tap("right")
        for _ in range(8):
            tap("backspace")
        text = ("#" if self.cfg["hash_prefix"] else "") + hexstr.lower()
        for ch in text:
            type_char(ch)
        tap("enter")
        precise_sleep(self.cfg["color_wait_ms"] / 1000.0)

    def step_parts(self, st, prev):
        return step_parts(self.cfg, st, prev)

    def step_cost(self, st, prev):
        """Predicted seconds for one step (calibrated by past runs)."""
        return step_seconds(self.cfg, st, prev)

    def step_costs(self, plan):
        return [self.step_cost(st, plan[i - 1] if i else None) for i, st in enumerate(plan)]

    def watch_timer(self):
        """Background: re-read the in-game timer every few seconds and update the deadline."""
        tl, br = self.cfg.get("timer_tl"), self.cfg.get("timer_br")
        while not self.finished and tl and br:
            t = time.time()
            secs = read_timer(tl, br)
            if secs is not None and hasattr(self, "_timing"):
                self._timing["clock"].append((t, secs))
            if secs is not None:
                new = t + secs / float(self.cfg.get("game_rate") or 1.0)  # game seconds -> real
                # ignore OCR glitches: accept readings within 15s of what we expect
                if self.deadline is None or abs(new - self.deadline) < 15:
                    self.deadline = new
            time.sleep(4)

    def run(self, plan, deadline=None, stage=None, upgrade=None):
        """plan: list of steps, or an iterator still being planned (drawing starts as soon as
        the first step is ready). deadline: time.time() when the round ends; with it (and the
        live timer if calibrated) the edge pass is dropped if drawing is running late."""
        (cx0, cy0), s = self.cfg["canvas_tl"], self.cfg["spacing"]
        total = sum(len(st) for _, _, st in plan) if isinstance(plan, list) else None
        self.deadline, self.finished = deadline, False
        costs = self.step_costs(plan) if isinstance(plan, list) and deadline else None
        if costs and self.cfg.get("timer_tl") and self.cfg.get("timer_br"):
            threading.Thread(target=self.watch_timer, daemon=True).start()
        skip_tiny, note = False, ""
        self._stage_override = stage
        self._upgrade = upgrade
        self._timing = {"pred": 0.0, "act": 0.0, "rows": [], "fit": [], "t0": time.time(), "clock": []}
        self._cut_strokes = bool(costs)  # long strokes stop mid-way when time runs out
        try:
            self._run(plan, cx0, cy0, s, total, costs, skip_tiny, note)
        finally:
            self._cut_strokes = False
            self.finished = True
            self._finish_timing()

    def _finish_timing(self):
        """Write the run log and report how actual time compared with the raw prediction, so
        future predictions can be scaled to match reality."""
        tm = self._timing
        if self._stage_override:  # correction passes don't overwrite the drawing's log
            return
        try:
            with open(LOG_PATH, "w") as f:
                f.write(f"run started {time.strftime('%H:%M:%S', time.localtime(tm['t0']))}  "
                        f"event delay {self.cfg['frame_ms']}ms, picker pause {self.cfg.get('picker_ms', 30)}ms, "
                        f"color wait {self.cfg['color_wait_ms']}ms, time factor {self.cfg.get('time_factor') or 1.0:.2f}\n")
                f.write("step  color   brush   strokes  predicted  actual  (cumulative predicted / actual)\n")
                cp = ca = 0.0
                for i, hx, tag, n, pr, ac in tm["rows"]:
                    cp += pr
                    ca += ac
                    f.write(f"{i:4d}  #{hx}  {tag:6s}  {n:7d}  {pr:8.1f}s  {ac:6.1f}s  ({cp:.0f}s / {ca:.0f}s)\n")
                f.write(f"total: predicted {tm['pred']:.1f}s (uncalibrated), actual {tm['act']:.1f}s\n")
        except OSError:
            pass
        if tm["act"] > 8 and tm["pred"] > 5 and getattr(self, "on_timing", None) and not self._stage_override:
            self.on_timing({"ratio": tm["act"] / tm["pred"], "rows": tm["fit"], "clock": tm["clock"]})

    def _stage_totals(self, plan, cache):
        """Predicted seconds per progress stage for every step known so far."""
        known = plan if isinstance(plan, list) else getattr(plan, "planned", [])
        for i in range(len(cache), len(known)):
            cache.append(self.step_cost(known[i], known[i - 1] if i else None))
        totals = {}
        for st, c in zip(known, cache):
            stg = self._stage_override or STAGE_OF[st[1]]
            totals[stg] = totals.get(stg, 0.0) + c
        return totals

    def _report(self, stage, done_s, totals, text, hx):
        a, span = self.stage_span
        frac = done_s / totals[stage] if totals.get(stage) else 0.0
        if totals.get(stage) and not self._stage_override:
            left = max(0.0, totals[stage] - done_s) * getattr(self, "_speed", 1.0)
            text = f"{frac:.0%} \u00b7 ~{left:.0f}s \u00b7 {text}"
        self.progress(stage, a + span * min(1.0, frac), text, hx)
        ov = getattr(self, "_ov", None)
        if ov and not self._stage_override:  # whole drawing: time spent, time left, round end
            t0, rem0 = ov
            rem = max(0.0, rem0 - (time.perf_counter() - t0))
            el = time.time() - self._run_t0
            dl = self.deadline - time.time() if self.deadline else -1.0
            self.progress("overall", el / max(1e-6, el + rem), f"{rem:.1f}|{dl:.1f}|{el:.1f}", "")

    def _run(self, plan, cx0, cy0, s, total, costs, skip_tiny, note):
        done = 0
        cache, done_s = [], {}
        stream = plan if isinstance(plan, PlanStream) else None
        self.activate()
        has_slider = bool(self.cfg["brush_min"] and self.cfg["brush_max"]) or has_arrows(self.cfg)
        pct = brush_pcts(self.cfg)
        cur_color, cur_size = None, None
        start = time.time()
        self._run_t0, self._ov = start, None
        spent_est = 0.0
        seq = list(plan) if isinstance(plan, list) else None  # a list can be re-planned mid-drawing
        it = None if seq is not None else iter(plan)
        plan = seq if seq is not None else plan
        ci, upgraded, simplified, pace = 0, False, False, []
        bucketed = False
        parts = []
        last_check = [0.0]
        base = {"t": None, "p": 0.0}  # pace is measured from here (reset after switching plans)

        def late_check(pred_now):
            """Running late with work already on the canvas: keep it, and drop the remaining
            steps that add the least for their time (small / thin details first). Called every
            ~2s, also during long steps; the pace of the last ~8s counts most."""
            nonlocal parts, note
            t0 = base["t"] if base["t"] is not None else start
            elapsed = time.time() - t0
            pred_now = pred_now - base["p"]
            pace.append((elapsed, pred_now))
            if elapsed - last_check[0] < 2 or elapsed < 6 or pred_now < 1.5:
                return
            if getattr(self, "_fallback", None) and not simplified and pred_now < 0.25 * (sum(costs) - base["p"]):
                return  # little drawn yet: a simpler whole picture (next step) beats trimming
            last_check[0] = elapsed
            old = next(((e, p) for e, p in pace if elapsed - e <= 8), pace[0])
            recent = (elapsed - old[0]) / max(0.1, pred_now - old[1]) if pred_now - old[1] > 0.3 else 0
            speed = max(1.0, elapsed / pred_now, recent)
            left = self.deadline - time.time() - 1.5
            future = list(range(ci, len(seq)))  # steps after the current one
            cur_left = max(0.0, sum(costs[:ci]) - base["p"] - pred_now)  # rest of the current step
            need = (cur_left + sum(costs[i] for i in future)) * speed
            if need <= left or not future:
                return
            value = {i: step_value(seq[i], self.cfg) / max(1e-3, costs[i]) for i in future}
            drop = set()
            for i in sorted(value, key=value.get):
                if need <= left:
                    break
                drop.add(i)
                need -= costs[i] * speed
            keep = [i for i in range(len(seq)) if i not in drop]
            seq[:] = [seq[i] for i in keep]
            costs[:] = [costs[i] for i in keep]
            del cache[ci:]
            parts = []
            note = f" | late: skipped {len(drop)} small details to finish"
            self.status(f"Running {speed:.1f}x slower than planned: skipping {len(drop)} "
                        "low-value detail steps to finish in time")
        while True:
            if seq is not None and ci >= len(seq) and getattr(self, "_pending", None) is not None:
                # the background is done: switch to the full plan (wait if it's still planning)
                pend, self._pending = self._pending, None
                w0 = time.time()
                if not pend.ready.is_set():
                    self.status("Background done; waiting for the plan to finish...")
                    self.progress("plan", -1, "finishing the plan...", "")
                    self.progress("overall", -1, "waiting for the plan to finish", "")
                while not pend.ready.wait(0.1):
                    self.check()
                start += time.time() - w0  # waiting isn't slow drawing
                if pend.result is not None:
                    self.cfg, new, desc = pend.result
                    new = [st for st in new if not (st[1] == "bucket" and bucketed)]
                    self.progress("plan", 1.0, desc[:60], "")
                    seq.extend(new)
                    if costs is not None:
                        costs.extend(self.step_costs(new))
                    pct = brush_pcts(self.cfg)
                    s = self.cfg["spacing"]
                    has_slider = bool(self.cfg["brush_min"] and self.cfg["brush_max"]) or has_arrows(self.cfg)
                    cur_size = None
                    del cache[ci:]
                    parts = []
                    base["t"], base["p"] = time.time(), sum(costs[:ci]) if costs else 0.0
                    pace.clear()
                    last_check[0] = 0.0
                    self.status(f"Drawing the rest: {desc}")
                    if getattr(self, "_on_swap", None):
                        self._on_swap(self.cfg, new)
                    total = sum(len(st) for _, _, st in seq)
                continue
            if seq is not None:
                if ci >= len(seq):
                    break
                hexstr, size, strokes = seq[ci]
            else:
                nxt = next(it, None)
                if nxt is None:
                    break
                hexstr, size, strokes = nxt
            ci += 1
            if seq is not None and costs and getattr(self, "_fallback", None) and not simplified:
                # early check on the real pace: if this plan can't finish in time, switch to a
                # simpler version of the whole picture (prepared in the background) that can.
                # Only while little is drawn - later on, trimming the rest (below) wastes nothing.
                elapsed = time.time() - start
                pred_done = sum(costs[:ci - 1])
                if pred_done > 0.25 * sum(costs):
                    simplified = True  # too late for a full redraw
                elif elapsed > 6 and pred_done > 1.5:
                    simplified = True
                    speed = elapsed / max(0.1, pred_done)
                    left = self.deadline - time.time() - 1.5
                    if sum(costs[ci - 1:]) * speed > left * 1.1:
                        res = self._fallback(left, speed)
                        if res:
                            self.cfg, new, desc = res
                            seq[ci - 1:] = new
                            costs[ci - 1:] = self.step_costs(new)
                            del cache[ci - 1:]
                            parts = []
                            pct = brush_pcts(self.cfg)
                            s = self.cfg["spacing"]
                            cur_size = None
                            hexstr, size, strokes = seq[ci - 1]
                            note = f" | simplified to finish in time ({desc})"
                            base["t"], base["p"] = time.time(), sum(costs[:ci - 1])  # fresh pace for the new plan
                            pace.clear()
                            last_check[0] = 0.0
                            self.status(f"Drawing {speed:.1f}x slower than planned: switching to a simpler "
                                        f"version ({desc}) to finish in time")
            if seq is not None and costs:
                late_check(sum(costs[:ci - 1]))
            if seq is not None and costs and self._upgrade and not upgraded and not skip_tiny and size == "tiny":
                # fills are done: if we're ahead of the clock, swap in a more detailed edge pass
                upgraded = True
                elapsed = time.time() - start
                speed = max(1.0, elapsed / spent_est) if spent_est > 5 else 1.0
                spare = (self.deadline - time.time() - 1.5) / speed
                new = self._upgrade(spare, sum(costs[ci - 1:]), self)
                if new:
                    seq[ci - 1:] = new
                    costs[ci - 1:] = self.step_costs(new)
                    del cache[ci - 1:]
                    parts = []
                    hexstr, size, strokes = seq[ci - 1]
                    note = " | ahead of time: sharpening more edges"
                    self.status("Ahead of time: switching to a more detailed edge pass")
            if costs:
                # running late? predict the remaining time (scaled by how fast we've actually
                # been going) and speed the mouse up as needed (down to the fastest allowed delay;
                # back toward normal when ahead again). If even full speed can't make it, the
                # edge pass (drawn last) is simply cut off when time runs out.
                elapsed = time.time() - start
                speed = max(1.0, elapsed / spent_est) if spent_est > 5 else 1.0
                left = self.deadline - time.time() - 1.5
                if left <= 0:
                    self.status(f"Time's up: stopped after {done} strokes")
                    return
                if len(parts) != len(plan):
                    parts = [self.step_parts(st, plan[i - 1] if i else None) for i, st in enumerate(plan)]
                rest = parts[ci - 1:]
                rem_ticks = sum(t for t, _, _ in rest)
                rem_fixed = sum(hd * (self.frame0 + TICK_OVERHEAD) + f for _, hd, f in rest)
                need = (left / speed - rem_fixed) / max(1, rem_ticks) - TICK_OVERHEAD  # delay that just fits
                new_frame = min(self.frame0, max(self.min_frame, need * 0.95))
                if abs(new_frame - self.frame) > 0.0005:
                    faster = new_frame < self.frame
                    self.frame = new_frame
                    if new_frame < self.frame0:
                        note = f" | running late: mouse sped up to {new_frame * 1000:.0f}ms"
                        if faster:
                            self.status(f"Running late: speeding up the mouse ({new_frame * 1000:.0f}ms per move)")
                    else:
                        note = ""
                remaining = (rem_ticks * (self.frame + TICK_OVERHEAD) + rem_fixed) * speed
                self._speed = speed
                if remaining > left and self.frame <= self.min_frame + 1e-6:
                    note = " | running late even at full speed: edges will be cut short"
                t_i, h_i, f_i = parts[ci - 1]
                spent_est += t_i * (self.frame + TICK_OVERHEAD) + h_i * (self.frame0 + TICK_OVERHEAD) + f_i
                note_t = f" | {left:.0f}s left"
            else:
                note_t = ""
            stage = self._stage_override or STAGE_OF[size]
            totals = self._stage_totals(plan, cache)
            step_s = cache[ci - 1] if ci - 1 < len(cache) else 0.0
            rem0 = remaining if costs else sum(totals.values()) - sum(done_s.values())
            self._ov = (time.perf_counter(), max(0.0, rem0))
            if stream is not None and stream.complete:
                self.progress("plan", 1.0, "done", "")
            if skip_tiny and size == "tiny":
                self.progress("edges", -2, "skipped: running late", "")
                continue
            label = size
            self._report(stage, done_s.get(stage, 0.0), totals, label, hexstr)
            step_t0 = time.perf_counter()
            step_comp = step_components(self.cfg, (hexstr, size, strokes), getattr(self, "_prev_step", None))
            step_pred = sum(step_comp)
            if size == "bucket":
                if done == 0 and not bucketed:  # only on a blank canvas (a re-plan mid-drawing
                    self.set_color(hexstr)      # would flood over what's drawn)
                    cur_color = hexstr
                    self.bucket_fill(cx0 + (strokes[0][0][0] + 0.5) * s, cy0 + (strokes[0][0][1] + 0.5) * s)
                bucketed = True
                done_s[stage] = done_s.get(stage, 0.0) + step_s
                continue
            if hexstr != cur_color:
                self.set_color(hexstr)
                cur_color = hexstr
            if has_slider and size != cur_size:
                self.set_brush(pct[size])
                cur_size = size
            per_stroke = step_s / max(1, len(strokes))
            step_pred0 = sum(costs[:ci - 1]) if costs else 0.0
            step_cost_now = costs[ci - 1] if costs and ci - 1 < len(costs) else 0.0
            for si, pts in enumerate(strokes, 1):
                self.stroke([(cx0 + (gx + 0.5) * s, cy0 + (gy + 0.5) * s) for gx, gy in pts])
                done += 1
                if costs and seq is not None:
                    late_check(step_pred0 + step_cost_now * si / len(strokes))
                done_s[stage] = done_s.get(stage, 0.0) + per_stroke
                if si % 3 == 0 or si == len(strokes):
                    self._report(stage, done_s[stage], totals, f"{label} {si}/{len(strokes)}", hexstr)
                if done % 10 == 0:
                    self.status(f"Step {ci} #{hexstr} ({size}) - {done}" + (f"/{total}" if total else "")
                                + " strokes" + note_t + note)
                if costs and time.time() > self.deadline:
                    self.status(f"Time's up: stopped after {done} strokes")
                    return
            dt = time.perf_counter() - step_t0
            self._timing["pred"] += step_pred
            self._timing["act"] += dt
            self._timing["rows"].append((ci, hexstr, size, len(strokes), step_pred, dt))
            self._timing["fit"].append((*step_comp, dt))
            self._prev_step = (hexstr, size, strokes)
        self.status(f"Done: {done} strokes in {time.time() - start:.0f}s")
        if not self._stage_override:
            self.progress("plan", 1.0, "done", "")
            for stg in ("fill", "edges", "lines"):
                used = stg in self._stage_totals(plan, cache)
                self.progress(stg, 1.0 if used else -2, "done" if used else "not used", "")

    def measure_brush(self):
        """Draw a black dot per brush size and measure each from a screenshot.
        Returns [(key, diameter_pt or None, (cx, cy))]."""
        self.activate()
        self.set_color("000000")
        tl, br = self.cfg["canvas_tl"], self.cfg["canvas_br"]
        has_slider = bool(self.cfg["brush_min"] and self.cfg["brush_max"])
        packer = DotPacker(tl, br)
        bp = brush_pcts(self.cfg)
        has_slider = has_slider or has_arrows(self.cfg)
        dots = [("brush_pt", bp["fine"], self.cfg.get("brush_pt") or 20)]
        if has_slider and self.cfg["big_brush_pct"] > 0:
            dots.append(("big_brush_pt", bp["big"], self.cfg.get("big_brush_pt") or 60))
        if has_slider and self.cfg["tiny_brush_pct"] > 0:
            dots.append(("tiny_brush_pt", bp["tiny"], self.cfg.get("tiny_brush_pt") or 10))
        out = []
        for key, pct, guess in dots:
            side = min(max(16.0, guess * 1.5 + 10), packer.max_side())
            pos = packer.place(side)
            if pos is None:
                break
            if has_slider:
                self.set_brush(pct)
            self.stroke([pos])
            self.move(br[0] + 5, br[1] + 5)  # keep the cursor off the dot
            time.sleep(0.4)
            d = measure_dot(pos[0], pos[1], side / 2)
            out.append((key, d if d is None or d < side * 0.92 else None, pos))
        return out

    def measure_batch(self, items, half):
        """Draw a black dot per (slider_pct, (cx, cy)[, half]), then measure all from one
        screenshot. Returns a diameter (pt) or None per item."""
        items = [(it[0], it[1], it[2] if len(it) > 2 else half) for it in items]
        for pct, (cx, cy), _ in items:
            self.check()
            self.set_brush(pct)
            self.stroke([(cx, cy)])
        br = self.cfg["canvas_br"]
        self.park()  # keep the cursor off the dots
        time.sleep(0.3)
        x0 = int(min(c[0] - hf for _, c, hf in items))
        y0 = int(min(c[1] - hf for _, c, hf in items))
        x1 = max(c[0] + hf for _, c, hf in items)
        y1 = max(c[1] + hf for _, c, hf in items)
        cap = capture(x0, y0, x1 - x0, y1 - y0)
        if cap is None:
            return [None] * len(items)
        img, scale = cap
        return [dot_diameter(img, scale, x0, y0, cx, cy, hf) for _, (cx, cy), hf in items]

    def probe(self, packer, pct, guess):
        """Draw one test dot at slider pct in a slot sized for `guess` (pt) and measure it.
        Returns diameter, "big" (bigger than its slot), "full" (no room left) or None."""
        side = max(16.0, guess * 1.5 + 10)
        pos = packer.place(side)
        if pos is None:
            return "full"
        side = min(side, packer.max_side())
        self.check()
        if isinstance(pct, (tuple, list)) or (self.cfg.get("brush_min") and self.cfg.get("brush_max")):
            self.set_brush(pct)
        self.stroke([pos])
        br = self.cfg["canvas_br"]
        self.move(br[0] + 5, br[1] + 5)  # keep the cursor off the dot
        time.sleep(0.3)
        d = measure_dot(pos[0], pos[1], side / 2)
        if d is not None and d >= side * 0.92:
            return "big"
        return d

    def auto_brush(self, pick_sizes):
        """Trial and error: sweep the brush slider measuring dot sizes, let pick_sizes(curve)
        choose target diameters, then home in on the slider position for each target. Test
        dots are packed into whatever canvas space there is (works on small/squished canvases).
        Returns {"fine": (pct, d), "big": (pct, d) or None, "tiny": (pct, d) or None, "curve": [...]}."""
        tl, br = self.cfg["canvas_tl"], self.cfg["canvas_br"]
        packer = DotPacker(tl, br)
        prior = sorted(self.cfg.get("brush_curve") or [])

        def guess_for(p, last):
            if prior and len(prior) >= 2:
                xs, ys = zip(*prior)
                return float(np.interp(p, xs, ys))
            return 20.0 if last is None else last[1] * 1.7 + 6

        self.activate()
        self.set_color("000000")
        arrow_curve = []
        if has_arrows(self.cfg):  # exact small sizes with the arrows first
            self.status("Brush sweep: small sizes with the arrows...")
            last_d = None
            for n in (0, 1, 2, 3, 4, 6, 8, 11, 15, 20, 26):
                g = 6.0 if last_d is None else last_d * 1.4 + 4
                d = self.probe(packer, ("clicks", n), g)
                if d == "big":
                    d = self.probe(packer, ("clicks", n), g * 2.5 + 10)
                if d == "full" or d == "big":
                    break
                if isinstance(d, float):
                    arrow_curve.append((n, d))
                    last_d = d
                    self.status(f"Brush sweep: {n} clicks above minimum -> {d:.1f}pt")
                    if d > 35:
                        break
        sweep = (0, 1, 2, 3, 4, 6, 8, 13, 20, 30, 42, 56, 72, 100)  # low end too: the smallest brush matters
        self.status("Brush sweep: drawing test dots...")
        curve, seen_any, last = [], False, None
        for p in sweep:
            g = guess_for(p, last)
            if g > packer.max_side() * 0.9:  # dots this big don't fit this canvas: stop here
                break
            d = self.probe(packer, p, g)
            if d == "big":  # guessed too small: one more try with a bigger slot
                d = self.probe(packer, p, g * 2.5 + 10)
            if d == "full" or d == "big":
                break
            if d is None:
                if not seen_any and p >= 8:
                    raise RuntimeError("Can't see the test dots. Grant Screen Recording to the app running "
                                       "this script (System Settings > Privacy & Security), restart it, retry.")
                continue
            seen_any = True
            curve.append((p, d))
            last = (p, d)
            self.status(f"Brush sweep: slider {p}% -> {d:.1f}pt")
        if len(curve) < 2:
            raise RuntimeError("Not enough room or no size change across the slider: check the canvas and "
                               "brush slider calibration (or clear the canvas and retry).")

        targets = {k: t for k, t in pick_sizes(sorted(curve), arrow_curve).items() if t}
        best, clicks = {}, {}
        pending = dict(targets)
        acfg = dict(self.cfg, arrow_curve=arrow_curve)
        for k, t in list(pending.items()):  # within the arrow range: exact steps, nothing to tune
            hit = arrow_clicks_for(acfg, t)
            if hit:
                clicks[k] = hit[0]
                best[k] = (0.5, hit[1])
                pending.pop(k)
        for attempt in range(4):
            if not pending:
                break
            for k in list(pending):
                pct = self._pct_for(pending[k], curve)
                self.status(f"Tuning {k} brush to {pending[k]:.1f}pt: slider {pct:.1f}%")
                d = self.probe(packer, pct, pending[k])
                if d == "full":
                    pending.clear()
                    break
                if not isinstance(d, float):
                    continue
                curve.append((pct, d))
                t = targets[k]
                if k not in best or abs(d - t) < abs(best[k][1] - t):
                    best[k] = (round(pct, 1), d)
                if abs(d - t) <= 0.08 * t:
                    pending.pop(k)
        lo, hi = min(d for _, d in curve), max(d for _, d in curve)
        for k, t in targets.items():  # ran out of room while tuning: use what the curve predicts
            if k in best:
                continue
            if lo <= t <= hi:
                best[k] = (round(self._pct_for(t, curve), 1), round(t, 1))
            else:
                p, d = min(curve, key=lambda pd: abs(pd[1] - t))
                best[k] = (round(p, 1), d)
        if "fine" not in best:
            raise RuntimeError("Couldn't measure the fine brush")
        return {"fine": best["fine"], "big": best.get("big"), "tiny": best.get("tiny"), "curve": sorted(curve),
                "arrow_curve": arrow_curve, "clicks": clicks}

    @staticmethod
    def _pct_for(target, curve):
        """Slider position predicted to give `target` diameter (interpolating measurements)."""
        pts = sorted(curve)
        best = {}
        for p, d in pts:  # average repeated probes of one position
            best.setdefault(p, []).append(d)
        pts = [(p, sum(v) / len(v)) for p, v in sorted(best.items())]
        mono = []
        for p, d in pts:  # enforce non-decreasing size
            mono.append((p, max(d, mono[-1][1]) if mono else d))
        if target <= mono[0][1]:
            return mono[0][0]
        for (p0, d0), (p1, d1) in zip(mono, mono[1:]):
            if d0 <= target <= d1:
                return p0 if d1 == d0 else p0 + (p1 - p0) * (target - d0) / (d1 - d0)
        (p0, d0), (p1, d1) = mono[-2], mono[-1]  # extrapolate past the last measurement
        slope = (d1 - d0) / (p1 - p0) if p1 != p0 and d1 > d0 else None
        return min(100.0, p1 + (target - d1) / slope) if slope else 100.0

    def correct(self, target, passes, deadline=None):
        """Screenshot the canvas, find wrongly colored areas and repaint them; repeat."""
        if target is None:
            return
        tried, last_frac = None, None
        self.progress("correct", -1, "taking screenshot...", "")
        for i in range(passes):
            deadline = getattr(self, "deadline", None) or deadline  # live timer may have updated it
            if deadline and deadline - time.time() < 6:
                self.progress("correct", -2, "skipped: out of time", "")
                return
            br = self.cfg["canvas_br"]
            self.move(br[0] + 5, br[1] + 5)  # cursor off the canvas
            time.sleep(0.3)
            shot = capture_canvas(self.cfg, target.shape)
            if shot is None:
                self.status("Correction needs Screen Recording permission; skipped")
                self.progress("correct", -2, "skipped: needs Screen Recording", "")
                return
            if i == 0:
                sh = misaligned(target, shot)
                if sh:
                    self.status(f"Correction skipped: the canvas looks shifted by {sh[0]},{sh[1]} cells from the "
                                "calibration (camera moved?) - repainting would smear the drawing")
                    self.progress("correct", -2, "skipped: canvas doesn't line up", "")
                    return
                _, cols_, obs_, _ = find_errors(target, shot)
                off = [(c_, o_) for c_, o_ in zip(cols_, obs_) if min(c_) < 245 and
                       np.sqrt(((_to_lab(c_[None].astype(float)) - _to_lab(o_[None].astype(float))) ** 2).sum()) > 25]
                if off:
                    self.status("Colors coming out wrong (run Color check): " + ", ".join(
                        f"#{''.join(f'{int(v):02x}' for v in c_)}->#{''.join(f'{int(v):02x}' for v in o_)}"
                        for c_, o_ in off[:4]))
                    time.sleep(1.5)
            steps, frac, cells = correction_steps(target, shot, tried)
            n = sum(len(st) for _, _, st in steps)
            if frac > 0.25:  # this much wrong isn't drawing mistakes: something covers the canvas
                self.status(f"Correction skipped: {frac:.0%} of the screenshot doesn't match the drawing "
                            "(something covering the canvas, or the camera moved)")
                self.progress("correct", -2, "skipped: screen doesn't match", "")
                return
            if frac < 0.01 or not n or (last_frac is not None and frac > 0.75 * last_frac):
                self.status(f"Check {i + 1}: {frac:.1%} of the drawing is off - done")
                self.progress("correct", 1.0, f"done: {frac:.1%} off", "")
                return
            self.status(f"Check {i + 1}: {frac:.1%} of the drawing is off; fixing with {n} strokes")
            tried = cells if tried is None else tried | cells
            last_frac = frac
            self.stage_span = (i / passes, 1 / passes)
            self.run(steps, deadline=deadline, stage="correct")
            self.stage_span = (0.0, 1.0)
            self.progress("correct", (i + 1) / passes, f"pass {i + 1} done ({frac:.1%} was off)", "")
        self.progress("correct", 1.0, "done", "")

    # ---- Color check: does the picker give the colors asked for? ----

    def _patch(self, x, y, size, b):
        """Solid square patch (top-left x, y) with the current color and brush b."""
        rows = max(2, int(math.ceil(size / (0.8 * b))))
        pts = []
        for i in range(rows):
            yy = y + b / 2 + i * (size - b) / max(1, rows - 1)
            a, c = (x + b / 2, x + size - b / 2) if i % 2 == 0 else (x + size - b / 2, x + b / 2)
            pts += [(a, yy), (c, yy)]
        self.stroke(pts)

    def color_check(self):
        """Draw gray and dark-red swatches (and one gray over black), screenshot them, and
        compare with what was asked. Returns {"value_curve": [...], "transparent": bool,
        "report": [...]}."""
        base = dict(self.cfg)
        self.cfg = dict(base, value_curve=None)  # measure the raw picker response
        b = float(base.get("brush_pt") or base["spacing"] / 0.9)
        size = max(3 * b, 18.0)
        tl, br = base["canvas_tl"], base["canvas_br"]
        grays = [0.08, 0.15, 0.25, 0.35, 0.5, 0.7, 0.9]
        reds = [(0.95, 0.85, v) for v in (0.15, 0.25, 0.35, 0.5)] + [(0.95, 0.5, 0.4)]
        tests = [("gray", colorsys.hsv_to_rgb(0, 0, v), v) for v in grays] + \
                [("red", colorsys.hsv_to_rgb(*hsv), hsv) for hsv in reds]
        step = size + 10
        per_row = max(1, int((br[0] - tl[0] - 20) // step))
        spots = [(tl[0] + 10 + (i % per_row) * step, tl[1] + 10 + (i // per_row) * step) for i in range(len(tests) + 1)]
        self.activate()
        if base.get("brush_min") and base.get("brush_max"):
            self.set_brush(base["brush_pct"])
        hexes = ["".join(f"{int(round(c * 255)):02x}" for c in rgb) for _, rgb, _ in tests]
        for hx, (x, y) in zip(hexes, spots):
            self.status(f"Color check: swatch #{hx}")
            self.set_color(hx)
            self._patch(x, y, size, b)
        # transparency test: mid gray painted over a black patch
        bx_, by_ = spots[-1]
        self.set_color("000000")
        self._patch(bx_, by_, size + 2 * b, b)
        self.set_color("808080")
        self._patch(bx_ + b, by_ + b, size, b)
        self.park()
        time.sleep(0.4)
        x0, y0 = min(x for x, _ in spots), min(y for _, y in spots)
        x1, y1 = max(x for x, _ in spots) + size + 2 * b, max(y for _, y in spots) + size + 2 * b
        cap = capture(x0, y0, x1 - x0, y1 - y0)
        self.cfg = base
        if cap is None:
            raise RuntimeError("Can't screenshot the canvas. Grant Screen Recording to the app running "
                               "this script (System Settings > Privacy & Security), restart it, retry.")
        img, sc = cap

        def sample(x, y, w):
            cx, cy, r = int((x - x0) * sc), int((y - y0) * sc), max(1, int(w * sc / 2))
            return np.median(img[cy - r:cy + r, cx - r:cx + r].reshape(-1, 3), axis=0)

        report, value_curve = [], []
        for (kind, rgb, want), hx, (x, y) in zip(tests, hexes, spots):
            got = sample(x + size / 2, y + size / 2, size / 3)
            gh, gs, gv = colorsys.rgb_to_hsv(*(got / 255))
            if kind == "gray":
                value_curve.append((want, round(gv, 3)))
            else:
                report.append(f"#{hx} came out #{''.join(f'{int(v):02x}' for v in got)} "
                              f"(brightness {want[2]:.0%} -> {gv:.0%}, saturation {want[1]:.0%} -> {gs:.0%})")
        over = sample(bx_ + b + size / 2, by_ + b + size / 2, size / 3)
        plain = sample(spots[grays.index(0.5)][0] + size / 2, spots[grays.index(0.5)][1] + size / 2, size / 3)
        de = float(np.sqrt(((_to_lab(over[None].astype(float)) - _to_lab(plain[None].astype(float))) ** 2).sum()))
        transparent = de > 10
        worst = max(abs(o - r) for r, o in value_curve)
        report.insert(0, "brightness: " + ", ".join(f"{r:.0%}->{o:.0%}" for r, o in value_curve))
        # see-through paint makes every swatch look lighter: brightness can't be measured then
        ok_curve = worst > 0.04 and not transparent
        return {"value_curve": value_curve if ok_curve else None, "transparent": transparent,
                "over_black_de": de, "report": report, "worst_value_error": worst}

    # ---- Auto speed: find the fastest timing the game still draws correctly ----

    def _pattern(self, ox, oy, b, length=150):
        """Test strokes for a slot at (ox, oy) with brush diameter b (pt): a serpentine with
        sharp corners (missed corner = diagonal streak across a gap), one long row drawn as
        a single move (no interpolation = gaps), and short dashes (start/end registration)."""
        gap = 2.6 * b
        x0, x1 = ox + b, ox + b + length
        ys = [oy + b + i * gap for i in range(4)]
        zig = []
        for i, y in enumerate(ys):
            zig += [(x0, y), (x1, y)] if i % 2 == 0 else [(x1, y), (x0, y)]
        y4 = ys[-1] + gap
        y5 = y4 + gap
        n = max(2, min(6, int(length // 30)))
        dashes = [[(x0 + k * 30, y5), (x0 + k * 30 + 12, y5)] for k in range(n)]
        return [zig, [(x0, y4), (x0 + length * 1.27, y4)]] + dashes

    def _check_pattern(self, strokes, ox, oy, w, h, b):
        """Screenshot the slot and compare with the expected ink. Returns (ok, coverage, stray px)."""
        cap = capture(ox, oy, w, h)
        if cap is None:
            return None
        img, sc = cap
        dark = img.mean(2) < 90
        exp = np.zeros(dark.shape, np.uint8)
        th = max(1, int(round(b * sc)))
        for pts in strokes:
            P = np.round((np.array(pts) - [ox, oy]) * sc * 16).astype(np.int32)
            cv2.polylines(exp, [P], False, 1, th, cv2.LINE_8, 4)
            for q in P:
                cv2.circle(exp, (int(q[0]), int(q[1])), int(b * sc / 2 * 16), 1, -1, cv2.LINE_8, 4)
        k1 = max(1, int(b * sc * 0.35)) | 1
        core = cv2.erode(exp, np.ones((k1, k1), np.uint8)).astype(bool)
        k2 = int((b * 0.5 + 4) * sc) * 2 + 1
        near = cv2.dilate(exp, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k2, k2))).astype(bool)
        coverage = dark[core].mean() if core.any() else 0.0
        stray = int((dark & ~near).sum())
        ok = coverage >= 0.97 and stray <= (b * sc) ** 2 * 1.5
        return ok, coverage, stray

    def _color_check(self, ox, oy, b):
        """Alternate red/blue dots right after each color change; True if every dot came out
        in the color asked for (a too-short wait leaves the previous color)."""
        seq = ["ff0000", "0000ff", "ff0000", "0000ff"]
        pts = [(ox + b + 40 * i, oy + b) for i in range(len(seq))]
        for hx, p in zip(seq, pts):
            self.set_color(hx)
            self.stroke([p])
        br = self.cfg["canvas_br"]
        self.park()
        time.sleep(0.35)
        cap = capture(ox, oy, 40 * len(seq) + 2 * b, 2 * b)
        if cap is None:
            return None
        img, sc = cap
        for hx, (x, y) in zip(seq, pts):
            cx, cy = int((x - ox) * sc), int((y - oy) * sc)
            r, g, bl = np.median(img[max(0, cy - 2):cy + 3, max(0, cx - 2):cx + 3].reshape(-1, 3), axis=0)
            if (hx == "ff0000" and not r > bl + 60) or (hx == "0000ff" and not bl > r + 60):
                return False
        return True

    def _wipe_slots(self, used, w, h, b, cfg):
        """Clear the used test spots so a small canvas can be reused: paint bucket if
        calibrated, else (or if ink is left) brush white over them. Checks with a screenshot;
        False if the ink still shows (see-through paint)."""
        tl, br = cfg["canvas_tl"], cfg["canvas_br"]
        x0 = max(tl[0] + 1, min(x for x, _ in used) - 3)
        y0 = max(tl[1] + 1, min(y for _, y in used) - 3)
        x1 = min(br[0] - 1, max(x for x, _ in used) + w + 3)
        y1 = min(br[1] - 1, max(y for _, y in used) + h + 3)
        saved = (self.cfg, self.frame, self.frame0, self.min_frame)
        self.cfg = dict(cfg, frame_ms=max(8, cfg["frame_ms"]), segment=min(cfg["segment"], 12.0),
                        corner_frames=max(2, cfg["corner_frames"]))
        self.frame = self.frame0 = self.min_frame = self.cfg["frame_ms"] / 1000.0
        self.t = time.perf_counter()
        clean = False

        def ink_left():
            """Leftover ink mask and screenshot scale, or None if the screenshot failed."""
            self.park()
            time.sleep(0.35)
            cap = capture(x0, y0, x1 - x0, y1 - y0)
            return None if cap is None else ((cap[0].astype(np.int16).min(2) < 200).astype(np.uint8), cap[1])

        def is_clean():
            r = ink_left()
            return None if r is None else r[0].mean() < 0.001

        try:
            if cfg.get("bucket_tool") and cfg.get("brush_tool"):
                # two clicks: black over the background joins it with the black test ink,
                # then white over all of it clears the whole canvas
                self.status("Auto speed - out of room: clearing the canvas with the paint bucket")
                corner = (tl[0] + 2.5, tl[1] + 2.5)  # tests start 5pt in: always background
                for hx in ("000000", "ffffff"):
                    self.set_color(hx)
                    self.bucket_fill(*corner)
                r = ink_left()
                if r is not None and r[0].mean() >= 0.001:
                    # islands the black didn't join (other colors): one white fill on each
                    n, comp, stats, cent = cv2.connectedComponentsWithStats(r[0], connectivity=8)
                    for i in sorted(range(1, n), key=lambda i: -stats[i, cv2.CC_STAT_AREA])[:40]:
                        ys, xs = np.nonzero(comp == i)
                        k = int(np.argmin((xs - cent[i][0]) ** 2 + (ys - cent[i][1]) ** 2))
                        self.bucket_fill(x0 + xs[k] / r[1], y0 + ys[k] / r[1])
                    r = ink_left()
                if r is not None and r[0].mean() < 0.001:
                    return True
            self.status("Auto speed - out of room: painting the tests over")
            self.set_color("ffffff")
            for k in range(3):
                rows = max(2, int(math.ceil((y1 - y0 - b) / (0.6 * b))) + 1)
                off = (k % 2) * 0.3 * b  # each extra pass between the previous rows
                for i in range(rows):
                    yy = min(y1 - b / 2, y0 + b / 2 + off + i * (y1 - y0 - b) / max(1, rows - 1))
                    a, c = (x0 + b / 2, x1 - b / 2) if i % 2 == 0 else (x1 - b / 2, x0 + b / 2)
                    self.stroke([(a, yy), (c, yy)])
                res = is_clean()
                if res is None:
                    break
                if res:
                    clean = True
                    break
        finally:
            self.set_color("000000")
            self.cfg, self.frame, self.frame0, self.min_frame = saved
            self.t = time.perf_counter()
        return clean

    def auto_speed(self):
        """Try ever faster timing on test patterns, keep the fastest that draws correctly.
        Returns (settings dict, log lines)."""
        base = dict(self.cfg)
        b = float(base.get("brush_pt") or base["spacing"] / 0.9)
        tl, br = base["canvas_tl"], base["canvas_br"]
        length = max(40.0, min(150.0, (br[0] - tl[0] - 20 - 2 * b) / 1.35))  # fit narrow canvases
        w, h = length * 1.33 + 2 * b, 16 * b
        slots = [(float(x), float(y)) for y in np.arange(tl[1] + 5, br[1] - h, h + 8)
                 for x in np.arange(tl[0] + 5, br[0] - w, w + 8)]
        if not slots:
            raise RuntimeError("Canvas too small for speed tests - calibrate the canvas corners again")
        log = []
        cur = {k: base[k] for k in ("frame_ms", "corner_frames", "segment", "color_wait_ms")}
        room = {"left": iter(slots), "used": [], "safe": dict(cur), "wipes": 0}

        def next_slot():
            """Next free test spot. Small canvas: when all are used, paint them white and reuse."""
            if room.get("full"):
                return None
            pos = next(room["left"], None)
            if pos is None and room["used"]:
                if room["wipes"] >= 20 or not self._wipe_slots(room["used"], w, h, b, dict(base, **room["safe"])):
                    room["full"] = True
                    log.append("out of canvas room (couldn't paint the tests over): stopped early - "
                               "clear the canvas and run again to test faster settings")
                    return None
                room["wipes"] += 1
                log.append("small canvas: painted the tests over to reuse the space")
                room["left"], room["used"] = iter(slots), []
                pos = next(room["left"], None)
            if pos is not None:
                room["used"].append(pos)
            return pos

        def trial(label, **chg):
            pos = next_slot()
            if pos is None:
                return None
            st = dict(cur, **chg)
            self.cfg = dict(base, **st)
            self.frame = self.frame0 = self.min_frame = st["frame_ms"] / 1000.0
            self.t = time.perf_counter()
            if "color_wait_ms" in chg:
                res = self._color_check(*pos, b)
                ok = res
                msg = "colors ok" if res else "color came out wrong"
            else:
                strokes = self._pattern(*pos, b, length)
                for pts in strokes:
                    self.stroke(pts)
                self.park()
                time.sleep(0.35)
                res = self._check_pattern(strokes, *pos, w, h, b)
                if res is None:
                    raise RuntimeError("Can't screenshot the canvas. Grant Screen Recording to the app running "
                                       "this script (System Settings > Privacy & Security), restart it, retry.")
                ok = res[0]
                msg = f"coverage {res[1]:.0%}, stray ink {res[2]}px"
            log.append(f"{label}: {'PASS' if ok else 'fail'} ({msg})")
            self.status(f"Auto speed - {label}: {'pass' if ok else 'fail'}")
            if ok:
                cur.update(chg)
                if "frame_ms" not in chg:  # wipes use settings that drew cleanly, at a calm speed
                    room["safe"].update(chg)
            return ok

        def trial2(label, **chg):
            """A fail only counts if it happens twice (one lag spike shouldn't stop the search)."""
            r = trial(label, **chg)
            if r is False:
                r = trial(label + " (retry)", **chg)
            return r

        self.activate()
        self.set_color("000000")
        if self.cfg.get("brush_min") and self.cfg.get("brush_max"):
            self.set_brush(base["brush_pct"])
        interpolates = True
        if not trial2(f"current settings ({cur['frame_ms']}ms)"):
            # maybe the game only paints where the mouse is sampled: try many points per row
            if not trial("points every brush width", segment=max(2.0, round(0.8 * b, 1))):
                raise RuntimeError("Even the current settings don't draw cleanly - check the canvas calibration "
                                   "and brush size (Measure brush), or raise Event delay.")
            interpolates = False
        if cur["corner_frames"] > 1:
            trial2("corner hold 1 frame", corner_frames=1)
        if interpolates and cur["segment"] < 9999:
            trial2("long rows as one move", segment=9999)
        steps = [f for f in (14, 12, 10, 8, 7, 6, 5, 4, 3) if f < cur["frame_ms"]]
        passed = [cur["frame_ms"]]
        for f in steps:
            if not trial2(f"event delay {f}ms", frame_ms=f):
                break
            passed.append(f)
        # confirm the fastest once more; if flaky, step back one level at a time
        while len(passed) > 1:
            f = passed[-1]
            if trial(f"event delay {f}ms (confirm)", frame_ms=f) is not False:
                break
            passed.pop()
            cur["frame_ms"] = passed[-1]
            log.append(f"{f}ms is flaky: backing off to {cur['frame_ms']}ms")
        self.set_color("000000")
        for cw in (150, 100, 60, 30):
            if cw < cur["color_wait_ms"]:
                if not trial2(f"color wait {cw}ms", color_wait_ms=cw):
                    break
        self.cfg = base
        self.frame = self.frame0 = base["frame_ms"] / 1000.0
        result = dict(cur)
        result["min_frame_ms"] = int(cur["frame_ms"])
        # normal drawing keeps a small safety margin over the fastest clean speed (never slower
        # than what already worked); the fastest is used only when running late
        if cur["frame_ms"] < base["frame_ms"]:
            result["frame_ms"] = int(min(base["frame_ms"], math.ceil(cur["frame_ms"] * 1.15)))
        else:
            result["frame_ms"] = int(base["frame_ms"])
        return result, log

    def speed_test(self):
        """Draw a zigzag + long lines in the canvas top-left with the current color and
        settings. Solid lines with sharp corners = settings are safe."""
        (cx0, cy0), (cx1, _) = self.cfg["canvas_tl"], self.cfg["canvas_br"]
        s = self.cfg["spacing"]
        L = (cx1 - cx0) * 0.45
        x0, y0 = cx0 + 2 * s, cy0 + 2 * s
        zig = []
        for i in range(8):
            y = y0 + i * s * 1.5
            a, b = (x0, x0 + L) if i % 2 == 0 else (x0 + L, x0)
            zig += [(a, y), (b, y)]
        self.stroke(zig)
        y = y0 + 8 * s * 1.5 + 3 * s
        for i in range(4):
            self.stroke([(x0, y + i * 3 * s), (x0 + L, y + i * 3 * s)])
        for i in range(6):
            self.stroke([(x0 + i * 3 * s, y + 12 * s)])
        self.status("Speed test drawn: check for gaps, rounded corners or joined dots")


# ---------- screenshot correction ----------

def target_for(img, cfg):
    """The color target the plan draws, placed on the full canvas grid (HS px per cell)."""
    args = list(plan_args(img, cfg))
    args[6] = args[8] = 0.0  # brushes don't change the target
    preview, _ = build_plan(*args, target_only=True, **plan_kw(cfg))
    gw, gh = args[1], args[2]
    P = np.asarray(preview)
    h, w = P.shape[0] // HS, P.shape[1] // HS
    ox, oy = (gw - w) // 2, (gh - h) // 2
    T = np.full((gh * HS, gw * HS, 3), 255, np.uint8)
    T[oy * HS:(oy + h) * HS, ox * HS:(ox + w) * HS] = P
    return T


def capture_canvas(cfg, shape):
    """Screenshot of the drawing grid area, resampled to the target's resolution."""
    tl, sp = cfg["canvas_tl"], cfg["spacing"]
    gh, gw = shape[0] // HS, shape[1] // HS
    cap = capture(tl[0], tl[1], gw * sp, gh * sp)
    if cap is None:
        return None
    img = np.clip(cap[0], 0, 255).astype(np.uint8)
    return cv2.resize(img, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)


def find_errors(target, shot):
    """Compare a canvas screenshot with the target. Each target color's real on-screen look
    is measured first (the picker and screenshots shift colors a little), then every pixel
    is classified to the nearest one. Returns (target labels, colors, observed colors, wrong mask)."""
    colors, Lt = np.unique(target.reshape(-1, 3), axis=0, return_inverse=True)
    Lt = Lt.reshape(target.shape[:2])
    ker = np.ones((7, 7), np.uint8)
    observed = colors.astype(np.float64).copy()
    for i in range(len(colors)):
        core = cv2.erode((Lt == i).astype(np.uint8), ker).astype(bool)
        if core.sum() >= 30:
            observed[i] = np.median(shot[core], axis=0)
    lab_obs = _to_lab(observed)
    lab_shot = _to_lab(shot.reshape(-1, 3).astype(np.float64))
    Ls = np.empty(len(lab_shot), np.int64)
    for a in range(0, len(lab_shot), 200000):  # chunked nearest-color
        Ls[a:a + 200000] = ((lab_shot[a:a + 200000, None] - lab_obs[None]) ** 2).sum(2).argmin(1)
    # wrong = nearest to another color AND clearly off from how this color really looks
    # (texture/noise and small shade differences aren't worth repainting)
    off = np.sqrt(((lab_shot - lab_obs[Lt.reshape(-1)]) ** 2).sum(1)).reshape(Lt.shape) > 20
    wrong = ((Ls.reshape(Lt.shape) != Lt) & off).astype(np.uint8)
    wrong = cv2.morphologyEx(wrong, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))  # drop edge seams
    n, comp, stats, _ = cv2.connectedComponentsWithStats(wrong, connectivity=8)
    keep = np.zeros(n, bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= 12
    return Lt, colors, observed, keep[comp]


def _paint_for_look(look, asked, seen):
    """Paint color that shows up as `look`, from how the painted colors came out (per-channel
    straight-line fit asked -> seen). Too few colors to fit: the look itself."""
    look = np.asarray(look, float)
    if len(asked) < 3:
        return look
    out = look.copy()
    for c in range(3):
        x, y = np.asarray(asked, float)[:, c], np.asarray(seen, float)[:, c]
        if np.ptp(x) < 60:
            continue
        a, b = np.polyfit(x, y, 1)
        if 0.5 < a < 1.5:
            out[c] = (look[c] - b) / a
    return np.clip(out, 0, 255)


def misaligned(target, shot, max_cells=2):
    """(dx, dy) cells if the screenshot lines up clearly better with the target shifted
    (canvas calibration off or the camera moved), else None."""
    small = lambda a: cv2.resize(a, (a.shape[1] // HS, a.shape[0] // HS), interpolation=cv2.INTER_AREA)
    T = _to_lab(small(target).reshape(-1, 3).astype(float)).reshape(target.shape[0] // HS, -1, 3)
    S = _to_lab(small(shot).reshape(-1, 3).astype(float)).reshape(T.shape)
    m = max_cells
    if T.shape[0] <= 4 * m or T.shape[1] <= 4 * m:
        return None
    core = (slice(m, -m), slice(m, -m))
    errs = {}
    for dy in range(-m, m + 1):
        for dx in range(-m, m + 1):
            Ss = S[m + dy:S.shape[0] - m + dy, m + dx:S.shape[1] - m + dx]
            errs[(dx, dy)] = np.sqrt(((T[core] - Ss) ** 2).sum(2)).mean()
    best = min(errs, key=errs.get)
    return best if best != (0, 0) and errs[best] < 0.7 * errs[(0, 0)] else None


def correction_steps(target, shot, tried=None):
    """Repaint steps for every wrongly colored area (fine brush, kept inside its own color).
    tried: fill-grid cells already repainted once (still wrong = finer than the brush; skipped).
    Returns (steps, fraction of the drawing that's wrong, cells repainted)."""
    Lt, colors, observed, wrong = find_errors(target, shot)
    gh, gw = Lt.shape[0] // HS, Lt.shape[1] // HS
    px = HS // SUB
    D = Lt[HS // 2::HS, px // 2::px][:gh, :gw * SUB]
    bad = wrong.reshape(gh, HS, gw * SUB, px).mean(axis=(1, 3)) > 0.5
    if tried is not None:
        bad &= ~tried
    is_bg = colors.min(1) >= 245
    drawn = ~is_bg[Lt]
    frac = wrong.sum() / max(1, (drawn | wrong).sum())
    steps = []
    order = np.argsort(-np.bincount(Lt.reshape(-1), minlength=len(colors)))
    lab_c, lab_o = _to_lab(colors.astype(np.float64)), _to_lab(observed)
    for i in order:
        if not is_bg[i] and np.sqrt(((lab_c[i] - lab_o[i]) ** 2).sum()) > 25:
            continue  # the game doesn't show this color right (e.g. dark shades turn black): repainting won't help
        T = (D == i) & bad
        if not T.any():
            continue
        # background spills: paint the canvas color. Its screenshot look is shifted by the
        # game's lighting and the screen, so undo the shift measured on the painted colors
        rgb = _paint_for_look(observed[i], colors[~is_bg], observed[~is_bg]) if is_bg[i] else colors[i]
        hx = "{:02x}{:02x}{:02x}".format(*[int(round(v)) for v in np.clip(rgb, 0, 255)])
        strokes = _fill_strokes(D == i, T)
        if strokes:
            steps.append((hx, "fine", [[((x + 0.5) / SUB - 0.5, y) for x, y in p] for p in strokes]))
    return steps, frac, bad


# ---------- fitting a drawing into a time budget ----------

TINY_MAX_RATIO = 0.6  # edge pass needs the tiny brush at most this fraction of the fine brush


def brush_tiers(cfg):
    """Area brushes used before the fine brush, largest first: [(tag, diameter_pt)].
    "big" is the configured big brush; with a measured slider table (Auto brush) a "huge"
    brush (the largest measured) and a "medium" one (~2.5x fine) are added when they're
    clearly different sizes."""
    slider = bool(cfg.get("brush_min") and cfg.get("brush_max"))
    if not slider:
        return []
    fine = cfg.get("brush_pt") or cfg["spacing"] / 0.9
    tiers = []
    big = cfg.get("big_brush_pt") if cfg.get("big_brush_pct", 0) > 0 else None
    curve = cfg.get("brush_curve") or []
    d_max = max((d for _, d in curve), default=0)
    if d_max >= 1.5 * (big or 0) and d_max >= 4 * fine:
        tiers.append(("huge", d_max))
    if big:
        tiers.append(("big", big))
    medium = 2.5 * fine
    if curve and medium <= d_max and medium * 1.6 <= (big or d_max) and medium * 1.6 <= (tiers[0][1] if tiers else 1e9):
        tiers.append(("medium", medium))
    return tiers


def has_arrows(cfg):
    return bool(cfg.get("brush_dec") and cfg.get("brush_inc"))


def arrow_clicks_for(cfg, d):
    """Arrow clicks giving the brush closest to d (pt), if d is within the measured arrow range."""
    ac = cfg.get("arrow_curve")
    if not (has_arrows(cfg) and ac and d):
        return None
    if d > max(dd for _, dd in ac) * 1.08:
        return None
    return min(ac, key=lambda nd: abs(nd[1] - d))


def curve_range(cfg, curve):
    """Smallest and largest measured brush (slider and arrows)."""
    ds = [d for _, d in curve] + [d for _, d in (cfg.get("arrow_curve") or [])] if has_arrows(cfg) else [d for _, d in curve]
    return min(ds), max(ds)


def brush_pcts(cfg):
    """Slider position for every brush tag (or ("clicks", n) when set with the arrows)."""
    pct = {"fine": cfg["brush_pct"], "big": cfg.get("big_brush_pct", 0), "tiny": cfg.get("tiny_brush_pct", 0)}
    if has_arrows(cfg):
        for tag, key in (("fine", "brush_clicks"), ("big", "big_brush_clicks"), ("tiny", "tiny_brush_clicks")):
            if cfg.get(key) is not None:
                pct[tag] = ("clicks", int(cfg[key]))
    pct["line"] = pct["tiny"] if line_brush_pt(cfg) != (cfg.get("brush_pt") or cfg["spacing"] / 0.9) else pct["fine"]
    curve = cfg.get("brush_curve")
    for tag, d in brush_tiers(cfg):
        if tag not in pct or not pct[tag]:
            pct[tag] = Drawer._pct_for(d, curve) if curve else pct["fine"]
    return pct


def brush_sizes(cfg):
    """Diameter (pt) for every brush tag (for previews)."""
    sp = cfg["spacing"]
    out = {"fine": cfg.get("brush_pt") or sp / 0.9, "big": cfg.get("big_brush_pt") or 0,
           "tiny": cfg.get("tiny_brush_pt") or 0}
    out.update(dict(brush_tiers(cfg)))
    out["line"] = line_brush_pt(cfg)
    return out


def plan_args(img, cfg):
    """build_plan arguments for cfg's canvas, brushes and image settings."""
    tl, br, sp = cfg["canvas_tl"], cfg["canvas_br"], cfg["spacing"]
    gw, gh = int((br[0] - tl[0]) / sp), int((br[1] - tl[1]) / sp)
    slider = bool(cfg.get("brush_min") and cfg.get("brush_max"))
    big_r = [(tag, d / 2 / sp) for tag, d in brush_tiers(cfg)]
    tiny_r = cfg["tiny_brush_pt"] / 2 / sp if slider and cfg["tiny_brush_pct"] > 0 and cfg.get("tiny_brush_pt") else 0.0
    if tiny_r and cfg["tiny_brush_pt"] > TINY_MAX_RATIO * (cfg.get("brush_pt") or sp / 0.9):
        tiny_r = 0.0  # edge pass only helps with a clearly finer pen
    return (img, gw, gh, cfg["colors"], cfg["skip_white"], cfg["min_blob"], big_r,
            cfg["denoise"], tiny_r, cfg["edge_contrast"], cfg["merge_de"], 0.002, cfg["smooth"])


def line_brush_pt(cfg):
    """Brush used for line art: the tiny brush if it's clearly finer than the fine one."""
    fine = cfg.get("brush_pt") or cfg["spacing"] / 0.9
    tiny = cfg.get("tiny_brush_pt")
    slider = bool(cfg.get("brush_min") and cfg.get("brush_max"))
    if slider and cfg.get("tiny_brush_pct", 0) > 0 and tiny and tiny <= TINY_MAX_RATIO * fine:
        return tiny
    return fine


def plan_kw(cfg):
    """Keyword options for build_plan from cfg."""
    return {"line_fill": bool(cfg.get("line_fill", True)),
            "line_art_r": line_brush_pt(cfg) / 2 / cfg["spacing"] if cfg.get("line_art", True) else 0.0,
            "line_contrast": float(cfg.get("line_contrast", 35)),
            "bucket": has_bucket(cfg),
            "predrawn": [(hx, d / 2 / cfg["spacing"], [[(x / cfg["spacing"] - 0.5, y / cfg["spacing"] - 0.5)
                                                       for x, y in p] for p in strokes])
                         for hx, d, strokes in cfg.get("_predrawn") or []] or None}


def has_bucket(cfg):
    return bool(cfg.get("use_bucket", True) and cfg.get("bucket_tool") and cfg.get("brush_tool"))


def plan_for(img, cfg):
    """Build the plan for cfg's canvas/brushes. Returns (plan, estimated seconds)."""
    _, plan = build_plan(*plan_args(img, cfg), **plan_kw(cfg))
    return plan, estimate_seconds(plan, cfg, cfg["spacing"])


class Pending:
    """A plan being made in the background: .ready is set once .result = (cfg, plan, desc)."""

    def __init__(self):
        self.ready, self.result = threading.Event(), None


class PlanStream:
    """Plan being built in a background thread. Iterating yields steps as soon as each is
    planned (drawing can start on the first color right away); .planned holds every step
    planned so far and .complete turns True when planning is finished."""

    def __init__(self, img, cfg):
        import queue
        self.q, self.planned, self.complete = queue.Queue(), [], False
        self._done = object()
        threading.Thread(target=self._work, args=(img, cfg), daemon=True).start()

    def _add(self, step):
        self.planned.append(step)
        self.q.put(step)

    def _work(self, img, cfg):
        try:
            build_plan(*plan_args(img, cfg), sink=self._add, **plan_kw(cfg))
        except Exception as e:  # surfaced to the drawer
            self.q.put(e)
        finally:
            self.complete = True
            self.q.put(self._done)

    def __iter__(self):
        while True:
            item = self.q.get()
            if item is self._done:
                return
            if isinstance(item, Exception):
                raise item
            yield item


def stream_plan(img, cfg):
    return PlanStream(img, cfg)


def cfg_with_sizes(cfg, curve, fine, big, tiny):
    """Copy of cfg using the given brush diameters (pt); slider positions from the curve
    (curve=None: placeholder positions, enough for time estimates)."""
    pct = (lambda d: Drawer._pct_for(d, curve)) if curve else (lambda d: 1.0)
    c = dict(cfg)
    # small sizes: exact arrow steps when available (the slider is coarse/unreliable down there)
    hit = arrow_clicks_for(c, fine)
    c["brush_clicks"] = hit[0] if hit else None
    fine = hit[1] if hit else fine
    c["brush_pt"], c["spacing"] = fine, round(fine * 0.9, 1)
    c["brush_pct"] = round(pct(fine), 1)
    for key, d in (("big", big), ("tiny", tiny)):
        hit = arrow_clicks_for(c, d) if d else None
        c[f"{key}_brush_clicks"] = hit[0] if hit else None
        d = hit[1] if hit else d
        c[f"{key}_brush_pt"] = d or None
        c[f"{key}_brush_pct"] = max(0.5, round(pct(d), 1)) if d else 0
    return c


FINE_CANDIDATES = sorted({round(float(d), 1) for d in np.geomspace(3, 30, 20)})
_POOL = None


def pool():
    """Process pool for planning several brush sizes at once (one per CPU core)."""
    global _POOL
    if _POOL is None:
        import concurrent.futures as cf
        import multiprocessing as mp
        _POOL = cf.ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1),
                                       mp_context=mp.get_context("spawn"))
    return _POOL


def _noop():
    return None


_SEARCH_TAG = threading.local()  # set in a thread whose searches can be dropped (Preview)
_TAGGED_FUTS = []


def search_submit(fn, *args):
    """pool().submit for planning searches; futures from a tagged thread are remembered so
    cancel_searches() can drop them when that search is no longer wanted."""
    f = pool().submit(fn, *args)
    tag = getattr(_SEARCH_TAG, "tag", None)
    if tag is not None:
        _TAGGED_FUTS.append((tag, f))
    return f


def cancel_searches(tag):
    """Cancel the queued (not yet running) jobs of searches started under `tag`, so a newer
    search doesn't wait behind ones nobody needs any more."""
    keep = []
    for t, f in _TAGGED_FUTS:
        if t == tag:
            f.cancel()
        elif not f.done():
            keep.append((t, f))
    _TAGGED_FUTS[:] = keep


def sizes_for(fine, d_min, d_max):
    """Brush set for a fine size: big ~5x fine (if the slider goes that big), tiny = smallest."""
    big = min(d_max * 0.9, 5 * fine)
    return fine, (big if big >= 3 * fine else None), (d_min if d_min <= TINY_MAX_RATIO * fine else None)


def prep_configs(cfg):
    """One config per distinct drawing grid the brush search can try (every fine-brush
    candidate on this canvas), for preparing pictures ahead of time."""
    curve = cfg.get("brush_curve")
    if curve and len(curve) >= 2:
        d_min, d_max = curve_range(cfg, sorted(curve))
        fines = [f for f in FINE_CANDIDATES if d_min <= f <= d_max] or [round(min(max(d_min, 3.0), d_max), 1)]
        cands = [cfg_with_sizes(cfg, None, *sizes_for(f, d_min, d_max)) for f in fines]
    else:
        cands = [cfg]
    out, seen = [], set()
    for c in cands:
        a = plan_args(Image.new("RGB", (1, 1)), c)
        if (a[1], a[2]) not in seen:
            seen.add((a[1], a[2]))
            out.append(c)
    return out


def _prep_job(path, cfg):
    """Prepare one picture for one grid (worker process). Single-threaded: it's background
    work, and OpenCV / numpy's math library would otherwise use every core."""
    import contextlib
    try:
        from threadpoolctl import threadpool_limits
        limit = threadpool_limits(1)
    except Exception:
        limit = contextlib.nullcontext()
    n = cv2.getNumThreads()
    cv2.setNumThreads(1)
    try:
        with limit:
            img = Image.open(path)
            img.load()
            a, kw = plan_args(img, cfg), plan_kw(cfg)
            picture_prep(img, a[1], a[2], a[3], a[4], a[5], a[7], a[10], a[11], a[12],
                         kw["line_art_r"] > 0, kw["line_contrast"])
    finally:
        cv2.setNumThreads(n)


def _estimate_job(img, cfg, sizes):
    """(sizes, plan, est) for one brush set; runs in a worker process."""
    plan, est = plan_for(img, cfg_with_sizes(cfg, None, *sizes))
    return sizes, plan, est


class PlanMeter:
    """How far the planning search has got: candidate plans finished out of expected. Phases
    announce how many plans they'll make; the bar reads done / max(expected, guess)."""
    def __init__(self):
        self.reset()

    def reset(self, guess=0):
        self.done, self.expected, self.guess, self.t0 = 0, 0, guess, time.time()

    def expect(self, n):
        self.expected += n
        return self.done + n  # the count this phase ends at

    def tick(self, n=1):
        self.done += n

    def settle(self, end):  # a phase finished early (cancelled the rest)
        if self.done < end:
            self.done = end

    def frac(self):
        tot = max(self.expected, self.guess, 1)
        return min(0.99, self.done / tot)

    def text(self):
        tot = max(self.expected, self.guess)
        if not tot:
            return ""
        f, el = self.frac(), time.time() - self.t0
        eta = f" - ~{el * (1 - f) / f:.0f}s left" if f > 0.05 and el > 1 else ""
        return f"{min(self.done, tot)}/{tot} plans{eta}"


PLAN_METER = PlanMeter()


def search_guess(cfg, tune):
    """Rough number of plans the Start search makes (for the planning bar)."""
    curve = cfg.get("brush_curve")
    n = 2
    if curve and len(curve) >= 2:
        d0, d1 = curve_range(cfg, sorted(curve))
        lo, hi = max(3.0, d0), max(3.0, min(d1, 30))
        n += len([f for f in FINE_CANDIDATES if lo <= f <= hi]) + 1
    return n + (19 if tune else 4)


def prefetch_estimates(img, cfg, d_min, d_max):
    """Start planning every candidate fine size in the background (guessed tiny/big sizes),
    coarsest (fastest) first. Returns {fine: future -> (sizes, plan, est)}."""
    return {f: search_submit(_estimate_job, img, cfg, sizes_for(f, d_min, d_max))
            for f in sorted(FINE_CANDIDATES, reverse=True)}


def _plan_job(img, cfg):
    return plan_for(img, cfg)


def edges_job(img, cfg, contrast):
    """Edge-pass steps alone at a given contrast threshold (runs in a worker process)."""
    c = dict(cfg, edge_contrast=contrast)
    _, steps = build_plan(*plan_args(img, c), edges_only=True, **plan_kw(c))
    return steps


def detail_levels(cfg):
    """Increasingly detailed variants of cfg (more colors, finer merging, more edges)."""
    c, m, e = int(cfg["colors"]), float(cfg["merge_de"]), float(cfg["edge_contrast"])
    return [("more colors", {"colors": min(32, c + 4)}),
            ("more colors + edges", {"colors": min(32, c + 4), "edge_contrast": max(8.0, e - 5)}),
            ("many more colors + edges", {"colors": min(32, c + 8), "merge_de": max(4.0, m - 3),
                                          "edge_contrast": max(8.0, e - 5)}),
            ("max detail", {"colors": min(32, c + 12), "merge_de": max(3.0, m - 4),
                            "edge_contrast": max(6.0, e - 8), "smooth": max(0.0, float(cfg["smooth"]) - 0.5)})]


def upgrade_detail(img, cfg, plan, est, budget, emit=lambda m: None, futs=None):
    """If the plan leaves lots of time unused, plan richer variants in parallel and return
    the most detailed one that still fits: (cfg, plan, est, description or None)."""
    if est > 0.8 * budget:
        return cfg, plan, est, None
    levels = detail_levels(cfg)
    emit(f"Plan needs ~{est:.0f}s of {budget:.0f}s: trying {len(levels)} more detailed versions...")
    futs = futs or [(desc, dict(cfg, **chg), search_submit(_plan_job, img, dict(cfg, **chg))) for desc, chg in levels]
    end = PLAN_METER.expect(len(futs))
    try:
        for n, (desc, c, f) in enumerate(reversed(futs)):  # most detailed first: stop at the first that fits
            p, e = f.result()
            PLAN_METER.tick()
            if e <= budget:
                for _, _, other in futs[:len(futs) - 1 - n]:
                    other.cancel()
                return c, p, e, desc
        return cfg, plan, est, None
    finally:
        PLAN_METER.settle(end)


def choose_sizes(img, cfg, curve, budget, emit=lambda m: None, prefetched=None):
    """Finest fine brush whose plan fits `budget` seconds; tiny = smallest brush; big ~5x fine.
    curve = [(slider_pct, diameter_pt)]. Candidates are planned in parallel (or taken from
    `prefetched` futures), then the pick is re-planned exactly.
    Returns (cfg_with_sizes, plan, est, sizes)."""
    curve = sorted(curve)
    d_min, d_max = curve_range(cfg, curve)
    # every size, the finest included, is planned in parallel (the finest alone can take ~30s)
    # finest option is the smallest brush itself (the edge pass then switches off: it needs a
    # clearly finer pen); time decides whether that or a bigger fine brush + edges wins
    lo_d, hi_d = max(3.0, d_min), max(3.0, min(d_max, 30))
    cands = [f for f in FINE_CANDIDATES if lo_d <= f <= hi_d] or [round(min(max(lo_d, 3.0), hi_d), 1)]
    finest = round(min(lo_d, hi_d), 1)
    if finest not in cands:
        cands = sorted(cands + [finest])
    import concurrent.futures as cf
    futs = dict(prefetched or {})
    for f in sorted(cands, reverse=True):  # coarsest first: fastest to plan
        if f not in futs:
            futs[f] = search_submit(_estimate_job, img, cfg, sizes_for(f, d_min, d_max))
    emit(f"Planning {len(cands)} brush sizes in parallel...")
    end = PLAN_METER.expect(len(cands) + 1)
    # time only grows as the brush shrinks: once a size is over budget, finer ones are skipped
    results, too_slow = {}, 0.0
    waiting = {futs[f]: f for f in cands}
    while waiting:
        done, _ = cf.wait(list(waiting), return_when=cf.FIRST_COMPLETED)
        for fut in done:
            f = waiting.pop(fut)
            results[f] = fut.result()
            PLAN_METER.tick()
            if results[f][2] > budget:
                too_slow = max(too_slow, f)
        for fut, f in list(waiting.items()):
            if f < too_slow:  # finer than a size that's already too slow
                fut.cancel()
                waiting.pop(fut)
                PLAN_METER.tick()
    for f in [g for g in futs if g not in cands or g < too_slow]:
        futs[f].cancel()
    fits = sorted(f for f, r in results.items() if r[2] <= budget)
    order = fits + sorted((f for f in results if f not in fits), reverse=True)
    for f in order[:3]:  # use the pick if its sizes match reality, else re-plan it exactly
        sz = sizes_for(f, d_min, d_max)
        guess_sz, plan, e = results[f]
        same = all((a is None) == (b is None) and (a is None or abs(a - b) <= 0.03 * b)
                   for a, b in zip(guess_sz, sz))
        c = cfg_with_sizes(cfg, curve, *sz)
        if not same:
            plan, e = plan_for(img, c)
        emit(f"Planning: fine brush {f:.1f}pt -> ~{e:.0f}s")
        if e <= budget:
            break
    PLAN_METER.settle(end)
    return c, plan, e, sz


# ---------- auto settings: pick what looks best within the time limit ----------

def render_plan(plan, cfg, shape):
    """Draw the plan offline (round brushes at their real sizes) onto a canvas of the target's
    shape (HS px per cell) - what the game should end up showing."""
    sizes = brush_sizes(cfg)
    sp = cfg["spacing"]
    out = np.full(shape, 255, np.uint8)
    pre = [(hx, "bucket" if d <= 0 else ("pre", d), [[(x / sp - 0.5, y / sp - 0.5) for x, y in p] for p in st])
           for hx, d, st in cfg.get("_predrawn") or []]  # background drawn before this plan
    for hx, tag, strokes in pre + list(plan):
        col = tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4))
        if tag == "bucket":
            out[:] = col
            continue
        if isinstance(tag, tuple):
            sizes[tag] = tag[1]
        r = sizes.get(tag, sp / 0.9) / sp * HS / 2
        for pts in strokes:
            P = np.array([[(x + 0.5) * HS * 16, (y + 0.5) * HS * 16] for x, y in pts], np.int32)
            if len(P) > 1:
                cv2.polylines(out, [P], False, col, max(1, int(round(2 * r))), cv2.LINE_8, 4)
            for q in (P[[0, -1]] if len(P) > 1 else P):
                cv2.circle(out, (int(q[0]), int(q[1])), max(1, int(r * 16)), col, -1, cv2.LINE_8, 4)
    return out


def source_on_canvas(img, cfg, shape):
    """The original picture, placed and scaled on the canvas like the plan places it."""
    im = ImageOps.exif_transpose(img).convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    bg.alpha_composite(im)
    im = bg.convert("RGB")
    gh, gw = shape[0] // HS, shape[1] // HS
    sc = min(gw / im.width, gh / im.height)
    w, h = max(1, min(gw, round(im.width * sc))), max(1, min(gh, round(im.height * sc)))
    src = np.full(shape, 255, np.uint8)
    ox, oy = (gw - w) // 2, (gh - h) // 2
    src[oy * HS:(oy + h) * HS, ox * HS:(ox + w) * HS] = np.asarray(im.resize((w * HS, h * HS), Image.LANCZOS))
    return src


def _quality_job(img, cfg):
    """(look error, predicted seconds) for one candidate: average color difference between
    the rendered drawing and the original (lower = looks more like the picture)."""
    plan, est = plan_for(img, cfg)
    tl, br, sp = cfg["canvas_tl"], cfg["canvas_br"], cfg["spacing"]
    shape = (int((br[1] - tl[1]) / sp) * HS, int((br[0] - tl[0]) / sp) * HS, 3)
    # compare inside the picture only (the bucket also fills the empty margins beside it)
    gh, gw = shape[0] // HS, shape[1] // HS
    iw, ih = ImageOps.exif_transpose(img).size
    sc = min(gw / iw, gh / ih)
    w, h = max(1, min(gw, round(iw * sc))), max(1, min(gh, round(ih * sc)))
    ox, oy = (gw - w) // 2, (gh - h) // 2
    sl = (slice(oy * HS, (oy + h) * HS), slice(ox * HS, (ox + w) * HS))
    a = render_plan(plan, cfg, shape)[sl]
    b = source_on_canvas(img, cfg, shape)[sl]
    a = cv2.resize(a, (a.shape[1] // 2, a.shape[0] // 2), interpolation=cv2.INTER_AREA)
    b = cv2.resize(b, (b.shape[1] // 2, b.shape[0] // 2), interpolation=cv2.INTER_AREA)
    la = _to_lab(a.reshape(-1, 3).astype(np.float64))
    lb = _to_lab(b.reshape(-1, 3).astype(np.float64))
    return float(np.sqrt(((la - lb) ** 2).sum(1)).mean()), est


AUTO_SETTINGS = [  # setting, values to try (in this order, one setting at a time)
    ("colors", [8, 12, 16, 20, 24, 32]),
    ("smooth", [0.0, 0.5, 1.0, 1.5, 2.0]),
    ("merge_de", [3.0, 5.0, 8.0, 12.0]),
    ("denoise", [0, 3, 5]),
    ("line_art", [True, False]),
    ("edge_contrast", [8.0, 12.0, 15.0, 20.0, 30.0]),
]


def auto_settings(img, cfg, budget, emit=lambda m: None):
    """Pick brush sizes (from the measured slider) and image settings that make the drawing
    look most like the picture while fitting `budget` seconds. Each setting's values are
    rendered and scored in parallel. Returns (best cfg, error, seconds, changes)."""
    c = dict(cfg)
    if c.get("brush_curve") and c.get("brush_min") and c.get("brush_max"):
        emit("Auto settings: picking brush sizes...")
        c, _, _, _ = choose_sizes(img, c, c["brush_curve"], budget, emit)
    err, est = _quality_job(img, c)
    emit(f"Auto settings: start - look error {err:.2f}, ~{est:.0f}s")
    changes = []
    tiny_on = plan_args(img, c)[8] > 0  # an edge pass is possible with these brushes
    for key, values in AUTO_SETTINGS:
        if key == "edge_contrast" and not tiny_on:
            continue  # no edge pass with these brushes
        tries = [(v, search_submit(_quality_job, img, dict(c, **{key: v}))) for v in values if v != c.get(key)]
        best = (err, est, c.get(key))
        for v, f in tries:
            e2, t2 = f.result()
            if t2 <= budget and e2 < best[0] - 0.02:
                best = (e2, t2, v)
        if best[2] != c.get(key):
            changes.append(f"{key} {c.get(key)} -> {best[2]}")
            c[key] = best[2]
            err, est = best[0], best[1]
        emit(f"Auto settings: {key} = {c.get(key)} (look error {err:.2f}, ~{est:.0f}s)")
    return c, err, est, changes


def auto_tune(img, cfg, budget, emit=lambda m: None):
    """Quick per-picture tuning for Start: brush sizes from the measured slider, then 18
    combinations of colors / shape smoothing / line art scored in parallel by how much the
    rendered drawing looks like the picture. Returns (cfg, plan, seconds, description)."""
    c = dict(cfg)
    curve = c.get("brush_curve")
    if curve and len(curve) >= 2 and ((c.get("brush_min") and c.get("brush_max")) or has_arrows(c)):
        emit("Auto-tune: picking brush sizes...")
        c, _, _, _ = choose_sizes(img, c, curve, budget, emit)
    combos = [dict(c, colors=n, smooth=sm, line_art=la)
              for n in (12, 20, 28) for sm in (0.5, 1.0, 1.5) for la in (True, False)]
    emit(f"Auto-tune: trying {len(combos)} setting combinations...")
    futs = [(cc, search_submit(_quality_job, img, cc)) for cc in combos]
    end = PLAN_METER.expect(len(futs) + 1)
    scored = []
    for cc, f in futs:
        try:
            err, est = f.result()
            scored.append((err, est, cc))
        except Exception:
            pass
        PLAN_METER.tick()
    fits = [t for t in scored if t[1] <= budget]
    err, est, best = min(fits, key=lambda t: t[0]) if fits else min(scored, key=lambda t: t[1])
    plan, est = plan_for(img, best)
    PLAN_METER.settle(end)
    if est > budget:  # nothing fit: simplify the best-looking pick until it does
        emit("Auto-tune: no combination fits the time, simplifying...")
        best, plan, est, d = fit_to_time(img, best, budget, emit)
        return best, plan, est, f"auto-tuned + {d}"
    desc = (f"auto-tuned: {best['colors']} colors, smoothing {best['smooth']}, "
            f"line art {'on' if best['line_art'] else 'off'}, fine {best.get('brush_pt') or best['spacing'] / 0.9:.1f}pt")
    return best, plan, est, desc


_FIT_CACHE = {}


def fit_to_time(img, cfg, budget, emit=lambda m: None):
    """Cached wrapper: Preview and Start on the same picture/settings reuse the result."""
    import hashlib
    keys = [k for k in sorted(cfg) if k not in ("time_limit",)]
    key = (hashlib.md5(img.tobytes()).hexdigest(), img.size, round(budget, 0),
           json.dumps({k: cfg[k] for k in keys}, sort_keys=True, default=str))
    if key in _FIT_CACHE:
        emit("Using the plan from Preview")
        return _FIT_CACHE[key]
    res = _fit_to_time(img, cfg, budget, emit)
    _FIT_CACHE.clear()
    _FIT_CACHE[key] = res
    return res


# ---- pre-made plans: plan a picture ahead of a round, Start then draws it with no planning ----

PLAN_DIR = os.path.join(LIB_DIR, ".plans")
# settings that don't change the plan itself: a stored plan stays valid when only these change
PLAN_FREE_KEYS = {"time_limit", "time_factor", "timing_model", "game_rate", "correct_passes", "autotune",
                  "auto_settings_for", "timer_tl", "timer_br", "focus_spot", "key_ms"}


def plan_settings_key(cfg):
    """Everything a plan depends on (brushes, colors, canvas, image and timing settings)."""
    import hashlib
    keep = {k: cfg[k] for k in sorted(cfg) if k not in PLAN_FREE_KEYS and not k.startswith("_")}
    return hashlib.md5(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()


def picture_id(img):
    import hashlib
    rgb = img.convert("RGB")
    return hashlib.md5(rgb.tobytes() + repr(rgb.size).encode()).hexdigest()


def _plan_file(img):
    return os.path.join(PLAN_DIR, picture_id(img) + ".pkl")


def preplan_index():
    """{picture id: [{"key", "window", "tune", "est"}]} - what's stored, without loading plans."""
    try:
        with open(os.path.join(PLAN_DIR, "index.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _index_set(pid, entries):
    idx = preplan_index()
    if entries:
        idx[pid] = [{k: e[k] for k in ("key", "window", "tune", "est")} for e in entries]
    else:
        idx.pop(pid, None)
    try:
        os.makedirs(PLAN_DIR, exist_ok=True)
        with open(os.path.join(PLAN_DIR, "index.json"), "w") as f:
            json.dump(idx, f)
    except OSError:
        pass


def load_preplans(img, cfg):
    """Stored plans for this picture still valid for cfg: [{"window", "tune", "est", "result"}].
    Plans made with other settings are dropped from the file."""
    import pickle
    path = _plan_file(img)
    try:
        with open(path, "rb") as f:
            entries = pickle.load(f)
    except (OSError, EOFError, pickle.UnpicklingError, AttributeError, ImportError):
        return []
    key = plan_settings_key(cfg)
    good = [e for e in entries if e.get("key") == key]
    if len(good) != len(entries):
        try:
            if good:
                with open(path, "wb") as f:
                    pickle.dump(good, f)
            else:
                os.remove(path)
        except OSError:
            pass
        _index_set(os.path.basename(path)[:-4], good)
    return good


def save_preplan(img, cfg, window, tune, result):
    """Keep one plan per (window, auto-tune) for this picture."""
    import pickle
    os.makedirs(PLAN_DIR, exist_ok=True)
    entries = [e for e in load_preplans(img, cfg) if not (e["window"] == window and e["tune"] == tune)]
    entries.append({"key": plan_settings_key(cfg), "window": window, "tune": tune, "est": result[2],
                    "result": result, "made": time.time()})
    with open(_plan_file(img), "wb") as f:
        pickle.dump(entries, f)
    _index_set(picture_id(img), entries)


def pick_preplan(img, cfg, tune, avail):
    """The most detailed stored plan that fits `avail` drawing seconds: (cfg, plan, est, desc) or None."""
    fits = [e for e in load_preplans(img, cfg) if e["tune"] == tune and e["est"] <= avail]
    if not fits:
        return None
    c, plan, est, desc = max(fits, key=lambda e: e["est"])["result"]
    c = dict(c, **{k: cfg[k] for k in PLAN_FREE_KEYS if k in cfg})  # current timer/calibration extras
    return c, plan, est, desc


def _fit_to_time(img, cfg, budget, emit=lambda m: None):
    """Settings + plan that finish within `budget` seconds. Uses the measured brush curve to
    pick brush sizes when available (more time = finer brushes); otherwise trims the edge pass
    and colors. Returns (cfg, plan, est, description)."""
    curve = cfg.get("brush_curve")
    if cfg.get("brush_min") and cfg.get("brush_max") and curve and len(curve) >= 2:
        c, plan, est, (fine, big, tiny) = choose_sizes(img, cfg, curve, budget, emit)
        desc = f"fine {fine:.1f}pt" + (f", big {big:.0f}pt" if big else "") + (f", tiny {tiny:.1f}pt" if tiny else "")
        if est <= budget:
            c, plan, est, more = upgrade_detail(img, c, plan, est, budget, emit)
            return c, plan, est, desc + (f", {more}" if more else "")
        cfg = c  # even the biggest brush is too slow: also trim edges/colors below
        prefix = desc + ", "
    else:
        prefix = ""
    steps = [("current settings", {}),
             ("fewer edges", {"edge_contrast": max(25, cfg["edge_contrast"])}),
             ("no edge pass", {"edge_contrast": max(25, cfg["edge_contrast"]), "tiny_brush_pct": 0}),
             ("fewer colors", {"tiny_brush_pct": 0, "colors": max(6, int(cfg["colors"] * 0.7))}),
             ("half the colors", {"tiny_brush_pct": 0, "colors": max(4, int(cfg["colors"] * 0.5))})]
    for desc, change in steps[1:] if prefix else steps:
        c = dict(cfg, **change)
        PLAN_METER.expect(1)
        plan, est = plan_for(img, c)
        PLAN_METER.tick()
        emit(f"Planning ({desc}): ~{est:.0f}s")
        if est <= budget:
            if not change:  # current settings fit: use spare time for more detail
                c, plan, est, more = upgrade_detail(img, c, plan, est, budget, emit)
                desc = more or desc
            break
    return c, plan, est, prefix + desc


# ---------- GUI (Qt) ----------

def _mmss(sec):
    sec = max(0, int(round(sec)))
    return f"{sec // 60}:{sec % 60:02d}"


class ProgressPanel(QWidget):
    """Overall bar (time spent, time left, round end) above one bar per stage. Finished stages
    fill green, the current one is highlighted (with a swatch of the color being drawn),
    skipped ones go gray. The time left counts down between updates."""
    STAGES = [("plan", "Planning"), ("countdown", "Countdown"), ("fill", "Filling colors"),
              ("edges", "Sharpening edges"), ("lines", "Line art"), ("correct", "Correcting")]
    STYLE = {"wait": "#d4d4d4", "cur": "#3b82f6", "busy": "#60a5fa", "done": "#22a35a", "skip": "#a3a3a3",
             "stop": "#d97706", "late": "#dc2626"}

    @staticmethod
    def _bar_css(color, h):
        return (f"QProgressBar {{ border: none; border-radius: {h // 2}px; background: #e5e7eb; "
                f"color: #111; font-size: 11px; font-weight: 600; }}"
                f"QProgressBar::chunk {{ background: {color}; border-radius: {h // 2}px; }}")

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 4, 0, 4)
        v.setSpacing(4)
        head = QHBoxLayout()
        self.ov_title = QLabel("Drawing")
        f = self.ov_title.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 1)
        self.ov_title.setFont(f)
        head.addWidget(self.ov_title)
        head.addStretch(1)
        self.ov_left = QLabel("")
        self.ov_left.setFont(f)
        head.addWidget(self.ov_left)
        v.addLayout(head)
        self.ov_bar = QProgressBar()
        self.ov_bar.setRange(0, 1000)
        self.ov_bar.setFixedHeight(20)
        self.ov_bar.setFormat("%p%")
        self.ov_bar.setAlignment(Qt.AlignCenter)
        self.ov_bar.setStyleSheet(self._bar_css(self.STYLE["cur"], 20))
        v.addWidget(self.ov_bar)
        self.ov_info = QLabel("")
        self.ov_info.setStyleSheet("color: #555; font-size: 11px;")
        v.addWidget(self.ov_info)
        grid = QGridLayout()
        grid.setContentsMargins(0, 2, 0, 0)
        grid.setVerticalSpacing(3)
        grid.setHorizontalSpacing(6)
        self.rows, self.order, self.state = {}, [k for k, _ in self.STAGES], {}
        for r, (key, name) in enumerate(self.STAGES):
            lbl = QLabel(name)
            lbl.setMinimumWidth(lbl.fontMetrics().horizontalAdvance("\u25b6 Sharpening edges") + 8)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setFixedHeight(10)
            bar.setMinimumWidth(90)
            bar.setTextVisible(False)
            sw = QLabel()
            sw.setFixedSize(12, 12)
            info = QLabel("")
            info.setStyleSheet("color: #666; font-size: 11px;")
            info.setFixedWidth(190)
            grid.addWidget(lbl, r, 0)
            grid.addWidget(bar, r, 1)
            grid.addWidget(sw, r, 2)
            grid.addWidget(info, r, 3)
            self.rows[key] = (lbl, bar, sw, info)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)
        self.ov = None  # (time of update, seconds left, seconds to round end, seconds spent)
        self.t_start = None
        self.tick = QTimer(self)
        self.tick.timeout.connect(self._tick)
        self.begin(())
        self.setVisible(False)

    def _paint(self, key, state):
        lbl, bar, sw, info = self.rows[key]
        if self.state.get(key) == state and getattr(bar, "_styled", None) == state:
            return  # restyling is expensive; skip when unchanged
        bar._styled = state
        self.state[key] = state
        bar.setStyleSheet(self._bar_css(self.STYLE[state], 10))
        lbl.setText(("▶ " if state in ("cur", "busy") else "✓ " if state == "done" else "")
                    + dict(self.STAGES)[key])
        lbl.setStyleSheet("color: #888;" if state in ("skip", "wait") else "")
        f = lbl.font()
        f.setBold(state in ("cur", "busy"))
        lbl.setFont(f)
        if state != "cur":
            sw.setStyleSheet("")
            sw._hx = None

    def begin(self, stages):
        self.setVisible(bool(stages))
        for key in self.order:
            vis = key in stages
            for wdg in self.rows[key]:
                wdg.setVisible(vis)
            _, bar, _, info = self.rows[key]
            bar.setRange(0, 1000)
            bar.setValue(0)
            info.setText("waiting" if vis else "")
            self._paint(key, "wait")
        self.ov, self.t_start, self.wait_txt = None, time.time(), ""
        self.ov_title.setText("Getting ready")
        self.ov_left.setText("")
        self.ov_bar.setRange(0, 1000)
        self.ov_bar.setValue(0)
        self.ov_bar.setStyleSheet(self._bar_css(self.STYLE["cur"], 20))
        self.ov_info.setText("")
        if stages:
            self.tick.start(500)

    def _tick(self):
        """Count down between drawer updates so the time left never looks frozen."""
        if self.ov is None:
            if self.t_start:
                since = f"{_mmss(time.time() - self.t_start)} since Start"
                self.ov_info.setText(f"{self.wait_txt}  \u00b7  {since}" if self.wait_txt else since)
            return
        t, rem, dl, el = self.ov
        now = time.time() - t
        rem, el = max(0.0, rem - now), el + now
        self.ov_left.setText(f"~{_mmss(rem)} left")
        parts = [f"{_mmss(el)} drawing"]
        if dl >= 0:
            dl = max(0.0, dl - now)
            parts.append(f"round ends in {_mmss(dl)}")
            spare = dl - rem
            parts.append(f"{spare:.0f}s to spare" if spare >= 0 else f"{-spare:.0f}s short: simplifying")
            late = spare < 0
            if getattr(self.ov_bar, "_late", None) != late:
                self.ov_bar._late = late
                self.ov_bar.setStyleSheet(self._bar_css(self.STYLE["late" if late else "cur"], 20))
        self.ov_info.setText("  ·  ".join(parts))

    def update_stage(self, key, frac, text, hx):
        if key == "end":
            return self.finish(text)
        if key == "overall":
            if frac < 0:
                self.ov = None
                self.ov_left.setText("")
                self.ov_title.setText("Waiting for the plan")
                self.wait_txt = text
                return self._tick()
            rem, dl, el = (float(x) for x in text.split("|"))
            self.ov = (time.time(), rem, dl, el)
            self.ov_title.setText("Drawing")
            self.ov_bar.setValue(int(min(1.0, frac) * 1000))
            return self._tick()
        if key not in self.rows or not self.rows[key][1].isVisible():
            return
        lbl, bar, sw, info = self.rows[key]
        if key == "plan" and 0 <= frac < 1 and self.state.get("plan") == "done":
            return  # a late meter tick after planning finished
        if key == "plan" and self.ov is None and 0 <= frac < 1:
            if self.ov_title.text() != "Waiting for the plan":
                self.ov_title.setText("Planning")
            self.ov_bar.setValue(int(frac * 1000))
            self.wait_txt = f"planning {text}" if text else ""
            self.ov_left.setText(text.split("~")[-1].replace(" left", "") + " left" if "~" in text else "")
        if frac == -2:  # skipped
            bar.setRange(0, 1000)
            bar.setValue(1000)
            info.setText(text or "skipped")
            return self._paint(key, "skip")
        # everything above the current stage is finished (background work like planning
        # keeps running until it reports in)
        if frac >= 0 and key != "plan":
            for k in self.order[:self.order.index(key)]:
                if k != "plan" and self.state.get(k) in ("wait", "cur") and self.rows[k][1].isVisible():
                    b = self.rows[k][1]
                    b.setRange(0, 1000)
                    b.setValue(1000)
                    self.rows[k][3].setText("done")
                    self._paint(k, "done")
        if frac >= 1.0:
            bar.setRange(0, 1000)
            bar.setValue(1000)
            info.setText(text or "done")
            return self._paint(key, "done")
        if frac < 0:  # busy in the background, length unknown
            bar.setRange(0, 0)
            info.setText(text)
            return self._paint(key, "busy")
        bar.setRange(0, 1000)
        bar.setValue(int(frac * 1000))
        info.setText(text)
        self._paint(key, "cur")
        if hx and getattr(sw, "_hx", None) != hx:
            sw._hx = hx
            sw.setStyleSheet(f"background: #{hx}; border: 1px solid #555; border-radius: 2px;")

    def finish(self, reason):
        self.tick.stop()
        took = _mmss(time.time() - self.t_start) if self.t_start else ""
        self.ov = None
        self.ov_left.setText("")
        if reason == "done":
            self.ov_title.setText("Done")
            self.ov_bar.setValue(1000)
            self.ov_bar.setStyleSheet(self._bar_css(self.STYLE["done"], 20))
            self.ov_info.setText(f"finished in {took}")
        else:
            self.ov_title.setText("Stopped")
            self.ov_bar.setStyleSheet(self._bar_css(self.STYLE["stop"], 20))
            self.ov_info.setText(f"{reason} after {took}")
        for key in self.order:
            if not self.rows[key][1].isVisible() or self.state.get(key) in ("done", "skip"):
                continue
            _, bar, _, info = self.rows[key]
            started = self.state.get(key) in ("cur", "busy")
            bar.setRange(0, 1000)
            if reason == "done":
                bar.setValue(1000)
                info.setText("done")
                self._paint(key, "done")
            else:
                info.setText(reason if started else "not reached")
                self._paint(key, "stop" if started else "skip")


class Bridge(QObject):
    status = Signal(str)
    finished = Signal()
    measured = Signal(list)
    speed_done = Signal(dict, list)
    color_done = Signal(dict)
    timing = Signal(dict)
    settings_done = Signal(dict)
    progress = Signal(str, float, str, str)
    auto_done = Signal(dict)
    preview_done = Signal(int, object)
    lib_scored = Signal(str, object)
    lib_planned = Signal(str, object)


class ClickCatcher(QObject):
    """Waits for the next mouse click outside our window and reports where it was. The click
    is swallowed (an event tap; needs Accessibility) so it doesn't draw on the canvas or press
    game buttons; Esc is reported as `skipped`. Without the tap it falls back to watching the
    mouse button (the click then also reaches the game)."""
    clicked = Signal(float, float)
    skipped = Signal()

    def __init__(self, own_rect):
        super().__init__()
        self.own_rect = own_rect  # () -> (x, y, w, h) of our window, clicks there are ignored
        self.active, self._loop, self._swallow_up = False, None, False
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_tick)

    def _inside_own(self, x, y):
        rx, ry, rw, rh = self._rect
        return rx <= x < rx + rw and ry <= y < ry + rh

    def start(self):
        self._rect = self.own_rect()
        if self.active:
            return
        self.active = True
        ok = threading.Event()
        self._tap_ok = False
        threading.Thread(target=self._tap_thread, args=(ok,), daemon=True).start()
        ok.wait(1.0)
        if not self._tap_ok:  # no event tap: poll the button (the click goes through)
            self._was_down = True
            self._esc_was = True
            self._poll.start(15)

    def stop(self):
        self.active = False
        self._poll.stop()
        if self._loop is not None:
            Quartz.CFRunLoopStop(self._loop)
            self._loop = None

    def _poll_tick(self):
        down = bool(Quartz.CGEventSourceButtonState(Quartz.kCGEventSourceStateCombinedSessionState,
                                                    Quartz.kCGMouseButtonLeft))
        esc = stop_key_down()
        if down and not self._was_down:
            x, y = mouse_pos()
            if not self._inside_own(x, y):
                self.clicked.emit(x, y)
        if esc and not self._esc_was:
            self.skipped.emit()
        self._was_down, self._esc_was = down, esc

    def _tap_thread(self, ok):
        Q = Quartz
        mask = (Q.CGEventMaskBit(Q.kCGEventLeftMouseDown) | Q.CGEventMaskBit(Q.kCGEventLeftMouseUp)
                | Q.CGEventMaskBit(Q.kCGEventKeyDown))
        holder = {}

        def cb(proxy, etype, event, refcon):
            if etype in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
                if holder.get("tap") is not None:
                    Q.CGEventTapEnable(holder["tap"], True)
                return event
            if not self.active:
                return event
            if etype == Q.kCGEventLeftMouseDown:
                x, y = Q.CGEventGetLocation(event)
                if self._inside_own(x, y):
                    return event
                self._swallow_up = True
                self.clicked.emit(float(x), float(y))
                return None
            if etype == Q.kCGEventLeftMouseUp and self._swallow_up:
                self._swallow_up = False
                return None
            if etype == Q.kCGEventKeyDown and \
                    Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventKeycode) == ESC_KEYCODE:
                self.skipped.emit()
                return None
            return event

        tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap, Q.kCGEventTapOptionDefault,
                                 mask, cb, None)
        if tap is None:
            ok.set()
            return
        holder["tap"] = tap
        src = Q.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._loop = Q.CFRunLoopGetCurrent()
        Q.CFRunLoopAddSource(self._loop, src, Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(tap, True)
        self._tap_ok = True
        ok.set()
        Q.CFRunLoopRun()
        Q.CGEventTapEnable(tap, False)


class App(QWidget):
    CAL = [("canvas_tl", "Canvas top-left", "all"),
           ("canvas_br", "Canvas bottom-right", "all"),
           ("wheel_center", "Color wheel center", "wheel"),
           ("wheel_edge", "Color wheel outer rim", "wheel"),
           ("value_top", "Brightness slider top (white)", "wheel"),
           ("value_bottom", "Brightness slider bottom (black)", "wheel"),
           ("hex_box", "Hex color text box", "hex"),
           ("ring_center", "Hue ring center", "ring"),
           ("ring_point", "Hue ring: middle of the colored band", "ring"),
           ("square_tl", "Color square top-left corner", "ring"),
           ("square_br", "Color square bottom-right corner", "ring"),
           ("timer_tl", "Timer box top-left (optional)", "all"),
           ("timer_br", "Timer box bottom-right (optional)", "all"),
           ("brush_min", "Brush slider left end (optional)", "all"),
           ("brush_max", "Brush slider right end (optional)", "all"),
           ("bucket_tool", "Paint bucket tool (optional)", "all"),
           ("brush_tool", "Brush / pen tool (optional, with bucket)", "all"),
           ("brush_dec", "Brush size - arrow (optional)", "all"),
           ("brush_inc", "Brush size + arrow (optional)", "all"),
           ("focus_spot", "Safe spot in the Roblox window (optional: skips the 5s countdown)", "all")]
    FIELDS = [("spacing", "Line spacing (pt) - use Measure brush", float),
              ("brush_pct", "Fine brush slider position (0-100)", float),
              ("big_brush_pct", "Big brush slider position (0 = off)", float),
              ("tiny_brush_pct", "Tiny edge brush slider position (0 = off)", float),
              ("edge_contrast", "Edge pass: min color difference (10-30)", float),
              ("merge_de", "Merge colors closer than (0 = off, ~8)", float),
              ("line_contrast", "Line art: min contrast (lower = more lines)", float),
              ("smooth", "Simplify shapes (cells, 0 = off, ~1.5)", float),
              ("time_limit", "Drawing time limit (s)", float),
              ("correct_passes", "Screenshot correction passes (0 = off)", int),
              ("corner_frames", "Hold each corner (frames)", int),
              ("min_frame_ms", "Fastest event delay when late (ms)", int),
              ("colors", "Max colors", int),
              ("frame_ms", "Event delay (ms)", int),
              ("segment", "Max drag step (pt)", float),
              ("color_wait_ms", "Wait after color change (ms)", int),
              ("min_blob", "Remove specks smaller than (cells)", int),
              ("denoise", "Smooth grain (0 = off, 3, 5)", int),
              ("key_ms", "Key delay (ms)", int),
              ("picker_ms", "Color picker click pause (ms)", int)]

    def __init__(self):
        super().__init__()
        self.cfg = dict(DEFAULTS)
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH) as f:
                    self.cfg.update(json.load(f))
            except (OSError, ValueError):
                pass
        # a factor learned from runs slowed by the old thread-priority bug isn't real
        tf = float(self.cfg.get("time_factor") or 1.0)
        if not 0.8 <= tf <= 10.0:
            self.cfg["time_factor"] = 1.0
        self.image_path = None
        self.stop_event = threading.Event()
        self.drawing = False
        self.bridge = Bridge()
        self.bridge.status.connect(self.set_status_main)
        self.bridge.finished.connect(self.on_finished)
        self.bridge.measured.connect(self.apply_brush)
        self.bridge.progress.connect(lambda k, f, t, h: self.progress_panel.update_stage(k, f, t, h))
        self.bridge.auto_done.connect(self.apply_auto_brush)
        self.bridge.preview_done.connect(self.show_preview)
        self._preview_gen, self._pv_done = 0, 0
        self._pv_timer = QTimer(self)
        self._pv_timer.timeout.connect(self._preview_tick)
        self.bridge.speed_done.connect(self.apply_auto_speed)
        self.bridge.color_done.connect(self.apply_color_check)
        self.bridge.timing.connect(self.learn_timing)
        self.bridge.settings_done.connect(self.apply_auto_settings)

        self.setWindowTitle("Roblox Drawer")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        root = QVBoxLayout(self)
        root.setSpacing(6)
        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        def hint(text):
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color: #666; font-size: 11px;")
            return lbl

        # --- Draw tab: picture + preview
        draw = QWidget()
        dl = QVBoxLayout(draw)
        row = QHBoxLayout()
        btn = QPushButton("Paste image")
        btn.setToolTip("Use the image on the clipboard")
        btn.clicked.connect(self.use_clipboard)
        row.addWidget(btn)
        btn = QPushButton("Open file...")
        btn.clicked.connect(self.load_image)
        row.addWidget(btn)
        btn = QPushButton("Preview")
        btn.clicked.connect(self.preview)
        row.addWidget(btn)
        dl.addLayout(row)
        self.img_label = QLabel("Source: clipboard")
        self.img_label.setStyleSheet("color: #666;")
        dl.addWidget(self.img_label)
        self.preview_label = QLabel("Copy an image, then press Paste image")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(200, 200)
        self.preview_label.setStyleSheet("background: #f5f5f5; border: 1px dashed #ccc; color: #888;")
        dl.addWidget(self.preview_label, 1)
        tabs.addTab(draw, "Draw")

        # --- Library tab: saved pictures (local folder), scored by how well they'd draw
        lib = QWidget()
        lv = QVBoxLayout(lib)
        lv.addWidget(hint("Click a picture to use it. Fit = how close the drawing gets to the picture with "
                          "your current brushes (higher is better); drop image files here to add them."))
        row = QHBoxLayout()
        for text, fn in [("Save pasted", self.lib_add_clipboard), ("Add files...", self.lib_add_files),
                         ("Score all", self.lib_score_all), ("Prep", self.lib_prep_selected),
                         ("Pre-plan...", self.lib_preplan)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lv.addLayout(row)
        self.lib_list = QListWidget()
        self.lib_list.setViewMode(QListView.IconMode)
        self.lib_list.setIconSize(QSize(104, 104))
        self.lib_list.setGridSize(QSize(124, 164))
        self.lib_list.setSelectionMode(QListView.ExtendedSelection)  # Cmd-click: pre-plan several
        self.lib_list.setResizeMode(QListView.Adjust)
        self.lib_list.setMovement(QListView.Static)
        self.lib_list.setWordWrap(True)
        self.lib_list.itemClicked.connect(self.lib_pick)
        lv.addWidget(self.lib_list, 1)
        row = QHBoxLayout()
        self.lib_sort = QComboBox()
        self.lib_sort.addItem("Best fit first", "fit")
        self.lib_sort.addItem("Newest first", "new")
        self.lib_sort.addItem("Name", "name")
        self.lib_sort.currentIndexChanged.connect(lambda _: self.lib_refresh())
        row.addWidget(QLabel("Sort:"))
        row.addWidget(self.lib_sort, 1)
        for text, fn in [("Remove", self.lib_remove), ("Open folder", self.lib_open_folder)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lv.addLayout(row)
        tabs.addTab(lib, "Library")
        self.setAcceptDrops(True)
        self.bridge.lib_scored.connect(self.lib_on_scored)
        self.bridge.lib_planned.connect(self.lib_on_planned)
        self.lib_planning = set()
        self.lib_scores, self.lib_scoring = self.lib_load_scores(), set()
        self.lib_refresh()

        # --- Calibrate tab: click each point in Roblox
        cal = QWidget()
        cv = QVBoxLayout(cal)
        cv.addWidget(hint("Press Set, then click that spot in Roblox (the click isn't passed to the game). "
                          "Calibrate all walks through every point; Esc skips one."))
        top = QHBoxLayout()
        top.addWidget(QLabel("Color picker:"))
        self.picker = QComboBox()
        self.picker.addItem("Color wheel + brightness slider", "wheel")
        self.picker.addItem("Hex text box", "hex")
        self.picker.addItem("Hue ring + color square", "ring")
        self.picker.setCurrentIndex({"wheel": 0, "hex": 1, "ring": 2}.get(self.cfg["picker"], 0))
        self.picker.currentIndexChanged.connect(self.update_picker_rows)
        top.addWidget(self.picker, 1)
        cv.addLayout(top)
        self.cal_all_btn = QPushButton("Calibrate all")
        self.cal_all_btn.setStyleSheet("font-weight: 600; padding: 6px;")
        self.cal_all_btn.clicked.connect(self.calibrate_all)
        cv.addWidget(self.cal_all_btn)
        self.cal_labels, self.cal_rows, self.cal_names = {}, [], {}
        for title, optional in (("Required", False), ("Optional", True)):
            gb = QGroupBox(title)
            gl = QGridLayout(gb)
            gl.setVerticalSpacing(2)
            r = 0
            for key, label, mode in self.CAL:
                if ("optional" in label) != optional:
                    continue
                name = label.replace(" (optional)", "").replace("(optional, ", "(").replace("(optional: ", "(")
                self.cal_names[key] = name
                nl = QLabel(name)
                nl.setWordWrap(True)
                vl = QLabel()
                vl.setMinimumWidth(90)
                b = QPushButton("Set")
                b.setFixedWidth(52)
                b.clicked.connect(lambda _=False, k=key: self.capture(k))
                ws = [nl, vl, b]
                gl.addWidget(nl, r, 0)
                gl.addWidget(vl, r, 1)
                gl.addWidget(b, r, 2)
                if optional:
                    x = QPushButton("Clear")
                    x.setFixedWidth(52)
                    x.clicked.connect(lambda _=False, k=key: self.clear_point(k))
                    gl.addWidget(x, r, 3)
                    ws.append(x)
                gl.setColumnStretch(0, 1)
                self.cal_labels[key] = vl
                self.cal_rows.append((mode, key, ws))
                r += 1
            cv.addWidget(gb)
        cv.addStretch(1)
        tabs.addTab(self.scrolled(cal), "Calibrate")
        self.catcher = ClickCatcher(lambda: (self.frameGeometry().x(), self.frameGeometry().y(),
                                             self.frameGeometry().width(), self.frameGeometry().height()))
        self.catcher.clicked.connect(self.on_cal_click)
        self.catcher.skipped.connect(self.on_cal_skip)
        self.cal_queue, self.cal_key, self._cal_total = [], None, 0

        # --- Tools tab: one-time game setup, per-picture helpers, tests
        tools = QWidget()
        tv = QVBoxLayout(tools)

        def tool(layout, text, fn, desc):
            b = QPushButton(text)
            b.clicked.connect(fn)
            layout.addWidget(b)
            if desc:
                layout.addWidget(hint(desc))

        gb = QGroupBox("Once per game (results are saved)")
        gv = QVBoxLayout(gb)
        tool(gv, "Auto brush", self.auto_brush, "Draws test dots to learn the brush slider sizes.")
        tool(gv, "Auto speed", self.auto_speed, "Finds the fastest mouse timing that still draws cleanly.")
        tool(gv, "Color check", self.color_check, "Tests the picker's colors (stops dark colors turning black).")
        tv.addWidget(gb)
        gb = QGroupBox("This picture")
        gv = QVBoxLayout(gb)
        tool(gv, "Auto settings", self.auto_settings,
             "Tries colors, smoothing, merging, line art and edges; keeps what looks best in the time limit.")
        tool(gv, "Correct now", self.correct_now, "Screenshots the canvas and repaints mistakes.")
        tv.addWidget(gb)
        gb = QGroupBox("Tests")
        gh = QHBoxLayout(gb)
        for text, fn in [("Measure brush", self.measure_brush), ("Test color", self.test_color),
                         ("Speed test", self.speed_test)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            gh.addWidget(b)
        tv.addWidget(gb)
        tv.addStretch(1)
        tabs.addTab(self.scrolled(tools), "Tools")

        # --- Settings tab: the common ones, the rest behind "advanced"
        groups = [("Basics", ["time_limit", "colors", "correct_passes"], False),
                  ("Brushes", ["spacing", "brush_pct", "big_brush_pct", "tiny_brush_pct"], True),
                  ("Image", ["merge_de", "smooth", "denoise", "min_blob", "edge_contrast", "line_contrast"], True),
                  ("Timing", ["frame_ms", "min_frame_ms", "corner_frames", "segment", "color_wait_ms", "picker_ms",
                              "key_ms"], True)]
        labels = {key: (label, typ) for key, label, typ in self.FIELDS}
        st = QWidget()
        st_l = QVBoxLayout(st)
        self.edits = {}
        forms, self.adv_boxes = {}, []
        for title, keys, adv in groups:
            gb = QGroupBox(title)
            fl = QFormLayout(gb)
            forms[title] = fl
            for key in keys:
                label, typ = labels[key]
                e = QLineEdit(str(self.cfg[key]))
                e.setMaximumWidth(80)
                fl.addRow(label, e)
                self.edits[key] = (e, typ)
            if adv:
                self.adv_boxes.append(gb)
            st_l.addWidget(gb)
            if title == "Basics":
                self.show_adv = QCheckBox("Show advanced settings")
                st_l.addWidget(self.show_adv)
        self.skip_white = QCheckBox("Skip white (canvas is white)")
        self.skip_white.setChecked(self.cfg["skip_white"])
        forms["Basics"].addRow(self.skip_white)
        self.use_bucket = QCheckBox("Bucket-fill the background")
        self.use_bucket.setToolTip("Needs the paint bucket and brush tool calibrated")
        self.use_bucket.setChecked(bool(self.cfg.get("use_bucket", True)))
        forms["Basics"].addRow(self.use_bucket)
        self.line_art = QCheckBox("Line art: draw thin curves as lines")
        self.line_art.setChecked(self.cfg.get("line_art", True))
        forms["Basics"].addRow(self.line_art)
        self.line_fill = QCheckBox("Follow thin shapes with lines (faster)")
        self.line_fill.setChecked(self.cfg.get("line_fill", True))
        forms["Image"].addRow(self.line_fill)
        self.hash_prefix = QCheckBox("Type '#' before hex")
        self.hash_prefix.setChecked(self.cfg["hash_prefix"])
        forms["Timing"].addRow(self.hash_prefix)
        self.show_adv.toggled.connect(lambda v: [g.setVisible(v) for g in self.adv_boxes])
        for g in self.adv_boxes:
            g.setVisible(False)
        st_l.addStretch(1)
        tabs.addTab(self.scrolled(st), "Settings")
        self.update_picker_rows()

        # --- always visible: progress (while drawing), start row, status
        self.progress_panel = ProgressPanel()
        root.addWidget(self.progress_panel)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Time left:"))
        self.time_left = QLineEdit()
        self.time_left.setPlaceholderText("95 or 1:35")
        self.time_left.setToolTip("Optional. Seconds left in the round: the drawing is planned to finish in time.")
        self.time_left.setMaximumWidth(90)
        bar.addWidget(self.time_left)
        self.autotune = QCheckBox("Auto-tune")
        self.autotune.setToolTip("Before drawing, pick brush sizes and settings for this picture "
                                 "(colors, smoothing, line art) that look best within the time.")
        self.autotune.setChecked(bool(self.cfg.get("autotune", False)))
        self.autotune.toggled.connect(lambda v: (self.cfg.update(autotune=bool(v)), self.save()))
        bar.addWidget(self.autotune)
        self.start_btn = QPushButton()
        self.start_btn.setMinimumHeight(34)
        self.start_btn.setStyleSheet("QPushButton { font-weight: 700; font-size: 14px; }")
        self.start_btn.clicked.connect(self.start)
        bar.addWidget(self.start_btn, 1)
        root.addLayout(bar)
        self.update_start_label()
        self.status_label = QLabel("Calibrate, copy an image, then Start. Press Esc (or move the mouse) to stop drawing.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #444;")
        root.addWidget(self.status_label)

        QTimer.singleShot(500, lambda: [pool().submit(_noop) for _ in range(os.cpu_count() or 2)])
        threading.Thread(target=warm_up_ocr, daemon=True).start()

        avail = QApplication.primaryScreen().availableGeometry() if QApplication.primaryScreen() else None
        max_h = int(avail.height() * 0.85) if avail else 700
        self.resize(460, min(720, max_h))
        self.setMinimumWidth(420)

    def update_start_label(self):
        self.start_btn.setText("Start drawing" if self.cfg.get("focus_spot") else "Start drawing (5s)")

    def set_cal_label(self, key):
        p = self.cfg.get(key)
        lbl = self.cal_labels[key]
        lbl.setText(f"✓ {p[0]:.0f}, {p[1]:.0f}" if p else "not set")
        lbl.setStyleSheet("color: #15803d;" if p else "color: #999;")

    def capture(self, key):
        """Wait for the next click outside this window and store it as point `key`."""
        self.cal_queue, self._cal_total = [], 0
        self._begin_capture(key)

    def calibrate_all(self):
        if self.cal_key is not None:  # pressed again: stop
            self._end_capture("Calibration stopped")
            return
        keys = [k for m, k, _ in self.cal_rows if m in ("all", self.picker.currentData())]
        req = [k for k in keys if "optional" not in dict((c[0], c[1]) for c in self.CAL)[k]]
        self.cal_queue = req + [k for k in keys if k not in req]
        self._cal_total = len(self.cal_queue)
        self._next_capture()

    def _begin_capture(self, key):
        if self.drawing:
            return
        self.cal_key = key
        for _, k, ws in self.cal_rows:
            ws[0].setStyleSheet("font-weight: 700; color: #1d4ed8;" if k == key else "")
        prog = f" ({self._cal_total - len(self.cal_queue)}/{self._cal_total})" if self._cal_total else ""
        self.set_status_main(f"Click: {self.cal_names[key]}{prog}  -  Esc = skip")
        self.cal_all_btn.setText("Stop calibrating")
        self.catcher.start()
        if not self.catcher._tap_ok:
            self.set_status_main(self.status_label.text() + "  (the click also reaches the game: allow "
                                 "Accessibility for this app to block it)")

    def _next_capture(self):
        if not self.cal_queue:
            return self._end_capture("Calibration done")
        key = self.cal_queue.pop(0)
        self._begin_capture(key)

    def _end_capture(self, msg):
        self.catcher.stop()
        self.cal_key, self.cal_queue, self._cal_total = None, [], 0
        for _, _, ws in self.cal_rows:
            ws[0].setStyleSheet("")
        self.cal_all_btn.setText("Calibrate all")
        self.set_status_main(msg)

    def on_cal_click(self, x, y):
        key = self.cal_key
        if key is None:
            return
        self.cfg[key] = [x, y]
        self.set_cal_label(key)
        self.save()
        if key == "focus_spot":
            self.update_start_label()
        if self.cal_queue:
            self._next_capture()
        else:
            self._end_capture("Calibration done" if self._cal_total else
                              f"Set {self.cal_names[key]} at ({x:.0f}, {y:.0f})")

    def on_cal_skip(self):
        if self.cal_key is None:
            return
        if self.cal_queue:
            self._next_capture()
        else:
            self._end_capture("Skipped")

    def clear_point(self, key):
        self.cfg[key] = None
        self.set_cal_label(key)
        self.save()
        if key == "focus_spot":
            self.update_start_label()

    @staticmethod
    def scrolled(widget):
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(widget)
        return area

    def update_picker_rows(self):
        mode = self.picker.currentData()
        for m, key, ws in self.cal_rows:
            for w in ws:
                w.setVisible(m in ("all", mode))
            self.set_cal_label(key)
        self.hash_prefix.setVisible(mode == "hex")
        self.cfg["picker"] = mode

    def missing_calibration(self, need_canvas=True):
        keys = [k for k, _, m in self.CAL if m == self.cfg["picker"]]
        if need_canvas:
            keys = ["canvas_tl", "canvas_br"] + keys
        names = {k: label for k, label, _ in self.CAL}
        miss = [names[k] for k in keys if not self.cfg.get(k)]
        if miss:
            raise ValueError("Calibrate first: " + ", ".join(miss))

    @staticmethod
    def fmt(p):
        return "not set" if not p else f"({p[0]:.0f}, {p[1]:.0f})"

    def set_status_main(self, text):
        self.status_label.setText(text)

    def error(self, msg):
        QMessageBox.critical(self, "Error", msg)

    def read_settings(self):
        for key, (e, typ) in self.edits.items():
            try:
                val = typ(e.text().strip())
            except ValueError:
                raise ValueError(f"Invalid value for {key}: {e.text()!r}")
            if val < 0 or (val == 0 and key not in ("color_wait_ms", "min_blob", "brush_pct", "big_brush_pct", "tiny_brush_pct", "denoise", "merge_de", "smooth", "correct_passes")):
                raise ValueError(f"{key} must be > 0")
            self.cfg[key] = val
        self.cfg["colors"] = max(1, min(256, self.cfg["colors"]))
        self.cfg["skip_white"] = self.skip_white.isChecked()
        self.cfg["use_bucket"] = self.use_bucket.isChecked()
        self.cfg["line_fill"] = self.line_fill.isChecked()
        self.cfg["line_art"] = self.line_art.isChecked()
        self.cfg["hash_prefix"] = self.hash_prefix.isChecked()
        self.cfg["picker"] = self.picker.currentData()
        self.cfg["brush_pct"] = max(0.0, min(100.0, self.cfg["brush_pct"]))
        self.cfg["big_brush_pct"] = max(0.0, min(100.0, self.cfg["big_brush_pct"]))
        self.cfg["tiny_brush_pct"] = max(0.0, min(100.0, self.cfg["tiny_brush_pct"]))
        self.save()

    def save(self):
        with open(CONFIG_PATH, "w") as f:
            json.dump(self.cfg, f, indent=2)

    # ---------- library ----------
    @staticmethod
    def lib_files():
        if not os.path.isdir(LIB_DIR):
            return []
        return [os.path.join(LIB_DIR, f) for f in os.listdir(LIB_DIR)
                if f.lower().endswith(LIB_EXT) and not f.startswith(".")]

    @staticmethod
    def lib_load_scores():
        try:
            with open(os.path.join(LIB_DIR, ".scores.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def lib_save_scores(self):
        try:
            os.makedirs(LIB_DIR, exist_ok=True)
            with open(os.path.join(LIB_DIR, ".scores.json"), "w") as f:
                json.dump(self.lib_scores, f, indent=1)
        except OSError:
            pass

    def lib_score_key(self):
        """Scores depend on the brushes, canvas and image settings: re-score when they change."""
        keys = ("spacing", "brush_pct", "big_brush_pct", "tiny_brush_pct", "brush_pt", "big_brush_pt",
                "tiny_brush_pt", "colors", "merge_de", "smooth", "denoise", "min_blob", "edge_contrast",
                "line_art", "line_fill", "use_bucket", "skip_white", "canvas_tl", "canvas_br",
                "bucket_tool", "brush_tool")
        return json.dumps([self.cfg.get(k) for k in keys], default=str)

    def lib_score_of(self, path):
        sc = self.lib_scores.get(os.path.basename(path))
        if sc and sc.get("mtime") == os.path.getmtime(path) and sc.get("key") == self.lib_score_key():
            return sc
        return None

    @staticmethod
    def fit_of(err):
        return max(0, min(100, round(100 - 4 * err)))

    def lib_refresh(self):
        files = self.lib_files()
        mode = self.lib_sort.currentData()
        if mode == "new":
            files.sort(key=os.path.getmtime, reverse=True)
        elif mode == "name":
            files.sort(key=lambda p: os.path.basename(p).lower())
        else:  # best fit first, unscored last
            def k(p):
                sc = self.lib_score_of(p)
                return (0, sc["err"]) if sc else (1, 0.0)
            files.sort(key=k)
        if not hasattr(self, "_lib_thumbs"):
            self._lib_thumbs = {}
        cur = self.image_path
        self.lib_list.clear()
        limit = float(self.cfg.get("time_limit") or 0)
        pidx, pkey = preplan_index(), plan_settings_key(self.cfg)
        for p in files:
            mt = os.path.getmtime(p)
            icon = self._lib_thumbs.get(p)
            if icon is None or icon[0] != mt:
                pid = None
                try:
                    im = Image.open(p)
                    pid = picture_id(im)
                    im = ImageOps.exif_transpose(im).convert("RGBA")
                    im.thumbnail((104, 104))
                    data = im.tobytes("raw", "RGBA")
                    q = QImage(data, im.width, im.height, 4 * im.width, QImage.Format_RGBA8888).copy()
                    icon = (mt, QIcon(QPixmap.fromImage(q)), pid)
                except Exception:
                    icon = (mt, QIcon(), pid)
                self._lib_thumbs[p] = icon
            windows = sorted({e["window"] for e in pidx.get(icon[2], []) if e["key"] == pkey})
            name = os.path.splitext(os.path.basename(p))[0]
            sc = self.lib_score_of(p)
            if p in self.lib_scoring:
                sub = "scoring..."
            elif sc:
                sub = f"fit {self.fit_of(sc['err'])} \u00b7 ~{sc['est']:.0f}s" + (" \u26a0" if limit and sc["est"] > limit else "")
            else:
                sub = "not scored"
            if p in self.lib_planning:
                sub += "\npre-planning..."
            elif windows:
                sub += "\n\u2713 planned " + ", ".join(f"{w}s" for w in windows)
            it = QListWidgetItem(icon[1], f"{name[:18]}\n{sub}")
            it.setData(Qt.UserRole, p)
            it.setToolTip(f"{os.path.basename(p)}\n{sub}" + ("\n\u26a0 takes longer than the time limit with "
                                                           "these brushes (Start simplifies it)" if "\u26a0" in sub else ""))
            self.lib_list.addItem(it)
            if p == cur:
                it.setSelected(True)

    def lib_unique(self, name, ext=".png"):
        os.makedirs(LIB_DIR, exist_ok=True)
        base = "".join(ch for ch in name if ch.isalnum() or ch in " -_").strip() or "picture"
        path, n = os.path.join(LIB_DIR, base + ext), 2
        while os.path.exists(path):
            path, n = os.path.join(LIB_DIR, f"{base} {n}{ext}"), n + 1
        return path

    def lib_add_clipboard(self):
        try:
            img = self.clipboard_image()
            img.load()
        except Exception as e:
            self.error(str(e))
            return
        name, ok = QInputDialog.getText(self, "Save to library", "Name:",
                                        text=time.strftime("picture %Y-%m-%d %H%M"))
        if not ok:
            return
        path = self.lib_unique(name)
        ImageOps.exif_transpose(img).save(path)
        self.lib_added([path])

    def lib_add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add pictures", "", "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp *.tiff)")
        self.lib_import(paths)

    def lib_import(self, paths):
        import shutil
        added = []
        for src in paths:
            if not src.lower().endswith(LIB_EXT) or os.path.dirname(os.path.abspath(src)) == LIB_DIR:
                continue
            stem, ext = os.path.splitext(os.path.basename(src))
            dst = self.lib_unique(stem, ext.lower())
            shutil.copy2(src, dst)
            added.append(dst)
        if added:
            self.lib_added(added)

    def lib_added(self, paths):
        self.set_status_main(f"Added {len(paths)} picture{'s' * (len(paths) != 1)} to the library")
        self.lib_score(paths)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.lib_import([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def lib_pick(self, item):
        p = item.data(Qt.UserRole)
        if not p or not os.path.exists(p):
            return self.lib_refresh()
        self.image_path = p
        self.img_label.setText(f"Library: {os.path.basename(p)}")
        self.preview()

    def lib_remove(self):
        items = self.lib_list.selectedItems()
        if not items:
            return
        p = items[0].data(Qt.UserRole)
        if QFile.moveToTrash(p):  # recoverable from the Trash
            self.lib_scores.pop(os.path.basename(p), None)
            self.lib_save_scores()
            if self.image_path == p:
                self.image_path = None
                self.img_label.setText("Source: clipboard")
            self.set_status_main(f"Moved {os.path.basename(p)} to the Trash")
        self.lib_refresh()

    def lib_open_folder(self):
        os.makedirs(LIB_DIR, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(LIB_DIR))

    def lib_score_all(self):
        self.lib_score([p for p in self.lib_files() if self.lib_score_of(p) is None] or self.lib_files())

    def lib_score(self, paths):
        """Plan + render each picture with the current brushes in the background (in parallel)."""
        try:
            self.read_settings()
            self.plan_checks()
        except Exception as e:
            self.lib_refresh()
            self.set_status_main(f"Added; not scored ({e})")
            return
        cfg, key = dict(self.cfg), self.lib_score_key()
        paths = [p for p in paths if p not in self.lib_scoring]
        self.lib_scoring.update(paths)
        self.lib_refresh()

        def work():
            futs = []
            for p in paths:
                try:
                    img = Image.open(p)
                    img.load()
                    futs.append((p, os.path.getmtime(p), pool().submit(_quality_job, img.convert("RGB"), cfg)))
                except Exception as e:
                    self.bridge.lib_scored.emit(p, e)
            for p, mt, f in futs:
                try:
                    err, est = f.result()
                    self.bridge.lib_scored.emit(p, {"mtime": mt, "key": key, "err": err, "est": est})
                except Exception as e:
                    self.bridge.lib_scored.emit(p, e)
        threading.Thread(target=work, daemon=True).start()

    def lib_preplan(self):
        """Plan the selected pictures now for a round of a given length, so Start draws them
        with no planning wait (the whole round goes to drawing)."""
        paths = [it.data(Qt.UserRole) for it in self.lib_list.selectedItems()]
        paths = [p for p in paths if p and os.path.exists(p) and p not in self.lib_planning]
        if not paths:
            return self.error("Select one or more pictures first (Cmd-click for several).")
        try:
            self.plan_checks()
        except Exception as e:
            return self.error(str(e))
        timer_set = bool(self.cfg.get("timer_tl") and self.cfg.get("timer_br"))
        ask = ("Seconds on the round timer when you'll press Start:" if timer_set
               else "Seconds to draw in (the Drawing time limit when you press Start):")
        window, ok = QInputDialog.getInt(self, "Pre-plan", ask, int(float(self.cfg.get("time_limit") or 90)), 10, 3600)
        if not ok:
            return
        cfg, tune = dict(self.cfg), self.autotune.isChecked()
        rate = float(cfg.get("game_rate") or 1.0) if timer_set else 1.0
        lead = 0.5 if cfg.get("focus_spot") else 5.0
        # the drawing time Start will have (see start(): countdown/focus lead and ~1.5s of clicks)
        budget = window / rate - lead - 1.5 if timer_set else window
        self.lib_planning.update(paths)
        self.lib_refresh()

        def work():
            boost_thread()
            for i, p in enumerate(paths, 1):
                self.bridge.status.emit(f"Pre-planning {os.path.basename(p)} ({i}/{len(paths)}) for {window}s...")
                try:
                    img = Image.open(p)
                    img.load()  # as Start opens it (same planning input)
                    res = auto_tune(img, cfg, 0.93 * budget) if tune else fit_to_time(img, cfg, 0.93 * budget)
                    save_preplan(img, cfg, window, tune, res)
                    self.bridge.lib_planned.emit(p, (window, res[2], res[3]))
                except Exception as e:
                    self.bridge.lib_planned.emit(p, e)
        threading.Thread(target=work, daemon=True).start()

    def lib_on_planned(self, p, res):
        self.lib_planning.discard(p)
        name = os.path.basename(p)
        if isinstance(res, Exception):
            self.set_status_main(f"Pre-planning {name} failed: {res}")
        else:
            w, est, desc = res
            self.set_status_main(f"Pre-planned {name} for {w}s: {desc}, ~{est:.0f}s"
                                 + ("" if self.lib_planning else " - Start will draw it right away"))
        self.lib_refresh()

    def lib_on_scored(self, p, res):
        self.lib_scoring.discard(p)
        if isinstance(res, dict):
            self.lib_scores[os.path.basename(p)] = res
            self.lib_save_scores()
        if not self.lib_scoring:
            self.set_status_main("Library scored")
        self.lib_refresh()

    def lib_prep_selected(self):
        paths = [it.data(Qt.UserRole) for it in self.lib_list.selectedItems()]
        paths = [p for p in paths if p and os.path.exists(p)]
        if not paths:
            self.set_status_main("Select one or more pictures to prep")
            return
        self.lib_prep(paths)

    def lib_prep(self, paths):
        """In the background, do the brush-independent part of planning (scaling, colors, line
        art) for these pictures on every grid the brush search may use, so planning at Start
        skips it. Pauses while drawing; one picture/grid at a time to leave the CPU free."""
        if not hasattr(self, "_prep_todo"):
            self._prep_todo, self._prep_running = [], False
        for p in paths:
            if p not in self._prep_todo:
                self._prep_todo.append(p)
        if self._prep_running or not self._prep_todo:
            return
        try:
            self.read_settings()
            self.plan_checks()
        except Exception as e:
            self._prep_todo.clear()
            self.set_status_main(f"Can't prep yet: {e}")
            return
        cfg = dict(self.cfg)
        self._prep_running = True

        def work():
            try:
                cfgs = prep_configs(cfg)
                done = 0
                while self._prep_todo:
                    p = self._prep_todo.pop(0)
                    done += 1
                    self.bridge.status.emit(f"Prepping {os.path.basename(p)} "
                                            f"({done}/{done + len(self._prep_todo)})...")
                    for c in cfgs:
                        while self.drawing:
                            time.sleep(1.0)
                        try:
                            pool().submit(_prep_job, p, c).result()
                        except Exception:
                            break  # unreadable picture: skip it
                self.bridge.status.emit(f"Prepped {done} picture(s): planning them at Start is faster")
            finally:
                self._prep_running = False
        threading.Thread(target=work, daemon=True).start()

    def load_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose image", "", "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp *.tiff);;All files (*)")
        if path:
            self.image_path = path
            self.img_label.setText(os.path.basename(path))
            self.preview()

    def use_clipboard(self):
        self.image_path = None
        self.img_label.setText("Source: clipboard")
        self.preview()

    @staticmethod
    def clipboard_image():
        md = QApplication.clipboard().mimeData()
        if md is not None and md.hasImage():
            qimg = QApplication.clipboard().image()
            if not qimg.isNull():
                qimg = qimg.convertToFormat(QImage.Format_RGBA8888)
                w, h, bpl = qimg.width(), qimg.height(), qimg.bytesPerLine()
                buf = bytes(qimg.constBits())[:bpl * h]
                return Image.frombuffer("RGBA", (w, h), buf, "raw", "RGBA", bpl, 1).copy()
        if md is not None and md.hasUrls():
            for url in md.urls():
                if url.isLocalFile():
                    try:
                        return Image.open(url.toLocalFile())
                    except OSError:
                        continue
        raise ValueError("No image on clipboard (copy an image, or use Load file)")

    def source_image(self):
        if self.image_path:
            return Image.open(self.image_path)
        return self.clipboard_image()

    def plan_checks(self):
        self.read_settings()
        self.missing_calibration()
        tl, br = self.cfg["canvas_tl"], self.cfg["canvas_br"]
        if br[0] <= tl[0] or br[1] <= tl[1]:
            raise ValueError("Bottom-right must be below and right of top-left")
        s = self.cfg["spacing"]
        gw, gh = int((br[0] - tl[0]) / s), int((br[1] - tl[1]) / s)
        if gw < 1 or gh < 1:
            raise ValueError("Spacing too large for canvas")

    def big_brush_on(self):
        return bool(self.cfg["brush_min"] and self.cfg["brush_max"] and self.cfg["big_brush_pct"] > 0
                    and self.cfg.get("big_brush_pt"))

    def tiny_brush_on(self):
        return bool(self.cfg["brush_min"] and self.cfg["brush_max"] and self.cfg["tiny_brush_pct"] > 0
                    and self.cfg.get("tiny_brush_pt")
                    and self.cfg["tiny_brush_pt"] <= TINY_MAX_RATIO * (self.cfg.get("brush_pt") or 99))

    def tiny_radius_cells(self):
        return self.cfg["tiny_brush_pt"] / 2 / self.cfg["spacing"] if self.tiny_brush_on() else 0.0

    def big_radius_cells(self):
        return self.cfg["big_brush_pt"] / 2 / self.cfg["spacing"] if self.big_brush_on() else 0.0

    @staticmethod
    def image_key(img):
        import hashlib
        return hashlib.md5(img.tobytes()).hexdigest()

    def can_repick(self, img=None):
        """Brush sizes can be re-picked per picture once Auto brush has measured the slider -
        unless Auto settings already picked everything for this exact picture."""
        if img is not None and self.cfg.get("auto_settings_for") == self.image_key(img):
            return False
        curve = self.cfg.get("brush_curve")
        return bool(self.cfg.get("brush_min") and self.cfg.get("brush_max") and curve and len(curve) >= 2)

    def preview(self):
        """Plan the picture in the background (the search can take a while) and show it."""
        try:
            img = self.source_image()
            img.load()
            self.read_settings()
            repick = self.can_repick(img)
            if repick:
                self.missing_calibration()
            else:
                self.plan_checks()
        except Exception as e:
            self.error(str(e))
            return
        self._preview_gen += 1
        gen, cfg0 = self._preview_gen, dict(self.cfg)
        msg = "Picking brush sizes for this picture..." if repick else "Planning preview..."
        self.set_status_main(msg)
        if repick:
            PLAN_METER.reset(search_guess(cfg0, False))
            self._pv_msg = msg
            self._pv_timer.start(300)

        cancel_searches("preview")  # an older preview's queued plans would delay this one

        def work():
            _SEARCH_TAG.tag = "preview"
            try:
                note = ""
                if repick:  # show what Start will draw: sizes re-picked for this picture
                    cfg, plan, est, desc = fit_to_time(img, dict(cfg0), 0.93 * cfg0["time_limit"])
                    note = f" | brushes for this picture: {desc}"
                else:
                    cfg = cfg0
                    _, plan = build_plan(*plan_args(img, cfg), **plan_kw(cfg))
                tl, br, sp = cfg["canvas_tl"], cfg["canvas_br"], cfg["spacing"]
                gw, gh = int((br[0] - tl[0]) / sp), int((br[1] - tl[1]) / sp)
                shown = simulate(plan, gw, gh, sp, brush_sizes(cfg), 380)
                self.bridge.preview_done.emit(gen, (cfg, plan, shown, note))
            except Exception as e:
                self.bridge.preview_done.emit(gen, e)
        threading.Thread(target=work, daemon=True).start()

    def _preview_tick(self):
        if self._pv_done == self._preview_gen:
            return self._pv_timer.stop()
        # only while the label still shows this meter: never write over another message
        if not self.drawing and self.status_label.text().startswith(self._pv_msg):
            self.set_status_main(f"{self._pv_msg} {PLAN_METER.text()}")

    def show_preview(self, gen, res):
        if gen != self._preview_gen:
            return  # an older preview finished after a newer one started
        self._pv_done = gen
        if isinstance(res, Exception):
            self.error(str(res))
            return
        cfg, plan, shown, note = res
        sp = cfg["spacing"]
        data = shown.tobytes("raw", "RGB")
        qimg = QImage(data, shown.width, shown.height, 3 * shown.width, QImage.Format_RGB888).copy()
        self.preview_label.setPixmap(QPixmap.fromImage(qimg))
        strokes = sum(len(r) for _, _, r in plan)
        ncol = len({hx for hx, _, _ in plan})
        est = estimate_seconds(plan, cfg, sp)
        big = " + ".join([t for t, _ in brush_tiers(cfg)] + ["fine"]
                         + (["tiny edges"] if any(st[1] == "tiny" for st in plan) else []))
        self.set_status_main(f"{ncol} colors, {strokes} strokes ({big}), ~{est:.0f}s{note}")

    def test_color(self):
        try:
            self.read_settings()
            self.missing_calibration(need_canvas=False)
        except Exception as e:
            self.error(str(e))
            return

        def job(d):
            d.activate()
            d.set_color("ff0000")
            d.status("Set color to red (FF0000) - check the picker")
        self.launch(job)

    def measure_brush(self):
        """Draw a black dot, measure it from a screenshot; fall back to hovering its edge."""
        try:
            self.read_settings()
            self.missing_calibration()
        except Exception as e:
            self.error(str(e))
            return

        def job(d):
            self.bridge.measured.emit(d.measure_brush())
        self.launch(job)

    def auto_brush(self):
        try:
            self.read_settings()
            self.missing_calibration()
            if not (self.cfg["brush_min"] and self.cfg["brush_max"]):
                raise ValueError("Calibrate the brush slider ends first (Calibrate tab)")
            img = self.source_image()
            img.load()
        except Exception as e:
            self.error(str(e))
            return
        cfg = dict(self.cfg)
        emit = self.bridge.status.emit
        # plan all candidate sizes in the background while the test dots are drawn
        old = cfg.get("brush_curve") or []
        d_min = min((d for _, d in old), default=cfg.get("tiny_brush_pt") or 2.0)
        d_max = max((d for _, d in old), default=60.0)
        prefetched = prefetch_estimates(img, cfg, d_min, d_max)

        def pick_sizes(curve, arrow_curve=None):
            budget = 0.93 * cfg["time_limit"]  # margin: real brush sizes differ slightly from targets
            c2 = dict(cfg, arrow_curve=arrow_curve or cfg.get("arrow_curve"))
            *_, (fine, big, tiny) = choose_sizes(img, c2, curve, budget, emit, None if arrow_curve else prefetched)
            return {"fine": fine, "big": big, "tiny": tiny}

        def job(d):
            self.bridge.auto_done.emit(d.auto_brush(pick_sizes))
        self.launch(job)

    def auto_settings(self):
        try:
            self.read_settings()
            self.missing_calibration()
            img = self.source_image()
            img.load()
        except Exception as e:
            self.error(str(e))
            return
        cfg = dict(self.cfg)
        emit = self.bridge.status.emit

        key = self.image_key(img)

        def work():
            try:
                c, err, est, changes = auto_settings(img, cfg, 0.93 * cfg["time_limit"], emit)
                c["auto_settings_for"] = key  # Start/Preview use these exact settings for this picture
                self.bridge.settings_done.emit({"cfg": c, "err": err, "est": est, "changes": changes})
            except Exception as e:
                emit(f"Auto settings failed: {e}")
        emit("Auto settings: working (this takes ~10-30s)...")
        threading.Thread(target=work, daemon=True).start()

    def apply_auto_settings(self, res):
        c = res["cfg"]
        for key in ("colors", "smooth", "merge_de", "denoise", "edge_contrast", "spacing", "brush_pct",
                    "big_brush_pct", "tiny_brush_pct"):
            if key in self.edits and key in c:
                self.edits[key][0].setText(str(c[key]))
        for key in ("brush_pt", "big_brush_pt", "tiny_brush_pt", "auto_settings_for"):
            if key in c:
                self.cfg[key] = c[key]
        self.line_art.setChecked(bool(c.get("line_art", True)))
        self.read_settings()
        _FIT_CACHE.clear()
        self.preview()
        changed = "; ".join(res["changes"]) or "your settings were already the best found"
        self.set_status_main(f"Auto settings: {changed} | ~{res['est']:.0f}s, look error {res['err']:.2f} | "
                             + self.status_label.text())

    def learn_timing(self, info):
        """A run finished (or was stopped): learn from it. Per part (mouse steps, color changes,
        brush changes) how real times compare with the model, and from the in-game timer how
        fast game seconds pass - so the next predictions and time fitting match reality."""
        ratio = info["ratio"]
        msgs = []
        clock = info.get("clock") or []
        if len(clock) >= 3:  # game seconds per real second, from the timer readings
            (t0, g0), (t1, g1) = clock[0], clock[-1]
            if t1 - t0 > 15 and g0 > g1:
                rate = (g0 - g1) / (t1 - t0)
                if 0.7 <= rate <= 1.3:
                    self.cfg["game_rate"] = round(0.5 * float(self.cfg.get("game_rate") or 1.0) + 0.5 * rate, 3)
                    msgs.append(f"game clock runs at {rate:.2f}x real time")
        if 0.4 <= ratio <= 10:
            model = fit_timing(info.get("rows") or [], self.cfg.get("timing_model"))
            if model:
                self.cfg["timing_model"] = model
                msgs.append("learned timing: mouse x{mouse:.2f}, color changes x{color:.2f}, "
                            "brush changes x{brush:.2f}".format(**model))
        if msgs:
            self.save()
            self.set_status_main(self.status_label.text() + " | " + "; ".join(msgs))
        self._learn_factor(ratio)

    def _learn_factor(self, ratio):
        old = float(self.cfg.get("time_factor") or 1.0)
        if not 0.4 <= ratio <= 10:  # absurd = something was broken (e.g. a stalled run): don't learn it
            self.set_status_main(self.status_label.text() + f" | this run took {ratio:.1f}x the prediction - "
                                 "too far off to learn from (see drawer_log.txt)")
            return
        # ratio is against the raw model; keep a running average, clamped to sane values
        new = min(10.0, max(0.8, 0.3 * old + 0.7 * ratio))
        self.cfg["time_factor"] = round(new, 3)
        self.save()
        self.set_status_main(self.status_label.text() + f" | this run took {ratio:.2f}x the raw prediction; "
                             f"predictions now x{new:.2f} (details in drawer_log.txt)")

    def color_check(self):
        try:
            self.read_settings()
            self.missing_calibration()
            if self.cfg["picker"] not in ("wheel", "ring"):
                raise ValueError("Color check is for the color wheel / hue ring pickers")
        except Exception as e:
            self.error(str(e))
            return
        self.launch(lambda d: self.bridge.color_done.emit(d.color_check()))

    def apply_color_check(self, res):
        self.cfg["value_curve"] = res["value_curve"]
        self.save()
        msg = []
        if res["value_curve"]:
            msg.append(f"brightness was off by up to {res['worst_value_error']:.0%} - corrected from now on")
        elif res["transparent"]:
            msg.append("brightness not measured (paint is see-through) - fix Transparency, then run again")
        else:
            msg.append("brightness is accurate")
        if res["transparent"]:
            msg.append("WARNING: colors painted over black come out different - set the game's "
                       "Transparency slider to 0 (see-through paint lets the black show through)")
        self.set_status_main("Color check: " + "; ".join(msg) + ". Clear the canvas. | " + " | ".join(res["report"]))
        if res["transparent"]:
            QMessageBox.warning(self, "Transparency", "Colors painted over black came out darker than on the "
                                "plain canvas. Set the game's Transparency slider to 0 - otherwise dark areas "
                                "drawn first show through later colors (reds turn black).")

    def auto_speed(self):
        try:
            self.read_settings()
            self.missing_calibration()
            if not self.cfg.get("brush_pt"):
                raise ValueError("Run Measure brush or Auto brush first (the test needs the brush size)")
        except Exception as e:
            self.error(str(e))
            return

        cancel_searches("preview")  # a busy CPU slows the game and makes the timing look worse
        self._pv_done = self._preview_gen
        self._pv_timer.stop()

        def job(d):
            res, log = d.auto_speed()
            self.bridge.speed_done.emit(res, log)
        self.launch(job)

    def apply_auto_speed(self, res, log):
        before = {k: self.cfg[k] for k in res}
        for k in ("frame_ms", "min_frame_ms", "corner_frames", "segment", "color_wait_ms"):
            self.edits[k][0].setText(str(res[k]))
        self.read_settings()
        changed = ", ".join(f"{k} {before[k]} -> {res[k]}" for k in res if before[k] != res[k]) or "no faster settings passed"
        try:
            with open(LOG_PATH, "w") as f:
                f.write("Auto speed: " + changed + "\n" + "\n".join(log) + "\n")
        except OSError:
            pass
        self.set_status_main("Auto speed: " + changed + ". Clear the canvas. | " + " | ".join(log))

    def apply_auto_brush(self, res):
        self.cfg["brush_curve"] = res.get("curve")  # reused by "Time left" fitting
        if res.get("arrow_curve"):
            self.cfg["arrow_curve"] = res["arrow_curve"]
        cl = res.get("clicks") or {}
        self.cfg["brush_clicks"] = cl.get("fine")
        self.cfg["big_brush_clicks"] = cl.get("big")
        self.cfg["tiny_brush_clicks"] = cl.get("tiny")
        fine_pct, fine_d = res["fine"]
        self.edits["brush_pct"][0].setText(str(fine_pct))
        for key, pct_key, pt_key in (("big", "big_brush_pct", "big_brush_pt"), ("tiny", "tiny_brush_pct", "tiny_brush_pt")):
            if res.get(key):
                # 0 means "off" in settings; 0.5% is the same brush size
                self.edits[pct_key][0].setText(str(max(0.5, res[key][0])))
                self.cfg[pt_key] = round(res[key][1], 1)
            else:
                self.edits[pct_key][0].setText("0")
        self.set_brush_size("brush_pt", fine_d)  # also sets line spacing and saves
        where = lambda k, p: f"+{cl[k]} clicks" if k in cl else f"@ {p}%"
        parts = [f"fine {fine_d:.1f}pt {where('fine', fine_pct)}"]
        for key in ("big", "tiny"):
            if res.get(key):
                parts.append(f"{key} {res[key][1]:.1f}pt {where(key, res[key][0])}")
        self.preview()
        self.set_status_main("Auto brush applied: " + ", ".join(parts) + " | " + self.status_label.text() +
                             ". Clear the canvas before drawing.")

    def apply_brush(self, results):
        """results: [(key, diameter or None, (cx, cy))]. Failed ones fall back to hovering."""
        self.pending_hover = []
        msgs = []
        for key, diam, (cx, cy) in results:
            if diam:
                self.set_brush_size(key, diam)
                msgs.append(f"{key.split('_')[0] if key != 'brush_pt' else 'fine'} {diam:.1f}pt")
            else:
                self.pending_hover.append((key, cx, cy))
        if msgs:
            self.set_status_main("Measured brush: " + ", ".join(msgs) +
                                 f". Line spacing {self.cfg['spacing']}pt. Clear the canvas, then Preview.")
        self.next_hover()

    def set_brush_size(self, key, diam):
        self.cfg[key] = round(diam, 1)
        if key == "brush_pt":
            self.edits["spacing"][0].setText(str(round(diam * 0.9, 1)))  # overlap: no gaps
        self.read_settings()

    def next_hover(self):
        if self.pending_hover:
            key, cx, cy = self.pending_hover.pop(0)
            self.measure_edge(cx, cy, key)

    def measure_edge(self, cx, cy, key="brush_pt", n=4):
        if n > 0:
            self.set_status_main("Auto-measure unavailable (grant Screen Recording to enable). "
                                 f"Hover the mouse on the RIGHT edge of the "
                                 f"{dict(big_brush_pt='big (right)', tiny_brush_pt='tiny (lower)').get(key, 'fine (top-left)')} "
                                 f"black dot... {n}")
            QTimer.singleShot(1000, lambda: self.measure_edge(cx, cy, key, n - 1))
            return
        x, y = mouse_pos()
        r = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        if r < 0.5 or r > 150:
            self.set_status_main(f"Measured radius {r:.1f}pt looks wrong; try again")
            self.pending_hover = []
            return
        self.set_brush_size(key, 2 * r)
        self.set_status_main(f"{key.split('_')[0].title() if key != 'brush_pt' else 'Fine'} brush {2 * r:.1f}pt. "
                             "Clear the canvas, then Preview.")
        self.next_hover()

    def speed_test(self):
        try:
            self.read_settings()
            if not self.cfg["canvas_tl"] or not self.cfg["canvas_br"]:
                raise ValueError("Calibrate canvas corners first")
        except Exception as e:
            self.error(str(e))
            return
        self.launch(lambda d: d.speed_test())

    def start(self):
        left = self.time_left.text().strip()
        timer_set = bool(self.cfg.get("timer_tl") and self.cfg.get("timer_br"))
        if not left and timer_set:
            secs = read_timer(self.cfg["timer_tl"], self.cfg["timer_br"])
            if secs is None:
                self.error("Couldn't read the timer. Check the timer box calibration and Screen Recording "
                           "permission, or type the time left.")
                return
            left = str(secs)
            self.set_status_main(f"Timer reads {secs // 60}:{secs % 60:02d}")
        try:
            self.read_settings()
            self.missing_calibration()
            tl, br = self.cfg["canvas_tl"], self.cfg["canvas_br"]
            if br[0] <= tl[0] or br[1] <= tl[1]:
                raise ValueError("Bottom-right must be below and right of top-left")
            img = self.source_image()
            img.load()
            if left:  # game time ("95" or "1:35")
                total = float(left.replace("s", "").split(":")[-1]) + (
                    60 * float(left.split(":")[0]) if ":" in left else 0)
            else:  # no clock given: the drawing time limit is the deadline
                total = float(self.cfg["time_limit"]) + (2.0 if self.cfg.get("focus_spot") else 6.5)
        except Exception as e:
            self.error(str(e))
            return
        clicked = time.time()
        cfg = dict(self.cfg)
        state = {}
        rate = float(cfg.get("game_rate") or 1.0) if left else 1.0  # game seconds per real second
        tune = self.autotune.isChecked()
        exact = not left and not tune and not self.can_repick(img)  # use these exact settings

        lead = 0.5 if self.cfg.get("focus_spot") else 5.0

        def left_for_drawing():
            # planning runs during the countdown; drawing starts when both are done (+~1.5s clicks)
            return total / rate - max(lead, time.time() - clicked) - 1.5

        curve = cfg.get("brush_curve")
        early = bool(curve and len(curve) >= 2 and ((cfg.get("brush_min") and cfg.get("brush_max")) or has_arrows(cfg)))

        def plan_full(note, budget, base):
            say = lambda m: note.update(msg=m)
            if tune:
                c, plan, est, desc = auto_tune(img, base, 0.93 * budget, say)
            elif exact:
                plan, est = plan_for(img, base)
                c, desc = base, "your settings"
                if est > budget:  # too much for the time: simplify instead of overrunning
                    c, plan, est, desc = fit_to_time(img, base, 0.93 * budget, say)
            else:
                for _ in range(2):
                    c, plan, est, desc = fit_to_time(img, base, 0.93 * budget, say)
                    if est <= left_for_drawing() or budget <= 5:
                        break
                    budget = left_for_drawing() - (est - left_for_drawing()) - 2  # planning ran long
            return c, plan, est, desc

        def prepare(note):
            budget = left_for_drawing()
            hit = None if exact else pick_preplan(img, cfg, tune, budget)
            if hit:  # planned ahead (Library > Pre-plan): draw right away
                PLAN_METER.reset(1)
                PLAN_METER.tick()
                c, plan, est, desc = hit
                note["planned"] = True
                state["plan"] = plan
                note["msg"] = f"pre-planned: {desc}, ~{est:.0f}s of {budget:.0f}s"
                return c
            PLAN_METER.reset(2 if exact else search_guess(cfg, tune))
            if not early:
                c, plan, est, desc = plan_full(note, budget, cfg)
                note["planned"] = True
                remaining = left_for_drawing()
                state["plan"] = plan
                note["msg"] = (f"{desc}, ~{est:.0f}s of {remaining:.0f}s"
                               + ("" if est <= remaining else " (won't fully fit: will simplify while drawing)"))
                return c
            # start drawing early: a quick coarse plan gives the background (largest color, big
            # brushes or the bucket), drawn while the full plan is made without it
            note["msg"] = "quick plan for the background..."
            d_min, d_max = curve_range(cfg, sorted(curve))
            qc = cfg_with_sizes(cfg, sorted(curve), *sizes_for(max(d_min, min(30.0, d_max)), d_min, d_max))
            qplan, _ = plan_for(img, qc)
            bg = []
            for st in qplan:
                if st[0] != qplan[0][0] or st[1] not in ("bucket", "huge", "big", "medium", "fine"):
                    break
                bg.append(st)
            sizes, sp = brush_sizes(qc), qc["spacing"]
            pre = [(hx, 0.0 if tag == "bucket" else sizes[tag],
                    [[((x + 0.5) * sp, (y + 0.5) * sp) for x, y in p] for p in strokes]) for hx, tag, strokes in bg]
            bg_s = estimate_seconds(bg, qc) if bg else 0.0
            pend = Pending()
            full_cfg = dict(cfg, _predrawn=pre) if pre else cfg

            def work():
                boost_thread()
                try:
                    # the rest can only start once planned: leave out the planning time too
                    wait = max(0.0, (35.0 if tune else 12.0) - bg_s)
                    c, plan, est, desc = plan_full(note, budget - bg_s - wait, full_cfg)
                    pend.result = (c, plan, f"{desc}, ~{est:.0f}s")
                except Exception as e:
                    note["msg"] = f"planning failed ({e}): using the quick plan"
                    pend.result = (qc, qplan[len(bg):], "quick plan")
                finally:
                    note["planned"] = True
                    pend.ready.set()

            threading.Thread(target=work, daemon=True).start()
            state["plan"], state["pending"] = bg, pend
            note["msg"] = (f"drawing the background ({len(bg)} step{'s' * (len(bg) != 1)}, ~{bg_s:.0f}s) "
                           "while planning the rest" if bg else "planning while starting")
            return qc

        self.launch(lambda d: self.draw_and_correct(d, img, state["plan"], clicked + total / rate,
                                                    state.get("pending")), prepare,
                    stages=self.draw_stages(self.cfg))

    @staticmethod
    def draw_stages(cfg):
        stages = ["plan", "countdown", "fill", "edges"] + (["lines"] if cfg.get("line_art", True) else [])
        if int(cfg.get("correct_passes", 0)) > 0:
            stages.append("correct")
        return stages

    def draw_and_correct(self, d, img, plan, deadline=None, pending=None):
        """Draw, then screenshot-check and repaint mistakes while time allows. With a deadline,
        spare time is spent on detail: a richer edge pass if the fills finish early, then
        correction passes until the time is up. pending: the full plan still being made
        (plan is then just the background, drawn meanwhile)."""
        passes = int(d.cfg.get("correct_passes", 0))
        if deadline and passes:
            passes = 50  # keep correcting until time's up / nothing left to fix
        box, th = {}, None

        def setup(cfg, full):
            nonlocal th
            if passes:  # build the comparison target alongside the drawing
                th = threading.Thread(target=lambda: box.update(t=target_for(img, cfg)), daemon=True)
                th.start()
            if deadline:
                d._upgrade = self.edge_upgrader(img, cfg, full, reserve=6.0 if passes else 1.0)
                d._fallback = self.simpler_plans(img, cfg)

        if pending is not None:
            d._pending, d._on_swap, d._fallback = pending, setup, None
        else:
            setup(d.cfg, plan)
        d.run(plan, deadline=deadline, upgrade=getattr(d, "_upgrade", None))
        self.last_drawn = (img, dict(d.cfg))
        if passes and th is not None:
            th.join()
            d.correct(box.get("t"), passes, deadline)

    @staticmethod
    def simpler_plans(img, cfg):
        """Plan simpler versions of the picture in the background (bigger brushes, fewer colors,
        no edges/line art). Returns fallback(left_s, speed) -> (cfg, plan, description) picking
        the most detailed one that finishes in time at the measured pace."""
        base = dict(cfg, line_art=False, tiny_brush_pct=0)
        curve = cfg.get("brush_curve")
        variants = []
        cols = int(cfg["colors"])
        if curve and len(curve) >= 2 and cfg.get("brush_min") and cfg.get("brush_max"):
            cv = sorted(curve)
            d_min, d_max = min(d for _, d in cv), max(d for _, d in cv)
            fine = cfg.get("brush_pt") or cfg["spacing"] / 0.9
            for mult, ncol, desc in ((1.7, max(6, cols - 4), "bigger brushes"),
                                     (2.8, max(5, int(cols * 0.6)), "much bigger brushes, fewer colors"),
                                     (4.5, 5, "simplest")):
                f = min(d_max, max(d_min, fine * mult))
                c = cfg_with_sizes(dict(base, colors=ncol), cv, *sizes_for(round(f, 1), d_min, d_max))
                c["tiny_brush_pct"], c["tiny_brush_pt"] = 0, None
                variants.append((desc, c))
        else:
            variants = [("fewer colors", dict(base, colors=max(6, cols - 4))),
                        ("half the colors", dict(base, colors=max(4, cols // 2), smooth=float(cfg["smooth"]) + 1))]
        futs = [(desc, c, pool().submit(_plan_job, img, c)) for desc, c in variants]

        def fallback(left, speed):
            pick = None
            for desc, c, f in futs:  # most detailed first
                try:
                    plan, est = f.result(timeout=4)
                except Exception:
                    continue
                pick = (c, plan, desc)
                if est * speed <= left * 0.95:
                    return pick
            return pick  # nothing fits: the simplest one gets furthest
        return fallback

    @staticmethod
    def edge_upgrader(img, cfg, plan, reserve):
        """Start planning more detailed edge passes in the background (they're ready by the
        time the fills finish). Returns upgrade(spare_s, current_edge_s, drawer) -> new edge
        steps, or None if none fits / nothing to upgrade."""
        if not isinstance(plan, list) or not any(st[1] == "tiny" for st in plan):
            return None
        ec = float(cfg["edge_contrast"])
        levels = sorted({max(4.0, ec - d) for d in (4, 7, 10)} - {ec})  # lower = more edges
        futs = [(lv, pool().submit(edges_job, img, cfg, lv)) for lv in levels]

        def upgrade(spare, current, drawer):
            import concurrent.futures as cf
            budget = spare - reserve
            if budget <= current * 1.15:  # not meaningfully ahead
                return None
            for lv, f in futs:  # most detailed first
                try:
                    steps = f.result(timeout=0.5)
                except cf.TimeoutError:
                    continue
                if steps and sum(drawer.step_costs(steps)) <= budget:
                    return steps
            return None
        return upgrade

    def correct_now(self):
        last = getattr(self, "last_drawn", None)
        if not last:
            self.error("Draw something first: correction compares the canvas with the last drawing.")
            return
        img, cfg = last
        cfg = dict(cfg, correct_passes=max(1, int(self.cfg.get("correct_passes", 0)) or 1))
        self.launch(lambda d: d.correct(target_for(img, d.cfg), cfg["correct_passes"]), lambda note: cfg,
                    stages=("countdown", "correct"))

    def on_finished(self):
        self.drawing = False
        self.start_btn.setEnabled(True)

    def launch(self, job, prepare=None, stages=("countdown",), planning=False):
        """Run job(drawer) after a 5s countdown. prepare(note) runs alongside the countdown
        and returns the cfg to draw with. planning: a background plan is already running."""
        if self.drawing:
            return
        if self.cfg.get("focus_spot"):
            stages = tuple(st for st in stages if st != "countdown")
        self.progress_panel.begin(stages)
        prog = self.bridge.progress.emit
        self.drawing = True
        self.stop_event.clear()
        self.start_btn.setEnabled(False)
        emit = self.bridge.status.emit

        focus = self.cfg.get("focus_spot")
        wait_s = 0.0 if focus else 5.0

        def worker():
            boost_thread()
            try:
                note, result = {}, {}
                if prepare or planning:
                    prog("plan", -1, "planning...", "")

                    def meter():  # planning bar: candidate plans done / expected, until planned
                        while not note.get("planned") and self.drawing:
                            if PLAN_METER.expected or PLAN_METER.guess:
                                prog("plan", PLAN_METER.frac(), PLAN_METER.text(), "")
                            time.sleep(0.3)
                    threading.Thread(target=meter, daemon=True).start()
                if prepare:  # plan during the countdown instead of before it
                    def run_prepare():
                        try:
                            result["cfg"] = prepare(note)
                        except Exception as e:
                            result["error"] = e
                    th = threading.Thread(target=run_prepare, daemon=True)
                    th.start()
                go_at, shown = time.time() + wait_s, None  # wall clock: background planning can't stretch it
                while (left := go_at - time.time()) > 0:
                    if stop_key_down():
                        raise Aborted("Esc pressed")
                    msg = f"Starting in {math.ceil(left)}... focus the Roblox window (Esc = stop)"
                    msg += f" | {note['msg']}" if note.get("msg") else ""
                    if msg != shown:
                        emit(msg)
                        shown = msg
                    prog("countdown", 1 - left / wait_s, f"{math.ceil(left)}s - focus the Roblox window", "")
                    time.sleep(min(0.05, max(0.0, left)))
                cfg = self.cfg
                if prepare:
                    while th.is_alive():  # planning outlasted the countdown
                        if stop_key_down():
                            raise Aborted("Esc pressed")
                        emit(f"Planning... {note.get('msg', '')}")
                        th.join(0.2)
                    if "error" in result:
                        raise result["error"]
                    cfg = result["cfg"]
                    if note.get("planned"):
                        prog("plan", 1.0, note.get("msg", "done")[:60], "")
                    emit(f"Drawing: {note.get('msg', '')}")
                prog("countdown", 1.0, "done", "")
                if focus:  # bring Roblox to the front and click the safe spot (no countdown)
                    focus_roblox(focus)
                drw = Drawer(cfg, self.stop_event, emit, prog)
                drw.on_timing = self.bridge.timing.emit
                job(drw)
                prog("end", 0, "done", "")
                note["planned"] = True
            except Aborted as e:
                _post(Quartz.kCGEventLeftMouseUp, *mouse_pos())
                emit(f"Aborted: {e}")
                prog("end", 0, f"stopped ({e})", "")
            except Exception as e:
                _post(Quartz.kCGEventLeftMouseUp, *mouse_pos())
                emit(f"Error: {e}")
                prog("end", 0, "error", "")
            finally:
                self.bridge.finished.emit()

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    keep_awake()
    app = QApplication([])
    app.aboutToQuit.connect(lambda: _POOL and _POOL.shutdown(wait=False, cancel_futures=True))
    w = App()
    w.show()
    app.exec()
