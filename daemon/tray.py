"""托盘图标：浮窗待机时完全隐藏，打开面板 / 暂停 / 退出就放在这里，同 voice-dictate。

图标就是浮窗上那枚渐变麦克风圆片，暂停时换成灰色。菜单回调跑在 pystray 自己的
线程里，只做「置标志 / 开浏览器」这种不碰 tkinter 的事。
"""
import threading

import pystray

import render

ICON_PX = 64


def _image(paused: bool):
    scale = ICON_PX / render.CHIP_BOX
    return render.chip("message" if paused else "listening", scale, 0.0, 0.0)


class Tray:
    def __init__(self, app):
        self.app = app
        self.icon = pystray.Icon("cc-voice", _image(False), "cc-voice 语音输入", menu=pystray.Menu(
            pystray.MenuItem("打开管理面板", lambda: app.open_panel(), default=True),
            pystray.MenuItem("暂停", self._toggle_pause, checked=lambda _: app.paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出语音输入", lambda: app.request_quit()),
        ))

    def _toggle_pause(self):
        self.app.set_paused(not self.app.paused)

    def refresh(self):
        """暂停状态变了（托盘或面板改的）就换图标。"""
        self.icon.icon = _image(self.app.paused)
        self.icon.title = "cc-voice 语音输入" + ("（已暂停）" if self.app.paused else "")

    def start(self):
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    def stop(self):
        self.icon.stop()
