#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""XMAPort SHALLOW stage / 精简阶段

精简与设备适配以跨平台 Python 实现，插入到 XMAPort 流水线的
「迁移之后 / 打包之前」执行。

流水线位置：
    Step1 下载 -> Step2 解压 -> Step3 payload -> Step4 解镜像
    -> Step5 迁移&补丁 (make_hyper.py)
    -> Step6 精简 (本模块, shallow_slim.py)      <== 新增
    -> Step7 打包 (pack_partitions.py + lpmake)
    -> Step8 汇总

目录约定（与 XMAPort 主流程一致）：
    workspace/source_filesystem/<partition>/...   待打包的源分区内容
    workspace/target_filesystem/<partition>/...   底包分区内容
    workspace/config.txt                          TARGET_DEVICE=<代号>
    devices/<device>/                             该机型的精简/适配资产

注意 system 分区在 Android 12+ 常见 <part>/<part>/ 双层结构
(system-as-root)，本模块对两种布局都能自适应定位。

用法：
    python tools/shallow_slim.py slim       # 完整精简（按 config.ini [shallow] 开关）
    python tools/shallow_slim.py apps       # 仅删除应用
    python tools/shallow_slim.py files      # 仅注入设备文件/适配
    python tools/shallow_slim.py prop       # 仅 build.prop 补丁
    python tools/shallow_slim.py sigv       # 仅去除系统签名校验
    python tools/shallow_slim.py icons      # 仅图标资源替换
    python tools/shallow_slim.py firmware   # 仅固件层（KernelSU/recovery/刷机脚本）
    python tools/shallow_slim.py oat        # 仅清理过期 services odex/vdex/art
    python tools/shallow_slim.py align      # 仅按完美包基线裁剪 product
    python tools/shallow_slim.py compat     # 仅注入跨版本兼容文件（VNDK/mapping）
    python tools/shallow_slim.py check      # 只体检不改动

