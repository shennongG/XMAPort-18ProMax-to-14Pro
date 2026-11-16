#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""base_first 模式：把 system / system_ext / product 内容树改成「310 底包为骨架 + 4.0.29 增量」。

为什么这么做
------------
XMAPort 默认路线是「源包为主」：system / system_ext / product 整体取自
4.0.29 源包(madrid)，再靠一堆补丁把机型差异抹平（密度 480→560、删 madrid
背屏/副屏资源、补 VNDK compat、补 30.0 SELinux mapping、处理失效 oat）。
同版本 X-MiPort 完美包实测是另一条路线：以 310 底包(shennong)为骨架，
只把 HyperOS 4 的 framework / 应用增量替换进去，因此密度、机型资源、
oat 天然配套。

实现思路（关键：不换目录名）
----------------------------
后续 shallow_slim / fix_density / pack_partitions 全部固定操作
workspace/source_filesystem，打包也从这里取内容。所以本脚本不引入新的
目录名，而是在 Step 5 之后原地重建 SRC_FS 的三个分区：

  1. 用 TGT_FS/<part> 的文件覆盖 SRC_FS/<part> 的同名文件（底包骨架）
  2. 规则里 `keep` 的路径跳过覆盖（保留 4.0.29 的 framework / 应用）
  3. 规则里 `drop` 的路径与 devices/<device>/perfect_remove_paths.txt
     里的清单在合并后删除（madrid 专属资源）
  4. 源包独有、底包没有的文件自然保留 —— 这就是 4.0.29 增量

system-as-root 的双层目录（source_filesystem/system/system/...）会自动探测，
源包与底包分别定位内容根后再按相对路径合并。

用法（一般由 XMAPort.py 在 Step 5 后调用，也可单独跑）:
    python tools/base_first_merge.py --device shennong
    python tools/base_first_merge.py --device shennong --dry
    python tools/base_first_merge.py --workspace /path/to/workspace --device shennong

退出码: 0 成功, 1 失败, 2 参数/环境错误
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
PARTS = ("system", "system_ext", "product")
MARKERS = ("framework", "etc", "app", "priv-app", "data-app", "overlay", "lib", "lib64")


def content_root(fs, part):
    """定位分区内容根，兼容 system-as-root 的 <part>/<part>/ 双层结构。"""
    base = fs / part
    if not base.is_dir():
        return None
    nested = base / part
    for cand in (nested, base):
        if cand.is_dir() and any((cand / m).exists() for m in MARKERS):
            return cand
    return base


def load_rules(path):
    """返回 (keep, drop, base)，元素为 (part_or_'*', 相对路径前缀)。

    keep  = 源包优先（底包不覆盖）
    base  = 底包优先，且优先级高于 keep（用于给宽泛 keep 开例外，
            例如 keep framework/ 但 base framework/oat/ 用底包 oat）
    drop  = 合并后删除
    """
    keep, drop, base = [], [], []
    if not path.is_file():
        return keep, drop, base
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        bits = line.split(None, 1)
        if len(bits) != 2 or bits[0] not in ("keep", "drop", "base"):
            continue
        action, spec = bits[0], bits[1].strip()
        if ":" in spec:
            part, rel = spec.split(":", 1)
        else:
            part, rel = "*", spec
        rel = rel.strip().lstrip("/").replace("\\", "/")
        if not rel:
            continue
        {"keep": keep, "drop": drop, "base": base}[action].append(
            (part.strip() or "*", rel))
    return keep, drop, base


def hit(rules, part, rel):
    for p, prefix in rules:
        if p in ("*", part) and (rel == prefix.rstrip("/") or rel.startswith(prefix)):
            return True
    return False


def load_denylist(path):
    if not path.is_file():
        return []
    return [l.strip().lstrip("/").replace("\\", "/")
            for l in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def main():
    ap = argparse.ArgumentParser(description="以 310 底包为骨架重建待打包内容树")
    ap.add_argument("--workspace", default=str(ROOT / "workspace"))
    ap.add_argument("--device", default="")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    ws = Path(args.workspace)
    src_fs, tgt_fs = ws / "source_filesystem", ws / "target_filesystem"
    if not src_fs.is_dir() or not tgt_fs.is_dir():
        print("[ERROR] 缺少 source_filesystem / target_filesystem：{}".format(ws))
        return 2

    device = args.device.strip()
    if not device:
        cfg = ws / "config.txt"
        if cfg.is_file():
            for line in cfg.read_text(encoding="gbk", errors="ignore").splitlines():
                if line.startswith("TARGET_DEVICE="):
                    device = line.split("=", 1)[1].strip()
                    break
    device = device or "shennong"
    dev_dir = ROOT / "devices" / device
    keep, drop, base = load_rules(dev_dir / "base_first_rules.conf")
    denylist = load_denylist(dev_dir / "perfect_remove_paths.txt")
    if not keep:
        print("[WARN] 未找到 base_first_rules.conf 的 keep 规则，将全部用底包覆盖")

    stats = {"device": device, "dry": args.dry, "parts": {},
             "rules_keep": len(keep), "rules_drop": len(drop),
             "rules_base": len(base), "denylist": len(denylist)}

    for part in PARTS:
        sroot, troot = content_root(src_fs, part), content_root(tgt_fs, part)
        if not sroot or not troot:
            print("[WARN] {} 缺少内容根，跳过 (src={}, tgt={})".format(part, sroot, troot))
            continue
        covered = kept = 0
        for root, _dirs, files in os.walk(troot):
            for name in files:
                tfile = Path(root) / name
                rel = tfile.relative_to(troot).as_posix()
                if hit(keep, part, rel) and not hit(base, part, rel):
                    kept += 1
                    continue
                sfile = sroot / rel
                if not args.dry:
                    sfile.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(tfile, sfile)
                covered += 1
        removed = 0
        # 规则 drop：目录或前缀
        for p, prefix in drop:
            if p not in ("*", part):
                continue
            target = sroot / prefix
            if target.is_dir():
                if not args.dry:
                    shutil.rmtree(target, ignore_errors=True)
                removed += 1
            elif target.is_file():
                if not args.dry:
                    target.unlink(missing_ok=True)
                removed += 1
        # 精确删除清单
        if part == "product":
            for rel in denylist:
                target = sroot / rel
                if target.is_file():
                    if not args.dry:
                        target.unlink()
                    removed += 1
        stats["parts"][part] = {
            "src_root": str(sroot), "tgt_root": str(troot),
            "base_overlaid": covered, "source_kept": kept, "removed": removed,
        }
        print("[{}] 底包覆盖 {} 个文件，保留源包 {} 个，删除 {} 项  ({})".format(
            part, covered, kept, removed, "dry-run" if args.dry else "written"))

    report = ws / "base_first_merge.json"
    if not args.dry:
        report.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
        print("[OK] 已写入 {}".format(report))
    print("[OK] base_first 内容树重建完成（后续 shallow / fix / pack 步骤照常作用于 SRC_FS）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
