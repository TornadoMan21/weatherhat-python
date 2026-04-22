#!/usr/bin/env python3
import math
import pathlib
import select
import subprocess
import threading
import time
import datetime
from datetime import timedelta

import gpiod
import gpiodevice
import st7789
import yaml
from fonts.ttf import ManropeBold as UserFont
from gpiod.line import Bias, Edge
from PIL import Image, ImageDraw, ImageFont

import weatherhat
from weatherhat import history

import os

# Create a unique filename for this run in data subfolder
data_dir = "data"
if not os.path.exists(data_dir):
    os.makedirs(data_dir)

RUN_FILENAME = os.path.join(data_dir, datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".txt")

FPS = 10

BUTTONS = [5, 6, 16, 24]
LABELS = ["A", "B", "X", "Y"]

DISPLAY_WIDTH = 240
DISPLAY_HEIGHT = 240
SPI_SPEED_MHZ = 80

COLOR_WHITE = (255, 255, 255)
COLOR_BLUE = (31, 137, 251)
COLOR_GREEN = (99, 255, 124)
COLOR_YELLOW = (254, 219, 82)
COLOR_RED = (247, 0, 63)
COLOR_BLACK = (0, 0, 0)
COLOR_GREY = (100, 100, 100)

# We can compensate for the heat of the Pi and other environmental conditions using a simple offset.
# Change this number to adjust temperature compensation!
OFFSET = -7.5

# ── Console settings ─────────────────────────────────────────────────────────
# Monospace font — DejaVu Sans Mono ships with Raspberry Pi OS.
# Install if missing:  sudo apt install fonts-dejavu-core
_CONSOLE_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
]
CONSOLE_FONT = next((p for p in _CONSOLE_FONT_CANDIDATES if os.path.exists(p)), None)
CONSOLE_FONT_SIZE = 9
CONSOLE_CHAR_H = 11          # pixel height per line at size 9
CONSOLE_COLS = 40            # characters per line  (240 px / ~6 px per char)
CONSOLE_ROWS = DISPLAY_HEIGHT // CONSOLE_CHAR_H   # ~21 rows

# ── evdev key maps (normal and shifted) ──────────────────────────────────────
_KEYMAP = {
    'KEY_A':'a','KEY_B':'b','KEY_C':'c','KEY_D':'d','KEY_E':'e',
    'KEY_F':'f','KEY_G':'g','KEY_H':'h','KEY_I':'i','KEY_J':'j',
    'KEY_K':'k','KEY_L':'l','KEY_M':'m','KEY_N':'n','KEY_O':'o',
    'KEY_P':'p','KEY_Q':'q','KEY_R':'r','KEY_S':'s','KEY_T':'t',
    'KEY_U':'u','KEY_V':'v','KEY_W':'w','KEY_X':'x','KEY_Y':'y',
    'KEY_Z':'z',
    'KEY_1':'1','KEY_2':'2','KEY_3':'3','KEY_4':'4','KEY_5':'5',
    'KEY_6':'6','KEY_7':'7','KEY_8':'8','KEY_9':'9','KEY_0':'0',
    'KEY_SPACE':' ','KEY_MINUS':'-','KEY_EQUAL':'=',
    'KEY_LEFTBRACE':'[','KEY_RIGHTBRACE':']',
    'KEY_SEMICOLON':';','KEY_APOSTROPHE':"'",
    'KEY_GRAVE':'`','KEY_BACKSLASH':'\\',
    'KEY_COMMA':',','KEY_DOT':'.','KEY_SLASH':'/',
    'KEY_TAB':'\t',
}
_KEYMAP_SHIFT = {
    'KEY_A':'A','KEY_B':'B','KEY_C':'C','KEY_D':'D','KEY_E':'E',
    'KEY_F':'F','KEY_G':'G','KEY_H':'H','KEY_I':'I','KEY_J':'J',
    'KEY_K':'K','KEY_L':'L','KEY_M':'M','KEY_N':'N','KEY_O':'O',
    'KEY_P':'P','KEY_Q':'Q','KEY_R':'R','KEY_S':'S','KEY_T':'T',
    'KEY_U':'U','KEY_V':'V','KEY_W':'W','KEY_X':'X','KEY_Y':'Y',
    'KEY_Z':'Z',
    'KEY_1':'!','KEY_2':'@','KEY_3':'#','KEY_4':'$','KEY_5':'%',
    'KEY_6':'^','KEY_7':'&','KEY_8':'*','KEY_9':'(','KEY_0':')',
    'KEY_SPACE':' ','KEY_MINUS':'_','KEY_EQUAL':'+',
    'KEY_LEFTBRACE':'{','KEY_RIGHTBRACE':'}',
    'KEY_SEMICOLON':':','KEY_APOSTROPHE':'"',
    'KEY_GRAVE':'~','KEY_BACKSLASH':'|',
    'KEY_COMMA':'<','KEY_DOT':'>','KEY_SLASH':'?',
}