返回码：0=全部成功 1=有步骤失败 2=参数/环境错误
"""

import configparser
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

# config.ini 可能是 UTF-8(GitHub 网页编辑) 或 GBK(本地 ANSI)，统一走编码兼容层
try:
    from config_codec import read as _ini_read
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config_codec import read as _ini_read

# ---------------------------------------------------------------- 控制台
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _supports_color():
    if os.environ.get("NO_COLOR"):
        return False
    if os.name == "nt":
        return True
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


if _supports_color():
    R, G, Y, C, D, N, BD, W = ("\x1b[91m", "\x1b[92m", "\x1b[93m", "\x1b[96m",
                               "\x1b[90m", "\x1b[0m", "\x1b[1m", "\x1b[97m")
else:
    R = G = Y = C = D = N = BD = W = ""


def info(msg):
    print("  {}[INFO]{} {}".format(G, N, msg), flush=True)


def warn(msg):
    print("  {}[WARN]{} {}".format(Y, N, msg), flush=True)


def err(msg):
    print("  {}[ERROR]{} {}".format(R, N, msg), flush=True)


def ok(msg):
    print("  {}[ OK ]{} {}".format(G, N, msg), flush=True)


def title(msg):
    print()
    print("  {}{}==== {} ===={}".format(C, BD, msg, N), flush=True)


# ---------------------------------------------------------------- 路径
ROOT = Path(__file__).resolve().parent.parent          # 项目根
TOOLS = ROOT / "tools"
LINUX_TOOLS = TOOLS / "linux"
SHALLOW_DIR = ROOT / "shallow"
DEVICES_DIR = ROOT / "devices"
WORKSPACE = ROOT / "workspace"
SRC_FS = WORKSPACE / "source_filesystem"
TGT_FS = WORKSPACE / "target_filesystem"
CONFIG = ROOT / "config.ini"
DEVCONF = WORKSPACE / "config.txt"

RESULT = {"ok": 0, "fail": 0, "skip": 0}


def _bump(key):
    RESULT[key] = RESULT.get(key, 0) + 1


# ---------------------------------------------------------------- 配置读取
DEFAULT_SHALLOW_CFG = {
    "enable": "true",
    "strict": "false",
    "remove_apps": "true",
    "inject_device_files": "true",
    "replace_camera": "true",
    "apply_fixes": "true",
    "patch_product_props": "true",
    "patch_build_prop": "true",
    "density": "",
    "aptx_lossless": "true",
    "build_date": "true",
    "disable_signature_verification": "false",
    "drop_stale_services_oat": "true",
    "align_perfect_product": "true",
    "perfect_icons": "false",
    "kernelsu": "false",
    "twrp_recovery": "false",
    "flash_tools": "false",
    "fix_init_rc": "true",
    "apps_list": "",
}


def read_shallow_config():
    """读取 config.ini 的 [shallow] 段（GBK/ANSI 编码，与 XMAPort 一致）"""
    cfg = dict(DEFAULT_SHALLOW_CFG)
    if not CONFIG.exists():
        return cfg
    text = _ini_read(CONFIG)[0]
    in_shallow = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("["):
            in_shallow = (line.lower() == "[shallow]")
            continue
        if not in_shallow or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().lower()
        if key in cfg:
            cfg[key] = val.strip()
    return cfg


def get_bool(cfg, key, default=False):
    return cfg.get(key, str(default)).strip().lower() in ("true", "1", "yes", "y", "on")


def read_target_device(argv_device=""):
    """目标机型代号：命令行 > workspace/config.txt"""
    if argv_device:
        return argv_device.strip()
    if DEVCONF.exists():
        try:
            for line in DEVCONF.read_text(encoding="gbk", errors="ignore").splitlines():
                if line.startswith("TARGET_DEVICE="):
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return ""


# ---------------------------------------------------------------- 路径解析
def part_roots(part, base=None):
    """返回分区内容根的候选（兼容 system-as-root 双层结构），按优先级排序。

    例 part=system：<fs>/system 与 <fs>/system/system 都存在时，
    真正想要的 system 内容实际在后者；两者都返回交给上层按子路径挑选。

    base 默认在调用时解析 SRC_FS（不能用默认参数绑定，import 后就固化了）。
    """
    base = SRC_FS if base is None else base
    base_root = base / part
    cands = [base_root]
    nested = base_root / part
    if nested.is_dir():
        cands.append(nested)
    return [p for p in cands if p.is_dir()]


def find_subdir(part, relpath, base=None):
    """在分区内定位 relpath（相对分区内容根），返回首个命中的 Path 或 None"""
    base = SRC_FS if base is None else base
    parts = [p for p in str(relpath).replace("\\", "/").split("/") if p]
    for root in part_roots(part, base):
        cand = root.joinpath(*parts)
        if cand.is_dir():
            return cand
    return None


def ensure_subdir(part, relpath, base=None):
    """定位或创建分区内目录（优先落在父链已存在的 root 上，避免造出空壳层级）"""
    base = SRC_FS if base is None else base
    hit = find_subdir(part, relpath, base)
    if hit:
        return hit
    parts = [p for p in str(relpath).replace("\\", "/").split("/") if p]
    best_root, best_depth = None, -1
    for root in part_roots(part, base):
        cur, depth = root, 0
        for seg in parts[:-1]:
            cur = cur / seg
            if cur.is_dir():
                depth += 1
            else:
                break
        if depth > best_depth:
            best_root, best_depth = root, depth
    target = (best_root or (base / part)).joinpath(*parts)
    target.mkdir(parents=True, exist_ok=True)
    return target


# 部分 ROM 用 data_app 而非 data-app，这里做等价别名
REL_ALIASES = {"data-app": "data_app", "data_app": "data-app"}


def resolve_scan_dir(part, relpath, base=SRC_FS):
    """宽松定位扫描目录：直接路径 / 去掉首层 part / data-app 别名"""
    seen = []
    cands = [relpath]
    seg = relpath.split("/")
    if seg and seg[0] == part and len(seg) > 1:
        cands.append("/".join(seg[1:]))
    for alt_from, alt_to in REL_ALIASES.items():
        if relpath == alt_from or relpath.endswith("/" + alt_from):
            cands.append(relpath.replace(alt_from, alt_to))
    for rel in cands:
        if rel in seen:
            continue
        seen.append(rel)
        hit = find_subdir(part, rel, base)
        if hit:
            return hit
    return None


def find_file_in_part(part, relpath, base=None):
    base = SRC_FS if base is None else base
    parts = [p for p in str(relpath).replace("\\", "/").split("/") if p]
    for root in part_roots(part, base):
        cand = root.joinpath(*parts)
        if cand.is_file():
            return cand
    return None


def safe_rm(path, root):
    """安全检查后删除：目标必须在 root 内且不是 root 本身"""
    try:
        path = Path(path).resolve()
        root = Path(root).resolve()
        if path == root or root not in path.parents:
            warn("refuse to delete outside workspace: {}".format(path))
            return False
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
        return True
    except Exception as e:
        warn("delete failed {}: {}".format(path, e))
        return False


# ---------------------------------------------------------------- 机型档案
PORT_CONF_NAME = "port.conf"


def read_device_profile(device):
    """读取 devices/<代号>/port.conf 机型档案（UTF-8，缺失返回空 dict）"""
    prof = {}
    if not device:
        return prof
    conf = DEVICES_DIR / device / PORT_CONF_NAME
    if not conf.is_file():
        return prof
    try:
        for raw in conf.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith(";"):
                continue
            if line.startswith("["):
                continue
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            prof[key.strip().lower()] = val.strip()
    except Exception as e:
        warn("port.conf read failed: {}".format(e))
    return prof


# ---------------------------------------------------------------- 清单解析
DEFAULT_APPS_LIST = """# XMAPort SHALLOW - 应用精简清单
# 格式: <partition>:<分区内目录>:<=精确名 或 子串名(不区分大小写)>
# 以 # 开头为注释；空行忽略。
#
# 以下条目迁移自 早期精简清单 的五组精简列表。
product:app:=AnalyticsCore
product:app:=CarWith
product:app:=CatchLog
product:app:=com.xiaomi.ugd
product:app:=GoogleLocationHistory
product:app:=HybridPlatform
product:app:=MiBugReport
product:app:=MIUIgreenguard
product:app:=MIUIReporter
product:app:=OtaProvision
product:app:=OTrPBroker
product:app:=PaymentService
product:app:=remoteSimLockAuthentication
product:app:=remotesimlockservice
product:app:=RideModeAudio
product:app:=SwitchAccess
product:app:=system
product:app:=talkback
product:app:=Updater
product:app:=UPTsmService
product:app:=WMService
product:data-app:=MIGalleryLockscreen
product:data-app:=MIpay
product:data-app:=MIUIDriveMode
product:data-app:=MIUIDuokanReader
product:data-app:=MIUIGameCenter
product:data-app:=MIUINewHome
product:data-app:=MIUIYoupin
product:data-app:=MIUIHuanJi
product:data-app:=MIUIMiDrive
product:data-app:=MIUIVirtualSim
product:data-app:=ThirdAppAssistant
product:data-app:=XMRemoteController
product:data-app:=MIUIVipAccount
product:data-app:=MiuiScanner
product:data-app:=Xinre
product:data-app:=SmartHome
product:data-app:=MiShop
product:data-app:=MiRadio
product:data-app:=MIUICompass
product:data-app:=BaiduIME
product:data-app:=iflytek.inputmethod
product:data-app:=MIUIEmail
product:data-app:=MIUIVideo
product:data-app:=MIUIMusicT
product:data-app:=Health
product:data-app:=MIService
product:pangu/system/priv-app:=SystemHelper
product:priv-app:=AutoRegistration
product:priv-app:=ConfigUpdater
product:priv-app:=MiGameCenterSDKService
product:priv-app:=MIbrowser
product:priv-app:=MIUIBarrageV2
product:priv-app:=RegService
system:system/priv-app:=BuiltInPrintService
system:system/priv-app:=CallLogBackup
system:system/priv-app:=CellBroadcastLegacyApp
system:system/priv-app:=CellBroadcastServiceModulePlatform
system:system/priv-app:=MusicFX
system:system/priv-app:=Tag
system:system/priv-app:=UserDictionaryProvider
"""

APPS_LIST_NAME = "remove_apps.conf"
KEEP_LIST_NAME = "keep_apps.conf"


def resolve_apps_list(device, cfg):
    """优先级：config.ini apps_list > devices/<device>/remove_apps.conf > shallow/remove_apps.conf > 内置默认"""
    custom = cfg.get("apps_list", "").strip()
    if custom:
        p = Path(custom)
        if not p.is_absolute():
            p = ROOT / custom
        if p.is_file():
            return p
        warn("apps_list not found, fallback: {}".format(p))
    if device:
        cand = DEVICES_DIR / device / APPS_LIST_NAME
        if cand.is_file():
            return cand
    common = SHALLOW_DIR / APPS_LIST_NAME
    if common.is_file():
        return common
    return None


def resolve_keep_lists(device):
    """保护名单：shallow/keep_apps.conf 与 devices/<device>/keep_apps.conf 同时生效"""
    paths = [p for p in (SHALLOW_DIR / KEEP_LIST_NAME,
                         DEVICES_DIR / device / KEEP_LIST_NAME if device else None)
             if p is not None and p.is_file()]
    return paths


def parse_rule_line(raw):
    """解析一条规则 -> (part, rel, exact:bool, pattern:小写匹配串) 或 None"""
    line = raw.strip()
    if not line.startswith("!"):
        pass
    else:
        return None
    seg = line.split(":")
    if len(seg) < 3:
        return None
    part = seg[0].strip()
    rel = seg[1].strip().strip("/")
    pat = ":".join(seg[2:]).strip()
    if not part or not pat:
        return None
    exact = pat.startswith("=")
    if exact:
        pat = pat[1:]
    return (part, rel, exact, pat.lower())


def load_rules(paths, default_text=None):
    """输入文件或文本列表 -> [(part, rel, exact, pattern), ...]"""
    rules = []
    for item in paths:
        if isinstance(item, str):
            text = item
        else:
            if item is None or not Path(item).is_file():
                continue
            text = Path(item).read_text(encoding="utf-8", errors="ignore")
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("!"):          # 临时停用
                continue
            rule = parse_rule_line(line)
            if rule is None:
                continue
            rules.append(rule)
    if not rules and default_text:
        for raw in default_text.splitlines():
            rule = parse_rule_line(raw)
            if rule:
                rules.append(rule)
    return rules


def match_name(dirname, rule):
    part, rel, exact, pat = rule
    name = dirname.lower()
    if exact:
        return name == pat
    return pat in name


# ---------------------------------------------------------------- Step: 精简应用
def step_remove_apps(device, cfg, dry=False):
    title("SHALLOW 1/7  精简应用 (remove apps)")
    list_path = resolve_apps_list(device, cfg)
    info("apps list: {}".format(list_path.name if list_path else "<built-in default>"))
    rules = load_rules([list_path], DEFAULT_APPS_LIST)
    keep_rules = load_rules(resolve_keep_lists(device))
    if keep_rules:
        info("keep list: {} rule(s) from {}".format(
            len(keep_rules), " / ".join(
                p.name for p in resolve_keep_lists(device))))
    removed = 0
    protected = 0
    for part, rel, exact, pat in rules:
        base_dir = resolve_scan_dir(part, rel)
        if base_dir is None:
            continue
        hits = []
        try:
            for cur, dirs, _files in os.walk(base_dir):
                for d in list(dirs):
                    full = Path(cur) / d
                    if not match_name(d, (part, rel, exact, pat)):
                        continue
                    if _is_protected(full, part, rel, keep_rules):
                        info("keep:  {}/{}/{}  (protected)".format(part, rel, d))
                        protected += 1
                        continue
                    hits.append(full)
        except Exception as e:
            warn("scan failed {}: {}".format(base_dir, e))
            continue
        for h in hits:
            if not h.exists():
                continue
            info("remove: {}/{}/{}".format(part, rel, h.name))
            if dry:
                removed += 1
                continue
            if safe_rm(h, WORKSPACE):
                removed += 1
    if protected:
        warn("protected {} director(y/ies) by keep list (skipped)".format(protected))
    if removed:
        ok("removed {} app director(y/ies)".format(removed))
    else:
        info("no matching app directory found (nothing removed)")
    _bump("ok")
    return True


def _is_protected(full_path, part, rel, keep_rules):
    """命中保护名单返回 True。分区支持通配 *，目录按同级/父级层级匹配"""
    if not keep_rules:
        return False
    name = full_path.name.lower()
    rel_tail = str(rel).replace("\\", "/").strip("/").lower()
    rel_segs = set([s for s in rel_tail.split("/") if s]) if rel_tail else set()
    parent_names = set()
    try:
        for p in full_path.parents:
            parent_names.add(p.name.lower())
    except Exception:
        pass
    for kpart, krel, kexact, kpat in keep_rules:
        if kpart not in ("*", part.lower()):
            continue
        if kexact:
            matched = (name == kpat)
        else:
            matched = (kpat in name)
        if not matched:
            continue
        if krel in ("*", rel_tail):
            return True
        # 目录层级兜底：名单写的是父层级（priv-app / app）或路径中间段
        if krel in rel_segs or krel in parent_names:
            return True
    return False


# ---------------------------------------------------------------- Step: 设备文件注入
def _unzip_into(zip_path, dest_dir, overwrite=True):
    """解压到目录；overwrite=True 时先清空目标目录(保持与原 make.sh 一致)"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if overwrite:
        for child in dest_dir.iterdir():
            safe_rm(child, WORKSPACE)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest_dir)
    return True


