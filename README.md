# cc-voice

Windows 上的中文语音输入。按住触发键说话，松开自动把文字上屏 —— Claude Code
终端、微信、浏览器、任何能打字的地方。**全程本地**，不联网、不需要 API Key。

> 本仓库 fork 自 [Mumumumuyi/cc-voice](https://github.com/Mumumumuyi/cc-voice)，保留了它的悬浮岛和管理面板，
> 识别内核从 sherpa-onnx（CPU）换成了 **Qwen3-ASR-1.7B（llama.cpp，显卡推理）**，并加了**识别上下文**：
> 把你的习惯、常用词、最近说过的话一起交给模型，专有名词和同音字更准。改动见文末「相对上游的改动」。

- 识别内核：[Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) 的 [GGUF](https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF)，跑在 [llama.cpp](https://github.com/ggml-org/llama.cpp) 的 Vulkan 版上（AMD / NVIDIA / Intel 显卡通用）
- 识别上下文：`context.txt`（你的习惯说明）+ `hotwords.txt`（专有名词）+ 最近几句上屏的话
- 悬浮岛：乳白玻璃质感的常驻药丸，可拖动，红灯待机 / 绿灯录音
- 管理面板：本地网页，可调触发键、生效范围、上下文、热词表、模型路径

## 从零安装

需要 Windows 10/11、[uv](https://github.com/astral-sh/uv)（管 Python 环境）、一块能跑 Vulkan 的显卡（常驻约 1.5GB 显存）。

```powershell
git clone https://github.com/xinkuangsi-spec/cc-voice.git
cd cc-voice

uv venv --python 3.11 .venv
uv pip install --python .venv\Scripts\python.exe sounddevice numpy pillow

pwsh -File tools\fetch_models.ps1        # 下载 Qwen3-ASR GGUF 到 D:\models\qwen3-asr-1.7b，约 2.4GB
```

llama.cpp 从 [releases](https://github.com/ggml-org/llama.cpp/releases) 下 Windows Vulkan 版解压，默认路径是
`D:\models\llama.cpp\bin\llama-server.exe`。模型和 llama.cpp 放在别处的话，在管理面板的「模型位置」里改。

模型、llama.cpp、虚拟环境都不入库，所以克隆后要跑上面这几步。

**显存按需占用**：llama-server 在第一次按下触发键时才启动（加载约 4 秒，和录音同时进行），
闲置 10 分钟（面板可调）后自动退出，显存全部还回去。退出守护进程时也会一并关掉。

做一个桌面快捷方式指向 `语音输入开关.cmd`，双击即可开关。想让它跟 Claude Code
一起自动启动，再执行：

```powershell
claude plugin marketplace add "$env:USERPROFILE\.claude-voice\plugin"
claude plugin install cc-voice@cc-voice-local
```

## 启动和关闭

桌面上一个快捷方式 **「语音输入」**，双击一次开，再双击一次关。

开着的时候屏幕底部有一枚乳白色悬浮岛（红灯=待机）—— **它就是状态指示**：
在，就是开着；不在，就是关着。

脚本本体是 `语音输入开关.cmd`（内部调 `tools/toggle.ps1`）。它靠守护进程的
命名互斥量判断当前状态，和守护进程自己做单实例判断用的是同一个权威源。

插件只负责「开 Claude Code 时顺手把守护进程拉起来」这一件事，**语音功能本身
不依赖它**。默认生效范围是「全局任意窗口」；在面板里改成「仅 Claude Code 终端」后，
闸门会自己枚举进程找 `claude.exe` 来认终端。插件钩子找不到项目时会回落到
`%USERPROFILE%\.claude-voice`，放在别处又想用插件，就在那里建一个目录联接指过来。

**跟 cc/cc2/cc3 一起自动启动**（可选，默认关闭）：

```
claude plugin enable  cc-voice@cc-voice-local     # 开：以后开 Claude Code 自动启动
claude plugin disable cc-voice@cc-voice-local     # 关：只用桌面快捷方式手动启停
```

## 怎么说话

1. 点一下要输入的窗口
2. **按住鼠标侧键 X2**（或**右 Ctrl**）→ 停半拍 → 说话 → 说完再松开
3. 灯变绿表示在听，松开后文字自动贴进输入框（不会自动回车）
4. 说错了：录音中或识别中按 **Esc** 取消，这一段不上屏

悬浮岛可以**拖到任何位置**，松手即记住；**双击**它打开管理面板。

## 首次要做的一件事：确认侧键

WMI 报不出鼠标按钮数，你的鼠标有没有 X2 侧键只能实测：

```
.venv\Scripts\python.exe daemon\ccvoice.py --probe
```

按提示依次按两个侧键。看到 `mouse x2 down` 就说明默认配置可用；只看到 `mouse x1`
就在管理面板里把「鼠标触发键」改成 X1；两个都没有，说明这只鼠标没有侧键，用右 Ctrl。

## 管理面板

`http://127.0.0.1:8731/`（双击悬浮岛也能打开）。可调：麦克风、触发键、生效范围、
上屏方式、悬浮岛不透明度、静音阈值、显存释放时间、模型路径，以及识别上下文、热词表与替换规则。
「我的习惯」卡片下方会显示**下一次识别实际发给模型的上下文**，所见即所得。
右下角还有「暂停 / 恢复」和「退出」。

「最近识别」会记录**每一次尝试**，包括没上屏的，并显示录音时长、音量峰值和未上屏
原因（太短 / 没听到说话 / 没识别到内容）—— 按了没反应时先看这里。

## 日志

```
logs/
  识别记录.log        人看的：对齐流水，√ 已上屏 / · 未上屏，记事本直接打开
  data/history.jsonl  面板读的结构化数据
  hook.log            只在插件钩子出错时才会出现
```

两种读者要的东西不一样 —— 面板要能解析，人要能一眼扫出哪条没上屏、为什么。
塞进同一个格式的结果是两边都难受，所以分开写。

## 识别上下文（让模型认识你）

Qwen3-ASR 支持在系统消息里放一段参考文本：说话的是谁、在聊什么、会出现哪些词。
模型会照着它去判断同音字和专有名词。每次识别都会拼上三路内容：

| 来源 | 放什么 |
|---|---|
| `context.txt` | 你的习惯：身份、在做的项目、常聊的话题、说话方式 |
| `hotwords.txt` | 专有名词，一行一个：项目代号、工具名、库名、人名 |
| 最近识别 | 最近几句上屏的话（默认 10 句，面板可调，0 = 不带），让模型跟上你这几天的话题 |

**写背景和词，不写指令。** 模型把它当参考文本而不是命令，「请输出简体」这类话它不会照做。

同一段 TTS 合成音频（Windows 自带 Huihui 语音），带与不带上下文的实测对比：

| 说的是 | 不带上下文 | 带上下文 |
|---|---|---|
| ComfyUI 的工作流 | 康飞优爱的工作流 | ComfyUI 工作流 |
| 新的工作树 | 新的工作数 | 新的工作树 |
| 用 Orca 开…派 Devin 去… | 用欧卡开…派德文去… | 用 Orca 开…派 Devin 去…（最近识别里出现过这两个词） |

代价很小（6 句平均推理耗时，AMD Radeon RX 9060 XT）：不带上下文 0.90s；习惯+热词（519 字）0.96s；
再加最近 10 句（678 字）0.99s。

**模型固定写错的，交给 `rules.txt`**（`原文=>替换`，识别完之后执行），比如带上下文时它偶尔把
GitHub 写成 `Git Hub`，规则里已经加了一条。

## 数字和繁简

Qwen3-ASR 习惯把数字写成汉字、偶尔整句答成繁体，识别后统一处理：

- 繁转简：Windows 自带的 `LCMapStringEx`，不加依赖
- 中文数字转阿拉伯数字：`daemon/chinese_itn.py`，取自 [CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline)（MIT）。
  「十六分钟」→「16分钟」、「百分之三十」→「30%」、「三点十五分」→「03:15」；
  成语和副词「十分重要」不动

## 目录

```
daemon/     守护进程：触发、录音、识别、注入、灵动岛、管理面板
  winapi.py   Win32 绑定（DPI、剪贴板、SendInput、低级钩子）
  trigger.py  低级鼠标/键盘钩子：触发判定 + 灵动岛拖动
  gate.py     会话闸门：枚举进程找 claude.exe，判断前台窗口是不是它的终端
  audio.py    麦克风采集      asr.py     Qwen3-ASR（llama-server 按需启停）
  context.py  识别上下文拼装   textfix.py 规则替换
  chinese_itn.py 中文数字转阿拉伯数字     inject.py  剪贴板上屏
  render.py   Pillow 出图      layered.py 分层窗口推送
  hud.py      灵动岛状态机     panel.py + web/  管理面板
context.txt 识别上下文：你的习惯说明
hotwords.txt 专有名词表（也进上下文）   rules.txt 识别后的精确替换
plugin/     本地插件市场：SessionStart 钩子 + /voice 命令
tools/      toggle.ps1（开关）、fetch_models.ps1（下载 Qwen3-ASR GGUF）
logs/       history.jsonl（识别历史）、gate.log、hook.log
```

## 踩过的坑（改代码前先读）

- **tkinter 不是线程安全的**：工作线程调 `root.after()` 会让进程静默崩溃、没有
  traceback。所有跨线程 UI 动作走 `ui_q` 队列，由主线程 `_pump` 排空。
- **ctypes 默认 restype 是 32 位有符号 int**：x64 上句柄/指针被截断后解引用 =
  访问违例。所有返回或接收句柄的 Win32 调用都必须显式声明类型。
- **PowerShell 5.1 的 `Set-Content -Encoding utf8` 一定写 BOM**，Python 侧
  `json.loads` 会抛异常。写用 `UTF8Encoding($false)`，读用 `utf-8-sig`。
- **灵动岛必须保持 `WS_EX_TRANSPARENT`**：一旦可点击，tkinter 处理点击时会把窗口
  顶到前台，而闸门靠前台窗口 PID 判断终端身份，语音输入会彻底失效。拖动因此
  由低级鼠标钩子在消息抵达窗口之前拦截实现。
- **ASR 模型没有「静音」这个输出**：喂它底噪一定会硬猜出字来。静音判定必须在送进
  模型之前按音频电平做（`min_level`）。Qwen3-ASR 的回答带 `language Chinese<asr_text>`
  前缀，llama.cpp 不会剥，必须自己剥掉，否则会被原样粘进输入框。
- **拼音模糊纠错和上下文互相打架**：上游原有一道「按拼音相似度强制替换成热词」的后处理，
  换成 Qwen3-ASR + 上下文后实测只帮倒忙（把认对的「这件事」改成「组件事」、「工作树」
  改回「工作流」），已删除。专有名词交给上下文，固定错写交给 `rules.txt`。
- **强杀守护进程会把 llama-server 留在显卡上**：它是子进程，不会跟着死。守护进程启动时
  按端口清理残留；`toggle.ps1` 先走面板接口正常退出，不行才连同 llama-server 一起杀。
- **venv 里的 `pythonw.exe` 只是启动器**：真正的解释器是它的子进程、路径在 venv 之外，
  按可执行文件路径找守护进程会漏掉，要按命令行里的 `ccvoice.py` 认。
- **端口别和 voice-dictate 共用**：它启动时会按端口清理 llama-server，所以这里默认用 8379。
  两个工具都挂鼠标侧键，同时开会重复上屏，二选一。
- **守护进程的互斥量名必须与钩子里 `OpenExisting` 的完全一致**。曾经一边 `Global\`
  一边 `Local\`，钩子永远探不到，于是每开一个 cc 窗口就多起一个守护进程 —— 多个
  进程抢同一个麦克风、重复注入，表现为「录音经常断」。
- **低级钩子回调里不能做文件 I/O**：Windows 有 LowLevelHooksTimeout，超时会把钩子
  静默摘掉，表现是「时好时坏地失灵」。闸门的会话刷新因此挪到主线程周期执行。
- **`.cmd` 文件内容必须是纯 ASCII**：批处理按系统 OEM 代码页(GBK)读文件，UTF-8 的
  中文注释会变成乱码字节并被当作命令执行。文件名用中文没问题。

## 相对上游的改动

- 识别内核：sherpa-onnx + FunASR-Nano/SenseVoice（CPU）→ Qwen3-ASR-1.7B Q8_0（llama.cpp Vulkan，显卡），
  按需启动、闲置释放显存
- 新增识别上下文：`context.txt` + 热词表 + 最近识别，面板可编辑、可预览
- 新增繁转简、中文数字转阿拉伯数字
- 新增 Esc 取消
- 删除拼音模糊热词替换（理由见「踩过的坑」）
- 默认生效范围改为全局任意窗口
- 面板：定时刷新不再冲掉正在编辑的表单；模型选择改为模型路径
- `toggle.ps1`：不再依赖安装在 `.claude-voice` 目录；关闭时先正常退出，释放显存