def _cvt(entries, func):
    """Return a list of history-like objects with values converted by func.
    Used to convert metric history entries to imperial for display."""
    class _E:
        __slots__ = ("value",)
        def __init__(self, v): self.value = v
    return [_E(func(e.value)) for e in entries]


class View:
    def __init__(self, image):
        self._image = image
        self._draw = ImageDraw.Draw(image)

        self.font_large = ImageFont.truetype(UserFont, 80)
        self.font = ImageFont.truetype(UserFont, 50)
        self.font_medium = ImageFont.truetype(UserFont, 44)
        self.font_small = ImageFont.truetype(UserFont, 28)

    @property
    def canvas_width(self):
        return self._image.size[0]

    @property
    def canvas_height(self):
        return self._image.size[1]

    def button_a(self):
        return False

    def button_b(self):
        return False

    def button_x(self):
        return False

    def button_y(self):
        return False

    def update(self):
        pass

    def render(self):
        self.clear()

    def clear(self):
        self._draw.rectangle((0, 0, self.canvas_width, self.canvas_height), (0, 0, 0))


class SensorView(View):
    title = ""
    GRAPH_BAR_WIDTH = 20

    def __init__(self, image, sensordata, settings=None):
        View.__init__(self, image)
        self._data = sensordata
        self._settings = settings

    def blend(self, a, b, factor):
        blend_b = factor
        blend_a = 1.0 - factor
        return tuple([int((a[i] * blend_a) + (b[i] * blend_b)) for i in range(3)])

    def heading(self, data, units):
        if data < 100:
            data = "{:0.1f}".format(data)
        else:
            data = "{:0.0f}".format(data)

        _, _, tw, th = self._draw.textbbox((0, 0), data, self.font_large)

        self._draw.text(
            (0, 32),
            data,
            font=self.font_large,
            fill=COLOR_WHITE,
            anchor="lm"
        )

        self._draw.text(
            (tw, 64),
            units,
            font=self.font_medium,
            fill=COLOR_WHITE,
            anchor="lb"
        )

    def footer(self, label):
        self._draw.text((int(self.canvas_width / 2), self.canvas_height - 30), label, font=self.font_medium, fill=COLOR_GREY, anchor="mm")

    def graph(self, values, graph_x=0, graph_y=0, width=None, height=None, vmin=0, vmax=1.0, bar_width=2, colors=None):
        if not len(values):
            return

        if width is None:
            width = self.canvas_width

        if height is None:
            height = self.canvas_height

        if colors is None:
            #         Blue          Teal           Green        Yellow         Red
            colors = [(0, 0, 255), (0, 255, 255), (0, 255, 0), (255, 255, 0), (255, 0, 0)]

        vrange = vmax - vmin
        vstep = float(height) / vrange

        if vmin >= 0:
            midpoint_y = height
        else:
            midpoint_y = vmax * vstep
            self._draw.line((graph_x, graph_y + midpoint_y, graph_x + width, graph_y + midpoint_y), fill=COLOR_GREY)

        max_values = int(width / bar_width)

        values = [entry.value for entry in values[-max_values:]]

        for i, v in enumerate(values):
            v = min(vmax, max(vmin, v))

            offset_y = graph_y

            if vmin < 0:
                bar_height = midpoint_y * float(v) / float(vmax)
            else:
                bar_height = midpoint_y * float(v - vmin) / float(vmax - vmin)

            if v < 0:
                offset_y += midpoint_y
                bar_height = (height - midpoint_y) * float(abs(v)) / abs(vmin)

            color = float(v - vmin) / float(vmax - vmin) * (len(colors) - 1)
            color_idx = int(color)      # The integer part of color becomes our index into the colors array
            blend = color - color_idx   # The fractional part forms the blend amount between the two colours
            bar_color = colors[color_idx]
            if color_idx < len(colors) - 1:
                bar_color = self.blend(colors[color_idx], colors[color_idx + 1], blend)
                bar_color = bar_color

            x = (i * bar_width)

            if v < 0:
                self._draw.rectangle((
                    graph_x + x, offset_y,
                    graph_x + x + int(bar_width / 2), offset_y + bar_height
                ), fill=bar_color)
            else:
                self._draw.rectangle((
                    graph_x + x, offset_y + midpoint_y - bar_height,
                    graph_x + x + int(bar_width / 2), offset_y + midpoint_y
                ), fill=bar_color)


