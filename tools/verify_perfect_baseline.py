#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按同版本「完美包」内容基线校验构建产物。

背景：2026-11-15 的 Actions 全新构建，LP 结构、无标签文件统计全部通过，
真机却反复重启进 RE、format data 后仍卡 HyperOS 二屏。对照 X-MiPort 同版本
可开机包与本项目 V5，发现两者在三个内容项上完全一致、全新构建全部偏离。
本工具把这条基线固化下来，让 CI / 本地在出包后就能拦住同类包。

两种输入（任选其一）：
  --tree DIR        CI 用。DIR = workspace/source_filesystem，直接检查内容树
                    与 config/*_file_contexts，不需要解包 super。
  --lists-dir DIR   本地验包用。DIR 里放 extract.erofs -p -s 导出的
                    list*_<part>.txt（例如 list_new_system_a.txt）。
                    配合 --tag new / perfect / 1 选择同一批清单。

示例：
  python tools/verify_perfect_baseline.py --tree workspace/source_filesystem
  python tools/verify_perfect_baseline.py --lists-dir _verify_new --tag new
  python tools/verify_perfect_baseline.py --lists-dir _verify_new --tag perfect

退出码：0 = 通过（可含 WARN），1 = 存在 FAIL，2 = 参数/环境错误。
"""
import argparse
import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = ROOT / "devices" / "shennong" / "perfect_baseline.json"
PARTS = ("system", "system_ext", "product", "odm", "vendor", "mi_ext",
         "vendor_dlkm", "system_dlkm")
FC_RE = re.compile(r"fsConfig=\[([^\]]*)\]\s+seLabel=\[([^\]]*)\]")
CRITICAL = (
    "/system/etc/selinux/mapping/30.0.cil",
    "/system/etc/selinux/mapping/30.0.compat.cil",
    "/system/bin/bugreporter",
)


def norm_system_path(p):
    """打包配置里会出现 /system/system/etc/... 的重复前缀，统一成 /system/etc/..."""
    p = p.replace("\\", "/")
    if p.startswith("/system/system/"):
        p = "/system" + p[len("/system/system"):]
    return p


def parse_list(path):
    d = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = FC_RE.search(line)
        if not m:
            continue
        key = m.group(1).split()[0]
        d[key] = (m.group(2).strip(), "type=FILE" in line)
    return d


def find_list(directory, part, tag):
    cands = sorted(directory.glob("list*_{}.txt".format(part + "_a")))
    if not cands:
        cands = sorted(directory.glob("list*_{}.txt".format(part)))
    if not cands:
        # 也接受 extract.erofs -p 直接重定向得到的 <part>.txt
        exact = directory / (part + ".txt")
        if exact.is_file():
            cands = [exact]
    if tag:
        prefer = [c for c in cands if tag in c.stem]
        if prefer:
            cands = prefer
    return cands[0] if cands else None


def collect_lists(directory, tag):
    data = {"selabels": {}, "product_paths": set(), "miui_oat": 0,
            "density": {}, "vibrator": [], "unlabeled_files": [],
            "entry_counts": {}, "mode": "lists"}
    for part in PARTS:
        f = find_list(directory, part, tag)
        if not f:
            continue
        d = parse_list(f)
        data["entry_counts"][part] = len(d)
        for k, (label, is_file) in d.items():
            nk = norm_system_path(k)
            if nk in CRITICAL:
                data["selabels"][nk] = label
            if is_file and not label:
                data["unlabeled_files"].append(k)
        if part == "system_ext":
            data["miui_oat"] = sum(1 for k in d
                                   if "miui-services." in k
                                   and (k.endswith(".odex") or k.endswith(".vdex")
                                        or k.endswith(".art")))
        if part == "product":
            # 与 --tree 模式口径一致：只统计文件，不含 type=DIR 目录项，
            # 否则 precust_theme / subscreencenter 会被目录项抬高而误报 FAIL
            data["product_paths"] = {
                k.lstrip("/") for k, (label, is_file) in d.items() if is_file
            }
    return data


def find_one(base, patterns):
    for pat in patterns:
        for p in sorted(base.glob(pat)):
            if p.is_file():
                return p
    return None


def collect_tree(tree):
    data = {"selabels": {}, "product_paths": set(), "miui_oat": 0,
            "density": {}, "vibrator": [], "unlabeled_files": [],
            "entry_counts": {}, "mode": "tree"}
    cfg = tree / "config"
    if cfg.is_dir():
        fc_files = sorted(cfg.glob("*file_contexts*"))
        # _fixed_ 是 pack_partitions 补全后的最终规则，最后读取以覆盖原始版本
        fc_files.sort(key=lambda p: p.name.startswith("_fixed_"))
        for f in fc_files:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = s.split(None, 1)
                if len(parts) != 2:
                    continue
                # file_contexts 里是正则，路径含 \. 等转义；去掉反斜杠再比对
                nk = norm_system_path(parts[0].rstrip("/").replace("\\", ""))
                if nk in CRITICAL:
                    data["selabels"][nk] = parts[1].strip()
    # 关键标签未命中时标为空，交给 check() 判 FAIL
    for k in CRITICAL:
        data["selabels"].setdefault(k, "")

    for part in PARTS:
        root = tree / part
        if root.is_dir():
            data["entry_counts"][part] = sum(1 for p in root.rglob("*") if p.is_file())
            if part == "product":
                data["product_paths"] = {
                    p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
                }
            if part == "system_ext":
                oat = root / "framework" / "oat"
                if not oat.is_dir():
                    oat = root / part / "framework" / "oat"
                if oat.is_dir():
                    data["miui_oat"] = sum(
                        1 for p in oat.glob("*/miui-services.*")
                        if p.suffix in (".odex", ".vdex", ".art"))

    bp = find_one(tree, ["product/etc/build.prop", "product/product/etc/build.prop"])
    if bp:
        text = bp.read_text(encoding="utf-8", errors="replace")
        for key in ("ro.sf.lcd_density", "persist.miui.density_v2"):
            m = re.search(r"^{}\s*=\s*(\S+)".format(re.escape(key)), text, re.M)
            if m:
                data["density"][key] = m.group(1).strip()
    for pat in ("odm/etc/vintf/manifest/*.xml", "odm/odm/etc/vintf/manifest/*.xml"):
        for vintf in sorted(tree.glob(pat)):
            if vintf.is_file():
                found = re.findall(
                    r"IVibrator/([A-Za-z0-9_]+)",
                    vintf.read_text(encoding="utf-8", errors="replace"))
                for name in found:
                    if name not in data["vibrator"]:
                        data["vibrator"].append(name)
    return data


