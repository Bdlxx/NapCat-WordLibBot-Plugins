# -*- coding: utf-8 -*-
"""本地 TTS 子进程（piper）：文本 → wav。**没有 handle，不会被当插件加载**（同 jm_worker.py）。

用法：
    <tts-venv>/python tts_worker.py --model <voice.onnx> --text-file <utf8.txt> --out <out.wav>

设计取舍：每次请求起一个进程（模型加载 ~4s + 合成 <1s）。好处是零常驻状态、坏了不影响 bot；
以后要压到亚秒级，再换成常驻 server 版（把 load 结果留在一个进程里，走 stdin 协议）。
"""
import argparse
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--text-file", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    with open(a.text_file, "r", encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        print("ERR 空文本", file=sys.stderr)
        return 2

    try:
        from piper import PiperVoice
    except Exception as e:
        print(f"ERR 未安装 piper: {e}", file=sys.stderr)
        return 3

    import wave
    try:
        voice = PiperVoice.load(a.model)
        with wave.open(a.out, "wb") as w:
            voice.synthesize_wav(text, w)
    except Exception as e:
        print(f"ERR 合成失败: {e}", file=sys.stderr)
        return 4

    if not os.path.exists(a.out) or os.path.getsize(a.out) == 0:
        print("ERR 输出为空", file=sys.stderr)
        return 5
    print(f"OK {a.out} {os.path.getsize(a.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