class MainView(SensorView):
    """Main Overview.

    Displays weather summary and navigation hints.

    """

    title = "Overview"

    def draw_info(self, x, y, color, label, data, desc, right=False, vmin=0, vmax=20, graph_mode=False):
        w = 200
        o_x = 0 if right else 40

        if graph_mode:
            vmax = max(vmax, max([h.value for h in data]))  # auto ranging?
            self.graph(data, x + o_x + 30, y + 20, 180, 64, vmin=vmin, vmax=vmax, bar_width=20, colors=[color])
        else:
            if isinstance(data, list):
                if len(data) > 0:
                    data = data[-1].value
                else:
                    data = 0

            if data < 100:
                data = "{:0.1f}".format(data)
            else:
                data = "{:0.0f}".format(data)

            self._draw.text(
                (x + w + o_x, y + 20 + 32),  # Position is the right, center of the text
                data,
                font=self.font_large,
                fill=color,
                anchor="rm"  # Using "rm" stops text jumping vertically
            )

        self._draw.text(
            (x + w + o_x, y + 90 + 40),
            desc,
            font=self.font,
            fill=COLOR_WHITE,
            anchor="rb"
        )
        label_img = Image.new("RGB", (130, 40))
        label_draw = ImageDraw.Draw(label_img)
        label_draw.text((0, 40) if right else (0, 0), label, font=self.font_medium, fill=COLOR_GREY, anchor="lb" if right else "lt")
        label_img = label_img.rotate(90, expand=True)
        if right:
            self._image.paste(label_img, (x + w, y))
        else:
            self._image.paste(label_img, (x, y))

    def render(self):
        SensorView.render(self)
        self.render_graphs()

    def render_graphs(self, graph_mode=False):
        self.draw_info(0, 0, (20, 20, 220), "RAIN", self._data.my_rain_total.total() * 0.0394, "in", vmax=self._settings.maximum_rain_mm, graph_mode=graph_mode)
        self.draw_info(0, 150, (20, 20, 220), "PRES",
                       self._data.pressure.history(), "hPa",
                       vmin=self._settings.minimum_pressure,
                       vmax=self._settings.maximum_pressure,
                       graph_mode=graph_mode)
        self.draw_info(0, 300, (20, 100, 220), "WIND", self._data.wind_speed.history(), "mph", graph_mode=graph_mode)

        x = int(self.canvas_width / 2)

        self.draw_info(x, 0, (10, 10, 220), "HUM", self._data.relative_humidity.history(), "%rh", right=True, graph_mode=graph_mode)
        self.draw_info(x, 150, (220, 100, 20), "LIGHT", self._data.lux.history(), "lux", right=True, graph_mode=graph_mode)
        self.draw_info(x, 300, (220, 20, 220), "GUST", self._data.wind_speed.gust_mph(), "mph", right=True, graph_mode=graph_mode)