def check(data, base, strict_counts=False):
    rows = []

    def add(ok, name, detail, warn=False):
        rows.append((ok, warn, name, detail))

    want = base.get("critical_selabels", {})
    for path, expect in want.items():
        got = data["selabels"].get(path, "")
        if not got:
            add(False, "标签 " + path, "未找到规则（缺标签会卡二屏）")
        elif got != expect:
            add(False, "标签 " + path, "实际 {} / 应为 {}".format(got, expect))
        else:
            add(True, "标签 " + path, got)

    oat_min = int(base.get("system_ext_min_miui_services_oat", 30))
    add(data["miui_oat"] >= oat_min, "system_ext miui-services oat",
        "{} 项 / 最少 {} 项".format(data["miui_oat"], oat_min))

    paths = data["product_paths"]
    if not paths:
        # 清单缺失/解析失败时，禁用目录与上限检查会全部「0 项 PASS」，
        # 属于假阳性；必须显式失败。
        add(False, "product 清单", "为空（-p 清单缺失或解析失败）")
    for d in base.get("product_forbidden_dirs", []):
        n = sum(1 for p in paths if p.startswith(d.rstrip("/") + "/") or p == d)
        add(n == 0, "product 禁用目录 " + d, "{} 项".format(n))

    # 完美包「补入」的应用必须存在：源包 madrid 4.0.x 本身没有这些
    # shennong 原生服务（WMService / UPTsmService / pangu NQNfcNci …），
    # 只删不补会让真机卡 HyperOS 二屏（2026-11-21 定性）。
    for d in base.get("product_required_dirs", []):
        n = sum(1 for p in paths if p.startswith(d.rstrip("/") + "/") or p == d)
        add(n > 0, "product 必需应用 " + d, "{} 项".format(n))

    for key, limit in base.get("product_max_counts", {}).items():
        n = sum(1 for p in paths if key in p)
        add(n <= limit, "product {} 条目".format(key),
            "{} 项 / 上限 {}".format(n, limit))

    for key, want_v in base.get("density", {}).items():
        got = data["density"].get(key)
        if not got:
            add(True, "密度 " + key, "清单模式无法读取，跳过", warn=True)
        else:
            add(got == str(want_v), "密度 " + key,
                "实际 {} / 期望 {}".format(got, want_v))

    vib_min = int(base.get("vibrator_instances", {}).get("project_min", 1))
    if data["vibrator"]:
        add(len(data["vibrator"]) >= vib_min, "振动 HAL 实例",
            ", ".join(data["vibrator"]))
    else:
        add(True, "振动 HAL 实例", "清单模式无法读取 VINTF，跳过", warn=True)

    bad = [p for p in data["unlabeled_files"] if p in CRITICAL]
    lim = int(base.get("max_unlabeled_critical_files", 0))
    add(len(bad) <= lim, "关键文件无标签", "{} 个".format(len(bad)))

    if strict_counts:
        for part, expect in base.get("partition_entry_counts", {}).items():
            got = data["entry_counts"].get(part)
            if got is None:
                continue
            add(abs(got - int(expect)) <= 200, "文件数 " + part,
                "{} / 完美包 {}".format(got, expect), warn=True)

    print("=" * 68)
    print(" 完美包内容基线校验  (mode={}, baseline={})".format(
        data["mode"], base.get("source_zip", "?")))
    print("=" * 68)
    fails = 0
    warns = 0
    for ok, warn, name, detail in rows:
        if ok and warn:
            tag, warns = "[WARN]", warns + 1
        elif ok:
            tag = "[PASS]"
        else:
            tag, fails = "[FAIL]", fails + 1
        print(" {:7s} {:<44s} {}".format(tag, name, detail))
    print("-" * 68)
    if fails:
        print(" [FAIL] {} 项不达标：该产物偏离已验证基线，不建议上机。".format(fails))
    else:
        print(" [PASS] 全部关键项对齐完美包基线" +
              ("（{} 项 WARN）".format(warns) if warns else ""))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description="按完美包内容基线校验产物")
    ap.add_argument("--tree", help="workspace/source_filesystem 内容树")
    ap.add_argument("--lists-dir", help="extract.erofs -p -s 清单目录")
    ap.add_argument("--tag", help="清单文件名中的批次标记，如 new / perfect / 1")
    ap.add_argument("--device", default="shennong",
                    help="机型代号，用于定位 devices/<device>/perfect_baseline.json")
    ap.add_argument("--baseline", default=None,
                    help="显式指定基线 JSON；默认 devices/<device>/perfect_baseline.json")
    ap.add_argument("--strict-counts", action="store_true",
                    help="额外比对各分区文件数（仅 WARN）")
    args = ap.parse_args()

    bp = (Path(args.baseline) if args.baseline else
          ROOT / "devices" / args.device / "perfect_baseline.json")
    if not bp.is_file():
        print("[ERROR] 基线文件不存在: {}".format(bp))
        return 2
    try:
        base = json.loads(bp.read_text(encoding="utf-8"))
    except Exception as e:
        print("[ERROR] 基线文件解析失败: {}".format(e))
        return 2

    if bool(args.tree) == bool(args.lists_dir):
        print("[ERROR] 必须且只能指定 --tree 或 --lists-dir 之一")
        return 2
    if args.tree:
        tree = Path(args.tree)
        if not tree.is_dir():
            print("[ERROR] 内容树不存在: {}".format(tree))
            return 2
        data = collect_tree(tree)
    else:
        d = Path(args.lists_dir)
        if not d.is_dir():
            print("[ERROR] 清单目录不存在: {}".format(d))
            return 2
        data = collect_lists(d, args.tag)
    return check(data, base, args.strict_counts)


if __name__ == "__main__":
    sys.exit(main())
