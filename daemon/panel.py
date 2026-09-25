"""本地管理面板：127.0.0.1 上的极小 HTTP 服务 + 单页前端。

不用任何 Web 框架 —— 只有几个 JSON 端点，标准库的 http.server 足够，
省掉一整条依赖链。只监听回环地址，不对外暴露。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import audio
import config
import tuning

ROOT = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent / "web"
HISTORY = ROOT / "logs" / "data" / "history.jsonl"


def _tail_history(n: int = 30) -> list:
    if not HISTORY.exists():
        return []
    lines = HISTORY.read_text(encoding="utf-8").splitlines()[-n:]
    fixed = tuning.load_corrections()
    out = []
    for ln in reversed(lines):
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        rid = row.get("id", "")
        row["truth"] = fixed.get(rid, {}).get("truth", "")
        row["has_audio"] = bool(rid and tuning.audio_path(rid))
        out.append(row)
    return out


# (录音 id, 上下文) -> 识别结果。上下文没改就不重复跑模型，批量评分改一处只重算变了的
_cache: dict = {}


def _recognize(app, rid: str, ctx: str) -> str:
    key = (rid, ctx)
    if key not in _cache:
        samples = tuning.load_audio(rid)
        if samples is None:
            raise FileNotFoundError(f"录音 {rid} 已被清理")
        _, clean, _ = app.recognizer.transcribe(samples, ctx)
        _cache[key] = app.fixer.apply(clean)
    return _cache[key]


def _compare(app, rid: str, recognized: str, truth: str, notes, hotwords) -> dict:
    """同一段录音：不带上下文 / 当前上下文 / 草稿上下文各识别一次。"""
    current = app.context.build()
    draft = app.context.build(notes=notes, hotwords=hotwords)
    out = {"id": rid, "recognized": recognized, "truth": truth,
           "none": _recognize(app, rid, ""), "current": _recognize(app, rid, current)}
    out["draft"] = _recognize(app, rid, draft) if draft != current else None
    return out


def _find_row(rid: str) -> dict:
    for row in _tail_history(2000):
        if row.get("id") == rid:
            return row
    return {}


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass                                   # 别把请求日志刷进控制台

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return True                            # 让 `a() or b()` 的分派在发出响应后停下

        def _local(self) -> bool:
            """只认面板自己发来的请求。

            只监听回环地址挡不住浏览器：你访问的任何网站都能往 127.0.0.1 发表单
            POST，改热词、改模型路径（下次按键就会执行那个路径上的程序）。跨站请求
            浏览器一定会带 Origin；Host 再校验一遍，防 DNS 重绑定。
            """
            port = self.server.server_address[1]
            ok = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origin = self.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] not in ok:
                return False
            return self.headers.get("Host") in ok

        def do_GET(self):
            if not self._local():
                return self._send(403, "{}")
            if self.path.startswith("/api/audio/"):
                p = tuning.audio_path(self.path.rsplit("/", 1)[-1].removesuffix(".wav"))
                if p is None:
                    return self._send(404, "{}")
                return self._send(200, p.read_bytes(), "audio/wav")
            if self.path in ("/", "/index.html"):
                return self._send(200, _read(WEB / "index.html"),
                                  "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, json.dumps({
                    "status": app.status,
                    "paused": app.paused,
                    "error": app.rec_error,
                    "config": app.cfg,
                    "asr_loaded": bool(app.recognizer and app.recognizer.loaded),
                    "devices": audio.list_devices(),
                    "stats": app.stats,
                    "sessions": [{"pid": s.get("claude_pid"), "entry": s.get("entry"),
                                  "cwd": s.get("cwd")} for s in app.gate.sessions()],
                    "hotwords": _read(ROOT / "hotwords.txt"),
                    "rules": _read(ROOT / "rules.txt"),
                    "context": _read(ROOT / "context.txt"),
                    # 下一次识别会发给模型的上下文，所见即所得
                    "context_preview": app.context.build() if app.cfg["context"]["enabled"] else "",
                    "history": _tail_history(),
                }, ensure_ascii=False))
            return self._send(404, "{}")

        def do_POST(self):
            if not self._local():
                return self._send(403, "{}")
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or "{}")
            try:
                return self._tuning(body) or self._settings(body)
            except Exception as e:                     # 识别失败要在面板上看得见
                return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"},
                                                  ensure_ascii=False))

        def _tuning(self, body):
            """调教台的四个接口。返回 None 表示不是这里的路径。"""
            if self.path == "/api/retry":
                row = _find_row(body.get("id", ""))
                truth = tuning.load_corrections().get(row.get("id"), {}).get("truth", "")
                return self._send(200, json.dumps(_compare(
                    app, row["id"], row.get("text") or row.get("raw", ""), truth,
                    body.get("notes"), body.get("hotwords")), ensure_ascii=False))
            if self.path == "/api/correct":
                row = _find_row(body.get("id", ""))
                recognized = row.get("text") or row.get("raw", "")
                truth = body.get("truth", "").strip()
                tuning.save_correction(row["id"], recognized, truth)
                return self._send(200, json.dumps({
                    "suggest": [w for w in tuning.suggest_hotwords(recognized, truth)
                                if w not in app.context.hotwords]}, ensure_ascii=False))
            if self.path == "/api/hotwords/add":
                f = ROOT / "hotwords.txt"
                have = set(app.context.hotwords)
                add = [w.strip() for w in body.get("words", []) if w.strip() and w.strip() not in have]
                if add:
                    text = _read(f).rstrip("\n") + "\n" + "\n".join(add) + "\n"
                    f.write_text(text, encoding="utf-8")
                    app.context.reload()
                return self._send(200, json.dumps({"added": add}, ensure_ascii=False))
            if self.path == "/api/eval":
                items, total = [], {"none": [0, 0], "current": [0, 0], "draft": [0, 0]}
                rows = {r["id"]: r for r in _tail_history(2000) if r.get("id")}
                for rid, c in tuning.load_corrections().items():
                    if rid not in rows or not tuning.audio_path(rid):
                        continue
                    r = _compare(app, rid, c["recognized"], c["truth"],
                                 body.get("notes"), body.get("hotwords"))
                    for k in total:
                        hyp = r[k] if r[k] is not None else r["current"]
                        e, n = tuning.cer(c["truth"], hyp)
                        total[k][0] += e
                        total[k][1] += n
                    items.append(r)
                cer = {k: (e / n if n else None) for k, (e, n) in total.items()}
                return self._send(200, json.dumps({"items": items, "cer": cer}, ensure_ascii=False))
            return None

        def _settings(self, body):
            if self.path == "/api/config":
                changed_asr = body.get("asr") != app.cfg.get("asr")
                app.cfg.update(body)
                config.save(app.cfg)
                app.gate.mode = app.cfg["gate_mode"]
                app.recorder.device = app.cfg["audio"]["device"]
                if changed_asr:
                    app.reload_model()
                return self._send(200, json.dumps({"ok": True}))
            if self.path == "/api/control":
                act = body.get("action")
                if act in ("pause", "resume"):
                    app.set_paused(act == "pause")
                elif act == "quit":
                    app.request_quit()
                return self._send(200, json.dumps({"ok": True, "paused": app.paused}))
            if self.path == "/api/text":
                # 只写请求里带了的：别的 AI 按 voice-fix 技能只改一份时，别把另外两份清空
                for key in ("hotwords", "rules", "context"):
                    if isinstance(body.get(key), str):
                        (ROOT / f"{key}.txt").write_text(body[key], encoding="utf-8")
                app.fixer.reload()
                app.context.reload()
                return self._send(200, json.dumps({"ok": True}))
            return self._send(404, "{}")

    return Handler


def serve(app, port: int) -> str:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    threading.Thread(target=srv.serve_forever, name="panel", daemon=True).start()
    return f"http://127.0.0.1:{port}/"