class MainViewGraph(MainView):
    title = "Overview: Graphs"

    def render(self):
        SensorView.render(self)
        self.render_graphs(graph_mode=True)


class WindDirectionView(SensorView):
    """Wind Direction."""

    title = "Wind"
    metric = "mph"  # was: m/sec — wind_speed is already stored in mph

    def __init__(self, image, sensordata, settings=None):
        SensorView.__init__(self, image, sensordata, settings)

    def render(self):
        SensorView.render(self)
        ox = self.canvas_width / 2
        oy = 40 + ((self.canvas_height - 60) / 2)
        needle = self._data.needle
        speed_mph = self._data.wind_speed.average(60)  # already in mph
        compass_direction = self._data.wind_direction.average_compass()

        radius = 80
        speed_max = 9.84  # mph (= 4.4 m/s * 2.23694) — full-arrow threshold
        speed = min(speed_mph, speed_max)
        speed /= float(speed_max)

        arrow_radius_min = 20
        arrow_radius_max = 60
        arrow_radius = (speed * (arrow_radius_max - arrow_radius_min)) + arrow_radius_min
        arrow_angle = math.radians(130)

        tx, ty = ox + math.sin(needle) * (radius - arrow_radius), oy - math.cos(needle) * (radius - arrow_radius)
        ax, ay = ox + math.sin(needle) * (radius - arrow_radius), oy - math.cos(needle) * (radius - arrow_radius)

        arrow_xy_a = ax + math.sin(needle - arrow_angle) * arrow_radius, ay - math.cos(needle - arrow_angle) * arrow_radius
        arrow_xy_b = ax + math.sin(needle) * arrow_radius, ay - math.cos(needle) * arrow_radius
        arrow_xy_c = ax + math.sin(needle + arrow_angle) * arrow_radius, ay - math.cos(needle + arrow_angle) * arrow_radius

        # Compass red end
        self._draw.line((
            ox,
            oy,
            tx,
            ty
        ), (255, 0, 0), 5)

        self._draw.polygon([arrow_xy_a, arrow_xy_b, arrow_xy_c], fill=(255, 0, 0))

        if self._settings.wind_trails:
            trails = 40
            trail_length = len(self._data.needle_trail)
            for i, p in enumerate(self._data.needle_trail):
                r = radius + trails - (float(i) / trail_length * trails)
                x = ox + math.sin(p) * r
                y = oy - math.cos(p) * r

                self._draw.ellipse((x - 2, y - 2, x + 2, y + 2), (int(255 / trail_length * i), 0, 0))

        radius += 60
        for direction, name in weatherhat.wind_degrees_to_cardinal.items():
            p = math.radians(direction)
            x = ox + math.sin(p) * radius
            y = oy - math.cos(p) * radius

            name = "".join([word[0] for word in name.split(" ")])
            _, _, tw, th = self._draw.textbbox((0, 0), name, font=self.font_small)
            x -= tw / 2
            y -= th / 2
            self._draw.text((x, y), name, font=self.font_small, fill=COLOR_GREY)

        self.heading(speed_mph, self.metric)
        self.footer(self.title.upper())

        direction_text = "".join([word[0] for word in compass_direction.split(" ")])

        self._draw.text(
            (self.canvas_width, 32),
            direction_text,
            font=self.font_large,
            fill=COLOR_WHITE,
            anchor="rm"
        )


class WindSpeedView(SensorView):
    """Wind Speed."""

    title = "WIND"
    metric = "mph"  # was: m/s — wind_speed is already stored in mph

    def render(self):
        SensorView.render(self)
        self.heading(
            self._data.wind_speed.latest().value,
            self.metric
        )
        self.footer(self.title.upper())

        self.graph(
            self._data.wind_speed.history(),
            graph_x=4,
            graph_y=70,
            width=self.canvas_width,
            height=self.canvas_height - 130,
            vmin=self._settings.minimum_wind_ms * 2.23694,  # convert m/s setting to mph
            vmax=self._settings.maximum_wind_ms * 2.23694,  # convert m/s setting to mph
            bar_width=self.GRAPH_BAR_WIDTH
        )


