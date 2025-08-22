# src/glyph/io.py
from __future__ import annotations

import dataclasses
import datetime as _dt
import io
import json
import os
import shutil
import sys
import textwrap
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence

# ──────────────────────────────────────────────────────────────────────────────
# Global state & configuration
# ──────────────────────────────────────────────────────────────────────────────

Verbosity = Literal["quiet", "normal", "verbose", "trace"]
ColorMode = Literal["auto", "always", "never"]

class _State:
    __slots__ = (
        "verbosity",
        "color_mode",
        "json_mode",
        "timestamps",
        "width",
        "log_fp",
        "_color_enabled_cached",
        "_lock",
    )
    def __init__(self) -> None:
        self.verbosity: Verbosity = "normal"
        self.color_mode: ColorMode = os.environ.get("GLYPH_COLOR", "auto")  # auto|always|never
        self.json_mode: bool = bool(int(os.environ.get("GLYPH_JSON", "0")))
        self.timestamps: bool = bool(int(os.environ.get("GLYPH_TIMESTAMPS", "0")))
        self.width: int = max(40, shutil.get_terminal_size((100, 20)).columns)
        self.log_fp: Optional[io.TextIOBase] = None
        self._color_enabled_cached: Optional[bool] = None
        self._lock = threading.RLock()

_STATE = _State()

# ──────────────────────────────────────────────────────────────────────────────
# Color / style helpers (ANSI, Windows-safe)
# ──────────────────────────────────────────────────────────────────────────────

# Basic ANSI SGR
_SGR = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "underline": "\x1b[4m",
    "fg": {
        "black": "\x1b[30m",
        "red": "\x1b[31m",
        "green": "\x1b[32m",
        "yellow": "\x1b[33m",
        "blue": "\x1b[34m",
        "magenta": "\x1b[35m",
        "cyan": "\x1b[36m",
        "white": "\x1b[37m",
        "default": "\x1b[39m",
    },
}

def _enable_ansi_on_windows() -> None:
    # Try to enable ANSI on Windows 10+ without third-party deps.
    if os.name != "nt":
        return
    try:
        import ctypes  # type: ignore
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE = -11
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):  # type: ignore[arg-type]
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass

def is_tty_stdout() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False

def is_tty_stderr() -> bool:
    try:
        return sys.stderr.isatty()
    except Exception:
        return False

def _color_enabled() -> bool:
    if _STATE._color_enabled_cached is not None:
        return _STATE._color_enabled_cached
    # Respect explicit modes/env
    if _STATE.color_mode == "never" or "NO_COLOR" in os.environ:
        _STATE._color_enabled_cached = False
    elif _STATE.color_mode == "always":
        _STATE._color_enabled_cached = True
    else:
        # auto
        _STATE._color_enabled_cached = is_tty_stderr()
    if _STATE._color_enabled_cached:
        _enable_ansi_on_windows()
    return _STATE._color_enabled_cached

def style(
    text: str,
    *,
    fg: Optional[str] = None,
    bold: bool = False,
    dim: bool = False,
    underline: bool = False,
) -> str:
    if not _color_enabled():
        return text
    out = []
    if bold:
        out.append(_SGR["bold"])
    if dim:
        out.append(_SGR["dim"])
    if underline:
        out.append(_SGR["underline"])
    if fg:
        out.append(_SGR["fg"].get(fg, ""))
    out.append(text)
    out.append(_SGR["reset"])
    return "".join(out)

def deansi(s: str) -> str:
    # Strip ANSI for width calculations
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", s)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration API
# ──────────────────────────────────────────────────────────────────────────────

def configure(
    *,
    verbosity: Optional[Verbosity] = None,
    color: Optional[ColorMode] = None,
    json_mode: Optional[bool] = None,
    timestamps: Optional[bool] = None,
    width: Optional[int] = None,
    log_path: Optional[str] = None,
) -> None:
    """Configure IO behavior globally. Safe to call multiple times."""
    with _STATE._lock:
        if verbosity is not None:
            _STATE.verbosity = verbosity
        if color is not None:
            _STATE.color_mode = color
            _STATE._color_enabled_cached = None  # recompute
        if json_mode is not None:
            _STATE.json_mode = bool(json_mode)
        if timestamps is not None:
            _STATE.timestamps = bool(timestamps)
        if width is not None:
            _STATE.width = int(width)
        if log_path is not None:
            try:
                if _STATE.log_fp:
                    try:
                        _STATE.log_fp.flush()
                        _STATE.log_fp.close()
                    except Exception:
                        pass
                _STATE.log_fp = open(log_path, "a", encoding="utf-8")
            except Exception:
                _STATE.log_fp = None

# Back-compat shims (names used in earlier drafts)
def set_mode_json(json_mode: bool) -> None: configure(json_mode=json_mode)
def set_verbosity(v: Verbosity) -> None: configure(verbosity=v)
def set_color_mode(m: ColorMode) -> None: configure(color=m)

# ──────────────────────────────────────────────────────────────────────────────
# Serialization helpers
# ──────────────────────────────────────────────────────────────────────────────

