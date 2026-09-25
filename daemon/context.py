"""识别上下文：告诉 Qwen3-ASR「说话的是谁、平时说什么」。

两路来源拼成一段系统消息，每次识别都带上：
  context.txt    你的习惯说明：身份、在做的项目、常聊的话题
  hotwords.txt   专有名词表；在面板里纠正识别结果时，改动的词会被提成候选热词

只放「可能出现的词和背景」，不放指令：Qwen3-ASR 把系统消息当参考文本而不是
命令，写「请输出简体」之类没有用，反而会挤占它对词表的注意力。

**不放整句的历史发言。** 曾经把「最近说过的 10 句」也放进来，实测会让模型把
其中一句原样当答案吐出来：念的是「让扣德克斯去读一下拉马点CPP的文档」，
输出成了上一句「让欧卡开一个新的工作树，然后派德文去跑测试」（温度 0，稳定复现；
只带习惯+热词、或只带历史句子时都不会）。认错几个字能改，整句换掉是灾难，
而它带来的那点好处（认出 Orca、Devin）热词表已经覆盖了。
"""
from pathlib import Path


def lines(text: str) -> list[str]:
    """去掉 # 注释和空行。"""
    out = []
    for line in (text or "").splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.append(line)
    return out


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


class Context:
    def __init__(self, context_file: Path, hotwords_file: Path):
        self.context_file, self.hotwords_file = Path(context_file), Path(hotwords_file)
        self.reload()

    def reload(self) -> None:
        self.notes = lines(_read(self.context_file))
        self.hotwords = lines(_read(self.hotwords_file))

    def build(self, notes: str | None = None, hotwords: str | None = None) -> str:
        """notes / hotwords 传文本时用它（面板里还没保存的草稿），否则用文件里的。"""
        n = self.notes if notes is None else lines(notes)
        h = self.hotwords if hotwords is None else lines(hotwords)
        parts = []
        if n:
            parts.append("\n".join(n))
        if h:
            parts.append("常用词：" + "、".join(h))
        return "\n\n".join(parts)