class RainView(SensorView):
    """Rain."""

    title = "Rain"
    metric = "in/hr"  # was: mm/s

    def render(self):
        SensorView.render(self)
        self.heading(
            self._data.rain_mm_sec.latest().value * 141.73,  # mm/s -> in/hr
            self.metric
        )
        self.footer(self.title.upper())

        self.graph(
            _cvt(self._data.rain_mm_sec.history(), lambda v: v * 141.73),  # mm/s -> in/hr
            graph_x=4,
            graph_y=70,
            width=self.canvas_width,
            height=self.canvas_height - 130,
            vmin=self._settings.minimum_rain_mm * 141.73,
            vmax=self._settings.maximum_rain_mm * 141.73,
            bar_width=self.GRAPH_BAR_WIDTH
        )


class TemperatureView(SensorView):
    """Temperature."""

    title = "TEMP"
    metric = "\u00b0F"  # was: corrupted °C encoding

    def render(self):
        SensorView.render(self)
        self.heading(
            self._data.temperature.latest().value * 9 / 5 + 32,  # C -> F
            self.metric
        )
        self.footer(self.title.upper())

        self.graph(
            _cvt(self._data.temperature.history(), lambda v: v * 9 / 5 + 32),  # C -> F
            graph_x=4,
            graph_y=70,
            width=self.canvas_width,
            height=self.canvas_height - 130,
            vmin=self._settings.minimum_temperature * 9 / 5 + 32,  # 14 °F
            vmax=self._settings.maximum_temperature * 9 / 5 + 32,  # 104 °F
            bar_width=self.GRAPH_BAR_WIDTH
        )


class LightView(SensorView):
    """Light."""

    title = "Light"
    metric = "lux"  # no imperial equivalent — unchanged

    def render(self):
        SensorView.render(self)
        self.heading(
            self._data.lux.latest().value,
            self.metric
        )
        self.footer(self.title.upper())

        self.graph(
            self._data.lux.history(int(self.canvas_width / self.GRAPH_BAR_WIDTH)),
            graph_x=4,
            graph_y=70,
            width=self.canvas_width,
            height=self.canvas_height - 130,
            vmin=self._settings.minimum_lux,
            vmax=self._settings.maximum_lux,
            bar_width=self.GRAPH_BAR_WIDTH
        )


class PressureView(SensorView):
    """Pressure."""

    title = "PRESSURE"
    metric = "hPa"

    def render(self):
        SensorView.render(self)
        self.heading(
            self._data.pressure.latest().value,
            self.metric
        )
        self.footer(self.title.upper())

        self.graph(
            self._data.pressure.history(int(self.canvas_width / self.GRAPH_BAR_WIDTH)),
            graph_x=4,
            graph_y=70,
            width=self.canvas_width,
            height=self.canvas_height - 130,
            vmin=self._settings.minimum_pressure,
            vmax=self._settings.maximum_pressure,
            bar_width=self.GRAPH_BAR_WIDTH
        )


class HumidityView(SensorView):
    """Humidity."""

    title = "Humidity"
    metric = "%rh"  # percentage — no conversion needed

    def render(self):
        SensorView.render(self)
        self.heading(
            self._data.relative_humidity.latest().value,
            self.metric
        )
        self.footer(self.title.upper())

        self.graph(
            self._data.relative_humidity.history(int(self.canvas_width / self.GRAPH_BAR_WIDTH)),
            graph_x=4,
            graph_y=70,
            width=self.canvas_width,
            height=self.canvas_height - 130,
            vmin=0,
            vmax=100,
            bar_width=self.GRAPH_BAR_WIDTH
        )


