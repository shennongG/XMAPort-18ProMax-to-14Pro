#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从已验证的本地镜像生成基线基准文件。

用法:
    python tools/gen_golden.py --perfect-dir <baseline-images>
    python tools/gen_golden.py --perfect-dir <dir> --full   # 大文件也全量算 md5

产出:
    devices/shennong/golden_baseline.json

为什么需要它:
    2026-11-06 真机确认该基线组合可正常进桌面。这是唯一
    一次可验证的「已知良好」状态。把它固化成结构化基准，后续 CI 包先离线
    比对再决定要不要上机，避免每次都拿手机试错、把好状态弄丢。
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import lp_layout  # noqa: E402

DEFAULT_OUT = ROOT / "devices" / "shennong" / "golden_baseline.json"
LARGE = 512 * 1024 * 1024
CHUNK = 1 << 22


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="生成 4.0.27 基线基准文件")
    ap.add_argument("--perfect-dir", required=True, help="含 super.img 与 firmware/ 的目录")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--full", action="store_true", help="对大文件也计算完整 md5")
    args = ap.parse_args()

    base = Path(args.perfect_dir)
    if not base.is_dir():
        print("[ERROR] 目录不存在: {}".format(base))
        return 2
    sup = base / "super.img"
    if not sup.is_file():
        print("[ERROR] 缺少 {}/super.img".format(base))
        return 2

    print("解析 super LP 元数据: {}".format(sup))
    layout = lp_layout.dump(sup)
    if not layout["partitions"]:
        print("[ERROR] lpdumps 未解析到分区")
        return 2
    print("  共 {} 个分区, super={} bytes".format(
        len(layout["partitions"]), layout["size_bytes"]))

    golden = {
        "description": "4.0.27 基线组合 (shennong) 真机验证进桌面",
        "source_zip": "shennong-baseline-OS4.0.27.zip",
        "verified": "2026-11-06 真机正常进入桌面，待机无发热",
        "device": "shennong",
        "super": layout,
        "images": {},
    }

    files = sorted(base.glob("*.img")) + sorted((base / "firmware").glob("*.img"))
    for p in files:
        key = p.name if p.parent == base else "firmware/" + p.name
        size = p.stat().st_size
        skip = not args.full and size > LARGE
        print("  {:<28} {:>12,} B {}".format(
            key, size, "(跳过 md5)" if skip else "md5..."), flush=True)
        golden["images"][key] = {
            "size": size,
            "md5": None if skip else md5(p),
        }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(golden, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print("\n[OK] 已写入 {}".format(out))
    print("     {} 个镜像, {} 个分区".format(len(golden["images"]), len(layout["partitions"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