def _json_default(o: Any) -> Any:
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if hasattr(o, "to_json") and callable(o.to_json):
        try:
            return o.to_json()
        except Exception:
            pass
    if hasattr(o, "to_dict") and callable(o.to_dict):
        try:
            return o.to_dict()
        except Exception:
            pass
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)

def dumps_json(obj: Any, *, sort_keys: bool = True, indent: Optional[int] = None) -> str:
    return json.dumps(obj, default=_json_default, sort_keys=sort_keys, indent=indent)

# ──────────────────────────────────────────────────────────────────────────────
# Core emitters (stdout/stderr discipline)
# ──────────────────────────────────────────────────────────────────────────────

def _ts_prefix() -> str:
    if not _STATE.timestamps:
        return ""
    now = _dt.datetime.now().strftime("%H:%M:%S")
    return style(f"[{now}] ", fg="blue", dim=True) if _color_enabled() else f"[{now}] "

def _log_to_file(line: str) -> None:
    fp = _STATE.log_fp
    if not fp:
        return
    try:
        fp.write(line.rstrip("\n") + "\n")
        fp.flush()
    except Exception:
        pass

def _emit(stream: io.TextIOBase, s: str, *, also_log: bool = True) -> None:
    try:
        stream.write(s)
        if not s.endswith("\n"):
            stream.write("\n")
        stream.flush()
    except Exception:
        # Avoid crashing on broken pipes; mirror typical CLI behavior
        try:
            stream.flush()
        except Exception:
            pass
    if also_log:
        _log_to_file(deansi(s))

# Public: human messages → stderr; JSON → stdout only

def emit_json(obj: Any) -> None:
    """Write JSON to stdout. In JSON mode, prefer this for the final payload."""
    s = dumps_json(obj, sort_keys=True)
    _emit(sys.stdout, s, also_log=True)

def emit_info(msg: str) -> None:
    if _STATE.verbosity == "quiet" or _STATE.json_mode:
        return
    _emit(sys.stderr, _ts_prefix() + msg)

def emit_note(msg: str) -> None:
    if _STATE.verbosity == "quiet" or _STATE.json_mode:
        return
    _emit(sys.stderr, _ts_prefix() + style(msg, fg="blue"))

def emit_success(msg: str) -> None:
    if _STATE.verbosity == "quiet" or _STATE.json_mode:
        return
    _emit(sys.stderr, _ts_prefix() + style(msg, fg="green"))

def emit_warn(msg: str) -> None:
    if _STATE.json_mode:
        return
    _emit(sys.stderr, _ts_prefix() + style(msg, fg="yellow"))

def emit_err(msg: str) -> None:
    # Always allowed (even in json_mode) because it's diagnostic
    _emit(sys.stderr, _ts_prefix() + style(msg, fg="red"))

def emit_verbose(msg: str) -> None:
    if _STATE.json_mode:
        return
    if _STATE.verbosity in ("verbose", "trace"):
        _emit(sys.stderr, _ts_prefix() + style(msg, fg="cyan"))

def emit_trace(msg: str) -> None:
    if _STATE.json_mode:
        return
    if _STATE.verbosity == "trace":
        _emit(sys.stderr, _ts_prefix() + style(msg, fg="magenta", dim=True))

def die(msg: str, code: int = 1) -> None:
    emit_err(msg)
    raise SystemExit(code)

# ──────────────────────────────────────────────────────────────────────────────
# Headings, rules, wrapping
# ──────────────────────────────────────────────────────────────────────────────

def hr(char: str = "─") -> str:
    width = _STATE.width
    return (char * max(10, width))[:width]

def heading(text: str) -> None:
    if _STATE.json_mode or _STATE.verbosity == "quiet":
        return
    t = style(text, bold=True)
    _emit(sys.stderr, t + "\n" + style(hr(), dim=True))

def wrap(text: str, *, indent: int = 0) -> str:
    w = max(20, _STATE.width - indent)
    prefix = " " * indent
    return "\n".join(prefix + ln for ln in textwrap.wrap(text, width=w))

# ──────────────────────────────────────────────────────────────────────────────
# Tables (TTY-friendly, width-aware)
# ──────────────────────────────────────────────────────────────────────────────