class ConsoleView(View):
    """Tiny interactive shell rendered on the 240x240 display.

    Prerequisites on the Pi:
        sudo apt install fonts-dejavu-core
        pip install evdev
        sudo usermod -a -G input $USER   # then log out and back in
    """

    PROMPT = "> "
    HISTORY_SIZE = 20

    def __init__(self, image):
        View.__init__(self, image)
        self._cfont = (ImageFont.truetype(CONSOLE_FONT, CONSOLE_FONT_SIZE)
                       if CONSOLE_FONT else ImageFont.load_default())
        self._lines    = ["Weather Console  [Y] to exit", ""]
        self._input    = ""
        self._history  = []
        self._hist_idx = -1
        self._shift    = False
        self._lock     = threading.Lock()
        self._running  = True
        threading.Thread(target=self._keyboard_reader, daemon=True).start()

    # ── keyboard thread ───────────────────────────────────────────────────────
    def _keyboard_reader(self):
        try:
            import evdev
        except ImportError:
            with self._lock:
                self._lines += ["[evdev not installed]", "pip install evdev"]
            return
        try:
            devices = [evdev.InputDevice(p) for p in evdev.list_devices()]
            kbds = [d for d in devices if evdev.ecodes.EV_KEY in d.capabilities()]
            if not kbds:
                with self._lock:
                    self._lines.append("[no keyboard detected]")
                return
            kbd = kbds[0]
            with self._lock:
                self._lines.append(f"kbd: {kbd.name[:34]}")
            for event in kbd.read_loop():
                if not self._running:
                    break
                if event.type != evdev.ecodes.EV_KEY:
                    continue
                key = evdev.ecodes.KEY.get(event.code, "")
                if not isinstance(key, str):
                    key = key[0] if key else ""
                # track shift state on both up and down
                if key in ('KEY_LEFTSHIFT', 'KEY_RIGHTSHIFT'):
                    self._shift = (event.value == 1)
                    continue
                if event.value in (1, 2):    # key down or repeat
                    self._handle_key(key)
        except Exception as e:
            with self._lock:
                self._lines.append(f"kbd error: {e}")

    # ── key handler (called from keyboard thread) ─────────────────────────────
    def _handle_key(self, key):
        with self._lock:
            if key == 'KEY_ENTER':
                cmd = self._input.strip()
                self._lines.append(f"{self.PROMPT}{cmd}")
                self._input    = ""
                self._hist_idx = -1
                if cmd:
                    if not self._history or self._history[-1] != cmd:
                        self._history.append(cmd)
                        if len(self._history) > self.HISTORY_SIZE:
                            self._history.pop(0)
                    threading.Thread(target=self._run,
                                     args=(cmd,), daemon=True).start()
            elif key == 'KEY_BACKSPACE':
                self._input    = self._input[:-1]
                self._hist_idx = -1
            elif key == 'KEY_UP':
                if self._history:
                    self._hist_idx = (len(self._history) - 1
                                      if self._hist_idx < 0
                                      else max(0, self._hist_idx - 1))
                    self._input = self._history[self._hist_idx]
            elif key == 'KEY_DOWN':
                if self._hist_idx >= 0:
                    self._hist_idx += 1
                    if self._hist_idx >= len(self._history):
                        self._hist_idx = -1
                        self._input    = ""
                    else:
                        self._input = self._history[self._hist_idx]
            else:
                km   = _KEYMAP_SHIFT if self._shift else _KEYMAP
                char = km.get(key)
                if char:
                    self._input += char

    # ── command runner (background thread) ───────────────────────────────────
    def _run(self, cmd):
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=15)
            output = (result.stdout + result.stderr).rstrip()
        except subprocess.TimeoutExpired:
            output = "[timeout after 15s]"
        except Exception as e:
            output = f"[error: {e}]"
        with self._lock:
            for raw in (output.split('\n') if output else []):
                while len(raw) > CONSOLE_COLS:
                    self._lines.append(raw[:CONSOLE_COLS])
                    raw = raw[CONSOLE_COLS:]
                self._lines.append(raw)
            if len(self._lines) > 500:
                self._lines = self._lines[-200:]

    def stop(self):
        self._running = False

    # ── render ────────────────────────────────────────────────────────────────
    def render(self):
        self.clear()
        with self._lock:
            output_rows = CONSOLE_ROWS - 1
            for i, line in enumerate(self._lines[-output_rows:]):
                self._draw.text(
                    (0, i * CONSOLE_CHAR_H),
                    line[:CONSOLE_COLS],
                    font=self._cfont,
                    fill=COLOR_WHITE)
            cursor    = "_" if int(time.time() * 2) % 2 == 0 else " "
            input_str = f"{self.PROMPT}{self._input}{cursor}"
            self._draw.text(
                (0, output_rows * CONSOLE_CHAR_H),
                input_str[:CONSOLE_COLS],
                font=self._cfont,
                fill=COLOR_GREEN)


