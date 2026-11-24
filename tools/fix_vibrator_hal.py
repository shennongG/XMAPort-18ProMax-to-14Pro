#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 odm 的振动 HAL 增加 IVibrator/default 实例（Android 17 必需）。

底包 310 的 odm 只注册 IVibrator/vibratorfeature：

    <hal format="aidl"><name>android.hardware.vibrator</name>
        <fqname>IVibrator/vibratorfeature</fqname></hal>

而 Android 17 的 VibratorManagerService 只认 IVibrator/default，
拿不到就 vibratorIds=[]，所有触感被判 IGNORED_UNSUPPORTED（真机表现：
完全不振 / 只有零星反馈）。MIUI framework 又硬等 vibratorfeature
（device compatibility matrix 里声明），只把 vibratorfeature 改成
default 会卡 HyperOS 开机界面。

因此必须两个实例同时存在。做法（与 V5 真机验证一致）：

    1. 复制 HAL 二进制为 ...service.default（二进制按 argv[0] 区分实例）
    2. manifest 追加 <fqname>IVibrator/default</fqname>
    3. init rc 追加 vibratorfeature-hal-service-default（不带参数）

新二进制的 SELinux 标签由 tools/pack_partitions.py 的 EXTRA_CONTEXTS
提供：/odm/bin/hw/vendor.xiaomi.hardware.vibratorfeature.service.default
-> u:object_r:hal_vibrator_default_exec:s0

2026-11-22：本文件曾在仓库重建时丢失，XMAPort.py 的 import 被 except
吞掉只打印一条 warn，导致振动修复静默失效（新包振动不正常）。现恢复，
并把 import 失败从 warn 升级为记录 ERROR。
"""
import re
import shutil
from pathlib import Path

HAL_REL = "bin/hw/vendor.xiaomi.hardware.vibratorfeature.service"
RC_REL = "etc/init/vendor.xiaomi.hardware.vibratorfeature.service.rc"
MANIFEST_REL = "etc/vintf/manifest/vendor.xiaomi.hardware.vibratorfeature.service.xml"

SERVICE_BLOCK = """
# Android 17 VibratorManagerService requires IVibrator/default.
# The original vibratorfeature instance stays up for MIUI framework.
service vibratorfeature-hal-service-default /odm/bin/hw/vendor.xiaomi.hardware.vibratorfeature.service.default
    class hal
    user system
    group system input audio
    setenv RICHTAP_DEVICE_PATH /dev/qcom_haptic
    setenv ENV_RICHTAP_CONFIG_PATH /odm/etc/aac_richtap.config
"""


def _patch_manifest(path, log):
    if not path.is_file():
        log.append("WARN manifest not found: " + str(path))
        return
    txt = path.read_text(encoding="utf-8", errors="replace")
    if "IVibrator/default" in txt:
        log.append("manifest already declares IVibrator/default")
        return
    m = re.search(r"([ \t]*)</hal>", txt)
    if not m:
        log.append("WARN </hal> not found in manifest, skip")
        return
    indent = m.group(1)
    insert = "{0}    <fqname>IVibrator/default</fqname>\n".format(indent)
    txt = txt[:m.start()] + insert + txt[m.start():]
    path.write_text(txt, encoding="utf-8", newline="\n")
    if b"\r\n" in path.read_bytes():
        log.append("ERROR manifest 换行被转成 CRLF，init/VINTF 可能解析失败")
    else:
        log.append("manifest += <fqname>IVibrator/default</fqname>")


def _patch_rc(path, log):
    if not path.is_file():
        log.append("WARN rc not found: " + str(path))
        return
    txt = path.read_text(encoding="utf-8", errors="replace")
    if "vibratorfeature-hal-service-default" in txt:
        log.append("rc already has default service")
        return
    if not txt.endswith("\n"):
        txt += "\n"
    path.write_text(txt + SERVICE_BLOCK, encoding="utf-8", newline="\n")
    if b"\r\n" in path.read_bytes():
        log.append("ERROR rc 换行被转成 CRLF，Android init 会把 \\r 带进参数")
    else:
        log.append("rc += vibratorfeature-hal-service-default")


def apply_to_tree(odm_root, cfg_dir=None):
    """在 odm 内容树里启用振动双实例。返回日志行列表（供调用方打印）。"""
    log = []
    root = Path(odm_root)
    hal = root / HAL_REL
    if not hal.is_file():
        log.append("SKIP vibrator HAL not found: " + HAL_REL)
        return log

    dst = hal.with_name(hal.name + ".default")
    if dst.is_file():
        log.append("hal .default already exists")
    else:
        try:
            shutil.copy2(hal, dst)
            log.append("copy {} -> {}".format(hal.name, dst.name))
        except OSError as e:
            log.append("WARN cannot copy hal: {}".format(e))
            return log

    # 关键：复制出来的二进制如果仍写着 /vibratorfeature，会再次注册同名实例，
    # addService 失败后 init 反复重启服务，表现就是卡 HyperOS。
    # 把只出现一次的实例名改成 /default，两个进程各注册一个实例。
    try:
        raw = dst.read_bytes()
        old = b"/vibratorfeature\x00"
        if raw.count(old) == 1:
            new = b"/default\x00" + b"\x00" * (len(old) - len(b"/default\x00"))
            dst.write_bytes(raw.replace(old, new, 1))
            log.append("hal .default instance string -> /default")
        else:
            log.append("WARN instance string count={} (expected 1), binary not patched"
                       .format(raw.count(old)))
    except OSError as e:
        log.append("WARN cannot patch hal instance: {}".format(e))

    _patch_manifest(root / MANIFEST_REL, log)
    _patch_rc(root / RC_REL, log)
    return log


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    for line in apply_to_tree(target):
        print(line)
