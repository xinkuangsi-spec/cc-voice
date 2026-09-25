"""Qwen3-ASR 识别：float32 波形 + 上下文 -> 中文文本。

模型跑在 llama.cpp 的 llama-server 上（Vulkan 版，显卡推理）。服务按需启动：
第一次按下触发键时拉起，加载约 4 秒，和录音同时进行，用户基本感觉不到；
闲置 idle_minutes 后自动退出，显存全部释放 —— 常驻要占约 1.5GB。

上下文走系统消息：Qwen3-ASR 会把它当作「这段话可能出现的词和背景」，
专有名词和同音字的识别明显变准（实测「康飞优爱」->「ComfyUI」，「工作数」
->「工作树」），而且只多几十个 token，延迟几乎不变。
"""
import base64
import collections
import io
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np

import winapi
from chinese_itn import chinese_to_num

LOAD_TIMEOUT_S = 60          # 实测加载 3.6s；冷盘会慢很多
REQUEST_TIMEOUT_S = 60
_NO_WINDOW = 0x08000000      # CREATE_NO_WINDOW：pythonw 下拉起子进程不闪黑框


def _wav_b64(samples: np.ndarray, sample_rate: int) -> str:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def clean(out: str) -> str:
    """模型原始输出 -> 可上屏的文本。

    Qwen3-ASR 的回答形如 `language Chinese<asr_text>……`，llama.cpp 不会替我们
    剥掉前缀（llama.cpp issue #26749）。它没有简繁开关，偶尔整句答成繁体，
    所以先转简体；数字它习惯写成汉字（「十六分钟」），再转回阿拉伯数字。
    """
    at = out.find("<asr_text>")
    text = out[at + len("<asr_text>"):] if at >= 0 else out
    return chinese_to_num(winapi.to_simplified(text.strip()))


class Recognizer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.url = f"http://127.0.0.1:{cfg['port']}"
        self._proc: subprocess.Popen | None = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._lock = threading.Lock()
        self._idle: threading.Timer | None = None
        missing = [k for k in ("llama_server", "model", "mmproj") if not Path(cfg[k]).is_file()]
        if missing:
            raise FileNotFoundError("找不到 " + "、".join(f"{k}={cfg[k]}" for k in missing))
        self._kill_stray()

    # ------------------------------------------------------------ 服务生命周期
    def _kill_stray(self) -> None:
        """守护进程被强杀时 llama-server 会留下来，继续占着显存和端口。"""
        cmd = ("Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
               f"? CommandLine -like '*--port {self.cfg['port']}*' | "
               "% { Stop-Process -Id $_.ProcessId -Force }")
        subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       creationflags=_NO_WINDOW, capture_output=True)

    @property
    def loaded(self) -> bool:
        return self._ready.is_set()

    def warm(self) -> None:
        """按下触发键时调用：服务没起就在后台拉起，模型加载藏在录音时间里。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._ready.clear()
            self._error = None
            self._proc = subprocess.Popen(
                [self.cfg["llama_server"], "-m", self.cfg["model"],
                 "--mmproj", self.cfg["mmproj"], "-ngl", "99", "-c", "4096",
                 # 默认 mmap 会让模型在内存里多挂约 0.8GB
                 "--load-mode", "none",
                 "--host", "127.0.0.1", "--port", str(self.cfg["port"])],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
            proc = self._proc
        threading.Thread(target=self._wait_ready, args=(proc,), daemon=True).start()
        self._touch()

    def _wait_ready(self, proc: subprocess.Popen) -> None:
        tail: collections.deque[bytes] = collections.deque(maxlen=20)
        stderr = proc.stderr
        # stderr 必须持续读空，否则管道写满后 llama-server 会卡住
        threading.Thread(target=lambda: [tail.append(l) for l in stderr or ()],
                         daemon=True).start()
        deadline = time.time() + LOAD_TIMEOUT_S
        while time.time() < deadline:
            if proc.poll() is not None:
                msg = b"".join(list(tail)[-3:]).decode("utf-8", "replace").strip()
                self._error = f"llama-server 退出了：{msg}"
                self._ready.set()
                return
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=1) as r:
                    if r.status == 200:
                        self._ready.set()
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.2)
        proc.kill()
        self._error = f"llama-server {LOAD_TIMEOUT_S}s 内没加载完"
        self._ready.set()

    def _touch(self) -> None:
        """每次使用都把闲置计时清零；到点就关服务，把显存还回去。"""
        if self._idle:
            self._idle.cancel()
        self._idle = threading.Timer(self.cfg["idle_minutes"] * 60, self.shutdown)
        self._idle.daemon = True
        self._idle.start()

    def shutdown(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.kill()
            self._proc = None
            self._ready.clear()

    # ------------------------------------------------------------ 识别
    def transcribe(self, samples: np.ndarray, context: str = "",
                   sample_rate: int = 16000) -> tuple[str, str, float]:
        """返回 (模型原始输出, 清洗后的文本, 推理耗时秒)。"""
        if samples.size == 0:
            return "", "", 0.0
        self.warm()
        self._ready.wait(LOAD_TIMEOUT_S + 5)
        if self._error:
            raise RuntimeError(self._error)
        t0 = time.time()
        messages = []
        if context:
            messages.append({"role": "system", "content": context})
        messages.append({"role": "user", "content": [{"type": "input_audio", "input_audio": {
            "data": _wav_b64(samples, sample_rate), "format": "wav"}}]})
        req = urllib.request.Request(
            self.url + "/v1/chat/completions",
            json.dumps({"temperature": 0, "messages": messages}).encode("utf-8"),
            {"Content-Type": "application/json"})
        out = ""
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as r:
                    out = json.loads(r.read())["choices"][0]["message"]["content"]
                break
            except urllib.error.HTTPError:
                raise
            except urllib.error.URLError:
                # 连接级失败重试一次：别的程序把系统套接字占满时（实测微信一个进程
                # 挂着 2700+ 连接）会偶发 WinError 10055，不重试这段录音就白说了
                if attempt == 2:
                    raise
                time.sleep(0.3)
        self._touch()
        return out, clean(out), time.time() - t0
