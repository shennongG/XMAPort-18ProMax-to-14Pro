# -*- coding: utf-8 -*-
"""刷机前校验 super.img 的 LP 元数据，避免刷到无法开机的包。

用法（脚本在仓库 tools 目录，用绝对路径调用即可，不要求切换工作目录）:
    python <仓库>\\tools\\verify_super.py F:\\Rom\\ROM\\images\\super.img.zst
    python <仓库>\\tools\\verify_super.py F:\\Rom\\ROM\\images\\super.img

支持直接传 .zst：自动找 zstd（包内 bin/Windows/zstd.exe > 仓库 tools > PATH），
解压到临时目录后校验，结束自动删除。需要约 8GB 临时磁盘空间。

检查项与「已知可开机的 基线镜像」对齐:
  1. 所有分区名 / 组名带 _a 槽位后缀
  2. Attributes 为 none
  3. 八个必需动态分区齐全
  4. mi_ext 大小符合底包特征（约 100MB；源包 madrid 为 166MB，属误用）
  5. 元数据为 A/B（metadata slots >= 2）且带 virtual_ab 标志
  6. 同级 images/vbmeta.img 必须未签名(algo=0)且 flags=3；签名版会让 ABL
     校验失败，设备秒回 fastboot
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REQUIRED = {
    "system", "system_ext", "product", "odm", "mi_ext",
    "vendor", "vendor_dlkm", "system_dlkm",
}


def dump(path):
    exe = TOOLS / "lpdumps.exe"
    if not exe.is_file():
        print("[ERROR] 缺少 tools/lpdumps.exe")
        sys.exit(2)
    p = subprocess.run([str(exe), str(path)], capture_output=True,
                       text=True, errors="replace")
    if p.returncode != 0 or "Partition table" not in p.stdout:
        print("[ERROR] lpdumps 读取失败，文件可能不是 raw super.img")
        print(p.stdout[-800:] + p.stderr[-400:])
        sys.exit(2)
    return p.stdout


def parse(text):
    parts = []
    for block in re.split(r"-{8,}", text.split("Partition table:")[1].split("Super partition layout:")[0]):
        name = re.search(r"Name:\s*(\S+)", block)
        if not name:
            continue
        group = re.search(r"Group:\s*(\S+)", block)
        attr = re.search(r"Attributes:\s*(\S+)", block)
        ext = re.search(r"Extents:.*?\n\s*0\s*\.\.\s*(\d+)\s+linear", block, re.S)
        size = (int(ext.group(1)) + 1) * 512 if ext else 0
        parts.append((name.group(1), group.group(1) if group else "",
                      attr.group(1) if attr else "", size))
    slots = re.search(r"Metadata slot count:\s*(\d+)", text)
    flags = re.search(r"Header flags:\s*(\S+)", text)
    return parts, (int(slots.group(1)) if slots else 0), (flags.group(1) if flags else "")


def find_zstd(zst):
    """优先用刷机包自带的 zstd，其次仓库 tools，最后系统 PATH。"""
    candidates = [
        zst.parent.parent / "bin" / "Windows" / "zstd.exe",
        zst.parent.parent / "bin" / "Windows" / "zstd",
        TOOLS / "zstd.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("zstd")


def decompress(zst, out):
    exe = find_zstd(zst)
    if not exe:
        sys.exit("[ERROR] 找不到 zstd.exe；请确认刷机包已完整解压（bin/Windows/zstd.exe）")
    free = shutil.disk_usage(out.parent).free
    if free < 9 * 1024 ** 3:
        sys.exit("[ERROR] 临时盘剩余 {:.1f} GB，解压 super 需要约 8GB，请先清理".format(free / 1024 ** 3))
    print("解压 {} -> 临时文件（约 7.75GB，需 1~3 分钟）...".format(zst.name))
    print("  zstd: {}".format(exe))
    cmd = [exe, "-d", "-f", "-T0", str(zst), "-o", str(out)]
    rc = subprocess.run(cmd).returncode
    if rc != 0 or not out.is_file():
        sys.exit("[ERROR] zstd 解压失败 (rc={})".format(rc))
    print("  解压完成: {:.2f} GB\n".format(out.stat().st_size / 1024 ** 3))


def check(path):
    parts, slots, flags = parse(dump(path))
    problems = []

    print("分区表 ({} 项):".format(len(parts)))
    for name, group, attr, size in parts:
        print("  {:<28s} {:<32s} {:<8s} {:>8.1f} MB".format(
            name, group, attr, size / 1048576))
        if not name.endswith("_a"):
            problems.append("分区名缺 _a 后缀: {} (init 将找不到它)".format(name))
        if not group.endswith("_a"):
            problems.append("组名缺 _a 后缀: {} <- {}".format(group, name))
        if attr != "none":
            problems.append("Attributes 应为 none: {} = {}".format(name, attr))
        base = name[:-2] if name.endswith("_a") else name
        if base == "mi_ext" and size > 140 * 1048576:
            problems.append("mi_ext 达 {:.0f}MB (>140MB)，疑似误用源包镜像；"
                            "底包 mi_ext 约 100MB".format(size / 1048576))

    found = {n[:-2] if n.endswith("_a") else n for n, _, _, _ in parts}
    missing = REQUIRED - found
    if missing:
        problems.append("缺少必需分区: {}".format(", ".join(sorted(missing))))
    if slots < 2:
        problems.append("metadata slot count={}，A/B 设备应 >=2".format(slots))
    if "virtual_ab" not in flags:
        problems.append("Header flags 未含 virtual_ab_device: {}".format(flags or "(空)"))

    print()
    if problems:
        print("[FAIL] 此 super.img 很可能无法开机：")
        for p in problems:
            print("  - " + p)
        return 1
    print("[PASS] 结构与已知可开机包一致，可以刷入。")
    print("       仍建议刷前保持电量充足、BL 解锁、并备份数据。")
    return 0


def check_vbmeta(super_path):
    """刷机包同级目录的 vbmeta.img 必须是「未签名 + flags=3」的空白禁用镜像。"""
    p = super_path.parent / "vbmeta.img"
    print("vbmeta 检查: {}".format(p))
    if not p.is_file():
        print("  [WARN] 未找到 vbmeta.img，跳过（无法排除 AVB 签名问题）")
        return 0
    h = p.read_bytes()[:128]
    if h[:4] != b"AVB0":
        print("  [FAIL] 不是合法 AVB 镜像 (magic != AVB0)")
        return 1
    algo = int.from_bytes(h[28:32], "big")
    flags = int.from_bytes(h[120:124], "big")
    print("  algorithm_type={} flags={}  (期望 0 / 3)".format(algo, flags))
    if algo != 0 or flags != 3:
        print("  [FAIL] vbmeta 是签名/未完全禁用的镜像：ABL 校验失败会直接回落 fastboot，")
        print("         且 recovery / fastbootd 都进不去（pstore 为空）。")
        print("  建议: 把 仓库\\tools\\assets\\vbmeta_disabled.img 复制为 images\\vbmeta.img")
        return 1
    print("  [PASS] 未签名 + flags=3")
    return 0


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python verify_super.py <super.img 或 super.img.zst>")
    path = Path(sys.argv[1])
    if not path.is_file():
        zst = path.with_name(path.name + ".zst")
        hint = ("\n       （该目录只有 {}，可直接传 .zst 路径）".format(zst.name)
                if zst.is_file() else "")
        sys.exit("[ERROR] 文件不存在: {}{}".format(path, hint))
    vb_rc = check_vbmeta(path)
    print()
    if path.suffix.lower() in (".zst", ".zstd"):
        with tempfile.TemporaryDirectory(prefix="verify-super-") as td:
            out = Path(td) / "super.img"
            decompress(path, out)
            return check(out) or vb_rc
    return check(path) or vb_rc


if __name__ == "__main__":
    sys.exit(main())