class ViewController:
    def __init__(self, views, image):
        self.views = views
        self._image = image
        self._current_view = 0
        self._current_subview = 0
        self._console = None          # ConsoleView instance when active

        config = {}
        for pin in BUTTONS:
            config[pin] = gpiod.LineSettings(
                edge_detection=Edge.FALLING,
                bias=Bias.PULL_UP,
                debounce_period=timedelta(milliseconds=20)
            )

        chip = gpiodevice.find_chip_by_platform()
        self._buttons = chip.request_lines(consumer="LTR559", config=config)
        self._poll = select.poll()
        self._poll.register(self._buttons.fd, select.POLLIN)

    def handle_button(self, pin):
        index = BUTTONS.index(pin)
        label = LABELS[index]

        if label == "A":  # Select View
            self.button_a()

        if label == "B":
            self.button_b()

        if label == "X":
            self.button_x()

        if label == "Y":
            self.button_y()

    @property
    def home(self):
        return self._current_view == 0 and self._current_subview == 0

    def next_subview(self):
        view = self.views[self._current_view]
        if isinstance(view, tuple):
            self._current_subview += 1
            self._current_subview %= len(view)

    def next_view(self):
        self._current_subview = 0
        self._current_view += 1
        self._current_view %= len(self.views)

    def prev_view(self):
        self._current_subview = 0
        self._current_view -= 1
        self._current_view %= len(self.views)

    def get_current_view(self):
        view = self.views[self._current_view]
        if isinstance(view, tuple):
            view = view[self._current_subview]

        return view

    @property
    def view(self):
        return self.get_current_view()

    def update(self):
        if self._poll.poll(10):
            for event in self._buttons.read_edge_events():
                self.handle_button(event.line_offset)
        if self._console is None:
            self.view.update()

    def render(self):
        if self._console is not None:
            self._console.render()
        else:
            self.view.render()

    def button_a(self):
        if not self.view.button_a():
            self.next_view()

    def button_b(self):
        self.view.button_b()

    def button_x(self):
        if not self.view.button_x():
            self.next_subview()
            return True
        return True

    def button_y(self):
        if self._console is None:
            self._console = ConsoleView(self._image)
        else:
            self._console.stop()
            self._console = None


class Config:
    """Class to hold weather UI settings."""
    def __init__(self, settings_file="settings.yml"):
        self._file = pathlib.Path(settings_file)

        self._last_save = None

        # Wind Settings
        self.wind_trails = True

        # BME280 Settings
        self.minimum_temperature = -10
        self.maximum_temperature = 40

        self.minimum_pressure = 1000
        self.maximum_pressure = 1100

        self.minimum_lux = 100
        self.maximum_lux = 1000

        self.minimum_rain_mm = 0
        self.maximum_rain_mm = 10

        self.minimum_wind_ms = 0
        self.maximum_wind_ms = 40

        self.load()

    def load(self):
        if not self._file.is_file():
            return False

        try:
            self._config = yaml.safe_load(open(self._file))
        except yaml.parser.ParserError as e:
            raise yaml.parser.ParserError(
                "Error parsing settings file: {} ({})".format(self._file, e)
            )

    @property
    def _config(self):
        options = {}
        for k, v in self.__dict__.items():
            if not k.startswith("_"):
                options[k] = v
        return options

    @_config.setter
    def _config(self, config):
        for k, v in self.__dict__.items():
            if k in config:
                setattr(self, k, config[k])


