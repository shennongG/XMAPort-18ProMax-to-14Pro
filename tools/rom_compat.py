#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 0 ROM 兼容性预检（可选工具）。

从两个官方 ROM 完整包直链里解析出：设备代号、MIUI/HyperOS 版本、Android 大版本，
判断源包 / 底包是否跨 Android 大版本，给出移植风险提示。

用法:
  python tools/rom_compat.py url <source_url> <target_url>
  python tools/rom_compat.py info <url>          # 只解析并打印一个 URL

退出码:
  0 = 版本匹配（可继续）
  2 = 跨 Android 大版本（调用方按 allow_api_mismatch 决定继续/中止）
  1 = 参数/解析错误（预检不确定，调用方继续）
"""
import re
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 官方包名里的 android 大版本 -> 年份映射（用于告警措辞）
ANDROID_LABEL = {
    "14": "Android 14", "15": "Android 15", "16": "Android 16",
    "17": "Android 17", "18": "Android 18",
}

# 已知代号 -> 机型（仅作提示，缺失不影响解析）
DEVICE_NAME = {
    "madrid": "Xiaomi 18 Pro Max",
    "shennong": "Xiaomi 14 Pro",
}


def parse_url(url):
    """从直链里解析 (codename, version, android_major)。任一项缺失为 None。"""
    name = url.split("?")[0].rstrip("/").split("/")[-1]
    codename = None
    version = None
    android = None

    m = re.search(r"(?:^|[-_/])(madrid|shennong|sky|nezha|fuxi|cupid|mondrian|dada|zorn|myron)[-_/]", name, re.I)
    if m:
        codename = m.group(1).lower()
    # ota 包: <device>-ota_full-<VERSION>-user-<ANDROID>.0-<hash>.zip
    m = re.search(r"ota_full[-_](OS[\d.]+[A-Z]+)[-_]user[-_](\d+)\.0", name, re.I)
    if m:
        version = m.group(1)
        android = m.group(2)
    # fastboot 包: <device>_images_<VERSION>_<date>_<android>.0_<region>...
    if not version:
        m = re.search(r"_images_(OS[\d.]+[A-Z]+)_", name, re.I)
        if m:
            version = m.group(1)
    if not android:
        m = re.search(r"_(\d{2})\.0_(?:cn|global|eu|in|tr|ru)", name, re.I)
        if m:
            android = m.group(1)
    return codename, version, android


def describe(url):
    codename, version, android = parse_url(url)
    parts = []
    if codename:
        label = DEVICE_NAME.get(codename, "")
        parts.append("代号={}{}".format(codename, " (" + label + ")" if label else ""))
    if version:
        parts.append("版本={}".format(version))
    if android:
        parts.append(ANDROID_LABEL.get(android, "Android " + android))
    return " | ".join(parts) if parts else name_only(url)


def name_only(url):
    return url.split("?")[0].rstrip("/").split("/")[-1]


def main(argv):
    if len(argv) < 2:
        print("usage: rom_compat.py url <source> <target> | info <url>")
        return 1
    if argv[1].lower() == "info":
        if len(argv) < 3:
            return 1
        print(describe(argv[2]))
        return 0
    if argv[1].lower() != "url" or len(argv) < 4:
        return 1

    src, tgt = argv[2], argv[3]
    print("源包: {}".format(describe(src)))
    print("底包: {}".format(describe(tgt)))
    _, _, sa = parse_url(src)
    _, _, ta = parse_url(tgt)
    if not sa or not ta:
        print("[WARN] 无法从包名解析 Android 大版本，跳过版本比对")
        return 1
    if sa == ta:
        print("[OK] Android 大版本一致 ({})".format(ANDROID_LABEL.get(sa, sa)))
        return 0
    print("[WARN] 跨 Android 大版本: 源 {} vs 底 {}".format(
        ANDROID_LABEL.get(sa, sa), ANDROID_LABEL.get(ta, ta)))
    print("[WARN] 跨版本移植通常需要注入 v30~v33 兼容 APEX / SELinux mapping（本项目已内置 inject_boot_compat）")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:
        print("ERROR: {}: {}".format(type(exc).__name__, exc))
        sys.exit(1)
