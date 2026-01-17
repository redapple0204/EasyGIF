from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from mss import mss
except Exception:  # pragma: no cover
    mss = None

try:
    from PIL import Image, ImageTk
except Exception:  # pragma: no cover
    Image = None
    ImageTk = None


def _enable_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
            return
        except Exception:
            pass
        try:
            shcore = ctypes.windll.shcore
            shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
            return
        except Exception:
            pass
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass


def _format_geometry(width: int, height: int, left: int, top: int) -> str:
    # Always anchor to the virtual screen origin (top-left). In Tk geometry strings,
    # a leading '-' means "from right/bottom edge", so we use "+-N" for negatives.
    return f"{int(width)}x{int(height)}+{int(left)}+{int(top)}"


def _format_position(left: int, top: int) -> str:
    return f"+{int(left)}+{int(top)}"


def _rects_intersect(a: Region, b: Region) -> bool:
    ax1, ay1 = a.left, a.top
    ax2, ay2 = a.left + a.width, a.top + a.height
    bx1, by1 = b.left, b.top
    bx2, by2 = b.left + b.width, b.top + b.height
    return (ax1 < bx2) and (ax2 > bx1) and (ay1 < by2) and (ay2 > by1)

def _get_virtual_screen_region() -> Region:
    if os.name == "nt":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            SM_XVIRTUALSCREEN = 76
            SM_YVIRTUALSCREEN = 77
            SM_CXVIRTUALSCREEN = 78
            SM_CYVIRTUALSCREEN = 79
            left = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
            top = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
            width = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
            height = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
            if width > 0 and height > 0:
                return Region(left, top, width, height)
        except Exception:
            pass
    return Region(0, 0, 0, 0)


_enable_windows_dpi_awareness()


def _get_cursor_pos(widget: tk.Misc | None = None, event: tk.Event | None = None) -> tuple[int, int]:
    if os.name == "nt":
        try:
            import ctypes

            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            pt = POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                return int(pt.x), int(pt.y)
        except Exception:
            pass

    try:
        if widget is not None:
            return int(widget.winfo_pointerx()), int(widget.winfo_pointery())
    except Exception:
        pass

    if event is not None:
        try:
            return int(getattr(event, "x_root", 0)), int(getattr(event, "y_root", 0))
        except Exception:
            pass

    return (0, 0)


def _set_window_clickthrough(widget: tk.Toplevel, enabled: bool) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020

        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
        if not hwnd:
            hwnd = widget.winfo_id()
        exstyle = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enabled:
            exstyle |= WS_EX_LAYERED | WS_EX_TRANSPARENT
        else:
            exstyle &= ~WS_EX_TRANSPARENT
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, exstyle)
    except Exception:
        pass


@dataclass
class Region:
    left: int
    top: int
    width: int
    height: int

    def clamp_min_size(self, min_size: int = 16) -> "Region":
        width = max(min_size, int(self.width))
        height = max(min_size, int(self.height))
        return Region(int(self.left), int(self.top), width, height)


class RegionSelector(tk.Toplevel):
    def __init__(self, master: tk.Tk):
        super().__init__(master)
        self.title("选择录制区域")
        self.configure(bg="black")
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.25)
        except tk.TclError:
            pass

        self._done = threading.Event()
        self._region: Region | None = None
        self._start: tuple[int, int] | None = None
        self._start_local: tuple[int, int] | None = None
        self._rect_id: int | None = None

        self.overrideredirect(True)
        vs = _get_virtual_screen_region()
        if vs.width > 0 and vs.height > 0:
            self.geometry(_format_geometry(vs.width, vs.height, vs.left, vs.top))
        else:
            self.geometry(_format_geometry(self.winfo_screenwidth(), self.winfo_screenheight(), 0, 0))

        self.canvas = tk.Canvas(self, highlightthickness=0, bg="black", cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_down)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_up)
        self.bind("<Escape>", lambda _e: self._cancel())

        hint = "拖拽选择区域，松开确认（Esc 取消）"
        self._hint_id = self.canvas.create_text(
            20,
            20,
            text=hint,
            anchor="nw",
            fill="white",
            font=("Segoe UI", 12, "bold"),
        )

    def _on_down(self, event: tk.Event) -> None:
        self._start = _get_cursor_pos(getattr(event, "widget", None), event)
        self._start_local = (event.x, event.y)
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
            self._rect_id = None

    def _on_drag(self, event: tk.Event) -> None:
        if not self._start_local:
            return
        x0, y0 = self._start_local
        x1, y1 = event.x, event.y
        left, right = sorted([x0, x1])
        top, bottom = sorted([y0, y1])
        if self._rect_id is None:
            self._rect_id = self.canvas.create_rectangle(
                left,
                top,
                right,
                bottom,
                outline="red",
                width=3,
            )
        else:
            self.canvas.coords(self._rect_id, left, top, right, bottom)

    def _on_up(self, event: tk.Event) -> None:
        if not self._start:
            return
        x0, y0 = self._start
        x1, y1 = _get_cursor_pos(getattr(event, "widget", None), event)
        left, right = sorted([x0, x1])
        top, bottom = sorted([y0, y1])
        width = right - left
        height = bottom - top
        if width < 16 or height < 16:
            self._start = None
            self._start_local = None
            return
        self._region = Region(left, top, width, height).clamp_min_size()
        self._done.set()
        self.destroy()

    def _cancel(self) -> None:
        self._region = None
        self._done.set()
        self.destroy()

    def wait_region(self) -> Region | None:
        self.grab_set()
        self.wait_window(self)
        self._done.wait(timeout=0.1)
        return self._region


