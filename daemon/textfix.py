"""识别后处理：rules.txt 精确替换 —— 治固定口误和写法偏好。

专有名词不在这里救，交给识别上下文（context.py）：热词表会作为上下文直接交给
Qwen3-ASR，由模型结合整句去判断。这里原先还有一道按拼音相似度的热词强制替换，
是给 sherpa 小模型兜底用的；换成 Qwen3-ASR 后实测它只帮倒忙 —— 把模型已经认对的
「这件事」改成「组件事」、「工作树」改回「工作流」，6 句里纠对 0 次、改错 2 次，
所以删掉了。
"""
from pathlib import Path


class TextFixer:
    def __init__(self, rules_file: Path):
        self.rules_file = Path(rules_file)
        self.rules: list[tuple[str, str]] = []
        self.reload()

    def reload(self) -> None:
        self.rules = []
        if self.rules_file.exists():
            for line in self.rules_file.read_text(encoding="utf-8").splitlines():
                line = line.split("#")[0].strip()
                if "=>" in line:
                    src, dst = line.split("=>", 1)
                    if src.strip():
                        self.rules.append((src.strip(), dst.strip()))

    def apply(self, text: str) -> str:
        for src, dst in self.rules:
            text = text.replace(src, dst)
        return text.strip()
