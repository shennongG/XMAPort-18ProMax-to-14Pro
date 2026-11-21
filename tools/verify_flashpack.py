#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验线刷 + 卡刷一体包（workspace/flashpack/XMAPort_*.zip）。

静态检查项（全部必须通过，否则 exit 1）：
  1. zip 存在且可读
  2. META-INF/com/google/android/update-binary 存在且非空
  3. bin/android/7zzs 与 bin/android/zstd 随包（卡刷解包必需）
  4. images/super.img.zst 存在、> 4 GB、zstd 帧头正确
  5. images/recovery_twrp.img 随包（线刷脚本强制刷 TWRP）
  6. XMAPort-Flash.bat 存在
  7. update-binary 走 7zzs 解包（ZIP64），且包含 super 刷写与 bootctl 收尾
  8. 非超大条目的 header_offset 均 < 4 GB（否则 360压缩 报「头部损坏」）

用法：
    python tools/verify_flashpack.py [--device shennong] [--zip 指定zip]
"""
import argparse
import sys
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT / "workspace"
ZIP64_LIMIT = 4 * 1024 ** 3
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def fail(msg):
    print(" [FAIL] " + msg)
    return 1


def main():
    ap = argparse.ArgumentParser(description="校验线刷+卡刷一体包")
    ap.add_argument("--device", default="shennong")
    ap.add_argument("--zip", default=None, help="显式指定 zip，默认取 workspace/flashpack 最新一个")
    ap.add_argument("--min-size-gb", type=float, default=5.0,
                    help="整包体积下限（GB），默认 5；自测时可调小")
    args = ap.parse_args()

    if args.zip:
        zp = Path(args.zip)
    else:
        cands = sorted((WORKSPACE / "flashpack").glob("XMAPort_*.zip"),
                       key=lambda p: p.stat().st_mtime)
        if not cands:
            print("[ERROR] workspace/flashpack 下没有 XMAPort_*.zip")
            return 1
        zp = cands[-1]

    print("=" * 68)
    print(" 一体包自检: {}".format(zp.name))
    print("=" * 68)
    if not zp.is_file():
        return fail("文件不存在: {}".format(zp))

    size_gb = zp.stat().st_size / (1024 ** 3)
    print(" 体积: {:.2f} GB".format(size_gb))
    if zp.stat().st_size < args.min_size_gb * 1024 ** 3:
        return fail("体积异常（{:.2f} GB < {:.1f} GB），移植包应含 6~7 GB super".format(
            size_gb, args.min_size_gb))

    fails = 0
    try:
        zf = zipfile.ZipFile(zp)
    except Exception as e:
        return fail("无法打开 zip: {}".format(e))

    with zf:
        names = {i.filename: i for i in zf.infolist() if not i.filename.endswith("/")}

        def require(name):
            nonlocal fails
            inf = names.get(name)
            if inf is None:
                fails += fail("缺少 {}".format(name))
                return None
            print(" [PASS] {} ({:.1f} MB)".format(name, inf.file_size / 1048576))
            return inf

        # 1) 卡刷入口
        ub = require("META-INF/com/google/android/update-binary")
        if ub is not None:
            txt = zf.read(ub).decode("utf-8", errors="replace")
            checks = [
                ('7zzs e -so', "卡刷用 7zzs 解包（ZIP64 必需）"),
                ('super.img.zst', "卡刷处理 super.img.zst"),
                ('/dev/block/bootdevice/by-name/super', "写入 super 分区"),
                ('bootctl set-active-boot-slot', "收尾切换 boot slot"),
                ('getprop ro.product', "设备代号校验"),
                ('patcherror.txt', "打包错误拒绝刷入"),
            ]
            for token, desc in checks:
                if token in txt:
                    print(" [PASS] update-binary: {}".format(desc))
                else:
                    fails += fail("update-binary 缺少: {} ({})".format(desc, token))
            if "unzip -p" in txt and "7zzs e -so" in txt:
                print(" [WARN] update-binary 仍保留 unzip -p 回退分支（仅 7zzs 缺失时用）")

        # 2) 卡刷解包工具
        require("bin/android/7zzs")
        require("bin/android/zstd")

        # 3) super
        sup = require("images/super.img.zst")
        if sup is not None:
            if sup.file_size <= ZIP64_LIMIT:
                fails += fail("super.img.zst 仅 {:.2f} GB，疑似构建截断".format(
                    sup.file_size / 1024 ** 3))
            else:
                with zf.open(sup) as f:
                    magic = f.read(4)
                if magic == ZSTD_MAGIC:
                    print(" [PASS] super.img.zst zstd 帧头正确")
                else:
                    fails += fail("super.img.zst 不是 zstd 流 (magic={!r})".format(magic))

        # 4) 线刷必需
        require("images/recovery_twrp.img")
        require("XMAPort-Flash.bat")

        # 5) 条目偏移：小条目不能落在 4 GB 之后
        bad = [i.filename for i in names.values()
               if i.file_size <= ZIP64_LIMIT and i.header_offset >= ZIP64_LIMIT]
        if bad:
            fails += fail("{} 个小条目 header_offset >= 4GB（360压缩会报头部损坏），例如 {}".format(
                len(bad), bad[0]))
        else:
            print(" [PASS] 小条目偏移均 < 4 GB")

        # 6) 镜像清单
        imgs = sorted(n[len("images/"):] for n in names if n.startswith("images/"))
        print(" 镜像 {} 个: {}".format(len(imgs), ", ".join(imgs[:14]) +
                                       (" ..." if len(imgs) > 14 else "")))

    print("-" * 68)
    if fails:
        print(" [FAIL] 一体包自检未通过，{} 项异常".format(fails))
        return 1
    print(" [PASS] 线刷 + 卡刷一体包结构完整")
    return 0


if __name__ == "__main__":
    sys.exit(main())
