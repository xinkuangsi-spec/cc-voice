"""cc-voice 守护进程：按住触发键说话，松开把中文上屏到前台窗口。

线程分工（低级钩子与 tkinter 各自需要不同的事件循环，无法合并）：
  主线程     tkinter mainloop —— 只画 HUD，绝不做阻塞操作
  trigger    Win32 消息泵 + 低级钩子 —— 只判定边沿、塞队列
  worker     录音 / 识别 / 注入 —— 耗时都在这里
  panel      本地管理面板 HTTP 服务
"""
import argparse
import ctypes
import json
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audio
import config
import inject
import tuning
import winapi
from context import Context
from gate import SessionGate
from hud import Hud
from textfix import TextFixer
from tray import Tray
from trigger import Trigger

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
HISTORY = LOGS / "data" / "history.jsonl"   # 面板读的结构化数据
READABLE = LOGS / "识别记录.log"             # 人看的对齐流水


MUTEX_NAME = "Local\\cc-voice-daemon"   # 必须与 hooks/session-start.ps1 里探测的名字一致
_MUTEX = None


def single_instance(name: str = MUTEX_NAME) -> bool:
    """同一时刻只允许一个守护进程 —— 三个入口都会尝试拉起它。

    用 Local\\ 而不是 Global\\：Global 命名空间需要 SeCreateGlobalPrivilege，
    普通用户会话下未必创建得了。更要紧的是名字必须和钩子里 OpenExisting 的
    完全一致 —— 曾经一边 Global 一边 Local，钩子永远探不到，于是每开一个
    cc 窗口就多起一个守护进程，多个进程抢同一个麦克风、重复注入。

    句柄必须挂在模块级变量上，否则被 GC 回收后互斥量随之释放。
    """
    global _MUTEX
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _MUTEX = kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS


