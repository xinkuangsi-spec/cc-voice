"""调教台：把「改了上下文，识别到底有没有变好」变成看得见的数字。

三样东西：
  录音     每次送识别的音频都存一份（logs/audio/<id>.wav，只留最近 KEEP 条），
           改了上下文可以拿原声重新识别，不用再说一遍
  纠正     你在面板里把识别结果改成「本来想说的」，存进 corrections.json ——
           这就是你的习惯数据：改动的词会被提成候选热词，一键加进上下文
  字错率   拿所有纠正过的录音当考卷，分别用不带上下文 / 当前上下文 / 草稿上下文
           重新识别，算字错率。改完上下文先跑一遍，分数降了再保存
"""
import json
import re
import threading
import wave
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
AUDIO = ROOT / "logs" / "audio"
CORRECTIONS = ROOT / "logs" / "data" / "corrections.json"
KEEP = 300                       # 约 300 条 × 平均 5 秒 × 32KB/s ≈ 50MB
SAMPLE_RATE = 16000

_lock = threading.Lock()


# ------------------------------------------------------------------ 录音
def save_audio(rid: str, samples: np.ndarray) -> None:
    AUDIO.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    with wave.open(str(AUDIO / f"{rid}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    keep = {rid} | set(load_corrections())            # 纠正过的是考卷，永远不删
    old = sorted(AUDIO.glob("*.wav"))[:-KEEP]
    for p in old:
        if p.stem not in keep:
            p.unlink(missing_ok=True)


def audio_path(rid: str) -> Path | None:
    if not re.fullmatch(r"[0-9-]+", rid or ""):        # id 来自 URL，只认数字和横线
        return None
    p = AUDIO / f"{rid}.wav"
    return p if p.exists() else None


def load_audio(rid: str) -> np.ndarray | None:
    p = audio_path(rid)
    if p is None:
        return None
    with wave.open(str(p)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


# ------------------------------------------------------------------ 纠正
def load_corrections() -> dict:
    try:
        return json.loads(CORRECTIONS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_correction(rid: str, recognized: str, truth: str) -> dict:
    with _lock:
        data = load_corrections()
        truth = truth.strip()
        if truth:                  # 和识别结果一样也存：确认识别正确的，同样是考卷
            data[rid] = {"recognized": recognized, "truth": truth}
        else:
            data.pop(rid, None)
        CORRECTIONS.parent.mkdir(parents=True, exist_ok=True)
        CORRECTIONS.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return data


# ------------------------------------------------------------------ 打分
_PUNCT = re.compile(r"[\s，。、！？；：,.!?;:\"'“”‘’（）()《》<>【】\[\]…—-]+")


def _norm(s: str) -> str:
    """只比字：去掉标点空白、英文统一小写 —— 标点和空格不算识别错误。"""
    return _PUNCT.sub("", s or "").lower()


def cer(truth: str, hyp: str) -> tuple[int, int]:
    """返回 (编辑距离, 参考长度)。汇总时先加总再相除，长句短句按字数加权。"""
    a, b = _norm(truth), _norm(hyp)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1], len(a)


def suggest_hotwords(recognized: str, truth: str) -> list[str]:
    """从「识别成 X、其实是 Y」里提候选热词：Y 里被改动的片段，扩到完整的词。

    只挑值得进热词表的：含英文的、或至少两个汉字的。单个错字多半是同音字，
    上下文里写清话题比塞一个字进热词表有用。
    """
    out = []
    sm = SequenceMatcher(None, recognized, truth, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        # 英文往两边扩到整个单词（改了 cpp 就提 llama.cpp）
        while j1 > 0 and re.match(r"[A-Za-z0-9.+#_-]", truth[j1 - 1]) and re.match(r"[A-Za-z0-9.+#_-]", truth[j1]):
            j1 -= 1
        while j2 < len(truth) and re.match(r"[A-Za-z0-9.+#_-]", truth[j2]) and j2 > 0 and re.match(r"[A-Za-z0-9.+#_-]", truth[j2 - 1]):
            j2 += 1
        seg = truth[j1:j2].strip(" ，。、！？,.!?")
        if not recognized[i1:i2].strip(" ，。、！？,.!?") and re.fullmatch(r"[一-鿿]", seg or ""):
            continue                # 只是漏了一个字（常见是「的」「了」），不是词的问题
        if re.fullmatch(r"[一-鿿]", seg or ""):
            # 单个汉字改错多落在词尾（工作数 -> 工作树），往前带两个汉字；
            # 在句首就往后带。面板里候选词可以再手改
            lo = j1
            while lo > 0 and j1 - lo < 2 and re.match(r"[一-鿿]", truth[lo - 1]):
                lo -= 1
            hi = j2 if lo < j1 else min(len(truth), j2 + 2)
            seg = truth[lo:hi].strip(" ，。、！？,.!?")
        if seg and (re.search(r"[A-Za-z]", seg) or len(re.findall(r"[一-鿿]", seg)) >= 2):
            if seg not in out:
                out.append(seg)
    return out
