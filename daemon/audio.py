"""麦克风采集：按下开始、松开结束的定长录音。

回调线程只做 append，不做任何重活 —— sounddevice 的回调超时会导致丢帧。
音量包络单独抽出来给 HUD 画波形。
"""
import threading

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000


class Recorder:
    def __init__(self, device=None, sample_rate: int = SAMPLE_RATE):
        self.device = device
        self.sample_rate = sample_rate
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self.level = 0.0                      # 0..1，峰值包络
        self.rms = 0.0                        # 0..1，最近一块的均方根，浮窗按 dBFS 画音量条

    def _cb(self, indata, frames, time_info, status):
        block = indata[:, 0].copy()
        with self._lock:
            self._chunks.append(block)
        peak = float(np.abs(block).max())
        self.rms = float(np.sqrt(np.mean(block * block)))
        # 慢降快升：让波形跟得上说话、又不会一停就塌成直线
        self.level = peak if peak > self.level else self.level * 0.75 + peak * 0.25

    def start(self) -> None:
        if self._stream is not None:
            return
        with self._lock:
            self._chunks = []
        self.level = self.rms = 0.0
        self._stream = sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32",
            blocksize=512, device=self.device, callback=self._cb,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        if self._stream is None:
            return np.zeros(0, dtype=np.float32)
        self._stream.stop()
        self._stream.close()
        self._stream = None
        self.level = self.rms = 0.0
        with self._lock:
            chunks, self._chunks = self._chunks, []
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    @property
    def recording(self) -> bool:
        return self._stream is not None


def device_name(index) -> str:
    """提示文案里用的麦克风名字；None 表示跟随系统默认。"""
    try:
        return sd.query_devices(index if index is not None else sd.default.device[0])["name"]
    except Exception:
        return "麦克风"


def list_devices() -> list[dict]:
    out = []
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append({"index": i, "name": d["name"],
                        "channels": d["max_input_channels"],
                        "default": i == default_in})
    return out