class App:
    def __init__(self, cfg: dict, announce: bool = False):
        self.cfg = cfg
        self.status = "启动中"
        self.recognizer = None
        self.rec_error = None
        self.started_at = time.time()
        self.stats = {"count": 0, "chars": 0, "last": None}

        LOGS.mkdir(parents=True, exist_ok=True)
        self.gate = SessionGate(ROOT / "sessions", cfg["gate_mode"])
        self.gate.start()                    # 后台每秒重扫进程树，见 gate.start()
        self.fixer = TextFixer(ROOT / "rules.txt")
        self.context = Context(ROOT / "context.txt", ROOT / "hotwords.txt")
        self.last_context = ""
        self._cancel = False
        self.recorder = audio.Recorder(device=cfg["audio"]["device"])
        self.cmd_q: queue.Queue = queue.Queue()      # 钩子线程 -> worker
        self.ui_q: queue.Queue = queue.Queue()       # worker -> tkinter 主线程
        self.record_t0 = 0.0
        self._pending_pos = None
        self.paused = False
        self._quit = False

        winapi.set_dpi_aware()
        # 建 tk 窗口的那一刻前台就会被本进程抢走，所以要在那之前记下原来的窗口
        self._fg_before = winapi.user32.GetForegroundWindow()
        self.root = tk.Tk()
        self.root.withdraw()
        self.hud = Hud(self.root, cfg["hud"].get("opacity", 0.95),
                       position=cfg["hud"].get("position"),
                       on_move=self._save_position,
                       on_double_click=self.open_panel)
        self._hud_seq = 0                 # 每次换状态 +1，过期的「稍后收起」据此作废
        self.root.after(200, lambda: self._prime_overlay(announce))
        self.tray = Tray(self)
        self.tray.start()

        threading.Thread(target=self._load_model, name="model", daemon=True).start()
        self.trigger = Trigger(cfg, self._gate,
                               lambda: self.cmd_q.put("down"),
                               lambda: self.cmd_q.put("up"),
                               island=self.hud, on_cancel=self._request_cancel)
        self.trigger.start()
        threading.Thread(target=self._worker, name="worker", daemon=True).start()
        self.root.after(30, self._pump)

    def _gate(self):
        """暂停时一律拦截：触发键原样透传给下层窗口，侧键仍是浏览器的前进后退。"""
        if self.paused:
            return False, "已暂停"
        return self.gate.is_open()

    def set_paused(self, on: bool):
        self.paused = self.hud.paused = bool(on)
        self.tray.refresh()

    def request_quit(self):
        """由面板线程调用：只置标志，真正退出交给主线程 —— 从 HTTP 线程直接
        关 tkinter 会重现之前那个静默崩溃。"""
        self._quit = True

    def _prime_overlay(self, announce: bool):
        """启动时先让浮窗出现一次，再把焦点还给原来的窗口。

        实测建 tk 窗口和首次 ShowWindow 都会让本进程拿到前台（之后就不会再抢了）。浮窗待机时
        是隐藏的，不在这里先「烧掉」这一次，第一次按键说话时焦点就会被抢走，
        文字贴不进终端。从快捷方式启动（--announce）时顺便提示一下已启动。
        """
        before = self._fg_before
        if announce and self.cfg["hud"]["enabled"]:
            self._ui("done", "语音输入已启动", hide_after=1500)
            self._pump_once()
        else:
            winapi.user32.ShowWindow(self.hud.hwnd, 4)
            winapi.user32.ShowWindow(self.hud.hwnd, 0)
        if before and winapi.user32.GetForegroundWindow() != before:
            winapi.user32.SetForegroundWindow(before)

    # -------------------------------------------------------------- 模型
    def reload_model(self):
        """面板改了模型路径/端口后重建识别器。旧的 llama-server 先关掉，
        否则它会一直占着显存和端口。"""
        old, self.recognizer, self.rec_error = self.recognizer, None, None
        if old:
            old.shutdown()
        threading.Thread(target=self._load_model, name="model", daemon=True).start()

    def _load_model(self):
        """只校验文件和清理残留进程；真正加载在第一次按键时，见 asr.warm()。"""
        from asr import Recognizer
        try:
            self.status = "检查模型"
            self.recognizer = Recognizer(self.cfg["asr"])
            self.status = "就绪"
        except Exception as e:                     # 模型缺失/损坏要在面板里看得见
            self.rec_error = f"{type(e).__name__}: {e}"
            self.status = "模型不可用"

    # ------------------------------------------------------- 录音/识别
    def _worker(self):
        while True:
            cmd = self.cmd_q.get()
            try:
                if cmd == "down":
                    self._begin()
                elif cmd == "up":
                    self._finish()
                elif cmd == "cancel":
                    self._abort()
            except Exception as e:
                self._record(0, 0, skipped=f"出错：{type(e).__name__}: {e}")
                self._ui("blocked", f"出错：{type(e).__name__}", hide_after=1500)

    def _request_cancel(self) -> bool:
        """由钩子线程调用（Esc）：录音中或识别中才接管这个键，否则原样放行。"""
        if not (self.recorder.recording or self.hud.state == "thinking"):
            return False
        self._cancel = True
        self.cmd_q.put("cancel")
        return True

    def _abort(self):
        if self.recorder.recording:
            self.recorder.stop()
            self._cancel = False
        self._ui("blocked", "已取消", hide_after=700)

    def _begin(self):
        if self.recognizer is None:
            self._ui("blocked", self.rec_error and "模型加载失败" or "模型加载中…")
            return
        self.record_t0 = time.time()
        self._cancel = False
        try:
            self.recorder.start()
        except Exception as e:                     # 同 voice-dictate：打不开就直说，别干等
            self._record(0, 0, skipped=f"麦克风打不开：{e}")
            self._ui("blocked", "麦克风打不开：" + audio.device_name(self.recorder.device),
                     hide_after=3000)
            return
        self.recognizer.warm()                     # 模型加载藏在录音时间里
        self._ui("listening", "0:00")

    def _finish(self):
        if not self.recorder.recording:
            return
        samples = self.recorder.stop()
        dur = len(samples) / audio.SAMPLE_RATE
        peak = float(np.abs(samples).max()) if samples.size else 0.0

        # 每一次尝试都记账，包括被拦下的。按了没反应时如果什么痕迹都不留，
        # 用户无从判断是没触发、太短、太轻，还是没识别出内容。
        if dur * 1000 < self.cfg["min_duration_ms"]:
            self._record(dur, peak, skipped="太短了，没听清")
            self._ui("blocked", "太短了，没听清", hide_after=900)
            return
        if peak < self.cfg["min_level"]:
            self._record(dur, peak, skipped="没听到说话（音量 %.3f）" % peak)
            self._ui("blocked", "没听到声音，检查「%s」是否静音"
                     % audio.device_name(self.recorder.device), hide_after=4000)
            return

        self._ui("thinking")
        rid = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        tuning.save_audio(rid, samples)            # 调教台拿原声重新识别用
        ctx = self.context.build() if self.cfg["context"]["enabled"] else ""
        self.last_context = ctx
        _, raw, ms = self.recognizer.transcribe(samples, ctx)
        text = self.fixer.apply(raw)
        if self._cancel:                           # 识别途中按了 Esc：结果作废
            self._cancel = False
            self._record(dur, peak, raw=raw, ms=ms, skipped="已取消", rid=rid)
            return
        if not text:
            self._record(dur, peak, raw=raw, ms=ms, skipped="没有识别到内容", rid=rid)
            self._ui("blocked", "没有识别到内容", hide_after=900)
            return

        inject.paste_text(text, self.cfg["inject"]["method"],
                          self.cfg["inject"]["restore_clipboard_ms"],
                          self.cfg["inject"]["auto_enter"])
        self._record(dur, peak, raw=raw, text=text, ms=ms, rid=rid)
        self._ui("done", text, hide_after=self.cfg["hud"]["done_ms"])

    def _record(self, dur, peak, raw="", text="", ms=0.0, skipped="", rid=""):
        """写两份：给面板的结构化数据，和给人扫的对齐流水。

        两种读者要的东西不一样 —— 面板要能解析，人要能一眼看出哪条没上屏、
        为什么。把它们塞进同一个格式，结果就是两边都难受。
        """
        if not skipped:
            self.stats["count"] += 1
            self.stats["chars"] += len(text)
            self.stats["last"] = text

        now = datetime.now()
        row = {"id": rid, "ts": now.isoformat(timespec="seconds"),
               "raw": raw, "text": text, "skipped": skipped,
               "audio_s": round(dur, 2), "peak": round(peak, 3),
               "infer_ms": round(ms * 1000)}
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + chr(10))

        if not READABLE.exists():
            # 表头按数据行的实际列位对齐；中文字符在等宽字体里占 2 个显示宽度，
            # 所以空格数不等于字符数差
            header = ["cc-voice 识别记录    √ = 已上屏    · = 未上屏",
                      "",
                      "日期   时间      时长    音量   耗时      结果",
                      "-" * 76]
            READABLE.write_text("\n".join(header) + "\n", encoding="utf-8")

        mark, body = ("·", skipped) if skipped else ("√", text)
        if raw and raw != text and not skipped:
            body += "        原始: " + raw          # 热词纠正前后不一致时才显示
        line = "%s  %s  %5.1fs  %5.3f  %7s   %s %s" % (
            now.strftime("%m-%d"), now.strftime("%H:%M:%S"), dur, peak,
            ("%dms" % round(ms * 1000)) if ms else "-", mark, body)
        with READABLE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ----------------------------------------------------------- HUD 驱动
    # tkinter/Tcl 不是线程安全的：从 worker 或钩子线程调 root.after() 会破坏
    # 解释器状态，表现为进程凭空消失且没有 traceback（实测踩过）。所有跨线程
    # 的 UI 动作一律经队列，由主线程的 _pump 排空。
    def _ui(self, state, text="", hide_after=0):
        self.ui_q.put((state, text, hide_after))

    def _save_position(self, origin):
        """由钩子线程调用：只记下待办，落盘交给主线程 —— 低级钩子回调里
        做文件 I/O 会拖慢整个系统的输入响应。"""
        self._pending_pos = list(origin)

    def _idle_state(self):
        return "hidden"

    def open_panel(self):
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{self.cfg['panel_port']}/")

    def _pump_once(self):
        """排空 UI 队列、喂音量、刷计时、落盘位置。"""
        while True:
            try:
                state, text, hide_after = self.ui_q.get_nowait()
            except queue.Empty:
                break
            if not self.cfg["hud"]["enabled"]:
                continue
            self._hud_seq += 1
            self.hud.show(state, text)
            if hide_after:
                # 只收起自己这一次显示的状态：期间又按下了触发键，就别把新的聆听收掉
                seq = self._hud_seq
                self.root.after(hide_after, lambda: seq == self._hud_seq
                                and self.hud.show(self._idle_state()))

        if self.hud.state == "listening":
            self.hud.set_level(self.recorder.rms)
            el = time.time() - self.record_t0
            self.hud.text = "%d:%02d" % (int(el // 60), int(el % 60))

        if self._pending_pos is not None:
            self.cfg["hud"]["position"], self._pending_pos = self._pending_pos, None
            config.save(self.cfg)

    def _pump(self):
        self._pump_once()
        if self._quit:
            self.root.quit()
            return
        # 录音时 33ms 一次跟上音量；其余时候 80ms 足够接住按键，待机开销更低
        self.root.after(33 if self.hud.state == "listening" else 80, self._pump)

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.tray.stop()
            if self.recognizer:
                self.recognizer.shutdown()          # 显存跟着守护进程一起还回去


def probe(seconds: int = 20):
    """探测模式：打印你按下的每一个鼠标/键盘事件，用来确认侧键是否存在。"""
    print(f"请在 {seconds} 秒内依次按：鼠标侧键（两个都试）、中键、右 Ctrl。Ctrl+C 提前结束。\n")
    seen = set()

    def log(msg):
        if msg not in seen:
            seen.add(msg)
            print(" ", msg, flush=True)

    cfg = config.load()
    cfg["trigger"] = {"mouse_button": "none", "key": "none", "key_enabled": True}
    t = Trigger(cfg, lambda: (False, "probe"), lambda: None, lambda: None, probe=log)
    t.start()
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        pass
    t.stop()
    print("\n看到 'mouse x2 down' = 侧键可用；只看到 'mouse x1' 就把配置改成 x1；"
          "两个都没有说明这只鼠标没有侧键，用右 Ctrl 触发。")


def main():
    ap = argparse.ArgumentParser(prog="cc-voice")
    ap.add_argument("--probe", action="store_true", help="探测鼠标/键盘触发键")
    ap.add_argument("--probe-seconds", type=int, default=20)
    ap.add_argument("--announce", action="store_true", help="启动时提示一下已启动（桌面快捷方式用）")
    args = ap.parse_args()

    winapi.set_dpi_aware()
    if args.probe:
        probe(args.probe_seconds)
        return
    if not single_instance():
        print("cc-voice 已在运行")
        return

    cfg = config.load()
    app = App(cfg, announce=args.announce)
    import panel
    panel.serve(app, cfg["panel_port"])
    app.run()


if __name__ == "__main__":
    main()