def _col_widths(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> list[int]:
    width = _STATE.width
    cols = len(headers)
    # Compute max content width per column (sans ANSI)
    maxw = [len(deansi(h)) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            if i >= cols:
                continue
            maxw[i] = max(maxw[i], len(deansi(str(cell))))
    # Fit to terminal width with simple shrinking from the right
    total = sum(maxw) + 3 * (cols - 1)
    while total > width and any(w > 8 for w in maxw):
        # shrink the widest column > 8
        i = max(range(cols), key=lambda j: maxw[j])
        if maxw[i] <= 8:
            break
        maxw[i] -= 1
        total -= 1
    return maxw

def render_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    # Convert to strings
    srows = [[str(c) for c in r] for r in rows]
    widths = _col_widths(srows, headers)
    def fmt_row(cells: Sequence[str]) -> str:
        parts = []
        for i, cell in enumerate(cells[: len(widths)]):
            w = widths[i]
            cell_s = str(cell)
            # Trim with ellipsis if needed
            if len(deansi(cell_s)) > w:
                # naive ellipsis (safe with ANSI removed for width calc)
                plain = deansi(cell_s)
                trimmed = plain[: max(0, w - 1)] + "…"
                # keep original styling if any at start (heuristic)
                cell_s = trimmed
            pad = " " * max(0, w - len(deansi(cell_s)))
            parts.append(cell_s + pad)
        return "   ".join(parts)
    header = style(fmt_row(headers), bold=True)
    sep = style(hr("─"), dim=True)
    body = "\n".join(fmt_row(r) for r in srows)
    return f"{header}\n{sep}\n{body}" if body else f"{header}\n{sep}"

# ──────────────────────────────────────────────────────────────────────────────
# Progress & spinner (TTY only; silent in JSON/non-TTY)
# ──────────────────────────────────────────────────────────────────────────────

class _Spinner:
    _frames = "|/-\\"
    def __init__(self, label: str) -> None:
        self.label = label
        self._stop = threading.Event()
        self._th: Optional[threading.Thread] = None
        self._i = 0

    def start(self) -> None:
        if _STATE.json_mode or not is_tty_stderr():
            return
        def _run():
            while not self._stop.is_set():
                f = self._frames[self._i % len(self._frames)]
                self._i += 1
                msg = f"{style(f, fg='cyan')} {self.label}"
                sys.stderr.write("\r" + msg + " " * max(0, _STATE.width - len(deansi(msg)) - 1))
                sys.stderr.flush()
                time.sleep(0.08)
            # clear line
            sys.stderr.write("\r" + " " * (_STATE.width - 1) + "\r")
            sys.stderr.flush()
        self._th = threading.Thread(target=_run, daemon=True)
        self._th.start()

    def stop(self, final: Optional[str] = None) -> None:
        if self._th is None:
            return
        self._stop.set()
        self._th.join(timeout=0.5)
        if final and not _STATE.json_mode:
            emit_info(final)

@contextmanager
def spinner(label: str):
    s = _Spinner(label)
    try:
        s.start()
        yield s
    finally:
        s.stop()

class ProgressBar:
    def __init__(self, total: int, label: str = "") -> None:
        self.total = max(1, int(total))
        self.label = label
        self.count = 0
        self._last_draw = 0.0

    def update(self, inc: int = 1) -> None:
        self.count = min(self.total, self.count + inc)
        self._draw()

    def _draw(self) -> None:
        if _STATE.json_mode or not is_tty_stderr():
            return
        now = time.time()
        if now - self._last_draw < 0.03 and self.count < self.total:
            return
        self._last_draw = now
        width = max(10, _STATE.width - 20)
        filled = int(width * (self.count / self.total))
        bar = "[" + "#" * filled + "-" * (width - filled) + "]"
        pct = int(100 * self.count / self.total)
        label = f" {self.label}" if self.label else ""
        line = f"{bar} {pct:3d}%{label}"
        sys.stderr.write("\r" + line[: _STATE.width - 1])
        sys.stderr.flush()
        if self.count >= self.total:
            sys.stderr.write("\n")
            sys.stderr.flush()

    def done(self) -> None:
        self.count = self.total
        self._draw()

@contextmanager
def progress(total: int, label: str = ""):
    p = ProgressBar(total, label)
    try:
        yield p
    finally:
        p.done()

# ──────────────────────────────────────────────────────────────────────────────
# Public helpers used around the codebase
# ──────────────────────────────────────────────────────────────────────────────

def ensure_stdout_is_json_only() -> None:
    """
    In JSON mode, ensure stdout stays clean (no accidental prints).
    Use emit_* for human messages (stderr). Call once at command start.
    """
    if not _STATE.json_mode:
        return
    # Optionally, monkey-patch print to stderr in json mode (safety net)
    builtins_print = print  # noqa: F821  (runtime object)
    def _warn_print(*args, **kwargs):
        msg = "[glyph] print() called in JSON mode; redirecting to stderr"
        emit_warn(msg)
        text = " ".join(str(a) for a in args)
        _emit(sys.stderr, text)
    try:
        import builtins  # type: ignore
        builtins.print = _warn_print  # type: ignore[attr-defined]
    except Exception:
        pass

def echo_payload_or_table(payload: Mapping[str, Any], *, json_out: bool, table_rows: Sequence[Sequence[Any]] | None = None, headers: Sequence[str] | None = None) -> None:
    """
    Convenience: if json_out → emit_json(payload); else pretty print a table.
    """
    if json_out or _STATE.json_mode:
        emit_json(payload)
        return
    if table_rows is not None and headers is not None:
        emit_info(render_table(headers, table_rows))
    else:
        # Generic pretty dump (human-readable)
        emit_info(dumps_json(payload, indent=2))

# Legacy name shims (keep earlier code working)
emit_debug = emit_verbose
print_json = emit_json
print_info = emit_info
print_err = emit_err
print_warn = emit_warn
