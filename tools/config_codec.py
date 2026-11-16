#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""config.ini 编码兼容层。

背景：config.ini 早期是 ANSI/GBK（Windows 记事本保存），但在 GitHub 网页里
编辑并保存后会被整体转成 UTF-8。CI 的 "Apply packing overrides" 曾硬编码
decode('gbk')，遇到 UTF-8 中文注释的 0x81 字节直接 UnicodeDecodeError 退出。

本模块统一：
  * 自动识别 UTF-8(BOM / 无 BOM) 与 GBK；
  * 写回时保持原编码与 CRLF/LF 换行，不破坏中文注释；
  * 提供只替换 ASCII 配置行的 CLI，供 workflow 的 pwsh 步骤调用。

CLI:
  python tools/config_codec.py config.ini check
  python tools/config_codec.py config.ini get pack_super
  python tools/config_codec.py config.ini set pack_super=true [allow_api_mismatch=false ...]
"""

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def detect_encoding(raw: bytes) -> str:
    """返回 'utf-8-sig' / 'utf-8' / 'gbk'。严格区分 BOM，避免写回时凭空加 BOM。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for enc in ("utf-8", "gbk"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "gbk"  # 兜底：解码时用 replace


def read_text(path) -> str:
    raw = Path(path).read_bytes()
    enc = detect_encoding(raw)
    return raw.decode(enc, errors="replace")


def read(path):
    """返回 (text, encoding, newline)。"""
    raw = Path(path).read_bytes()
    enc = detect_encoding(raw)
    nl = "\r\n" if b"\r\n" in raw else "\n"
    return raw.decode(enc, errors="replace"), enc, nl


def write_text(path, text: str, encoding: str = None):
    p = Path(path)
    if encoding is None:
        encoding = detect_encoding(p.read_bytes()) if p.exists() else "utf-8"
    p.write_bytes(text.encode(encoding, errors="replace"))


def get_key(path, key: str) -> str:
    text = read_text(path)
    for line in text.splitlines():
        s = line.strip()
        if s.startswith((";", "#", "[")) or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip().lower() == key.lower():
            return v.strip()
    return ""


def set_keys(path, pairs) -> str:
    """精确替换 key=value 行（跳过注释/段名），保持编码与换行；缺键则报错。"""
    p = Path(path)
    text, enc, nl = read(p)
    remaining = dict(pairs)
    out = []
    for line in text.splitlines():
        s = line.strip()
        hit = None
        if s and not s.startswith((";", "#", "[")) and "=" in s:
            k = s.split("=", 1)[0].strip().lower()
            for key in list(remaining):
                if k == key.lower():
                    hit = key
                    break
        if hit is not None:
            out.append("{}={}".format(hit, remaining.pop(hit)))
        else:
            out.append(line)
    if remaining:
        raise KeyError("config key(s) not found: " + ", ".join(remaining))
    new_text = nl.join(out)
    ends_nl = text.endswith("\n") or text.endswith("\r")
    if ends_nl:
        new_text += nl
    write_text(p, new_text, enc)
    return enc


def main(argv):
    if len(argv) < 3:
        print("usage: config_codec.py <config.ini> check|get <key>|set key=value ...")
        return 2
    path, action = argv[1], argv[2].lower()
    if action == "check":
        raw = Path(path).read_bytes()
        print("encoding={} size={} lines={}".format(
            detect_encoding(raw), len(raw), len(read_text(path).splitlines())))
    elif action == "get":
        if len(argv) < 4:
            print("usage: config_codec.py <config.ini> get <key>")
            return 2
        print("{}={}".format(argv[3], get_key(path, argv[3])))
    elif action == "set":
        pairs = []
        for item in argv[3:]:
            if "=" not in item:
                print("bad pair: " + item)
                return 2
            k, v = item.split("=", 1)
            pairs.append((k.strip(), v.strip()))
        if not pairs:
            print("no key=value given")
            return 2
        enc = set_keys(path, pairs)
        for k, v in pairs:
            print("{}={}".format(k, v))
        print("(encoding kept: {})".format(enc))
    else:
        print("unknown action: " + action)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:
        print("ERROR: {}: {}".format(type(exc).__name__, exc))
        sys.exit(1)