def step_inject_device_files(device, cfg, dry=False):
    title("SHALLOW 2/7  注入设备文件 (device files)")
    files_dir = DEVICES_DIR / device
    if not device or not files_dir.is_dir():
        warn("device asset dir not found: {} - skipped".format(files_dir))
        info("把该机型的文件放到 devices/<代号>/ 即可自动生效")
        _bump("skip")
        return True

    op_ok, op_fail, missed = 0, 0, 0

    def do(name, func):
        """func 返回 True=已改动 False=目标缺失(记 skipped) 抛异常=失败"""
        nonlocal op_ok, op_fail, missed
        if dry:
            info("[dry] {}".format(name))
            op_ok += 1
            return
        try:
            if func():
                ok(name)
                op_ok += 1
            else:
                info("{}: target not found, skipped".format(name))
                missed += 1
        except Exception as e:
            err("{} failed: {}".format(name, e))
            op_fail += 1

    files = files_dir

    # product 叠加层
    def _overlay():
        return _unzip_into(files / "overlay.zip", ensure_subdir("product", "overlay"))

    if (files / "overlay.zip").is_file():
        do("product overlay", _overlay)
    # device_features
    if (files / "device_features.zip").is_file():
        do("product device_features", lambda: _unzip_into(
            files / "device_features.zip",
            ensure_subdir("product", "etc/device_features")))
    # displayconfig
    if (files / "displayconfig.zip").is_file():
        do("product displayconfig", lambda: _unzip_into(
            files / "displayconfig.zip",
            ensure_subdir("product", "etc/displayconfig")))
    # 基线中特有的亮度控制 / 设置 RRO（golden-only）。
    # 4.0.29 源包和上面两个资源包都不含这些文件，缺了会导致自动亮度曲线
    # 退化、设置「全部参数与信息」缺项。overwrite=False：只补文件，不清空
    # product 根（清空会把整个 product 分区内容删掉）。
    if (files / "golden_product_files.zip").is_file():
        def _golden_product():
            root = ensure_subdir("product", "overlay").parent
            return _unzip_into(files / "golden_product_files.zip", root,
                               overwrite=False)
        do("product golden-only overlay / displayconfig", _golden_product)
    # PowerKeeper（精准电量 / 亮屏可用时长）
    if (files / "PowerKeeper.zip").is_file():
        do("PowerKeeper (battery stats)", lambda: _unzip_into(
            files / "PowerKeeper.zip",
            find_subdir("system", "app/PowerKeeper")
            or find_subdir("system", "system/app/PowerKeeper")
            or ensure_subdir("system", "app/PowerKeeper")))
    # 相机标定
    if (files / "CameraTools_beta.zip").is_file():
        do("CameraTools calibration", lambda: _unzip_into(
            files / "CameraTools_beta.zip",
            resolve_scan_dir("product", "app") or ensure_subdir("product", "app"),
            overwrite=False))
    # 开机动画
    if (files / "bootanimation.zip").is_file():
        do("bootanimation", lambda: shutil.copy2(
            files / "bootanimation.zip",
            ensure_subdir("product", "media") / "bootanimation.zip"))

    # vendor fstab（底包 vendor）
    if (files / "fstab.qcom").is_file():
        def _fstab():
            tgt = find_file_in_part("vendor", "etc/fstab.qcom", TGT_FS)
            if tgt is None:
                tgt = TGT_FS / "vendor" / "etc" / "fstab.qcom"
                if not tgt.parent.is_dir():
                    return False
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(files / "fstab.qcom", tgt)
            return True

        do("vendor fstab.qcom", _fstab)

    # 机型专属应用：手机管家（安全服务）与主题壁纸。
    # 从 devices/<代号>/ 取同版本 APK 覆盖到 product 分区；缺文件则跳过。
    for apk_name, rel_dir in (
        ("MIUISecurityCenter.apk", "priv-app/MIUISecurityCenter"),
        ("MIUIThemeManager.apk", "app/MIUIThemeManager"),
    ):
        if (files / apk_name).is_file():
            def _inject_apk(name=apk_name, rel=rel_dir):
                tgt = ensure_subdir("product", rel)
                shutil.copy2(files / name, tgt / name)
                return True
            do("{} ({})".format(apk_name, rel_dir), _inject_apk)

    info("injected={} skipped={} failed={}".format(op_ok, missed, op_fail))
    if op_fail:
        _bump("fail")
        return False
    _bump("ok")
    return True