class CaptureFrameOverlay(tk.Toplevel):
    def __init__(self, master: tk.Tk, border: int = 6):
        super().__init__(master)
        self._border = int(border)
        self._region: Region | None = None  # outer region (includes border)
        self._interactive_enabled = True
        self._visible = False

        self._min_inner = 16
        self._border_color = "red"
        self._hit_transparent_color = "#00ff00"
        self._use_transparent_hit_area = os.name == "nt"

        # Keep windows simple and reliable: draw the red frame with 4 solid border windows.
        # This avoids Windows layered/transparent quirks where the frame can disappear.
        self._border_parts: dict[str, tk.Toplevel] = {}
        self._border_bodies: dict[str, tk.Frame] = {}
        self._border_strips: dict[str, tk.Frame] = {}
        self._hit_border = max(int(self._border), 18)

        # When dragging the border, treat the area near each corner as a resize handle.
        # This keeps the implementation robust (no extra transparent windows) while
        # still giving a large "corner" target.
        self._corner_proximity_min = max(36, int(self._hit_border) * 2)
        self._corner_proximity_max = 140

        self._drag_anchor_root: tuple[int, int] | None = None
        self._drag_start_region: Region | None = None
        self._drag_mode: str | None = None  # "move" | "resize"
        self._resize_edges: set[str] = set()
        self._grab_widget: tk.Misc | None = None

        self._init_border_parts(master)

        self.withdraw()
        for w in self._border_parts.values():
            w.withdraw()

    def show(self, region: Region) -> None:
        min_outer = max(16, 2 * self._border + self._min_inner)
        self._region = region.clamp_min_size(min_outer)
        self._render()
        for w in self._border_parts.values():
            w.deiconify()
        self._lift_parts()
        self._visible = True

    def hide(self) -> None:
        for w in self._border_parts.values():
            w.withdraw()
        self.withdraw()
        self._visible = False

    def is_visible(self) -> bool:
        return self._visible

    def get_inner_region(self) -> Region | None:
        if not self._region:
            return None
        inner = Region(
            self._region.left + self._border,
            self._region.top + self._border,
            self._region.width - 2 * self._border,
            self._region.height - 2 * self._border,
        )
        return inner.clamp_min_size()

    def set_interactive(self, enabled: bool) -> None:
        self._interactive_enabled = bool(enabled)
        _ = enabled

    def clear(self) -> None:
        self._region = None
        self.hide()

    def _init_border_parts(self, master: tk.Tk) -> None:
        def create(part: str) -> None:
            w = tk.Toplevel(master)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            if self._use_transparent_hit_area:
                try:
                    w.attributes("-transparentcolor", self._hit_transparent_color)
                    w.configure(bg=self._hit_transparent_color)
                except tk.TclError:
                    self._use_transparent_hit_area = False
                    self._hit_border = int(self._border)
                    w.configure(bg=self._border_color)
            else:
                w.configure(bg=self._border_color)

            w.configure(cursor="fleur")
            body_bg = self._hit_transparent_color if self._use_transparent_hit_area else self._border_color
            body = tk.Frame(w, bg=body_bg, highlightthickness=0, cursor="fleur")
            body.pack(fill="both", expand=True)

            strip = tk.Frame(body, bg=self._border_color, highlightthickness=0)
            strip.place(x=0, y=0, width=1, height=1)

            def focus(_e: tk.Event) -> None:
                try:
                    w.focus_force()
                except Exception:
                    pass

            for widget in (body, strip):
                widget.bind("<ButtonPress-1>", lambda e: (focus(e), self._on_border_down(e)))
                widget.bind("<B1-Motion>", self._on_mouse_drag)
                widget.bind("<ButtonRelease-1>", self._on_mouse_up)
                widget.bind("<Motion>", self._on_border_motion)
            w.bind("<Escape>", lambda _e: master.focus_force())
            w.bind("<KeyPress>", self._on_key)
            self._border_parts[part] = w
            self._border_bodies[part] = body
            self._border_strips[part] = strip

        for part in ["top", "bottom", "left", "right"]:
            create(part)

    def _lift_parts(self) -> None:
        for part in ["top", "bottom", "left", "right"]:
            w = self._border_parts[part]
            try:
                w.lift()
            except Exception:
                pass

    def _begin_drag(self, mode: str, edges: set[str], event: tk.Event) -> None:
        if not self._region:
            return
        px, py = _get_cursor_pos(getattr(event, "widget", None), event)
        self._drag_anchor_root = (px, py)
        self._drag_start_region = self._region
        self._drag_mode = mode
        self._resize_edges = set(edges)

        # Keep receiving motion events even when dragging over other apps/windows.
        try:
            grab_target = getattr(event, "widget", None)
            if grab_target is not None:
                grab_target.grab_set_global()
                self._grab_widget = grab_target
        except Exception:
            self._grab_widget = None
            try:
                grab_target = getattr(event, "widget", None)
                if grab_target is not None:
                    grab_target = grab_target.winfo_toplevel()
                    grab_target.grab_set_global()
                    self._grab_widget = grab_target
            except Exception:
                self._grab_widget = None

    def _on_border_down(self, event: tk.Event) -> None:
        if not self._region:
            return
        if not self._interactive_enabled:
            return

        r = self._region
        x, y = _get_cursor_pos(getattr(event, "widget", None), event)
        left = int(r.left)
        top = int(r.top)
        right = int(r.left + r.width)
        bottom = int(r.top + r.height)

        # If the user grabs near a corner, prefer resize over move. This makes corner
        # resizing reliable without extra transparent "handle" windows.
        min_dim = int(min(r.width, r.height))
        corner_proximity = max(
            int(self._corner_proximity_min),
            min(int(self._corner_proximity_max), int(min_dim // 3) if min_dim > 0 else 0),
        )
        d_left = abs(x - left)
        d_right = abs(x - right)
        d_top = abs(y - top)
        d_bottom = abs(y - bottom)

        if d_left <= corner_proximity and d_top <= corner_proximity:
            self._begin_drag("resize", {"top", "left"}, event)
            return
        if d_right <= corner_proximity and d_top <= corner_proximity:
            self._begin_drag("resize", {"top", "right"}, event)
            return
        if d_left <= corner_proximity and d_bottom <= corner_proximity:
            self._begin_drag("resize", {"bottom", "left"}, event)
            return
        if d_right <= corner_proximity and d_bottom <= corner_proximity:
            self._begin_drag("resize", {"bottom", "right"}, event)
            return

        self._begin_drag("move", set(), event)

    def _on_border_motion(self, event: tk.Event) -> None:
        if not self._region:
            return
        if not self._interactive_enabled:
            return

        x, y = _get_cursor_pos(getattr(event, "widget", None), event)
        r = self._region
        left = int(r.left)
        top = int(r.top)
        right = int(r.left + r.width)
        bottom = int(r.top + r.height)

        min_dim = int(min(r.width, r.height))
        corner_proximity = max(
            int(self._corner_proximity_min),
            min(int(self._corner_proximity_max), int(min_dim // 3) if min_dim > 0 else 0),
        )

        d_left = abs(x - left)
        d_right = abs(x - right)
        d_top = abs(y - top)
        d_bottom = abs(y - bottom)

        cursor = "fleur"
        if (d_left <= corner_proximity and d_top <= corner_proximity) or (
            d_right <= corner_proximity and d_bottom <= corner_proximity
        ):
            cursor = "size_nw_se"
        elif (d_right <= corner_proximity and d_top <= corner_proximity) or (
            d_left <= corner_proximity and d_bottom <= corner_proximity
        ):
            cursor = "size_ne_sw"

        try:
            event.widget.configure(cursor=cursor)
        except Exception:
            pass
        try:
            event.widget.winfo_toplevel().configure(cursor=cursor)
        except Exception:
            pass

    def _layout_parts(self, region: Region) -> None:
        b = int(self._border)
        hit = max(int(self._hit_border), b)
        extra = hit - b
        left, top, width, height = (
            int(region.left),
            int(region.top),
            int(region.width),
            int(region.height),
        )

        x0 = left - extra
        y0 = top - extra
        w = width + 2 * extra
        h = height + 2 * extra

        self._border_parts["top"].geometry(_format_geometry(w, hit, x0, y0))
        self._border_parts["bottom"].geometry(
            _format_geometry(w, hit, x0, top + height - b)
        )
        self._border_parts["left"].geometry(_format_geometry(hit, h, x0, y0))
        self._border_parts["right"].geometry(
            _format_geometry(hit, h, left + width - b, y0)
        )

        # Keep the visible border thin (b), while allowing a larger hit area (hit).
        # The extra hit area is outside the actual rectangle and can be transparent.
        try:
            self._border_strips["top"].place_configure(
                x=extra, y=hit - b, width=width, height=b
            )
            self._border_strips["bottom"].place_configure(x=extra, y=0, width=width, height=b)
            self._border_strips["left"].place_configure(
                x=hit - b, y=extra, width=b, height=height
            )
            self._border_strips["right"].place_configure(x=0, y=extra, width=b, height=height)
        except Exception:
            pass

    def _render(self) -> None:
        if not self._region:
            return
        self._layout_parts(self._region)

    def _on_mouse_drag(self, event: tk.Event) -> None:
        if (
            not self._region
            or not self._drag_anchor_root
            or not self._drag_start_region
            or not self._drag_mode
        ):
            return
        ax, ay = self._drag_anchor_root
        px, py = _get_cursor_pos(getattr(event, "widget", None), event)
        dx = int(px - ax)
        dy = int(py - ay)

        if self._drag_mode == "move":
            r = self._drag_start_region
            self._region = Region(r.left + dx, r.top + dy, r.width, r.height).clamp_min_size()
            self._render()
            return

        if self._drag_mode == "resize":
            r = self._drag_start_region
            left, top, width, height = r.left, r.top, r.width, r.height
            min_size = max(16, 2 * self._border + self._min_inner)

            if "left" in self._resize_edges:
                new_left = left + dx
                new_width = width - dx
                if new_width < min_size:
                    new_left = left + (width - min_size)
                    new_width = min_size
                left, width = new_left, new_width
            if "right" in self._resize_edges:
                new_width = width + dx
                width = max(min_size, new_width)
            if "top" in self._resize_edges:
                new_top = top + dy
                new_height = height - dy
                if new_height < min_size:
                    new_top = top + (height - min_size)
                    new_height = min_size
                top, height = new_top, new_height
            if "bottom" in self._resize_edges:
                new_height = height + dy
                height = max(min_size, new_height)

            self._region = Region(int(left), int(top), int(width), int(height)).clamp_min_size(
                min_size
            )
            self._render()

    def _on_mouse_up(self, _event: tk.Event) -> None:
        try:
            if self._grab_widget is not None:
                self._grab_widget.grab_release()
        except Exception:
            pass
        self._grab_widget = None
        self._drag_anchor_root = None
        self._drag_start_region = None
        self._drag_mode = None
        self._resize_edges = set()

    def _on_key(self, event: tk.Event) -> None:
        if not self._region:
            return
        if not self._interactive_enabled:
            return

        step = 10 if (event.state & 0x0001) else 1  # Shift
        ctrl = bool(event.state & 0x0004)

        left, top, width, height = (
            self._region.left,
            self._region.top,
            self._region.width,
            self._region.height,
        )

        if ctrl:
            if event.keysym == "Left":
                width -= step
            elif event.keysym == "Right":
                width += step
            elif event.keysym == "Up":
                height -= step
            elif event.keysym == "Down":
                height += step
        else:
            if event.keysym == "Left":
                left -= step
            elif event.keysym == "Right":
                left += step
            elif event.keysym == "Up":
                top -= step
            elif event.keysym == "Down":
                top += step

        min_size = max(16, 2 * self._border + self._min_inner)
        self._region = Region(left, top, width, height).clamp_min_size(min_size)
        self._render()


class ScreenGifRecorder:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.frames: list[Image.Image] = []
        self.fps = 10
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start(self, region: Region, fps: int) -> None:
        if mss is None or Image is None:
            raise RuntimeError("缺少依赖：请先安装 mss 和 Pillow")
        self.frames = []
        self.fps = int(max(1, fps))
        self._stop.clear()
        self._last_error = None
        self._thread = threading.Thread(
            target=self._run, args=(region.clamp_min_size(),), daemon=True
        )
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout_s)
        self._thread = None

    def is_recording(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self, region: Region) -> None:
        frame_interval = 1.0 / max(1, self.fps)
        target = time.perf_counter()
        monitor = {
            "left": int(region.left),
            "top": int(region.top),
            "width": int(region.width),
            "height": int(region.height),
        }
        try:
            with mss() as sct:
                while not self._stop.is_set():
                    target += frame_interval
                    shot = sct.grab(monitor)
                    img = Image.frombytes("RGB", shot.size, shot.rgb)
                    self.frames.append(img)
                    sleep_for = target - time.perf_counter()
                    if sleep_for > 0:
                        time.sleep(sleep_for)
        except Exception as e:  # pragma: no cover
            self._last_error = str(e)


class TrimDialog(tk.Toplevel):
    def __init__(self, master: tk.Tk, frames: list[Image.Image], fps: int):
        super().__init__(master)
        self.title("剪辑并保存 GIF")
        self.resizable(False, False)
        self.frames = frames
        self.fps = max(1, int(fps))

        self.start_var = tk.IntVar(value=0)
        self.end_var = tk.IntVar(value=max(0, len(frames) - 1))
        self.playhead_var = tk.IntVar(value=0)
        self._preview_img: ImageTk.PhotoImage | None = None
        self._playing = False
        self._preview_target_size: tuple[int, int] | None = None
        self._preview_cache: "OrderedDict[int, ImageTk.PhotoImage]" = OrderedDict()
        self._preview_cache_max = 80
        self._play_next_t: float | None = None
        self._suppress_playhead_callback = False

        self._build()
        self._update_ui()

    def _ensure_preview_target_size(self) -> None:
        if self._preview_target_size is not None:
            return
        if not self.frames or Image is None:
            return
        frame = self.frames[0]
        max_w, max_h = 520, 300
        w, h = frame.size
        scale = min(max_w / max(1, w), max_h / max(1, h), 1.0)
        tw, th = max(1, int(w * scale)), max(1, int(h * scale))
        self._preview_target_size = (tw, th)
        self._preview_cache.clear()

    def _get_preview_photo(self, idx: int) -> ImageTk.PhotoImage | None:
        if not self.frames or ImageTk is None or Image is None:
            return None
        self._ensure_preview_target_size()
        idx = max(0, min(int(idx), len(self.frames) - 1))

        cached = self._preview_cache.get(idx)
        if cached is not None:
            self._preview_cache.move_to_end(idx)
            return cached

        frame = self.frames[idx]
        tw, th = self._preview_target_size or frame.size
        try:
            img = frame.resize((tw, th), Image.Resampling.BILINEAR)
        except Exception:
            img = frame.resize((tw, th))

        photo = ImageTk.PhotoImage(img)
        self._preview_cache[idx] = photo
        self._preview_cache.move_to_end(idx)
        while len(self._preview_cache) > int(self._preview_cache_max):
            self._preview_cache.popitem(last=False)
        return photo

    def _build(self) -> None:
        wrap = ttk.Frame(self, padding=12)
        wrap.pack(fill="both", expand=True)

        self.info = ttk.Label(wrap, text="")
        self.info.pack(fill="x")

        preview = ttk.LabelFrame(wrap, text="预览", padding=8)
        preview.pack(fill="both", expand=True, pady=(10, 0))
        self.preview_label = tk.Label(preview, bd=1, relief="solid", bg="black")
        self.preview_label.pack()

        self.preview_info = ttk.Label(preview, text="", foreground="#444")
        self.preview_info.pack(fill="x", pady=(6, 0))

        ph = ttk.Frame(preview)
        ph.pack(fill="x", pady=(8, 0))
        ttk.Label(ph, text="预览帧").grid(row=0, column=0, sticky="w")
        self.playhead_scale = ttk.Scale(
            ph,
            from_=0,
            to=max(0, len(self.frames) - 1),
            command=lambda _v: self._on_playhead(),
        )
        self.playhead_scale.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.play_btn = ttk.Button(ph, text="播放", command=self._toggle_play)
        self.play_btn.grid(row=0, column=2, sticky="e", padx=(8, 0))
        ph.columnconfigure(1, weight=1)

        self.timeline = tk.Canvas(wrap, width=520, height=22, highlightthickness=0)
        self.timeline.pack(pady=(10, 8), fill="x")

        scale_frame = ttk.Frame(wrap)
        scale_frame.pack(fill="x")

        ttk.Label(scale_frame, text="开始").grid(row=0, column=0, sticky="w")
        self.start_scale = ttk.Scale(
            scale_frame,
            from_=0,
            to=max(0, len(self.frames) - 1),
            command=lambda _v: self._on_start_scale(),
        )
        self.start_scale.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        ttk.Label(scale_frame, text="结束").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.end_scale = ttk.Scale(
            scale_frame,
            from_=0,
            to=max(0, len(self.frames) - 1),
            command=lambda _v: self._on_end_scale(),
        )
        self.end_scale.set(max(0, len(self.frames) - 1))
        self.end_scale.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))

        scale_frame.columnconfigure(1, weight=1)

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=(12, 0))

        ttk.Button(btns, text="保存剪辑后的 GIF…", command=self._save).pack(
            side="right"
        )
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=(0, 8))

        tip = "提示：拖动进度条选择开始/结束帧"
        ttk.Label(wrap, text=tip, foreground="#666").pack(fill="x", pady=(8, 0))

    def _on_start_scale(self) -> None:
        self._stop_playing()
        start = int(round(float(self.start_scale.get())))
        end = int(round(float(self.end_scale.get())))
        if end < start:
            end = start
            self.end_scale.set(end)
        self.start_var.set(start)
        self.end_var.set(end)
        self._set_playhead(start)
        self._update_ui()

    def _on_end_scale(self) -> None:
        self._stop_playing()
        start = int(round(float(self.start_scale.get())))
        end = int(round(float(self.end_scale.get())))
        if end < start:
            start = end
            self.start_scale.set(start)
        self.start_var.set(start)
        self.end_var.set(end)
        self._set_playhead(end)
        self._update_ui()

    def _on_playhead(self) -> None:
        if self._suppress_playhead_callback:
            return
        self._stop_playing()
        idx = int(round(float(self.playhead_scale.get())))
        self.playhead_var.set(idx)
        self._update_preview()

    def _set_playhead(self, idx: int) -> None:
        if not self.frames:
            return
        idx = max(0, min(int(idx), len(self.frames) - 1))
        self.playhead_var.set(idx)
        # Some Tk builds may invoke the scale `command` callback asynchronously for `.set()`.
        # Keep suppression enabled until the event loop goes idle.
        self._suppress_playhead_callback = True
        try:
            self.playhead_scale.set(idx)
        except Exception:
            pass
        self.after_idle(lambda: setattr(self, "_suppress_playhead_callback", False))

    def _toggle_play(self) -> None:
        if not self.frames:
            return
        self._playing = not self._playing
        self.play_btn.config(text="暂停" if self._playing else "播放")
        if self._playing:
            self._play_next_t = None
            self._play_tick(None)

    def _stop_playing(self) -> None:
        if self._playing:
            self._playing = False
            self._play_next_t = None
            self.play_btn.config(text="播放")

    def _play_tick(self, idx: int | None) -> None:
        if not self._playing or not self.frames:
            return
        start = self.start_var.get()
        end = self.end_var.get()

        if idx is None:
            idx = self.playhead_var.get()
        idx = int(idx)
        if idx < start or idx > end:
            idx = start
        self._set_playhead(idx)
        self._update_preview()

        next_idx = idx + 1
        if next_idx > end:
            next_idx = start

        interval_s = 1.0 / max(1, float(self.fps))
        now = time.perf_counter()
        if self._play_next_t is None:
            self._play_next_t = now + interval_s
        else:
            self._play_next_t += interval_s
            if self._play_next_t < now - interval_s:
                self._play_next_t = now + interval_s

        delay_ms = max(1, int((self._play_next_t - now) * 1000))
        self.after(delay_ms, lambda: self._play_tick(next_idx))

    def _update_ui(self) -> None:
        total = max(1, len(self.frames))
        start = self.start_var.get()
        end = self.end_var.get()
        duration_total = len(self.frames) / self.fps if self.frames else 0
        duration_clip = (end - start + 1) / self.fps if self.frames else 0

        self.info.config(
            text=f"总帧数：{len(self.frames)}（约 {duration_total:.2f}s）  "
            f"剪辑：{start} - {end}（约 {duration_clip:.2f}s）"
        )

        w = int(self.timeline.winfo_width() or 520)
        h = int(self.timeline.winfo_height() or 22)
        pad = 2
        self.timeline.delete("all")
        self.timeline.create_rectangle(pad, pad, w - pad, h - pad, fill="#ddd", outline="#bbb")

        if self.frames:
            x0 = pad + (w - 2 * pad) * (start / (total - 1 if total > 1 else 1))
            x1 = pad + (w - 2 * pad) * (end / (total - 1 if total > 1 else 1))
            self.timeline.create_rectangle(x0, pad, x1, h - pad, fill="#4a90e2", outline="")

        self._update_preview()

    def _update_preview(self) -> None:
        if not self.frames or ImageTk is None:
            self.preview_label.config(image="", text="无法预览（缺少 Pillow.ImageTk）")
            return

        idx = self.playhead_var.get()
        idx = max(0, min(idx, len(self.frames) - 1))
        photo = self._get_preview_photo(idx)
        if photo is None:
            self.preview_label.config(image="", text="Preview unavailable")
            return

        self._preview_img = photo
        self.preview_label.config(image=photo, text="")

        t = idx / max(1, self.fps)
        self.preview_info.config(text=f"预览：第 {idx} 帧 / {len(self.frames) - 1}（{t:.2f}s）")

    def _save(self) -> None:
        if not self.frames:
            messagebox.showerror("错误", "没有可保存的帧")
            return
        start = self.start_var.get()
        end = self.end_var.get()
        if end < start:
            messagebox.showerror("错误", "结束帧不能小于开始帧")
            return

        path = filedialog.asksaveasfilename(
            title="保存 GIF",
            defaultextension=".gif",
            filetypes=[("GIF", "*.gif")],
            initialfile=f"clip_{datetime.now().strftime('%Y%m%d_%H%M%S')}.gif",
        )
        if not path:
            return

        clip = self.frames[start : end + 1]
        duration_ms = int(1000 / max(1, self.fps))
        try:
            clip[0].save(
                path,
                save_all=True,
                append_images=clip[1:],
                duration=duration_ms,
                loop=0,
                optimize=False,
            )
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return

        messagebox.showinfo("完成", f"已保存：{path}")
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("toGIF 录屏工具")
        self.geometry("520x320")

        self.recorder = ScreenGifRecorder()
        self.capture_region: Region | None = None
        self.overlay = CaptureFrameOverlay(self, border=3)

        self.fps_var = tk.IntVar(value=10)
        self.status_var = tk.StringVar(value="未选择区域")
        self.frames_var = tk.StringVar(value="0")
        self.seconds_var = tk.StringVar(value="0.00")

        self._build()
        self._poll()
        self.bind_all("<F2>", lambda _e: self._toggle_overlay(), add="+")

    def _build(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        title = ttk.Label(root, text="GIF 截图/录屏", font=("Segoe UI", 14, "bold"))
        title.pack(anchor="w")

        info = ttk.Frame(root)
        info.pack(fill="x", pady=(10, 0))
        ttk.Label(info, text="状态：").grid(row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.status_var).grid(row=0, column=1, sticky="w")
        ttk.Label(info, text="帧数：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(info, textvariable=self.frames_var).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(info, text="时长：").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(info, textvariable=self.seconds_var).grid(row=2, column=1, sticky="w", pady=(6, 0))

        config = ttk.LabelFrame(root, text="参数", padding=10)
        config.pack(fill="x", pady=(12, 0))

        ttk.Label(config, text="FPS").grid(row=0, column=0, sticky="w")
        fps_spin = ttk.Spinbox(config, from_=1, to=30, textvariable=self.fps_var, width=6)
        fps_spin.grid(row=0, column=1, sticky="w", padx=(8, 0))

        ttk.Button(config, text="选择区域…", command=self._choose_region).grid(
            row=0, column=2, sticky="e", padx=(16, 0)
        )
        ttk.Button(config, text="显示/隐藏框", command=self._toggle_overlay).grid(
            row=0, column=3, sticky="e", padx=(8, 0)
        )
        config.columnconfigure(3, weight=1)

        actions = ttk.LabelFrame(root, text="操作", padding=10)
        actions.pack(fill="x", pady=(12, 0))

        self.start_btn = ttk.Button(actions, text="开始录制", command=self._start)
        self.stop_btn = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
        self.save_btn = ttk.Button(actions, text="保存 GIF…", command=self._save_raw, state="disabled")
        self.trim_btn = ttk.Button(actions, text="剪辑…", command=self._trim, state="disabled")

        self.start_btn.grid(row=0, column=0, sticky="ew")
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.save_btn.grid(row=0, column=2, sticky="ew", padx=(8, 0))
        self.trim_btn.grid(row=0, column=3, sticky="ew", padx=(8, 0))

        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        actions.columnconfigure(2, weight=1)
        actions.columnconfigure(3, weight=1)

        help_box = ttk.LabelFrame(root, text="快捷键（框选窗口）", padding=10)
        help_box.pack(fill="x", pady=(12, 0))
        ttk.Label(
            help_box,
            text="移动：拖动边框/方向键  |  缩放：拖动四角  |  Shift：步长×10  |  Ctrl+方向键：调整宽高",
            foreground="#444",
            wraplength=480,
        ).pack(anchor="w")

    def _require_deps(self) -> bool:
        if mss is None or Image is None:
            messagebox.showerror(
                "缺少依赖",
                "请先安装依赖：pip install -r requirements.txt",
            )
            return False
        return True

    def _choose_region(self) -> None:
        if not self._require_deps():
            return
        self.overlay.hide()
        self.withdraw()
        self.update_idletasks()
        selector = RegionSelector(self)
        region = selector.wait_region()
        self.deiconify()
        if not region:
            return
        self.capture_region = region
        border = self.overlay._border
        outer = Region(
            region.left - border,
            region.top - border,
            region.width + 2 * border,
            region.height + 2 * border,
        )
        self.overlay.show(outer)
        self._move_window_outside_region(outer)
        self.status_var.set("已选择区域（可移动/微调框）")

    def _move_window_outside_region(self, region: Region) -> None:
        self.update_idletasks()
        try:
            app_left = int(self.winfo_rootx())
            app_top = int(self.winfo_rooty())
            app_w = int(self.winfo_width() or 520)
            app_h = int(self.winfo_height() or 320)
        except Exception:
            return

        app_rect = Region(app_left, app_top, app_w, app_h)
        if not _rects_intersect(app_rect, region):
            return

        vs = _get_virtual_screen_region()
        if vs.width <= 0 or vs.height <= 0:
            vs = Region(0, 0, int(self.winfo_screenwidth()), int(self.winfo_screenheight()))

        margin = 12
        candidates: list[tuple[int, int]] = [
            (region.left + region.width + margin, region.top),
            (region.left - app_w - margin, region.top),
            (region.left, region.top + region.height + margin),
            (region.left, region.top - app_h - margin),
            (vs.left + margin, vs.top + margin),
            (vs.left + vs.width - app_w - margin, vs.top + margin),
            (vs.left + vs.width - app_w - margin, vs.top + vs.height - app_h - margin),
            (vs.left + margin, vs.top + vs.height - app_h - margin),
        ]

        for x, y in candidates:
            if x < vs.left or y < vs.top:
                continue
            if x + app_w > vs.left + vs.width or y + app_h > vs.top + vs.height:
                continue
            if _rects_intersect(Region(x, y, app_w, app_h), region):
                continue
            self.geometry(_format_position(x, y))
            return

    def _toggle_overlay(self) -> None:
        if self.overlay.is_visible():
            self.overlay.hide()
            return
        if not self.capture_region and not self.overlay._region:
            messagebox.showwarning("提示", "请先选择区域")
            return
        if self.overlay._region:
            outer = self.overlay._region
        else:
            border = self.overlay._border
            outer = Region(
                self.capture_region.left - border,
                self.capture_region.top - border,
                self.capture_region.width + 2 * border,
                self.capture_region.height + 2 * border,
            )
        self.overlay.show(outer)
        self._move_window_outside_region(outer)

    def _start(self) -> None:
        if not self._require_deps():
            return
        if not self.capture_region and not self.overlay.get_inner_region():
            messagebox.showwarning("提示", "请先选择区域")
            return
        inner = self.overlay.get_inner_region()
        region = inner or self.capture_region
        if not region:
            messagebox.showwarning("提示", "请选择录制区域后再开始")
            return
        if not self.overlay.is_visible():
            if self.overlay._region:
                self.overlay.show(self.overlay._region)
            else:
                border = self.overlay._border
                outer = Region(
                    region.left - border,
                    region.top - border,
                    region.width + 2 * border,
                    region.height + 2 * border,
                )
                self.overlay.show(outer)
        try:
            self.overlay.set_interactive(False)
            self.recorder.start(region, self.fps_var.get())
        except Exception as e:
            self.overlay.set_interactive(True)
            messagebox.showerror("无法开始录制", str(e))
            return
        self.status_var.set("录制中…（停止后可保存/剪辑）")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.save_btn.config(state="disabled")
        self.trim_btn.config(state="disabled")

    def _stop(self) -> None:
        self.recorder.stop()
        self.overlay.set_interactive(True)
        self.overlay.clear()
        self.capture_region = None
        if self.recorder.last_error:
            messagebox.showerror("录制出错", self.recorder.last_error)
        self.status_var.set("已停止（可保存/剪辑；如需重新录制请重新选区）" if self.recorder.frames else "已停止（无帧；如需重新录制请重新选区）")
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        if self.recorder.frames:
            self.save_btn.config(state="normal")
            self.trim_btn.config(state="normal")
        else:
            self.save_btn.config(state="disabled")
            self.trim_btn.config(state="disabled")

    def _save_raw(self) -> None:
        if not self.recorder.frames:
            return
        path = filedialog.asksaveasfilename(
            title="保存 GIF",
            defaultextension=".gif",
            filetypes=[("GIF", "*.gif")],
            initialfile=f"record_{datetime.now().strftime('%Y%m%d_%H%M%S')}.gif",
        )
        if not path:
            return
        duration_ms = int(1000 / max(1, self.recorder.fps))
        try:
            frames = self.recorder.frames
            frames[0].save(
                path,
                save_all=True,
                append_images=frames[1:],
                duration=duration_ms,
                loop=0,
                optimize=False,
            )
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return
        messagebox.showinfo("完成", f"已保存：{path}")

    def _trim(self) -> None:
        if not self.recorder.frames:
            return
        dlg = TrimDialog(self, self.recorder.frames, self.recorder.fps)
        dlg.transient(self)
        dlg.grab_set()

    def _poll(self) -> None:
        frames = len(self.recorder.frames)
        self.frames_var.set(str(frames))
        seconds = frames / max(1, self.recorder.fps) if frames else 0.0
        self.seconds_var.set(f"{seconds:.2f}s")
        if self.recorder.is_recording():
            self.status_var.set("录制中…（停止后可保存/剪辑）")
        self.after(200, self._poll)


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
