# -*- coding: utf-8 -*-
"""TTS 子进程：文本 → 音频文件。**没有 handle，不会被当插件加载**（同 jm_worker.py）。

两个后端：
  piper（离线、CPU、零费用，音色偏播音）
  edge （微软 Edge 在线音色，免费无需 key，音色最像真人；需要联网）

用法：
    python tts_worker.py --backend piper --model x.onnx --text-file t.txt --out o.wav
    python tts_worker.py --backend edge  --voice zh-CN-XiaoyiNeural --text-file t.txt --out o.mp3

输出：成功打印 "OK <path> <bytes>" 并 exit 0；失败打印 "ERR <原因>" 到 stderr 并 exit 非 0。
"""
import argparse
import asyncio
import os
import sys


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def do_piper(text, model, out):
    from piper import PiperVoice
    import wave
    voice = PiperVoice.load(model)
    with wave.open(out, "wb") as w:
        voice.synthesize_wav(text, w)


def do_edge(text, voice, out):
    import edge_tts
    asyncio.run(edge_tts.Communicate(text, voice).save(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="piper", choices=["piper", "edge"])
    ap.add_argument("--model", default="")
    ap.add_argument("--voice", default="zh-CN-XiaoyiNeural")
    ap.add_argument("--text-file", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    text = _read(a.text_file)
    if not text:
        print("ERR 空文本", file=sys.stderr)
        return 2
    try:
        if a.backend == "edge":
            do_edge(text, a.voice, a.out)
        else:
            do_piper(text, a.model, a.out)
    except Exception as e:
        print(f"ERR {type(e).__name__}: {e}", file=sys.stderr)
        return 4

    if not os.path.exists(a.out) or os.path.getsize(a.out) == 0:
        print("ERR 输出为空", file=sys.stderr)
        return 5
    print(f"OK {a.out} {os.path.getsize(a.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
