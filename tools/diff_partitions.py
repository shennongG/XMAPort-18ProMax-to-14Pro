#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐分区、逐文件对比两份 `extract.erofs -p` 清单，输出 Markdown 差异报告。

用于「可开机参考基线 vs 本项目构建产物」的定位：真机卡 HyperOS 二屏时，
静态基线（数量/关键标签）可能全过，但文件级差异才是根因。

清单目录约定：<dir>/<part>.txt（由 extract.erofs -p -i xxx.img -s 生成）。
每个清单行形如：
    Extract: type=FILE dataLayout=INLINE fsConfig=[/etc/build.prop 0 0 0644] seLabel=[u:object_r:system_file:s0]

用法：
    python tools/diff_partitions.py --left perfect429 --right new2 --out diff.md
    python tools/diff_partitions.py --left A --right B --only product,system_ext --verbose
"""
import argparse
import collections
import re
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


PARTS = ("system", "system_ext", "product", "odm", "vendor", "mi_ext",
         "vendor_dlkm", "system_dlkm")
FC = re.compile(
    r"type=(\w+) dataLayout=\w+ fsConfig=\[(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\] "
    r"seLabel=\[(.*?)\]")


def load(path):
    """返回 {path: (type, uid, gid, mode, selabel)}"""
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = FC.search(line)
        if not m:
            continue
        out[m.group(2)] = (m.group(1), m.group(3), m.group(4), m.group(5),
                           m.group(6).strip())
    return out


def group_key(p, depth=2):
    seg = p.strip("/").split("/")
    return "/".join(seg[:depth]) if len(seg) > 1 else seg[0]


def diff_part(part, left, right, verbose):
    lines = []
    lset, rset = set(left), set(right)
    only_l = sorted(lset - rset)   # 参考基线有、产物没有 → 缺失
    only_r = sorted(rset - lset)   # 产物有、参考基线没有 → 多余
    label_diff, meta_diff = [], []
    for p in sorted(lset & rset):
        lt, lu, lg, lm, ll = left[p]
        rt, ru, rg, rm, rl = right[p]
        if ll != rl:
            label_diff.append((p, ll, rl))
        if (lu, lg, lm) != (ru, rg, rm):
            meta_diff.append((p, "{}/{}/{}".format(lu, lg, lm),
                              "{}/{}/{}".format(ru, rg, rm)))

    lines.append("## {}".format(part))
    lines.append("")
    lines.append("| 项 | 数值 |")
    lines.append("| --- | --- |")
    lines.append("| 参考基线条目 | {} |".format(len(left)))
    lines.append("| 产物条目 | {} |".format(len(right)))
    lines.append("| 缺失（参考基线有） | {} |".format(len(only_l)))
    lines.append("| 多余（产物独有） | {} |".format(len(only_r)))
    lines.append("| 标签不同 | {} |".format(len(label_diff)))
    lines.append("| 属主/权限不同 | {} |".format(len(meta_diff)))
    lines.append("")

    if only_l:
        c = collections.Counter(group_key(p) for p in only_l)
        lines.append("### 缺失 Top 目录")
        lines.append("")
        for k, v in c.most_common(12):
            lines.append("- `{}` — {} 项".format(k, v))
        lines.append("")
    if only_r:
        c = collections.Counter(group_key(p) for p in only_r)
        lines.append("### 多余 Top 目录")
        lines.append("")
        for k, v in c.most_common(12):
            lines.append("- `{}` — {} 项".format(k, v))
        lines.append("")

    if label_diff:
        lines.append("### 标签不同（高危：直接影响 SELinux 放行）")
        lines.append("")
        lines.append("| 路径 | 参考基线 | 产物 |")
        lines.append("| --- | --- | --- |")
        for p, a, b in label_diff[:60]:
            lines.append("| `{}` | `{}` | `{}` |".format(p, a, b))
        if len(label_diff) > 60:
            lines.append("")
            lines.append("> 仅列前 60 条，共 {} 条".format(len(label_diff)))
        lines.append("")

    if meta_diff:
        lines.append("### 属主/权限不同")
        lines.append("")
        lines.append("| 路径 | 参考基线 uid/gid/mode | 产物 |")
        lines.append("| --- | --- | --- |")
        for p, a, b in meta_diff[:40]:
            lines.append("| `{}` | `{}` | `{}` |".format(p, a, b))
        if len(meta_diff) > 40:
            lines.append("")
            lines.append("> 仅列前 40 条，共 {} 条".format(len(meta_diff)))
        lines.append("")

    if verbose:
        if only_l:
            lines.append("### 缺失明细")
            lines.append("")
            for p in only_l:
                lines.append("- `{}`".format(p))
            lines.append("")
        if only_r:
            lines.append("### 多余明细")
            lines.append("")
            for p in only_r:
                lines.append("- `{}`".format(p))
            lines.append("")
    return lines


def main():
    ap = argparse.ArgumentParser(description="逐文件对比两份分区清单")
    ap.add_argument("--left", required=True, help="基准目录（可开机参考基线清单）")
    ap.add_argument("--right", required=True, help="待检目录（本项目产物清单）")
    ap.add_argument("--only", default="", help="只比这些分区，逗号分隔")
    ap.add_argument("--out", default=None, help="输出 Markdown 路径")
    ap.add_argument("--verbose", action="store_true", help="输出缺失/多余全量明细")
    args = ap.parse_args()

    ld, rd = Path(args.left), Path(args.right)
    parts = [p.strip() for p in args.only.split(",")] if args.only else list(PARTS)
    lines = ["# 分区内容差异报告", "",
             "- 基准（参考基线）：`{}`".format(ld), "- 待检（产物）：`{}`".format(rd), ""]
    total_missing = total_extra = total_label = 0
    for part in parts:
        left = load(ld / (part + ".txt"))
        right = load(rd / (part + ".txt"))
        if not left and not right:
            continue
        blk = diff_part(part, left, right, args.verbose)
        # 汇总计数
        for ln in blk:
            if ln.startswith("| 缺失"):
                total_missing += int(ln.split("|")[2].strip())
            elif ln.startswith("| 多余"):
                total_extra += int(ln.split("|")[2].strip())
            elif ln.startswith("| 标签不同"):
                total_label += int(ln.split("|")[2].strip())
        lines += blk

    lines.insert(4, "> 汇总：缺失 {} 项 / 多余 {} 项 / 标签不同 {} 项".format(
        total_missing, total_extra, total_label))
    lines.insert(5, "")
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print("报告: {} ({} 行)".format(args.out, len(lines)))
    else:
        print(text)
    print("汇总: 缺失 {} / 多余 {} / 标签不同 {}".format(
        total_missing, total_extra, total_label))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