class SensorData:
    AVERAGE_SAMPLES = 120
    WIND_DIRECTION_AVERAGE_SAMPLES = 60
    COMPASS_TRAIL_SIZE = 120

    def __init__(self):
        self.sensor = weatherhat.WeatherHAT()

        self.temperature = history.History()

        self.pressure = history.History()

        self.humidity = history.History()
        self.relative_humidity = history.History()
        self.dewpoint = history.History()

        self.lux = history.History()

        self.wind_speed = history.WindSpeedHistory()
        self.wind_direction = history.WindDirectionHistory()

        self.rain_mm_sec = history.History()
        self.rain_total = 0
        self.my_rain_total = history.History()

        self.rain_total

        # Track previous average values to give the compass a trail
        self.needle_trail = []

    def update(self, interval=5.0):
        self.sensor.temperature_offset = OFFSET
        self.sensor.update(interval)

        self.temperature.append(self.sensor.temperature)

        self.pressure.append(self.sensor.pressure)

        self.humidity.append(self.sensor.humidity)
        self.relative_humidity.append(self.sensor.relative_humidity)
        self.dewpoint.append(self.sensor.dewpoint)

        self.lux.append(self.sensor.lux)

        if self.sensor.updated_wind_rain:
            self.rain_total = self.sensor.rain_total
            self.my_rain_total.append(self.sensor.rain_total)

        self.wind_speed.append(self.sensor.wind_speed * 2.23694)
        self.wind_direction.append(self.sensor.wind_direction)

        self.rain_mm_sec.append(self.sensor.rain)

        self.needle = math.radians(self.wind_direction.average(self.WIND_DIRECTION_AVERAGE_SAMPLES))
        self.needle_trail.append(self.needle)
        self.needle_trail = self.needle_trail[-self.COMPASS_TRAIL_SIZE:]


def save_weather_data(temp_c, pressure_hpa, rain_total, wind_speed, wind_gust):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")

    temp_f = (temp_c * 9 / 5) + 32
    rain_inches = rain_total * 0.0393701

    line = (f"{timestamp}, Temp_F: {temp_f:.2f}, Pressure_hPa: {pressure_hpa:.2f}, "
            f"Rain_in: {rain_inches:.3f}, Wind_mph: {wind_speed:.2f}, Gust_mph: {wind_gust:.2f}\n")
    with open(RUN_FILENAME, "a") as f:
        f.write(line)


def main():
    display = st7789.ST7789(
        rotation=90,
        port=0,
        cs=1,
        dc=9,
        backlight=12,
        spi_speed_hz=SPI_SPEED_MHZ * 1000 * 1000
    )
    image = Image.new("RGBA", (DISPLAY_WIDTH * 2, DISPLAY_HEIGHT * 2), color=(255, 255, 255))
    sensordata = SensorData()
    settings = Config()
    viewcontroller = ViewController(
        (
            (
                MainView(image, sensordata, settings),
                MainViewGraph(image, sensordata, settings)
            ),
            (
                WindDirectionView(image, sensordata, settings),
                WindSpeedView(image, sensordata, settings)
            ),
            RainView(image, sensordata, settings),
            LightView(image, sensordata, settings),
            (
                TemperatureView(image, sensordata, settings),
                PressureView(image, sensordata, settings),
                HumidityView(image, sensordata, settings)
            ),
        ),
        image
    )

    while True:
        sensordata.update(interval=5.0)
        viewcontroller.update()
        viewcontroller.render()
        display.display(image.resize((DISPLAY_WIDTH, DISPLAY_HEIGHT)).convert("RGB"))
        # Save weather data to file
        temp_c = sensordata.temperature.latest().value
        pressure = sensordata.pressure.latest().value
        rain_total = sensordata.rain_total
        wind_speed = sensordata.wind_speed.latest().value
        wind_gust = sensordata.wind_speed.gust_mph()
        save_weather_data(temp_c, pressure, rain_total, wind_speed, wind_gust)
        time.sleep(1.0 / FPS)


if __name__ == "__main__":
    main()