# ---------------------------------------------------------------- Step: 相机 + 功能适配
def step_replace_camera(device, cfg, dry=False):
    title("SHALLOW 3/7  替换相机 & 占位/适配 APK")
    files_dir = DEVICES_DIR / device
    if not files_dir.is_dir():
        _bump("skip")
        return True

    # 分片 apk 合并（原 make.sh 的 .1/.2/.3 切片）
    parts = sorted(files_dir.glob("MiuiCamera.apk.*"),
                   key=lambda p: p.name)
    camera_apks = [f for f in parts if re.search(r"\.\d+$", f.name)]
    dst_dir = find_subdir("product", "priv-app/MiuiCamera") or resolve_scan_dir(
        "product", "priv-app/MiuiCamera")
    tmp_dir = WORKSPACE / "shallow_tmp"
    if camera_apks and dst_dir:
        # 合并结果放 workspace 临时目录，避免污染 devices/ 仓库内容
        tmp_dir.mkdir(parents=True, exist_ok=True)
        merged = tmp_dir / "MiuiCamera.apk"
        if dry:
            info("[dry] merge camera from {} slices".format(len(camera_apks)))
        else:
            try:
                if not merged.is_file() or merged.stat().st_size == 0:
                    with open(merged, "wb") as out:
                        for p in camera_apks:
                            out.write(p.read_bytes())
                for child in dst_dir.iterdir():
                    safe_rm(child, WORKSPACE)
                shutil.copy2(merged, dst_dir / merged.name)
                ok("MiuiCamera replaced ({} slices -> {:.1f}MB)".format(
                    len(camera_apks), merged.stat().st_size / 1048576))
            except Exception as e:
                err("camera merge failed: {}".format(e))
                _bump("fail")
                return False

    # MiAONService（注视感知）
    if (files_dir / "MiAONService.apk").is_file():
        d = ensure_subdir("product", "app/MiAONService")
        if not dry:
            for child in d.iterdir():
                safe_rm(child, WORKSPACE)
            shutil.copy2(files_dir / "MiAONService.apk", d / "MiAONService.apk")
            ok("MiAONService installed")

    # MSA（占位广告组件）
    if (files_dir / "MSA.apk").is_file():
        d = ensure_subdir("product", "app/MSA")
        if not dry:
            for child in d.iterdir():
                safe_rm(child, WORKSPACE)
            shutil.copy2(files_dir / "MSA.apk", d / "MSA.apk")
            ok("MSA placeholder installed")
    _bump("ok")
    return True


# ---------------------------------------------------------------- Step: build.prop
def _prop_set(path, key, value, add_if_missing=False):
    """原地写回单个 prop；返回是否写入"""
    p = Path(path)
    if not p.is_file():
        return False
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    lines = text.splitlines()
    hit = False
    out = []
    for line in lines:
        if "=" in line and line.split("=", 1)[0].strip() == key:
            out.append("{}={}".format(key, value))
            hit = True
        else:
            out.append(line)
    if not hit and add_if_missing:
        out.append("{}={}".format(key, value))
        hit = True
    if hit:
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return hit


def _prop_get(path, key):
    p = Path(path)
    if not p.is_file():
        return ""
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if "=" in s and s.split("=", 1)[0].strip() == key:
            return s.split("=", 1)[1].strip()
    return ""


def step_patch_product_props(device, cfg, dry=False):
    """把 product/build.prop 改成「目标机型」，并写入跨版本移植必需属性。

    2026-11-21 逐键对比 X-MiPort 完美包（真机可开机）发现两处本项目完全没做的事：

    1. 机型标识仍是源机：`ro.product.product.name=miproduct_madrid`、
       `ro.build.flavor=miproduct_madrid-user`、`ro.build.description=madrid-user ...`、
       `ro.product.build.fingerprint=Xiaomi/madrid/...`。而 odm/vendor 是 shennong，
       机型特性与资源按 madrid 加载，与底包错位。
    2. 源包的 ART 预加载 / 预缓存 / prestart 全部开着。跨版本移植后源包的
       预编译产物在目标底包上失效，开启预加载会加载失效类，表现为卡开机动画。

    完美包的做法：机型标识改成 shennong，并把
    `persist.sys.{art_startup_class_preload,precache,preload,app_dexfile_preload}.enable`
    与 `persist.sys.prestart.*` 全部设为 false。清单见
    devices/<device>/product_props.conf。
    """
    title("SHALLOW 4/9  移植机型属性 (product/build.prop)")
    if not get_bool(cfg, "patch_product_props", True):
        info("patch_product_props=false, skipped")
        _bump("skip")
        return True
    bp = find_file_in_part("product", "etc/build.prop") or \
        find_file_in_part("product", "build.prop")
    if not bp:
        warn("product build.prop 未找到, skipped")
        _bump("skip")
        return True

    name = _prop_get(bp, "ro.product.product.name")
    src = name.rsplit("_", 1)[-1] if name else ""
    changed = 0
    if src and device and src != device:
        # 只改这几个标识键，避免误伤其它含源代号的 value
        for key in ("ro.product.product.name", "ro.build.flavor",
                    "ro.build.description", "ro.product.build.fingerprint",
                    "ro.build.fingerprint"):
            val = _prop_get(bp, key)
            if not val or src not in val:
                continue
            newval = val.replace(src, device)
            if dry:
                info("[dry] {}: {} -> {}".format(key, val, newval))
            elif _prop_set(bp, key, newval):
                changed += 1
                info("{}: {} -> {}".format(key, val, newval))
        for key in ("persist.vendor.bluetooth.lhdc_v4.supported",
                    "persist.vendor.bluetooth.lhdc_v5.supported"):
            val = _prop_get(bp, key)
            if val and val == src:
                if not dry and _prop_set(bp, key, device):
                    changed += 1
        if not dry:
            _prop_set(bp, "ro.product.port.device", src, True)
        ok("机型标识 {} -> {}（{} 处）".format(src, device, changed))
    elif name:
        info("机型标识已是 {}，跳过替换".format(name))

    conf = DEVICES_DIR / device / "product_props.conf"
    applied = 0
    if conf.is_file():
        for line in conf.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k, v = k.strip(), v.strip()
            if not k:
                continue
            if dry:
                applied += 1
                continue
            if _prop_set(bp, k, v, True):
                applied += 1
        ok("移植属性: {} 项（预加载/prestart 关闭 + 机型调优）".format(applied))
    else:
        info("no {} found, skip props".format(conf.name))

    # 「关于手机」里的 HyperOS 版本号来自 odm（整包来自底包），
    # 不同步的话 system 明明是 4.0.31，设置里仍显示 3.0.310，
    # 会被误认为刷了底包（2026-11-22 实测踩过）。
    port_ver = _prop_get(bp, "ro.build.version.incremental") or ""
    odm_bp = find_file_in_part("odm", "etc/build.prop", TGT_FS)
    if odm_bp and port_ver.startswith("OS"):
        old_ver = _prop_get(odm_bp, "ro.mi.os.version.incremental")
        seg = port_ver.split(".")
        os_name = ".".join(seg[:2]) if len(seg) >= 2 else port_ver
        os_code = os_name[2:].split(".")[0] if os_name.startswith("OS") else ""
        n_odm = 0
        if not dry:
            for k, v in (("ro.mi.os.version.incremental", port_ver),
                         ("ro.mi.os.version.name", os_name)):
                if _prop_set(odm_bp, k, v, True):
                    n_odm += 1
            if os_code and _prop_set(odm_bp, "ro.mi.os.version.code", os_code, True):
                n_odm += 1
        ok("HyperOS 版本显示: {} -> {}（{} 项）".format(
            old_ver or "?", port_ver, n_odm))
    elif not odm_bp:
        warn("odm/etc/build.prop 未找到，HyperOS 版本号无法同步")

    _bump("ok")
    return True


