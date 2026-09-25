"""灵动岛：只在用的时候出现的深色胶囊，外观和节奏移植自 voice-dictate。

形态（对应 dictate.cs 的 Kind）：
  聆听 listening -> listening  渐变麦克风圆片 + SlicedWaves 音量条 + 计时和提示
  识别 thinking  -> busy       圆片外加转圈光环 + ShinyText「识别中…」
  完成 done      -> notice     对勾 + 识别结果
  受阻 blocked   -> message    橙色警告 + 原因
  待机 idle      ->（隐藏）

**待机时完全隐藏，帧循环也停掉。** 上游版本待机时常驻并以 25 帧/秒重绘、每帧
还做整张高斯模糊，守护进程常年占约 7% 单核，而且置顶分层窗口的每次更新都要
DWM 重新合成，整机会跟着发卡。voice-dictate 的做法是不用就不存在，这里照搬。
打开面板、暂停、退出改走托盘图标（tray.py）。

交互：显示期间可以拖动（松开记住位置）、双击打开管理面板。拖动由低级鼠标钩子
在消息抵达窗口之前拦截实现 —— 窗口一旦被点到前台，闸门就认不出终端了。
"""
import math
import time
import tkinter as tk

import render
import winapi
from layered import LayeredSurface

FRAME_MS = 28                 # 约 36 帧/秒：音量条够顺，重绘开销只有 60 帧的一半多
IN_MS, OUT_MS = 180, 140      # 出现 / 消失；比 Lovable 的 0.6s 快，HUD 要跟得上按键
LIFT = 12                     # 出现时从下方浮上来的距离（逻辑像素）
BOTTOM_GAP = 72               # 默认位置：胶囊底边离工作区底边的距离

KIND = {"listening": "listening", "thinking": "busy", "done": "notice", "blocked": "message"}
LISTEN_HINT = "松开结束 · Esc 取消"
BUSY_TEXT, BUSY_HINT = "识别中…", "Esc 取消"


class Hud:
    """只在 tkinter 主线程被调用；其它线程通过 App 的队列间接驱动。"""

    def __init__(self, root: tk.Tk, opacity: float = 0.95, position=None,
                 on_move=None, on_double_click=None):
        self.root = root
        self.opacity = opacity
        self.on_move = on_move
        self.on_double_click = on_double_click

        self.state = "hidden"
        self.paused = False
        self.text = ""                # 聆听时是计时，完成/受阻时是要显示的文字
        self.scale = 1.0
        self._kind = "listening"
        self._level = 0.0             # 平滑后的 0..1
        self._level_in = 0.0
        self._width = 200.0
        self._target_w = 200.0
        self._appear = 0.0
        self._wanted = False
        self._running = False
        self._shown_at = self._state_at = time.time()
        # 记的是胶囊中心点（物理像素）。宽度随状态变化，记左上角会让胶囊往一边长
        self._center = tuple(position) if position and len(position) == 2 else None

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.geometry("10x10+0+0")
        self.win.withdraw()
        self.win.update_idletasks()
        # 之后一律用 ShowWindow(SW_SHOWNOACTIVATE) 显示：tkinter 的 deiconify()
        # 会把窗口顶到前台，而闸门靠前台窗口 PID 判断终端身份。
        self.hwnd = winapi.user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
        winapi.make_overlay(self.hwnd)
        self.surface = LayeredSurface(self.hwnd)

    # ------------------------------------------------------------ 状态
    def show(self, state: str, text: str = ""):
        if state not in KIND:                      # idle / hidden：收起
            self.state = "hidden"
            self._wanted = False
            return
        now = time.time()
        if state != self.state:
            self._state_at = now
        self.state, self.text, self._kind = state, text, KIND[state]
        if self._appear <= 0:                      # 从隐藏出现：跟着光标所在的屏
            _, _, _, _, self.scale = winapi.monitor_work_area_at(*winapi.cursor_pos())
            self._shown_at = now
            self._level = 0.0
        self._target_w = render.width_for(self._kind, *self._labels())
        if self._appear <= 0:
            self._width = self._target_w
        self._wanted = True
        winapi.user32.ShowWindow(self.hwnd, 4)                          # SW_SHOWNOACTIVATE
        winapi.user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x0013)   # 每次都重申置顶
        if not self._running:
            self._running = True
            winapi.timer_precision(True)
            self._tick()

    def set_level(self, rms: float):
        """rms 是 0..1 的线性电平，换成和 voice-dictate 一样的 -60..0 dBFS 刻度。"""
        db = 20 * math.log10(rms) if rms > 0 else -100
        self._level_in = max(0.0, min(100.0, (db + 60) / 60 * 100))

    def _labels(self) -> tuple[str, str]:
        if self._kind == "listening":
            return "", f"{self.text or '0:00'}  {LISTEN_HINT}"
        if self._kind == "busy":
            return BUSY_TEXT, BUSY_HINT
        return self.text, ""

    # ------------------------------------------------------------ 帧循环
    def _tick(self):
        step = FRAME_MS / (IN_MS if self._wanted else OUT_MS)
        self._appear = min(1.0, self._appear + step) if self._wanted else max(0.0, self._appear - step)
        if self._appear <= 0:
            winapi.user32.ShowWindow(self.hwnd, 0)                      # SW_HIDE
            self._running = False
            winapi.timer_precision(False)
            return                                                      # 隐藏后不再有任何定时器

        target = max(0.0, min(1.0, (self._level_in - 12) / 55))
        self._level += (target - self._level) * (0.5 if target > self._level else 0.15)
        if self._kind == "listening":
            self._target_w = render.width_for(self._kind, *self._labels())
        self._width += (self._target_w - self._width) * 0.45
        now = time.time()
        try:
            img = render.frame(self._kind, *self._labels(), self._width, self.scale,
                               now - self._shown_at, now - self._state_at, self._level)
            ease = 1 - (1 - self._appear) ** 3
            cx, cy = self._center_or_default()
            x = round(cx - img.width / 2)
            y = round(cy - img.height / 2 + (1 - ease) * LIFT * self.scale)
            self.surface.update(img, x, y, opacity=round(255 * self.opacity * ease))
        except (tk.TclError, OSError):
            pass
        self.root.after(FRAME_MS, self._tick)

    def _center_or_default(self) -> tuple[float, float]:
        if self._center is None:
            l, _, r, b, _ = winapi.monitor_work_area_at(*winapi.cursor_pos())
            return (l + r) / 2, b - (BOTTOM_GAP + render.H / 2) * self.scale
        return self._center

    # ------------------------------------- 拖动（由低级鼠标钩子线程调用）
    def hit_test(self, x: int, y: int) -> bool:
        if self._appear <= 0:
            return False
        cx, cy = self._center_or_default()
        return (abs(x - cx) <= self._width * self.scale / 2
                and abs(y - cy) <= render.H * self.scale / 2)

    def move_by(self, dx: int, dy: int) -> None:
        """钩子线程直接改坐标，下一帧自然跟上 —— 元组赋值是原子的，不碰 tkinter。"""
        cx, cy = self._center_or_default()
        self._center = (cx + dx, cy + dy)

    def finish_move(self) -> None:
        if self.on_move and self._center:
            self.on_move(self._center)              # 落盘记住位置

    def double_click(self) -> None:
        if self.on_double_click:
            self.on_double_click()
