"""识别上下文：告诉 Qwen3-ASR「说话的是谁、平时说什么」。

三路来源拼成一段系统消息，每次识别都带上：
  context.txt    你手写的习惯说明：身份、在做的项目、说话方式
  hotwords.txt   专有名词表 —— 同一份表，模型先看到它，事后的拼音纠错再兜一次底
  最近识别        最近几句上屏的话，让模型跟上你这几天在聊的话题

只放「可能出现的词和背景」，不放指令：Qwen3-ASR 把系统消息当参考文本而不是
命令，写「请输出简体」之类没有用，反而会挤占它对词表的注意力。
"""
from collections import deque
from pathlib import Path


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.append(line)
    return out


class Context:
    def __init__(self, context_file: Path, hotwords_file: Path, recent: int = 10):
        self.context_file, self.hotwords_file = Path(context_file), Path(hotwords_file)
        self.recent: deque[str] = deque(maxlen=max(recent, 0))
        self.reload()

    def reload(self) -> None:
        self.notes = _lines(self.context_file)
        self.hotwords = _lines(self.hotwords_file)

    def resize(self, n: int) -> None:
        self.recent = deque(self.recent, maxlen=max(n, 0))

    def remember(self, text: str) -> None:
        if self.recent.maxlen and text:
            self.recent.append(text)

    def build(self) -> str:
        parts = []
        if self.notes:
            parts.append("\n".join(self.notes))
        if self.hotwords:
            parts.append("常用词：" + "、".join(self.hotwords))
        if self.recent:
            parts.append("最近说过：\n" + "\n".join(self.recent))
        return "\n\n".join(parts)