def step_patch_build_prop(device, cfg, dry=False):
    title("SHALLOW 4/7  build.prop 补丁")
    if not get_bool(cfg, "patch_build_prop", True):
        info("patch_build_prop=false, skipped")
        _bump("skip")
        return True

    now = datetime.now()
    stamp = now.strftime("%a %b %d %H:%M:%S CST %Y")
    utc = str(int(now.timestamp()))
    touched = 0

    if get_bool(cfg, "build_date", True):
        # 固定候选位置，避免对整个文件系统 rglob（真实分区有几十万文件）
        candidates = []
        for base in (SRC_FS, TGT_FS):
            if not base.is_dir():
                continue
            for part in ("system", "system_ext", "product", "mi_ext", "odm", "vendor"):
                for rel in ("build.prop", "etc/build.prop",
                            "{0}/build.prop".format(part), "{0}/etc/build.prop".format(part)):
                    candidates.append(base / part / rel)
        for bp in candidates:
            if not bp.is_file():
                continue
            if dry:
                touched += 1
                continue
            if _prop_set(bp, "build.date", stamp):
                touched += 1
            _prop_set(bp, "build.date.utc", utc)

    density = cfg.get("density", "").strip()
    if density and density != "0":
        bp = find_file_in_part("product", "etc/build.prop") or \
            find_file_in_part("product", "build.prop")
        if bp and not dry and _prop_set(bp, "persist.miui.density_v2", density, True):
            ok("density -> {}".format(density))
            touched += 1

    if get_bool(cfg, "aptx_lossless", True):
        cand = find_file_in_part("vendor", "build.prop", TGT_FS) or (TGT_FS / "vendor" / "build.prop")
        if cand.is_file() and not dry:
            text = cand.read_text(encoding="utf-8", errors="replace")
            if "aptxadaptiver2_2_support" not in text:
                kb = text.splitlines()
                out = []
                inserted = False
                for line in kb:
                    if line.strip().startswith("# end of file") and not inserted:
                        out.append("persist.vendor.qcom.bluetooth.aptxadaptiver2_2_support=true")
                        inserted = True
                    out.append(line)
                if not inserted:
                    out.append("persist.vendor.qcom.bluetooth.aptxadaptiver2_2_support=true")
                cand.write_text("\n".join(out) + "\n", encoding="utf-8")
                ok("aptX Lossless enabled")

    info("build.prop entries touched: {}".format(touched))
    _bump("ok")
    return True


# ---------------------------------------------------------------- Step: 去签名校验
def _find_java():
    for cand in (TOOLS / "jre" / "bin" / "java.exe", TOOLS / "jre" / "bin" / "java"):
        if cand.exists():
            return str(cand)
    found = shutil.which("java")
    return found if found else ""


def step_disable_signature_verification(device, cfg, dry=False):
    title("SHALLOW 5/7  去除 Android 签名校验 (services.jar)")
    if not get_bool(cfg, "disable_signature_verification", False):
        info("disable_signature_verification=false, skipped (需要 java + apktool)")
        _bump("skip")
        return True

    java = _find_java()
    apktool = TOOLS / "apktool.jar"
    if not java or not apktool.is_file():
        warn("java/apktool.jar missing, skipped")
        _bump("skip")
        return True

    jar = find_file_in_part("system", "framework/services.jar")
    if jar is None:
        warn("services.jar not found in system partition")
        _bump("skip")
        return True
    if dry:
        info("[dry] would patch {}".format(jar))
        _bump("ok")
        return True

    work = WORKSPACE / "shallow_apk"
    if work.exists():
        safe_rm(work, WORKSPACE)
    work.mkdir(parents=True, exist_ok=True)
    target = work / "services.apk"
    shutil.copy2(jar, target)

    if subprocess.run([java, "-jar", str(apktool), "d", "-q", "-f", str(target)],
                      cwd=str(work)).returncode != 0:
        err("apktool decode failed")
        _bump("fail")
        return False

    sym = "getMinimumSignatureSchemeVersionForTargetSdk"
    decoded = work / "services"
    patched = 0
    roots = [decoded / "smali_classes2" / "com" / "android" / "server" / "pm",
             decoded / "smali_classes2" / "com" / "android" / "server" / "pm" / "pkg" / "parsing"]
    for root in roots:
        if not root.is_dir():
            continue
        for f in root.glob("*.smali"):
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            idx = [i for i, l in enumerate(lines) if sym in l]
            if not idx:
                continue
            i = idx[0]
            end = next((j for j in range(i, len(lines)) if "move-result" in lines[j]), None)
            if end is None:
                continue
            m = re.search(r"move-result\s+(\w+)\s+(v\d+)", lines[end])
            reg = m.group(2) if m else "v0"
            lines[i:end + 1] = ["    const/4 {}, 0x0".format(reg)]
            f.write_text("\n".join(lines) + "\n", encoding="utf-8")
            patched += 1

    if not patched:
        warn("no signature scheme method found, nothing patched")
        _bump("skip")
        return True

    if subprocess.run([java, "-jar", str(apktool), "b", "-q", "-f", "-c", str(decoded),
                       "-o", str(work / "services.jar")]).returncode != 0:
        err("apktool build failed")
        _bump("fail")
        return False
    built = work / "services.jar"
    if built.is_file():
        shutil.copy2(built, jar)
        ok("services.jar patched ({} method(s))".format(patched))
    safe_rm(work, WORKSPACE)
    _bump("ok")
    return True


