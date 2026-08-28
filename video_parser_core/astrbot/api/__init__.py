# astrbot.api 兼容层


class _Logger:
    """AstrBot logger 兼容：输出到 bot 标准输出（runtime.log）"""

    def _fmt(self, level, msg):
        return f"[parser] {msg}"

    def debug(self, msg):
        print(self._fmt("DEBUG", msg))

    def info(self, msg):
        print(self._fmt("INFO", msg))

    def warning(self, msg):
        print(f"[parser] ⚠ {msg}")

    def warn(self, msg):
        print(f"[parser] ⚠ {msg}")

    def error(self, msg):
        print(f"[parser] ❌ {msg}")

    def exception(self, msg):
        import traceback
        print(f"[parser] ❌ {msg}")
        traceback.print_exc()


logger = _Logger()