# ---------------------------------------------------------------- Step: 图标资源
def step_perfect_icons(device, cfg, dry=False):
    title("SHALLOW 6/7  图标资源替换")
    if not get_bool(cfg, "perfect_icons", False):
        info("perfect_icons=false, skipped (需要联网 clone 图标仓库)")
        _bump("skip")
        return True

    icons_root = find_subdir("product", "media/theme")
    if icons_root is None:
        warn("product media/theme not found")
        _bump("skip")
        return True
    if dry:
        info("[dry] would rebuild icon assets from the configured source")
        _bump("ok")
        return True

    clone_dir = ROOT / ".shallow_icons"
    if clone_dir.exists():
        safe_rm(clone_dir, ROOT)
    rc = subprocess.run(["git", "clone", "--depth=1",
                         "https://github.com/pzcn/Perfect-Icons-Completion-Project.git",
                         str(clone_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if rc.returncode != 0 or not clone_dir.is_dir():
        warn("icon repo clone failed, skipped")
        _bump("skip")
        return True

    default_dir = icons_root / "default"
    default_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(default_dir / "icons.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in (clone_dir / "icons").rglob("*"):
            if f.is_file():
                z.write(f, "res/drawable-xxhdpi/" + f.relative_to(clone_dir / "icons").as_posix())
    with zipfile.ZipFile(default_dir / "dynamicicons.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for sub in ("Hyper", "common"):
            src = clone_dir / "themes" / sub / "layer_animating_icons"
            if src.is_dir():
                for f in src.rglob("*"):
                    if f.is_file():
                        z.write(f, f.relative_to(src).as_posix())
    safe_rm(clone_dir, ROOT)
    ok("icons rebuilt")
    _bump("ok")
    return True


# ---------------------------------------------------------------- Step: 固件层
def step_firmware_extras(device, cfg, dry=False):
    title("SHALLOW 7/7  固件层处理 (KernelSU / recovery / 刷机脚本)")
    files_dir = DEVICES_DIR / device
    did = False

    # 固件目录候选（完整包结构才存在）
    fw = None
    for cand in (WORKSPACE / "target_rom" / "firmware-update",
                 WORKSPACE / "target_payload" / "firmware-update",
                 WORKSPACE / "combined" / "firmware-update"):
        if cand.is_dir():
            fw = cand
            break

    # KernelSU：需要 Linux ksud + init_boot.img
    if get_bool(cfg, "kernelsu", False):
        ksud = LINUX_TOOLS / "ksud"
        if os.name == "nt" or not ksud.exists():
            warn("kernelsu requires linux tools/ksud, skipped")
        elif fw is None or not (fw / "init_boot.img").is_file():
            warn("init_boot.img not found, KernelSU skipped")
        else:
            if dry:
                info("[dry] would patch init_boot.img with KernelSU")
            else:
                rc = subprocess.run([str(ksud), "boot-patch", "-b", str(fw / "init_boot.img"),
                                     "--magiskboot", str(LINUX_TOOLS / "magiskboot"),
                                     "--kmi", "android14-6.1"], cwd=str(fw)).returncode
                if rc == 0:
                    ok("KernelSU init_boot patched")
                else:
                    err("ksud boot-patch failed")
                    _bump("fail")
                    return False
            did = True

    # TWRP recovery
    if get_bool(cfg, "twrp_recovery", False) and files_dir.is_dir():
        if (files_dir / "recovery.zip").is_file():
            if fw is None:
                warn("no firmware-update dir, recovery skipped")
            elif dry:
                info("[dry] would install recovery")
            else:
                _unzip_into(files_dir / "recovery.zip", fw, overwrite=False)
                ok("recovery installed")
            did = True

    # 刷机脚本
    if get_bool(cfg, "flash_tools", False) and (TOOLS / "flashtools.zip").is_file():
        dest = WORKSPACE / "combined"
        dest.mkdir(parents=True, exist_ok=True)
        if not dry:
            _unzip_into(TOOLS / "flashtools.zip", dest, overwrite=False)
            ok("flash tools extracted -> {}".format(dest))
        did = True

    # init.qcom.rc 崩溃处理
    if get_bool(cfg, "fix_init_rc", True):
        rc_file = find_file_in_part("vendor", "etc/init/hw/init.qcom.rc", TGT_FS)
        if rc_file and rc_file.is_file():
            if not dry:
                lines = rc_file.read_text(encoding="utf-8", errors="replace").splitlines()
                kept = [l for l in lines if l.strip() != "start qti-testscripts"]
                if len(kept) != len(lines):
                    rc_file.write_text("\n".join(kept) + "\n", encoding="utf-8")
                    ok("removed 'start qti-testscripts' from init.qcom.rc")
            did = True

    if not did:
        info("nothing to do in firmware layer")
    _bump("ok")
    return True


def step_drop_stale_services_oat(device, cfg, dry=False):
    """删除与被改写 services.jar 不匹配的预编译 oat 产物。

    跨大版本移植时 step_disable_signature_verification 会用 apktool 重新编译
    services.jar（classes3.dex 变化），但 system/framework/oat/*/services.odex、
    vdex、art 以及 .fsv_meta 仍指向旧的 dex checksum（且带 fs-verity 元数据）。
    ART 加载 system_server 时会拿到失效的 oat，导致开机动画无限循环、机身不发热、
    pstore 为空、logcat 无 system_server。仅对 system/framework/oat 下 services.jar
    对应的 oat 执行删除；system_ext 的 miui-services / MiuiSystemUI oat 必须保留
    （2026-11-15 实测，见下方说明）。

    即使没有开启 disable_signature_verification 也应执行：源 ROM 的 oat 与目标
    底包 kernel/ART 版本不一致时同样会失效。
    """
    title("SHALLOW 7/7  清理失效的 services oat/vdex/art")
    if not get_bool(cfg, "drop_stale_services_oat", True):
        info("drop_stale_services_oat=false, skipped")
        _bump("skip")
        return True

    targets = []
    # system: services.jar 对应的 oat
    for root in part_roots("system", SRC_FS):
        oat = root / "framework" / "oat"
        if not oat.is_dir():
            continue
        for arch_dir in sorted(oat.iterdir()):
            if not arch_dir.is_dir():
                continue
            for f in sorted(arch_dir.glob("services.*")):
                if f.is_file() or f.is_symlink():
                    targets.append(f)

    # system_ext: 保留 miui-services / MiuiSystemUI 的预编译 oat。
    # 2026-11-15 借鉴同版本 X-MiPort 4.0.29 完美包（真机可开机）与本项目
    # 已验证 V5：两者 system_ext/framework/oat 下均保留 34~38 个
    # miui-services.* oat；而 Actions 全新构建把 system_ext 这批 oat 全删后
    # 出现「反复重启进 RE、format data 后仍卡 HyperOS 二屏」。
    # config.ini 为 disable_signature_verification=false，jar 未被改写，
    # 这些 oat 仍然有效，删除反而破坏了已验证基线。
    info("keep system_ext miui-services/MiuiSystemUI oat (verified-good baseline)")

    targets = sorted(set(targets))

    if dry:
        info("[dry] would remove {} stale services oat file(s)".format(len(targets)))
        for f in targets:
            info("   " + str(f))
        _bump("ok")
        return True

    n = 0
    for f in targets:
        try:
            f.unlink()
            n += 1
            info("remove: {}".format(f))
        except OSError as e:
            warn("cannot remove {}: {}".format(f, e))
    if n:
        ok("dropped {} stale services oat file(s)".format(n))
    else:
        info("no stale services oat found (already clean)")
    _bump("ok")
    return True


# ---------------------------------------------------------------- main
# ---------------------------------------------------------------------------
# 跨版本兼容注入（OS4.0.29 源包 + OS3.0.310 底包）
#
# 2026-11-06 真机日志定位：4.0.29 源包相比 4.0.27 移除了 4 个 VNDK APEX
# (v30..v33) 和 30.0 SELinux mapping。Android 16 vendor 的 HAL/服务在
# Android 17 system 上因此起不来，表现为卡 HyperOS 二屏、机身不热，且
# init 里刷 "Context u:object_r:xxx:s0 is not valid (left unmapped)"。
# 这里把基线中的对应文件补回去，使 4.0.29/310 组合可启动。
# ---------------------------------------------------------------------------
_COMPAT_PARTS = ("system", "system_ext", "product", "mi_ext")
_COMPAT_ARCHIVES = [
    # (压缩包, 目标分区, 解压到分区内相对目录)
    # VNDK APEX 未压缩合计 245MB，压完 39MB，因此随仓库分发、打包时解压。
    ("vndk-apex-30-33.7z", "system_ext", "apex"),
]


def _compat_dest_root(part):
    """分区内容根；system 优先取 system-as-root 的深层目录。"""
    roots = part_roots(part, SRC_FS)
    return roots[-1] if roots else SRC_FS / part


def step_inject_boot_compat(device, cfg, dry=False):
    """把 devices/<device>/compat/ 下的跨版本兼容文件补进源分区。"""
    src = DEVICES_DIR / (device or "") / "compat"
    if not (device and src.is_dir()):
        info("no devices/{}/compat, skip cross-version compat".format(device or "?"))
        _bump("skip")
        return True
    # 必须已经解包过源分区。否则宁可报错，也不要凭空 mkdir 出一个假 workspace
    # 把 245MB APEX 写进去还显示成功（2026-11-06 实测踩过这个坑）。
    if not SRC_FS.is_dir():
        err("workspace/source_filesystem missing, run Step 4/5 (unpack + port) first")
        _bump("fail")
        return False

    added = kept = 0
    for part in _COMPAT_PARTS:
        part_src = src / part
        if not part_src.is_dir():
            continue
        roots = part_roots(part, SRC_FS)
        if not roots:
            err("source_filesystem/{} not found, cannot inject compat".format(part))
            _bump("fail")
            return False
        dest_root = roots[-1]
        part_added = 0
        for f in sorted(part_src.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(part_src)
            dest = dest_root / rel
            if dest.is_file() and dest.stat().st_size == f.stat().st_size:
                kept += 1
                continue
            if dry:
                info("[dry] {} -> {}".format(rel, dest))
                part_added += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(f), str(dest))
            part_added += 1
        added += part_added
        if part_added and not dry:
            print("  {}  {}/: +{} file(s){}".format(G, part, part_added, N))

    for arc_name, part, sub in _COMPAT_ARCHIVES:
        arc = src / arc_name
        if not arc.is_file():
            warn("compat archive missing: {}".format(arc_name))
            _bump("fail")
            return False
        dest_dir = _compat_dest_root(part) / sub
        if dry:
            info("[dry] extract {} -> {}".format(arc_name, dest_dir))
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        sevenzip = TOOLS / "7z.exe"
        if not sevenzip.is_file():
            sevenzip = shutil.which("7z")
        if not sevenzip:
            err("7z not found, cannot extract {}".format(arc_name))
            _bump("fail")
            return False
        before = {p.name for p in dest_dir.glob("*.apex")}
        rc = subprocess.run([str(sevenzip), "x", str(arc), "-o" + str(dest_dir), "-y"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        after = {p.name for p in dest_dir.glob("*.apex")}
        if rc != 0 or not after:
            err("failed to extract {} (rc={})".format(arc_name, rc))
            _bump("fail")
            return False
        new = after - before
        info("VNDK APEX ready in {}: {}{}".format(
            part, ", ".join(sorted(after)),
            " ({} new)".format(len(new)) if new else " (already present)"))

    ok("cross-version compat: +{} added, {} already present".format(added, kept))
    _bump("ok")
    return True


def step_align_perfect_product(device, cfg, dry=False):
    """按 X-MiPort 同版本完美包裁剪 product 中的 madrid 专属资源。

    2026-11-15 三方对比：完美包与 V5 的 product 均不含 media/rearscreen
    及多余的 precust_theme / subscreencenter / overlay 条目；而 shallow 全新
    构建带入了 353 项两个可开机包都没有的内容，真机反复重启进 RE、卡二屏。
    清单 devices/<device>/perfect_remove_paths.txt 由三方清单求差生成，
    只删「NEW 独有且 PERFECT/V5 都不含」的文件，目录保留（空目录无害）。
    """
    title("SHALLOW 8/8  对齐完美包 product 内容")
    if not get_bool(cfg, "align_perfect_product", True):
        info("align_perfect_product=false, skipped")
        _bump("skip")
        return True
    if not device:
        info("device unknown, skipped")
        _bump("skip")
        return True
    denylist = DEVICES_DIR / device / "perfect_remove_paths.txt"
    if not denylist.is_file():
        info("no {} found, skipped".format(denylist.name))
        _bump("skip")
        return True
    roots = part_roots("product", SRC_FS)
    if not roots:
        warn("product tree not found, skipped")
        _bump("skip")
        return True
    removed = 0
    for line in denylist.read_text(encoding="utf-8", errors="replace").splitlines():
        rel = line.strip()
        if not rel or rel.startswith("#"):
            continue
        for root in roots:
            target = root / rel
            if target.is_file():
                if dry:
                    info("[dry] would remove: {}".format(rel))
                else:
                    try:
                        target.unlink()
                    except OSError as e:
                        warn("cannot remove {}: {}".format(rel, e))
                        break
                removed += 1
                break
    ok("perfect-baseline product trim: {} file(s) {}".format(
        removed, "would be removed" if dry else "removed"))

    # ---- 补入：完美包有、源包没有的底包原生文件 ----
    # 只删不补会让产物缺 WMService / UPTsmService / pangu NQNfcNci 等
    # shennong 原生服务与 system_ext 的 miui-services oat，真机卡 HyperOS
    # 二屏（2026-11-21 定性）。清单第一段是分区名，其余为分区内相对路径。
    addlist = DEVICES_DIR / device / "perfect_add_paths.txt"
    added = 0
    absent = []
    if addlist.is_file():
        entries = []
        for line in addlist.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "/" not in s:
                continue
            part, rel = s.split("/", 1)
            if part not in ("system", "system_ext", "product", "mi_ext",
                            "odm", "vendor", "vendor_dlkm", "system_dlkm"):
                warn("unknown partition in add list: " + s)
                continue
            entries.append((part, rel))
        for part in sorted({p for p, _ in entries}):
            src_roots = part_roots(part, TGT_FS)
            dst_roots = part_roots(part, SRC_FS)
            if not src_roots or not dst_roots:
                warn("跳过补入 {}: 底包或源包内容树缺失".format(part))
                continue
            src_root, dst_root = src_roots[-1], dst_roots[-1]
            for _, rel in [e for e in entries if e[0] == part]:
                src = src_root / rel
                if not src.is_file():
                    absent.append("{}/{}".format(part, rel))
                    continue
                dst = dst_root / rel
                if dst.exists():
                    continue
                if dry:
                    info("[dry] would add: {}/{}".format(part, rel))
                else:
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                    except OSError as e:
                        warn("cannot add {}/{}: {}".format(part, rel, e))
                        continue
                added += 1
        ok("perfect-baseline add: {} file(s) {}".format(
            added, "would be added" if dry else "added"))
        if absent:
            warn("底包缺少 {} 个补入文件".format(len(absent)))
            for rel in absent[:10]:
                warn("   missing in base: " + rel)
            if len(absent) > 10:
                warn("   ... 其余 {} 条省略".format(len(absent) - 10))
    else:
        info("no {} found, skip add".format(addlist.name))

    _bump("ok")
    return True


STEPS = [
    ("inject_boot_compat", step_inject_boot_compat),
    ("remove_apps", step_remove_apps),
    ("inject_device_files", step_inject_device_files),
    ("replace_camera", step_replace_camera),
    ("patch_product_props", step_patch_product_props),
    ("patch_build_prop", step_patch_build_prop),
    ("disable_signature_verification", step_disable_signature_verification),
    ("perfect_icons", step_perfect_icons),
    ("firmware_extras", step_firmware_extras),
    ("drop_stale_services_oat", step_drop_stale_services_oat),
    ("align_perfect_product", step_align_perfect_product),
]


def run_pipeline(cmd, device, cfg, dry=False):
    started = time.time()
    print()
    print("  {}{}============================================================{}".format(C, BD, N))
    print("  {}{}  SHALLOW stage  (Port -> Slim -> Pack){}".format(C, BD, N))
    print("  {}{}============================================================{}".format(C, BD, N))
    print("  {}  Device: {}{}{}   Mode: {}{}{}".format(N, G, device or "<unknown>", N, G, cmd, N))
    if dry:
        print("  {}  [DRY-RUN] no change will be written{}".format(Y, N))

    selected = STEPS
    if cmd == "compat":
        selected = [s for s in STEPS if s[0] == "inject_boot_compat"]
    elif cmd == "apps":
        selected = [s for s in STEPS if s[0] == "remove_apps"]
    elif cmd == "files":
        selected = [s for s in STEPS if s[0] in ("inject_device_files", "replace_camera")]
    elif cmd == "prop":
        selected = [s for s in STEPS if s[0] == "patch_build_prop"]
    elif cmd == "sigv":
        selected = [s for s in STEPS if s[0] == "disable_signature_verification"]
    elif cmd == "icons":
        selected = [s for s in STEPS if s[0] == "perfect_icons"]
    elif cmd == "firmware":
        selected = [s for s in STEPS if s[0] == "firmware_extras"]
    elif cmd == "oat":
        selected = [s for s in STEPS if s[0] == "drop_stale_services_oat"]
    elif cmd == "align":
        selected = [s for s in STEPS if s[0] == "align_perfect_product"]

    for name, func in selected:
        gate = {
            "inject_boot_compat": "inject_boot_compat",
            "remove_apps": "remove_apps",
            "inject_device_files": "inject_device_files",
            "replace_camera": "replace_camera",
            "patch_build_prop": "patch_build_prop",
            "disable_signature_verification": "disable_signature_verification",
            "perfect_icons": "perfect_icons",
            "firmware_extras": None,
            "drop_stale_services_oat": None,
            # 新步骤不设 gate：函数内部用 get_bool(..., True) 默认执行，
            # 避免 config_codec / 段位置变化导致整步被 SKIP（2026-11-15 CI 实测）
            "align_perfect_product": None,
        }.get(name)
        if gate and not get_bool(cfg, gate, name in ("remove_apps", "inject_boot_compat")):
            print()
            print("  {}[SKIP]{} {} ({}=false)".format(D, N, name, gate))
            _bump("skip")
            continue
        if not func(device, cfg, dry=dry):
            err("step failed: {}".format(name))
            if get_bool(cfg, "strict", False):
                return 1

    print()
    print("  {}{}============================================================{}".format(C, BD, N))
    print("  {}{}  SHALLOW finished in {:.1f}s{}".format(C, BD, time.time() - started, N))
    print("  {}  ok={} fail={} skip={}{}".format(D, RESULT.get("ok", 0),
                                                 RESULT.get("fail", 0),
                                                 RESULT.get("skip", 0), N))
    print()
    return 1 if RESULT.get("fail", 0) else 0


# 对照 <baseline-images>（OS4.0.27，已验证可开机）的跨版本兼容文件。
# OS4.0.29 源包移除了这批旧兼容组件，但 Android 17 system 跑在 Android 16
# vendor 上时仍需要它们；缺失的典型症状就是开机停在 HyperOS 二屏且不发热，
# dmesg 里伴随 "Context u:object_r:xxx:s0 is not valid (left unmapped)"。
BOOT_COMPAT_FILES = [
    ("system", "etc/selinux/mapping/30.0.cil"),
    ("system", "etc/selinux/mapping/30.0.compat.cil"),
    ("system", "etc/keystube.zip"),
    ("system", "etc/toast.dex"),
    ("system", "bin/bugreporter"),
    ("system_ext", "etc/selinux/mapping/30.0.cil"),
    ("system_ext", "etc/selinux/mapping/30.0.compat.cil"),
    # VNDK v30~v33：4.0.29 只保留 v34，Android 16 vendor 的 HAL 会因此加载失败
    ("system_ext", "apex/com.android.vndk.v30.apex"),
    ("system_ext", "apex/com.android.vndk.v31.apex"),
    ("system_ext", "apex/com.android.vndk.v32.apex"),
    ("system_ext", "apex/com.android.vndk.v33.apex"),
    ("product", "etc/selinux/mapping/30.0.cil"),
    ("mi_ext", "product/etc/permissions/rustruntime_cfg_v3_v5.xml"),
]


def missing_boot_compat():
    """返回缺失的跨版本兼容文件（相对分区内容根），用于出包前体检。"""
    miss = []
    for part, rel in BOOT_COMPAT_FILES:
        if not any((root / rel).exists() for root in part_roots(part, SRC_FS)):
            miss.append("{}/{}".format(part, rel))
    return miss


def do_check(device, cfg):
    print()
    print("  {}{}  SHALLOW health check{}".format(C, BD, N))
    keep_paths = resolve_keep_lists(device)
    checks = [
        ("workspace", WORKSPACE.is_dir()),
        ("source_filesystem", SRC_FS.is_dir()),
        ("target_filesystem", TGT_FS.is_dir()),
        ("device assets (devices/{})".format(device or "?"),
         bool(device) and (DEVICES_DIR / device).is_dir()),
        ("device profile (port.conf)",
         bool(device) and (DEVICES_DIR / device / PORT_CONF_NAME).is_file()),
        ("apps list", resolve_apps_list(device, cfg) is not None),
        ("keep list ({})".format(len(keep_paths)), bool(keep_paths)),
        ("boot-compat files (4.0.27 baseline)", not missing_boot_compat()),
        ("java for smali patch", bool(_find_java())),
        ("linux toolchain", LINUX_TOOLS.is_dir()),
    ]
    for name, good in checks:
        tag = "{}[OK]{}".format(G, N) if good else "{}[--]{}".format(Y, N)
        print("  {}  {:<40}".format(tag, name))

    prof = read_device_profile(device)
    if prof:
        print()
        print("  {}{}  device profile: {}{}".format(
            C, BD, prof.get("name") or (device or "?"), N))
        for label, key in (("codename", "codename"), ("SoC", "soc"),
                           ("display", "display"),
                           ("vendor Android", "vendor_android"),
                           ("super size", "super_size"),
                           ("density", "density")):
            val = prof.get(key, "")
            if val:
                print("  {}  {:<14} {}{}{}".format(W, label, G, val, N))

    miss = missing_boot_compat()
    if miss:
        print()
        print("  {}{}  WARNING: {} cross-version compat file(s) missing{}".format(
            Y, BD, len(miss), N))
        print("  Baseline = OS4.0.27 verified on shennong.")
        for m in miss:
            print("    - " + m)
        print("  OS4.0.29 source ROM drops these; the flash may hang at the")
        print("  HyperOS logo. Use the 4.0.27 source ROM, or ship these files")
        print("  in devices/<device>/compat/ and inject them before packing.")
    print()
    return 0


def main():
    cfg = read_shallow_config()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--device" in sys.argv:
        i = sys.argv.index("--device")
        if i + 1 < len(sys.argv):
            os.environ["SHALLOW_DEVICE"] = sys.argv[i + 1]
    device = os.environ.get("SHALLOW_DEVICE", "") or read_target_device()
    cmd = args[0].lower() if args else "slim"

    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if cmd not in ("slim", "apps", "files", "prop", "sigv", "icons", "firmware", "oat", "compat", "check"):
        err("unknown command: {}".format(cmd))
        print("supported: slim | apps | files | prop | sigv | icons | firmware | oat | compat | check")
        return 2

    if not get_bool(cfg, "enable", True) and cmd != "check":
        info("[shallow] enable=false, nothing to do")
        return 0

    if cmd == "check" or os.environ.get("SHALLOW_DRY") == "1":
        rc = do_check(device, cfg)
        if cmd == "check":
            return rc

    return run_pipeline(cmd, device, cfg, dry=False)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
