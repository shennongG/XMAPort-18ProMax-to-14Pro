#!/usr/bin/env python3
# -*- coding: utf-8 -*-


import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path



R = "\x1b[91m"
G = "\x1b[92m"
Y = "\x1b[93m"
B = "\x1b[94m"
M = "\x1b[95m"
C = "\x1b[96m"
W = "\x1b[97m"
D = "\x1b[90m"
N = "\x1b[0m"
BD = "\x1b[1m"

# ---------------- 控制常量 ----------------
DEBUG_MODE = "1"
TOOL_TIMEOUT = 10 * 60

# auto 模式标志（--auto CLI），控制是否静默外部工具实时进度输出
AUTO_MODE = False

# 用完即删（config [settings] auto_clean）：下载包 / payload / 解出的镜像在各阶段
# 用完后删除，避免大 ROM 在磁盘有限的 CI runner 上撑爆磁盘（WinError 112）。
AUTO_CLEAN = True


# ---------------- 控制台准备 ----------------
def init_console(auto=False):
    global AUTO_MODE
    AUTO_MODE = auto
    # 启用 ANSI 虚拟终端
    os.system("")
    # 设置控制台窗口标题（auto 模式下跳过）
    if not auto:
        os.system("title XMAPort 260824.Beta")
    # 防止非 UTF-8 终端下中文输出崩溃
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # 子进程脚本同样用 UTF-8 输出，避免 CI cp1252 下中文崩溃
    os.environ["PYTHONIOENCODING"] = "utf-8"


# ---------------- 路径（脚本所在目录为根） ----------------
ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
WORKSPACE = ROOT / "workspace"
SHALLOW_DIR = ROOT / "shallow"
DEVICES_DIR = ROOT / "devices"


PY = sys.executable
if not PY:
    PY = "python"

ARIA2 = TOOLS / "aria2c.exe"
SZ = TOOLS / "7z.exe"
PDUMP = TOOLS / "payload-dumper-go.exe"
S2I = TOOLS / "simg2img.exe"
I2S = TOOLS / "img2simg.exe"
LPU = TOOLS / "lpunpack.exe"
LPM = TOOLS / "lpmake.exe"
LPD = TOOLS / "lpdumps.exe"
HLP = TOOLS / "img_helper.py"

CONFIG = ROOT / "config.ini"


def read_config_text(path):
    """读取 config.ini，兼容 UTF-8(GitHub 网页编辑保存) 与 GBK/ANSI(本地记事本保存)。

    旧代码固定用 gbk + errors="ignore"：遇 UTF-8 中文注释会吞掉字节，虽然 ASCII
    配置项仍能解析，但任何按 GBK 写回配置的操作都会把整份文件搞成乱码。
    """
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", errors="replace")


SRC_DL = WORKSPACE / "download_source"
TGT_DL = WORKSPACE / "download_target"
SRC_ROM = WORKSPACE / "source_rom"
TGT_ROM = WORKSPACE / "target_rom"
SRC_UNPACK = WORKSPACE / "source_payload"
TGT_UNPACK = WORKSPACE / "target_payload"
OUT_DIR = WORKSPACE / "output"
SRC_FS = WORKSPACE / "source_filesystem"
TGT_FS = WORKSPACE / "target_filesystem"
PACK_OUT = WORKSPACE / "packed"

ALL_DIRS = [
    WORKSPACE, SRC_DL, TGT_DL, SRC_ROM, TGT_ROM, SRC_UNPACK,
    TGT_UNPACK, OUT_DIR, SRC_FS, TGT_FS, PACK_OUT,
]

UNPACK_PARTS = ["system", "system_ext", "product", "odm", "mi_ext", "vendor"]
PACK_SOURCE_PARTS = ["system", "system_ext", "product"]
ALL_OUTPUT_PARTS = ["system", "system_ext", "product", "odm", "mi_ext", "vendor", "vendor_dlkm", "system_dlkm"]

# ---------------- 日志文件（日期-小时.log） ----------------
_now = datetime.now()
LOG_FILE = WORKSPACE / "{}-{}.log".format(_now.strftime("%Y-%m-%d"), _now.hour)


def log_write(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(datetime.now().strftime("%H:%M:%S"), msg))
    except Exception:
        pass


# ---------------- 输出辅助（INFO 仅在 DEBUG_MODE=1 时显示） ----------------
def info(msg):
    if DEBUG_MODE == "1":
        print("  {}[INFO]{}   {}".format(G, N, msg), flush=True)


def err(msg):
    print("  {}[ERROR]{} {}".format(R, N, msg), flush=True)


def warn(msg):
    print("  {}[WARN]{}  {}".format(Y, N, msg), flush=True)


def ok(msg):
    print("  {}[OK]{}    {}".format(G, N, msg), flush=True)


def prompt(text):
    # EOF 时优雅退出而非抛出 Traceback
    sys.stdout.write(text)
    sys.stdout.flush()
    try:
        return input()
    except EOFError:
        print()
        sys.exit(0)


def pause():
    # 等待任意键（非交互环境自动跳过）
    if sys.stdin and sys.stdin.isatty():
        sys.stdout.write("Please press any key to continue . . . ")
        sys.stdout.flush()
        try:
            import msvcrt
            msvcrt.getch()
            print(flush=True)
        except Exception:
            input()
    else:
        # 非交互环境自动跳过
        pass


def pause_seconds(seconds):
    time.sleep(seconds)


DEVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def is_safe_device_name(name):
    return bool(DEVICE_NAME_RE.fullmatch(name.strip()))


def scaled_timeout(size_bytes, base=None, per_gb_min=3, cap_min=180):
    """按数据量放大工具超时：base 分钟起步，每 GB 追加 per_gb_min 分钟，封顶 cap_min 分钟。
    大包（如 12GB 的旗舰 ROM）在 CI 慢盘上 10 分钟默认值远远不够。"""
    if base is None:
        base = TOOL_TIMEOUT
    try:
        gb = float(size_bytes) / (1024 ** 3)
    except Exception:
        gb = 0
    minutes = base // 60 + int(gb * per_gb_min) + 1
    minutes = max(base // 60, min(minutes, cap_min))
    return minutes * 60


# ---------------- 磁盘空间管理 ----------------
def disk_free_gb():
    """ROOT 所在盘剩余空间（GB），失败返回 -1"""
    try:
        return shutil.disk_usage(str(ROOT)).free / (1024 ** 3)
    except Exception:
        return -1.0


def disk_report(tag):
    free = disk_free_gb()
    if free >= 0:
        info("Disk free [{}]: {:.1f} GB".format(tag, free))
        log_write("Disk free [{}]: {:.1f} GB".format(tag, free))
    return free


def _clean_files(label, paths):
    """删除给定文件集合，打印释放空间。仅 AUTO_CLEAN 开启时生效。"""
    if not AUTO_CLEAN:
        return
    freed = 0
    removed = 0
    for p in paths:
        p = Path(p)
        try:
            if p.is_file():
                freed += p.stat().st_size
                p.unlink()
                removed += 1
            elif p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        freed += f.stat().st_size
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
        except Exception as e:
            warn("clean failed {}: {}".format(p, e))
    if freed:
        ok("Cleaned {}: freed {:.1f} GB ({} item(s))".format(
            label, freed / (1024 ** 3), removed))
        log_write("Cleaned {}: freed {} bytes ({} items)".format(label, freed, removed))


def clean_download_archives():
    """Step 2 解压成功后删除下载的 zip 卡刷包（解压产物已在 *_rom 目录）"""
    _clean_files("download archives",
                 list(SRC_DL.glob("*.zip")) + list(TGT_DL.glob("*.zip")))


def clean_payload_bins():
    """Step 3 解包成功后删除 payload.bin / *.dat 等中间包（解出的 *.img 已在 *_payload）"""
    pats = []
    for d in (SRC_ROM, TGT_ROM):
        for pat in ("payload.bin", "*.new.dat", "*.new.dat.br", "*.new.dat.xz",
                    "*.transfer.list", "*.patch.dat", "care_map*.txt"):
            pats.extend(d.rglob(pat))
    _clean_files("payload blobs", pats)


def clean_unpacked_imgs():
    """Step 7 打包完成后删除 *_payload 下的分区镜像（已复制/重打包进 packed）"""
    _clean_files("unpacked partition images",
                 list(SRC_UNPACK.glob("*.img")) + list(TGT_UNPACK.glob("*.img")))


# super.img 已合并的动态分区 —— 打包完成后单个镜像不再单独需要
# (一体包也只收固件类镜像, 动态分区一律走 super)
DYNAMIC_PART_NAMES = {
    "system", "system_ext", "product", "odm", "vendor", "vendor_dlkm",
    "odm_dlkm", "system_dlkm", "mi_ext", "mi_product",
}


def clean_pre_flashpack_imgs():
    """Step 7.5 一体包构建前的空间回收（上次 CI 在 simg2img 时磁盘爆满）。

    super.img 创建成功后:
      - packed 下的动态分区 *.img 已全部合并进 super, 删除（保留 vbmeta*/super.img）
      - source_payload 的 *.img 一体包不用, 全删
      - target_payload 只保留固件类镜像（boot/abl/xbl/modem...）, 动态分区删除
    super.img 不存在时(打包失败)不动任何文件, 保留现场排查。
    """
    if not (PACK_OUT / "super.img").is_file():
        return
    victims = []
    victims.extend((PACK_OUT / (p + ".img") for p in DYNAMIC_PART_NAMES))
    victims.extend(SRC_UNPACK.glob("*.img"))
    victims.extend(TGT_UNPACK / (p + ".img") for p in DYNAMIC_PART_NAMES)
    _clean_files("pre-flashpack redundant partition images", victims)


def check_disk_space(auto, min_gb=45):
    """Step 0 后预检磁盘空间。返回 True=继续，False=用户主动放弃。"""
    free = disk_free_gb()
    if free < 0:
        return True
    if free < min_gb:
        warn("磁盘剩余空间仅 {:.1f} GB，12GB 级 ROM 全流程约需 60GB，可能中途爆满(WinError 112)".format(free))
        log_write("WARNING: low disk space {:.1f} GB (< {} GB)".format(free, min_gb))
        if auto:
            return True
        try:
            ans = prompt("  空间不足，仍要继续吗？[y/N]: ").strip().lower()
        except Exception:
            ans = ""
        return ans in ("y", "yes")
    info("Disk space OK: {:.1f} GB free".format(free))
    return True


def run_tool(cmd, timeout=None, **kwargs):
    # 外部工具超时终止，避免流程永久挂起；timeout 可按数据量放大（见 scaled_timeout）。
    label = Path(cmd[0]).name if cmd else "external tool"
    limit = timeout if timeout else TOOL_TIMEOUT
    try:
        return subprocess.run(cmd, timeout=limit, **kwargs)
    except FileNotFoundError:
        err("{} not found (tool missing or blocked by antivirus)".format(label))
        log_write("ERROR: tool not found: {}".format(cmd[0]))
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr="")
    except subprocess.TimeoutExpired:
        err("{} timed out after {} minutes".format(label, limit // 60))
        log_write("ERROR: {} timed out after {} seconds".format(label, limit))
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")


# ---------------- 全局状态 ----------------
TARGET_DEVICE = ""
SRC_URL = ""
TGT_URL = ""
# 下载设置默认值
THREADS = 16
MAX_CONN = 16
TIMEOUT = 300
RETRY = 5
RETRY_WAIT = 3

# ROM 兼容性预检（Step 0）
API_CHECK = True        # 下载前用官方包名核对源/底包 Android 大版本
ALLOW_API_MISMATCH = True  # 跨大版本时在 auto 模式放行（仍打印告警）

# ---------------- CLI 覆盖参数（由 argparse 写入） ----------------
CLI_SOURCE_URL = ""
CLI_TARGET_URL = ""
CLI_DEVICE = ""

# Step 6 精简阶段开关（CLI --skip-shallow 可关闭）
SHALLOW_ENABLED = True


# ---------------- [A] 工具状态检查 ----------------
def check_tool(name, src):
    p = TOOLS / name
    try:
        ok = p.exists() and p.stat().st_size > 0
    except Exception:
        ok = False
    tag = "{}[OK]{}".format(G, N) if ok else "{}[N/A]{}".format(R, N)
    print("  {}{}  {}  {}{}{}".format(W, name, tag, D, src, N))


def tool_status(pause_after=True):
    os.system("cls" if os.name == "nt" else "clear")
    print()
    print("  {}+----------------------------------------------------------+{}".format(C, N))
    print("  {}|  Tools Checking, please wait for 5 seconds                |{}".format(C, N))
    print("  {}+----------------------------------------------------------+{}".format(C, N))
    print()
    print("  {}{:<20} {}".format(W, "Tool", "Component" + N))
    print("  " + D + "----------------------------------------------------------" + N)
    check_tool("aria2c.exe", "multi-threaded downloader")
    check_tool("7z.exe", "archive extractor")
    check_tool("payload-dumper-go.exe", "OTA payload extractor")
    check_tool("simg2img.exe", "sparse image converter")
    check_tool("img2simg.exe", "sparse image converter")
    check_tool("lpunpack.exe", "dynamic partition tool")
    check_tool("lpmake.exe", "dynamic partition tool")
    check_tool("lpdumps.exe", "dynamic partition tool")
    check_tool("img_helper.py", "bundled script")
    check_tool("pack_partitions.py", "bundled script")
    print("  " + D + "----------------------------------------------------------" + N)
    if pause_after:
        pause()


# ---------------- [C] 随附组件 ----------------
def show_credits_entry(no, name, desc, lic):
    print("  {}  {}. {}{}".format(G, no, name, N))
    print("  {}     {}{}".format(W, desc, N))
    print("  {}     license: {}{}".format(D, lic, N))
    print()


def show_credits():
    os.system("cls" if os.name == "nt" else "clear")
    print()
    print("  {}{}============================================================{}".format(C, BD, N))
    print("  {}{}  Bundled components{}".format(C, BD, N))
    print("  {}{}============================================================{}".format(C, BD, N))
    print()
    show_credits_entry(1, "aria2c", "Multi-threaded downloader", "GPL v2")
    show_credits_entry(2, "7-Zip (7z.exe)", "Archive extractor", "GNU LGPL")
    show_credits_entry(3, "payload-dumper-go", "Android OTA payload.bin extractor", "MIT")
    show_credits_entry(4, "AOSP partition tools", "lpunpack, lpmake, lpdumps", "Apache 2.0")
    show_credits_entry(5, "erofs-utils", "EROFS image creation and extraction", "GPL v2")
    show_credits_entry(6, "e2fsprogs", "ext4 tools: mke2fs, e2fsdroid", "GPL v2 / LGPL v2")
    show_credits_entry(7, "Google Brotli", "Block OTA Brotli decompression", "MIT")
    show_credits_entry(8, "Magisk tools", "init_boot / vbmeta handling", "GPL v3")
    print("  " + D + "----------------------------------------------------------" + N)
    pause()


# ---------------- [D] 清理 workspace ----------------
def clean_workspace():
    global TARGET_DEVICE
    os.system("cls" if os.name == "nt" else "clear")
    print()
    print("  {}  This will delete all extracted .img and payload.bin files.{}".format(Y, N))
    print("  {}  Including:{}".format(Y, N))
    for path in [
        str(SRC_UNPACK / "*.img"), str(TGT_UNPACK / "*.img"),
        str(SRC_ROM / "payload.bin"), str(TGT_ROM / "payload.bin"),
        str(WORKSPACE / "config.txt"),
    ]:
        print("  {}    - {}{}".format(D, path, N))
    print()
    answer = prompt("  {}Are you sure? (Y/N): {}".format(R, N))
    if answer.strip().lower() != "y":
        return
    info("Cleaning workspace...")
    for pattern in [SRC_UNPACK / "*.img", TGT_UNPACK / "*.img"]:
        for f in pattern.parent.glob(pattern.name):
            try:
                f.unlink()
            except Exception:
                pass
    for f in [SRC_ROM / "payload.bin", TGT_ROM / "payload.bin", WORKSPACE / "config.txt"]:
        try:
            if f.exists():
                f.unlink()
        except Exception:
            pass
    info("Workspace cleaned.")
    pause_seconds(2)


# ---------------- 配置读取 ----------------
def read_config():
    global SRC_URL, TGT_URL, THREADS, MAX_CONN, TIMEOUT, RETRY, TOOL_TIMEOUT, AUTO_CLEAN
    global API_CHECK, ALLOW_API_MISMATCH
    if not CONFIG.exists():
        info("config.ini not found, will create template")
        create_config()
        print("  {}  [!] Edit config.ini first{}".format(Y, N))
        pause()
        raise ReturnToMenu()

    info("Reading config.ini...")
    in_source = False
    in_target = False
    for raw in read_config_text(CONFIG).splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            key = key.strip()
        else:
            key, val = line, ""
        if line == "[source]":
            in_source, in_target = True, False
        elif line == "[target]":
            in_target, in_source = True, False
        elif line == "[settings]":
            in_source, in_target = False, False
        elif line == "[packing]":
            in_source, in_target = False, False
        elif key == "url":
            if in_source:
                SRC_URL = val.strip()
            elif in_target:
                TGT_URL = val.strip()
        elif key == "threads":
            THREADS = int(val.strip())
        elif key == "max-connection":
            MAX_CONN = int(val.strip())
        elif key == "timeout":
            TIMEOUT = int(val.strip())
        elif key == "retry":
            RETRY = int(val.strip())
        elif key == "tool_timeout":
            # 外部工具（payload-dumper-go / lpmake 等）基础超时，单位：分钟
            TOOL_TIMEOUT = max(1, int(val.strip())) * 60
        elif key == "auto_clean":
            # 用完即删：下载包 / payload / 解出镜像在各阶段用完后删除，防止磁盘爆满
            AUTO_CLEAN = val.strip().lower() in ("true", "1", "yes", "y", "on")
        elif key == "api_check":
            API_CHECK = val.strip().lower() in ("true", "1", "yes", "y", "on")
        elif key == "allow_api_mismatch":
            ALLOW_API_MISMATCH = val.strip().lower() in ("true", "1", "yes", "y", "on")
    info("Config loaded. SRC_URL=[{}]".format(SRC_URL))
    info("Config loaded. TGT_URL=[{}]".format(TGT_URL))
    # CLI 覆盖（--source / --target 优先于 config.ini）
    if CLI_SOURCE_URL:
        SRC_URL = CLI_SOURCE_URL
        info("SRC_URL overridden by --source")
        log_write("SRC_URL overridden by --source CLI arg")
    if CLI_TARGET_URL:
        TGT_URL = CLI_TARGET_URL
        info("TGT_URL overridden by --target")
        log_write("TGT_URL overridden by --target CLI arg")


def detect_legacy_erofs_marker():
    # 检测源 ROM 是否为 V13 DEV 版本，若是则全局启用老版 erofs 工具
    bp = SRC_FS / "product" / "etc" / "build.prop"
    if not bp.exists():
        return "false"
    try:
        text = bp.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
        for line in text.splitlines():
            if line.startswith("ro.product.build.version.incremental="):
                val = line.split("=", 1)[1].strip()
                if "V13" in val and "DEV" in val:
                    info("Legacy erofs marker detected: {}".format(line))
                    return "true"
    except Exception:
        pass
    return "false"


def read_shallow_section():
    """读取 config.ini 的 [shallow] 段（合并自 早期精简脚本 的精简开关）"""
    keys = ["enable", "strict", "remove_apps", "inject_device_files", "replace_camera",
            "apply_fixes", "patch_build_prop", "density", "aptx_lossless", "build_date",
            "disable_signature_verification", "perfect_icons", "kernelsu",
            "twrp_recovery", "flash_tools", "fix_init_rc", "apps_list"]
    cfg = {k: "" for k in keys}
    cfg["enable"] = "true"
    if not CONFIG.exists():
        return cfg
    in_shallow = False
    for raw in read_config_text(CONFIG).splitlines():
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


def slim_cfg_strict(cfg):
    """[shallow] strict=true 时，精简失败中断整个移植流程"""
    return str(cfg.get("strict", "false")).strip().lower() in ("true", "1", "yes", "y", "on")


def read_saved_device():
    """从 workspace/config.txt 读取上次使用的机型代号"""
    cfg_txt = WORKSPACE / "config.txt"
    if cfg_txt.exists():
        try:
            for line in cfg_txt.read_text(encoding="gbk", errors="ignore").splitlines():
                if line.startswith("TARGET_DEVICE="):
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return TARGET_DEVICE


def slim_menu():
    """[S] 单独运行精简阶段（适合改完清单后重复验证）"""
    slim = TOOLS / "shallow_slim.py"
    if not slim.exists():
        err("tools/shallow_slim.py missing")
        pause()
        return

    commands = {
        "1": "slim", "2": "apps", "3": "files", "4": "prop",
        "5": "sigv", "6": "icons", "7": "firmware", "8": "check",
    }
    device = read_saved_device()
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print()
        print("  {}{}============================================================{}".format(C, BD, N))
        print("  {}{}  Shallow / Slim stage  (Port -> Slim -> Pack){}".format(C, BD, N))
        print("  {}{}============================================================{}".format(C, BD, N))
        print("  {}  Device: {}{}{}".format(W, G, device or "<unknown>", N))
        print("  {}  Assets: {}{}{}".format(W, D, DEVICES_DIR / (device or "<device>"), N))
        print()
        print("  {}  [1] Full slim{}              {}所有已开启的精简项{}".format(W, N, D, N))
        print("  {}  [2] Remove apps only{}       {}按清单删除应用{}".format(W, N, D, N))
        print("  {}  [3] Device files + camera{}".format(W, N))
        print("  {}  [4] Patch build.prop{}".format(W, N))
        print("  {}  [5] Disable signature check{}  {}需 java + apktool{}".format(W, N, D, N))
        print("  {}  [6] Perfect icons{}          {}需联网{}".format(W, N, D, N))
        print("  {}  [7] Firmware layer{}         {}KernelSU / recovery{}".format(W, N, D, N))
        print("  {}  [8] Health check{}".format(W, N))
        print("  {}  [0] Back to main menu{}".format(W, N))
        print()
        ch = prompt("  {}{}Select [0-8]: {}".format(Y, BD, N)).strip().lower()
        if ch in ("0", "q", "exit"):
            return
        if ch == "9":
            device = prompt("  {}Enter device codename: {}".format(W, N)).strip()
            if not is_safe_device_name(device):
                err("Invalid device codename")
                pause_seconds(1)
                device = read_saved_device()
            continue
        cmd = commands.get(ch)
        if not cmd:
            print("  {}  Invalid input{}".format(R, N))
            pause_seconds(1)
            continue
        env = os.environ.copy()
        env["SHALLOW_DEVICE"] = device or ""
        env["PYTHONIOENCODING"] = "utf-8"
        rc = run_tool([PY, str(slim), cmd], env=env).returncode
        if rc == 0:
            ok("shallow '{}' finished".format(cmd))
        else:
            err("shallow '{}' returned rc={}".format(cmd, rc))
        pause()


def run_shallow_slim(device, auto=False):
    """Step 6：精简（移植之后、打包之前）。返回 (returncode, executed)"""
    slim = TOOLS / "shallow_slim.py"
    if not SHALLOW_ENABLED:
        # 精简可以关，但跨版本兼容注入不能少：OS4.0.29 源包删掉了
        # VNDK v30~v33 与 30.0 SELinux mapping，不补回会卡 HyperOS 二屏。
        info("Shallow stage disabled via --skip-shallow (compat injection kept)")
        log_write("Step 6: Shallow skipped (--skip-shallow), compat injection kept")
        if slim.is_file():
            env = os.environ.copy()
            env["SHALLOW_DEVICE"] = device or ""
            env["PYTHONIOENCODING"] = "utf-8"
            run_tool([PY, str(slim), "compat"], env=env)
        return 0, False
    if not slim.exists():
        err("tools/shallow_slim.py missing, shallow stage skipped")
        log_write("WARNING: shallow_slim.py missing, Step 6 skipped")
        return 0, False
    env = os.environ.copy()
    env["SHALLOW_DEVICE"] = device or ""
    env["PYTHONIOENCODING"] = "utf-8"
    rc = run_tool([PY, str(slim), "slim"], env=env).returncode
    return rc, True


def run_compat_precheck(auto=False):
    """Step 0：源 ROM / 底包 Android 版本兼容性预检。

    返回 0 = 继续；1 = 中止（回到菜单 / auto 模式退出）。
    依赖 tools/rom_compat.py，仅在两个 URL 都可用时执行。
    """
    tool = TOOLS / "rom_compat.py"
    if not API_CHECK:
        info("Compatibility precheck disabled by api_check=false")
        log_write("Step 0: precheck skipped (api_check=false)")
        return 0
    if not (SRC_URL and TGT_URL):
        info("Compatibility precheck skipped (reusing existing workspace)")
        log_write("Step 0: precheck skipped (no URL pair)")
        return 0
    if not tool.is_file():
        warn("tools/rom_compat.py missing, precheck skipped")
        log_write("WARNING: rom_compat.py missing")
        return 0

    info("=== Step 0/8: ROM compatibility precheck ===")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    rc = run_tool([PY, str(tool), "url", SRC_URL, TGT_URL], env=env).returncode
    if rc == 0:
        ok("Step 0 passed: Android versions match")
        log_write("Step 0: compat check OK")
        return 0
    if rc != 2:
        warn("Step 0 precheck inconclusive (rc={}), continuing".format(rc))
        log_write("Step 0: inconclusive rc={}".format(rc))
        return 0

    # rc == 2：跨 Android 大版本
    msg = "Step 0: source and base ROM differ by Android major version"
    log_write("WARNING: " + msg)
    if auto:
        if ALLOW_API_MISMATCH:
            warn("Auto mode: Android major version mismatch allowed by allow_api_mismatch=true")
            return 0
        err("Auto mode: Android major version mismatch, abort (set allow_api_mismatch=true to proceed)")
        log_write("ERROR: Step 0 aborted on version mismatch")
        return 1
    ans = prompt("  {}  跨 Android 大版本移植有较高不开机风险，仍要继续？(Y/N): {}".format(Y, N))
    if ans.strip().lower() == "y":
        info("User accepted cross-version risk, continuing")
        log_write(msg + " -> user accepted")
        return 0
    log_write(msg + " -> user declined")
    raise ReturnToMenu()


class ReturnToMenu(Exception):
    # 请求返回主菜单
    pass


# ---------------- 打包配置读取 ----------------
def read_packing_config():
    cfg = {
        "format": "erofs",
        "compression": "lz4hc",
        "compression_level": "9",
        "device_size": "6979321856",
        "metadata_size": "65536",
        "sparse": "true",
        "pack_super": "false",
        "super_name": "super",
        "super_group": "main",
        "metadata_slots": "3",
        "virtual_ab": "true",
        "is_skip_apex": "false",
        "enable_adb_debug": "false",
        "patch_vbmeta": "true",
        "utc_stamp": "",
        "erofs_old_kernel": "false",
        "device_platform": "qualcomm",
        "auto_fit": "true",
        "auto_fit_rounds": "8",
        "build_flashpack": "true",
        "base_first": "false",
    }
    if CONFIG.exists():
        for raw in read_config_text(CONFIG).splitlines():
            line = raw.strip()
            if not line or line.startswith(";") or line.startswith("["):
                continue
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip().lower()
            if key in cfg:
                cfg[key] = val.strip()
    return cfg


# ---------------- 配置模板生成 ----------------
def create_config():
    content = """; ============================================
; XMAPort config file
; ============================================

; Device platform: Qualcomm/MTK, must be filled in correctly
device_platform=Qualcomm

[source]
; Source ROM download URL (direct URL, must not start with ultimateota)
; Used to extract system / system_ext / product / mi_ext partitions
url=https://example.com/<source-device-full-rom>.zip

[target]
; Base ROM download URL (direct URL, must not start with ultimateota)
; Used to extract odm / vendor target partitions
url=https://bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com/OS2.0.204.0.VMWCNXM/sky-ota_full-OS2.0.204.0.VMWCNXM-user-15.0-5b8f723fe7.zip

[settings]
; aria2c download settings
; Download threads
threads=16

; Max connections
max-connection=16

; Timeout (seconds)
timeout=300

; Retry count
retry=5

; Base timeout (minutes) for external tools (payload-dumper-go / lpmake etc.)
; Large ROMs auto-scale beyond this by payload/partition size
tool_timeout=10

; Auto-clean intermediates after each stage to save disk (zip / payload.bin / unpacked img)
; true = delete as soon as no longer needed (recommended for CI and small disks)
auto_clean=true

; Step 0 compatibility precheck: compare source/base Android major version
; true = warn loudly when they differ (see python tools/rom_compat.py)
api_check=true

; Auto-mode behaviour on Android major version mismatch
; true = warn and continue, false = abort unless allow_api_mismatch=true
allow_api_mismatch=true


[packing]
; ============================================
; Image format parameters
; ============================================
format=erofs

; true = 以底包(310)为骨架重建 system/system_ext/product, 只把源包(4.0.29)
; 的 framework/应用增量替换进去; false = 现有「源包为主」路线。
; 规则见 devices/<代号>/base_first_rules.conf, 默认关闭(实验性)。
base_first=false

; Whether to pack super.img
pack_super=false

; Whether to generate sparse format image
sparse=true

; Leave empty to auto use UTC timestamp, auto decide if needed
utc_stamp=

; ============================================
; Super partition settings, fill in per device
; ============================================
; Total size allocated for device super partition, default 6.5GB
; Must match the target device: run python tools/probe_super.py --write
device_size=6979321856

; Keep the rest as default
; metadata partition size (bytes)
metadata_size=65536

; metadata slots
metadata_slots=3

; Whether to enable Virtual A/B
virtual_ab=true

; Super partition name
super_name=super

; Super group name
super_group=qti_dynamic_partitions

; ============================================
; erofs compression options
; ============================================
; erofs compression algorithm
compression=lz4hc

; erofs compression level
compression_level=8

; Compatibility with old kernels (-E legacy-compress)
erofs_old_kernel=false

; Auto-fit super: when lpmake reports insufficient space, iteratively remove the
; largest unprotected apps from product (keep_apps.conf protected ones are never
; touched), repack and retry until super.img fits device_size
auto_fit=true

; Build fastboot+recovery flash pack after packing (XMAPort-Flash.bat +
; META-INF recovery installer + images/ + zstd super). Needs ~12GB extra
; disk while building. Output: workspace/flashpack/
build_flashpack=true

; ============================================
; Keep this part as default
; ============================================
; When false, system_ext will be packed with legacy mkfs.erofs in tools/erofs-utils-cygwin
; When ro.product.build.version.incremental in source_filesystem/product/etc/build.prop contains both V13 and DEV, erofs packing auto uses legacy tool
is_skip_apex=false

; Whether to inject adb debug
enable_adb_debug=true

; Force Vulkan/Skia renderer on OS4 ports. Keep false: the verified shennong baseline
; carries none of these props, and mixing an
; Android 17 HWUI with an Android 16 vendor GPU driver hangs bootanimation.
inject_vulkan_props=false

; Whether to disable vbmeta verification
patch_vbmeta=true

; ============================================
; SHALLOW / slim stage (Step 6: after port, before pack)
; SHALLOW stage implemented and maintained by this project
; Device assets live in: devices/<TARGET_DEVICE>/
; ============================================
[shallow]
; Master switch. false = skip the whole slim stage and pack directly
enable=true

; true = abort porting when a slim step fails (default false = warn only)
strict=false

; Remove bloated apps listed in the apps list
; Search order: apps_list > devices/<codename>/remove_apps.conf > shallow/remove_apps.conf
remove_apps=true
apps_list=

; Inject device files: overlay / device_features / displayconfig / fstab / PowerKeeper
inject_device_files=true

; Replace MiuiCamera (merged from slices) + CameraTools calibration
replace_camera=true

; build.prop patches: build.date / build.date.utc unified to build time
patch_build_prop=true
build_date=true

; Screen density (persist.miui.density_v2), empty or 0 = keep unchanged
density=

; Enable aptX Lossless in vendor build.prop
aptx_lossless=true

; Remove Android signature verification (services.jar smali patch, needs java + apktool.jar)
disable_signature_verification=false

; Perfect icon pack replacement (requires network clone, slow)
perfect_icons=false

; Firmware layer: KernelSU (needs Linux + tools/linux/ksud + init_boot.img)
kernelsu=false

; Firmware layer: install devices/<codename>/recovery.zip (needs firmware-update dir)
twrp_recovery=false

; Firmware layer: extract tools/flashtools.zip into workspace/combined
flash_tools=false

; Firmware layer: drop "start qti-testscripts" from vendor init.qcom.rc
fix_init_rc=true

; ============================================
;  Build Prop Patch List, you can add props yourself, boot not guaranteed if modified
; ============================================
; patch build prop list
####################################
# from device/xiaomi/mivendor/common_rw.prop
####################################
# For common rw override property
#support night charge property
persist.vendor.night.charge=true
####################################
# from device/xiaomi/zero/odm/special_rw.prop
####################################
#support accelerate charge property
persist.vendor.accelerate.charge=true
#support battery health
persist.vendor.battery.health=true
#enable extreme battery
persist.vendor.extreme.mode=true
#support high temp dialog
persist.vendor.hightemp.notice=true
#support smart charge engine
persist.vendor.smartchg=854
#camera wechat beauty
persist.vendor.vcb.ability=true
persist.vendor.vcb.enable=false
ro.vendor.cabc.enable=fasle
ro.vendor.bcbc.enable=true
ro.vendor.dfps.enable=false
ro.vendor.fps.switch.default=true
ro.vendor.gcp.enable=1
ro.vendor.display.default_fps=90
ro.vendor.sre.enable=true
ro.vendor.localhbm.enable=true
ro.vendor.video_box.version=2
"""
    CONFIG.write_text(content, encoding="utf-8")
    info("Created config template: {}".format(CONFIG))


# ---------------- Step 1 下载（两包并行 + 每包一行实时进度） ----------------
# aria2 自带的 --max-tries 只重试瞬时网络错误（超时/断连），
# HTTP 状态错误（403/404/5xx 等）会直接退出，因此必须在 Python 层整体重试。
# 每次重试借助 --continue=true 从断点续传，不会从头下载。
DL_SUMMARY_RE = re.compile(
    r"\[#\w+ (\S+)/(\S+)\((\d+|--)%\)[^\]]*?DL:(\S+)")


def _aria2_cmd(url, out_dir):
    return [
        str(ARIA2), url,
        "-d", str(out_dir),
        "-x", str(MAX_CONN),
        "-s", str(THREADS),
        "-j", "1",
        "--min-split-size=1M",
        "--console-log-level=notice",
        "--summary-interval=1",
        "--file-allocation=falloc",
        "--timeout={}".format(TIMEOUT),
        "--max-tries={}".format(max(RETRY, 1)),
        "--retry-wait={}".format(RETRY_WAIT),
        "--lowest-speed-limit=10K",
        "--continue=true",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--log-level=notice",
    ]


def _download_worker(url, out_dir, name, state):
    # 单包下载线程：整体重试，进度实时写入 state，不直接打印（避免打乱进度条）。
    # retry=0 视为只下载一次（不重试），保证至少尝试 1 次。
    total = max(RETRY, 1)
    state["attempts"] = total
    try:
        os.makedirs(out_dir, exist_ok=True)
        cmd = _aria2_cmd(url, out_dir)
        for attempt in range(1, total + 1):
            state["attempt"] = attempt
            state["status"] = "connecting"
            log_write("Download attempt {}/{} start: {}".format(attempt, total, name))
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        errors="replace")
            except Exception:
                state["status"] = "failed"
                state["rc"] = 1
                state["error"] = "aria2c not runnable"
                log_write("ERROR: aria2c not runnable for {}".format(name))
                return
            for line in proc.stdout:
                m = DL_SUMMARY_RE.search(line)
                if m:
                    state["status"] = "downloading"
                    (state["done"], state["total"],
                     state["pct"], state["speed"]) = m.groups()
            rc = proc.wait()
            if rc == 0:
                state["status"] = "done"
                state["rc"] = 0
                log_write("Download done: {}".format(name))
                return
            log_write("Download attempt {}/{} failed for {}: exit {}".format(
                attempt, total, name, rc))
            if attempt < total:
                state["status"] = "retry_wait"
                for left in range(RETRY_WAIT, 0, -1):
                    state["retry_left"] = left
                    time.sleep(1)
        state["status"] = "failed"
        state["rc"] = 1
        state["error"] = "failed after {} attempts".format(total)
        log_write("ERROR: download failed after {} attempts: {}".format(total, name))
    except Exception as e:
        # 线程内任何意外异常都落到 failed 帧，避免进度条定格假进度、rc 悬空
        state["status"] = "failed"
        state["rc"] = 1
        state["error"] = "worker error: {}".format(e)
        log_write("ERROR: download worker crashed for {}: {}".format(name, e))


def _render_dl_line(name, st, bar_width=30):
    s = st["status"]
    if s == "done":
        total = st.get("total", "")
        return "  {:<10} [{}] 100%  {}  done".format(name, "#" * bar_width, total)
    if s == "failed":
        return "  {:<10} [FAILED] {}".format(name, st.get("error", ""))
    if s == "retry_wait":
        return "  {:<10} attempt {}/{} failed, retrying in {}s ...".format(
            name, st.get("attempt", 1), st.get("attempts", RETRY),
            st.get("retry_left", RETRY_WAIT))
    if s in ("starting", "connecting"):
        return "  {:<10} connecting ...".format(name)
    done = st.get("done", "?")
    total = st.get("total", "?")
    speed = st.get("speed", "?")
    try:
        p = int(st.get("pct", "0"))
    except ValueError:
        p = 0
    filled = int(bar_width * p / 100)
    bar = "#" * filled + "-" * (bar_width - filled)
    return "  {:<10} [{}] {:>3}%  {} / {}  DL:{}".format(
        name, bar, p, done, total, speed)


def download_roms(jobs):
    # 并行下载多个 ROM。jobs: [(url, out_dir, name), ...]；返回 {name: rc}。
    # 交互终端下每秒重绘进度（每包一行）；非 TTY（CI 日志）静默等待。
    states = {name: {"status": "starting", "rc": None} for _, _, name in jobs}
    threads = []
    for url, out_dir, name in jobs:
        t = threading.Thread(target=_download_worker,
                             args=(url, out_dir, name, states[name]), daemon=True)
        t.start()
        threads.append(t)

    if sys.stdout.isatty():
        printed = False
        while True:
            if printed:
                sys.stdout.write("\x1b[{}A".format(len(jobs)))
            for _, _, name in jobs:
                sys.stdout.write("\x1b[2K" + _render_dl_line(name, states[name]) + "\n")
            sys.stdout.flush()
            printed = True
            if all(not t.is_alive() for t in threads):
                break
            time.sleep(1)
    else:
        for t in threads:
            t.join()

    return {name: states[name]["rc"] for _, _, name in jobs}


# ---------------- Step 2 解包 ----------------
def extract_archive(src_dir, out_dir, label):
    src_dir = Path(src_dir)
    os.makedirs(out_dir, exist_ok=True)
    count = 0
    fail = 0
    for ext in ["*.zip", "*.tar", "*.gz", "*.tgz", "*.7z", "*.rar"]:
        for f in sorted(src_dir.glob(ext)):
            count += 1
            info("Processing: {}".format(f.name))
            rc = run_tool([str(SZ), "x", str(f), "-o" + str(out_dir), "-y"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
            if rc == 0:
                info("Extracted: {}".format(f.name))
            else:
                err("Extract failed: {}".format(f.name))
                log_write("ERROR: extract failed: {} (rc={})".format(f.name, rc))
                fail += 1
    if count == 0:
        err("No archives found in {}".format(src_dir))
        log_write("ERROR: no archives found in {}".format(src_dir))
        return 1
    if fail:
        err("{}/{} archive(s) failed to extract".format(fail, count))
        log_write("ERROR: {}/{} archive(s) failed to extract".format(fail, count))
        return 1
    return 0


# ---------------- Step 3 payload 解包 ----------------
REQUIRED_PAYLOAD_IMAGES = {
    "system.img",
    "system_ext.img",
    "product.img",
    "vendor.img",
    "odm.img",
}


def check_payload_extracted(target_dir):
    target_dir = Path(target_dir)
    if not target_dir.exists():
        return False
    found = {p.name.lower() for p in target_dir.rglob("*.img") if p.is_file()}
    if len(found) >= 6 and REQUIRED_PAYLOAD_IMAGES.issubset(found):
        info("Already extracted ({} partition images, required partitions present), skipping.".format(
            len(found)))
        return True
    return False


def detect_rom_format(rom_dir):
    """识别解压后 ROM 的格式，返回 (format, payload_path)"""
    # 1. A/B OTA
    for f in rom_dir.rglob("payload.bin"):
        return "payload", f
    # 2. block OTA（.dat / .dat.br / .dat.xz / 分卷）
    block_patterns = ["*.transfer.list", "*.new.dat", "*.new.dat.br",
                      "*.new.dat.xz", "*.new.dat.1"]
    if any(True for p in block_patterns for _ in rom_dir.rglob(p)):
        return "block_dat", None
    # 3. 已经存在 .img
    return "img", None


def extract_payload_bin(rom_dir, out_dir):
    rom_dir = Path(rom_dir)
    out_dir = Path(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    fmt, payload_file = detect_rom_format(rom_dir)

    if fmt == "payload":
        info("Found payload.bin (A/B OTA), extracting...")
        # 大包（如 12GB+ 的旗舰 ROM）10 分钟默认超时不够，按 payload.bin 体积放大
        try:
            payload_size = payload_file.stat().st_size
        except Exception:
            payload_size = 0
        dump_timeout = scaled_timeout(payload_size, per_gb_min=3, cap_min=180)
        info("payload.bin size: {:.1f} GB, timeout: {} min".format(
            payload_size / (1024 ** 3), dump_timeout // 60))
        log_write("payload.bin size={} bytes, timeout={}s".format(payload_size, dump_timeout))
        cmd = [str(PDUMP), "-o", str(out_dir), str(payload_file)]
        if AUTO_MODE:
            proc = run_tool(cmd, timeout=dump_timeout,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            errors="replace")
            rc = proc.returncode
        else:
            rc = run_tool(cmd, timeout=dump_timeout).returncode
        if rc != 0:
            err("payload-dumper-go failed")
            if AUTO_MODE and proc.stdout:
                tail = "\n".join(proc.stdout.strip().splitlines()[-30:])
                print(tail, flush=True)
                log_write(tail)
            return 1
        info("Payload extracted to: {}".format(out_dir))
        for f in sorted(out_dir.glob("*.img")):
            mb = round(f.stat().st_size / 1024 / 1024, 1)
            info("  {}.img  {} MB".format(f.stem, mb))
        return 0

    if fmt == "block_dat":
        # block OTA（.dat / .dat.br / .dat.xz / 分卷）→ 转换为 .img
        info("Found block OTA (.dat/.dat.br), converting to .img...")
        rc = run_tool([PY, str(TOOLS / "extract_dat.py"),
                       str(rom_dir), str(out_dir)]).returncode
        if rc != 0:
            err("block OTA .dat conversion failed")
            log_write("ERROR: extract_dat.py failed")
            return 1
        info("block OTA .dat partition(s) converted to .img")
        return 0

    # fmt == "img"：没有 payload.bin 和 .dat，直接复制已有的 .img
    info("No payload.bin / .dat found, copying existing .img files...")
    count = 0
    fail = 0
    for f in rom_dir.rglob("*.img"):
        count += 1
        try:
            shutil.copy2(f, out_dir / f.name)
        except Exception as e:
            err("Failed to copy {}: {}".format(f.name, e))
            log_write("ERROR: copy {} failed: {}".format(f.name, e))
            fail += 1
    info("Copied {} img file(s)".format(count))
    if count == 0:
        err("No .img files found in {}".format(rom_dir))
        log_write("ERROR: no .img files found in {}".format(rom_dir))
        return 1
    return 1 if fail else 0


# ---------------- Step 4 镜像解包 ----------------
def unpack_all_img(img_dir, out_dir, label):
    img_dir = Path(img_dir)
    out_dir = Path(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    fail = 0
    for part in UNPACK_PARTS:
        img = img_dir / (part + ".img")
        if img.exists():
            info("Processing {}.img ...".format(part))
            os.makedirs(out_dir / part, exist_ok=True)
            rc = run_tool([PY, str(TOOLS / "extract_img.py"), str(img), str(out_dir / part)]).returncode
            if rc != 0:
                err("Failed to extract {}.img".format(part))
                log_write("ERROR: extract {}.img failed (rc={})".format(part, rc))
                fail += 1
            else:
                info("{}.img extracted".format(part))
    if fail:
        err("{}/{} image(s) failed to unpack".format(fail, len(UNPACK_PARTS)))
        return 1
    return 0


# ---------------- Step 5 注入 adb debug ----------------
def inject_adb_debug(pack_cfg):
    if pack_cfg.get("enable_adb_debug", "false").lower() != "true":
        return
    target_prop = TGT_FS / "odm" / "etc" / "build.prop"
    if not target_prop.exists():
        err("enable_adb_debug=true but odm build.prop not found")
        log_write("WARNING: adb debug inject skipped, build.prop not found")
        return
    try:
        content = target_prop.read_text(encoding="utf-8", errors="replace")
    except Exception:
        content = ""
    if "# XMAport adb debug" in content:
        info("adb debug props already injected, skipping")
        log_write("adb debug props already present, skip")
        return
    lines = [
        "# XMAport adb debug",
        "ro.debuggable=1",
        "ro.secure=0",
        "ro.adb.secure=0",
        "persist.sys.usb.config=adb",
        "persist.adb.notify=0",
        "service.adb.root=1",
        "persist.sys.root_access=3",
    ]
    with open(target_prop, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    info("adb debug props injected into odm build.prop")
    log_write("adb debug props injected: {}".format(target_prop))


# ---------------- HyperOS 版本属性对齐 ----------------
# shennong 基线镜像把这些键写进 mi_ext：OS4.0 / code=4 / ai.code=7 /
# ai.name=OS4.0 / incremental=OS4.0.x。我们保留的是底包 mi_ext（OS3.0 / code=3），
# 而 system 来自 OS4.0 源包 —— framework 读到错误的 OS 大版本会走错分支，
# 典型症状就是卡在 HyperOS 开机动画。
# odm build.prop 在 init 中先于 mi_ext 加载，ro.* 先设置者生效，
# 所以写在 odm 即可覆盖，不必重打包 mi_ext 的 erofs 镜像。
OS_VERSION_KEYS = (
    "ro.mi.xms.version.incremental",
    "ro.ai.os.version.code",
    "ro.ai.os.version.name",
    "ro.mi.os.version.code",
    "ro.mi.os.version.name",
    "ro.mi.os.version.incremental",
)


def _read_prop_keys(path, keys):
    out = {}
    if not path.is_file():
        return out
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in keys and k not in out and v.strip():
                out[k] = v.strip()
    except Exception:
        pass
    return out


OS_PROPS_MARKER = "# XMAPort HyperOS version (baseline aligned)"


def _write_props_file(target, keys, vals, marker):
    """删掉旧值、在文件尾追加新值；幂等，不改动其它属性。"""
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        warn("failed to read {}: {}".format(target, e))
        return False
    lines = raw.splitlines()
    kept = [l for l in lines
            if not any(l.strip().startswith(k + "=") for k in keys)]
    kept += ["", marker]
    for k in keys:
        if vals.get(k):
            kept.append("{}={}".format(k, vals[k]))
    try:
        target.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return True
    except Exception as e:
        warn("failed to write {}: {}".format(target, e))
        return False


def inject_os_version_props(pack_cfg):
    """把源 ROM 的 HyperOS 版本属性写进 odm 与 mi_ext 的 build.prop。

    只写 odm 不够：底包 mi_ext 里仍是
    ro.mi.os.version.code=3 / name=OS3.0 / incremental=OS3.0.310...，
    ro.* 属性先设置者生效，加载顺序一变就会读到 OS3，framework 走错分支
    （自动亮度不生效、设置「全部参数与信息」缺项）。因此两处都对齐成 OS4。
    """
    targets = [TGT_FS / "odm" / "etc" / "build.prop",
               TGT_FS / "mi_ext" / "etc" / "build.prop"]
    existing = [p for p in targets if p.is_file()]
    if not existing:
        warn("odm/mi_ext build.prop not found, skip HyperOS version props")
        log_write("WARNING: odm/mi_ext build.prop missing, HyperOS version props not injected")
        return
    vals = {}
    for rel in (("mi_ext", "etc", "build.prop"),
                ("product", "etc", "build.prop"),
                ("system", "system", "build.prop"),
                ("system", "build.prop")):
        p = SRC_FS.joinpath(*rel)
        for k, v in _read_prop_keys(p, OS_VERSION_KEYS).items():
            vals.setdefault(k, v)
        inc = _read_prop_keys(
            p, ("ro.product.build.version.incremental",)
        ).get("ro.product.build.version.incremental", "")
        if inc.upper().startswith("OS4"):
            vals.setdefault("ro.mi.os.version.incremental", inc)
    if not vals:
        warn("source HyperOS version props not found, skip injection")
        log_write("WARNING: source HyperOS version props not found")
        return
    inc = vals.get("ro.mi.os.version.incremental", "")
    is_os4 = inc.upper().startswith("OS4")
    fallbacks = {
        "ro.mi.xms.version.incremental": "D01",
        "ro.ai.os.version.code": "7" if is_os4 else "",
        "ro.ai.os.version.name": "OS4.0" if is_os4 else "",
        "ro.mi.os.version.code": "4" if is_os4 else "",
        "ro.mi.os.version.name": "OS4.0" if is_os4 else "",
    }
    for k, v in fallbacks.items():
        if v and k not in vals:
            vals[k] = v
    done = 0
    for target in existing:
        if _write_props_file(target, OS_VERSION_KEYS, vals, OS_PROPS_MARKER):
            done += 1
            info("HyperOS version props -> {}: {}".format(target.name, inc or "?"))
    log_write("HyperOS version props ({}/{} files): ".format(done, len(existing)) +
              ", ".join("{}={}".format(k, v) for k, v in vals.items()))


# ---------------- 机型标识对齐（设置「全部参数与信息」/ 自动亮度） ----------------
# 4.0.29 源包的 product/etc/build.prop 写的是 madrid（小米18 Pro Max），
# 而 4.0.27 基线镜像是 shennong。机型名不匹配会让小米的设置页与设备特性
# 框架读错配置：自动亮度曲线退化、参数页缺项。按基线把标识改回来。
PRODUCT_IDENTITY_KEYS = (
    "ro.product.product.name",
    "ro.product.build.fingerprint",
    "ro.build.flavor",
    "ro.build.description",
    "persist.vendor.bluetooth.lhdc_v5.supported",
    "persist.vendor.bluetooth.lhdc_v4.supported",
)
PRODUCT_IDENTITY_MARKER = "# XMAport v2: shennong identity (aligned with OS4.0.27 golden)"
# 4.0.27 基线镜像有、4.0.29 源包丢失的设备特性开关（排除 BL 伪装与 dex2oat 调优）
PRODUCT_IDENTITY_APPEND = (
    "ro.mi.version.mi_product=empty",
    "ro.product.port.autor=xmiport",
    "ro.product.port.device=madrid",
    "ro.miui.special.aod.mask=0.57",
    "ro.odm.mm.vibrator.audio_haptic_support=true",
    "sys.haptic.media=true",
    "ro.vendor.audio.scenario.support=true",
    "ro.vendor.audio.sfx.scenario=true",
    "persist.sys.support_ultra_hdr=true",
    "ro.surface_flinger.game_default_frame_rate_override=60",
    "fw.max_users=1000",
    "fw.show_multiuserui=1",
    "ro.hwui.use_vulkan=true",
)


def inject_product_identity_props(pack_cfg):
    """把 product/etc/build.prop 里的 madrid 标识改成目标机型。"""
    target = TGT_FS / "product" / "etc" / "build.prop"
    if not target.is_file():
        warn("product build.prop not found, skip identity props")
        return
    device = (TARGET_DEVICE or "").strip()
    if not device:
        warn("target device unknown, skip identity props")
        return
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        warn("failed to read product build.prop: {}".format(e))
        return

    def transform(key, old):
        if key == "ro.product.product.name":
            return "miproduct_{}".format(device)
        if key == "ro.product.build.fingerprint":
            seg = old.split("/")
            if len(seg) >= 2:
                seg[1] = device
                return "/".join(seg)
            return old
        if key == "ro.build.flavor":
            return "miproduct_{}-user".format(device)
        if key == "ro.build.description":
            seg = old.split(" ", 1)
            return "{}-user {}".format(device, seg[1]) if len(seg) == 2 else old
        return device  # lhdc_v4 / v5

    out, hits = [], 0
    for line in raw.splitlines():
        s = line
        for key in PRODUCT_IDENTITY_KEYS:
            if s.strip().startswith(key + "="):
                s = key + "=" + transform(key, s.split("=", 1)[1])
                hits += 1
                break
        out.append(s)
    appended = 0
    if PRODUCT_IDENTITY_MARKER not in raw:
        out += ["", PRODUCT_IDENTITY_MARKER] + list(PRODUCT_IDENTITY_APPEND)
        appended = len(PRODUCT_IDENTITY_APPEND)
    try:
        target.write_text("\n".join(out) + "\n", encoding="utf-8")
    except Exception as e:
        warn("failed to write product identity props: {}".format(e))
        return
    info("product identity -> {} (overridden={} appended={})".format(device, hits, appended))
    log_write("product identity: device={} overridden={} appended={}".format(
        device, hits, appended))


GOLDEN_PERF_MARKER = "# XMAport v3: golden performance / scheduler props (OS4.0.27 baseline)"


def _read_perf_prop_file(path):
    """解析 devices/<device>/performance.props：忽略注释与空行，保持顺序。"""
    vals = {}
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        vals[k.strip()] = v.strip()
    return vals


def inject_golden_perf_props(pack_cfg):
    """把 4.0.27 基线镜像的性能调度属性写进 product/etc/build.prop。

    OS4.0.29 源包(madrid) 相对基线丢了小米动画绑核 / SF 绑核 /
    Vulkan 管线缓存属性，又把 prestart / preload / Preread 开关设成 true。
    实测：动画线程不绑大核、RenderEngine shaders cached total=0、
    dex_preload_t 进程泛滥 + 1609 条 SELinux denied、20 条
    "Slow operation: startProcess"、HWC/GPU missed frame 上万，
    表现为「桌面滑动尚可，打开应用和操作卡顿」。

    属性表放在 devices/<device>/performance.props，与本地离线重打包
    (local-fix/v3_fix.py) 共用同一份数据，保证云端与本地一致。

    ro.* 是只读属性：必须替换原行，单纯在文件末尾追加不会生效。
    """
    target = TGT_FS / "product" / "etc" / "build.prop"
    if not target.is_file():
        warn("product build.prop not found, skip golden perf props")
        return
    src = DEVICES_DIR / (TARGET_DEVICE or "").strip() / "performance.props"
    if not src.is_file():
        info("no performance.props for {}, skip".format(TARGET_DEVICE))
        return
    try:
        pairs = _read_perf_prop_file(src)
        raw = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        warn("failed to read performance.props / build.prop: {}".format(e))
        return
    if not pairs:
        return
    nl = "\r\n" if "\r\n" in raw else "\n"
    out, hit = [], set()
    for ln in raw.splitlines():
        s = ln.strip()
        if not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in pairs:
                out.append("{}={}".format(k, pairs[k]))
                hit.add(k)
                continue
        out.append(ln)
    missing = [k for k in pairs if k not in hit]
    if missing and GOLDEN_PERF_MARKER not in raw:
        out += ["", GOLDEN_PERF_MARKER] + [
            "{}={}".format(k, pairs[k]) for k in missing]
    try:
        target.write_bytes((nl.join(out) + nl).encode("utf-8"))
    except Exception as e:
        warn("failed to write golden perf props: {}".format(e))
        return
    info("golden perf props: {} overridden, {} appended (from {})".format(
        len(hit), len(missing), src.name))
    log_write("golden performance props: {} keys, overridden={} appended={}".format(
        len(pairs), len(hit), len(missing)))


# ---------------- vbmeta 禁验 ----------------
def _vbmeta_header(path):
    """读取 AVB 头的 (algorithm_type, flags)，均为大端；非 AVB0 返回 (None, None)。"""
    try:
        with open(path, "rb") as f:
            h = f.read(256)
        if len(h) < 128 or h[:4] != b"AVB0":
            return None, None
        return int.from_bytes(h[28:32], "big"), int.from_bytes(h[120:124], "big")
    except OSError:
        return None, None


def fix_vibrator_hal_tree():
    """Step 6: 让 odm 同时提供 IVibrator/vibratorfeature 与 IVibrator/default。

    2026-11-09 真机定案：Android 17 framework 要求 IVibrator/default，
    MIUI framework 又硬等 IVibrator/vibratorfeature（device compatibility
    matrix 声明）。底包 odm 只注册后者 -> vibratorIds=[] -> 触感全部
    IGNORED_UNSUPPORTED；只改成 default 又会卡在 HyperOS 开机界面。
    因此必须两个实例同时存在。实现与原理见 tools/fix_vibrator_hal.py。

    本步骤失败只告警，不阻断移植（工具缺失 / 非 shennong 机型会直接跳过）。
    """
    odm = TGT_FS / "odm"
    if not odm.is_dir():
        warn("odm tree not found, skip vibrator HAL dual-instance fix")
        return
    cfg_dir = None
    for p in (TGT_FS / "config", WORKSPACE / "config"):
        if p.is_dir():
            cfg_dir = p
            break
    try:
        if str(TOOLS) not in sys.path:
            sys.path.insert(0, str(TOOLS))
        import fix_vibrator_hal
        for line in fix_vibrator_hal.apply_to_tree(odm, cfg_dir):
            log_write("vibrator HAL fix: " + line)
            if line.startswith("WARN") or line.startswith("SKIP"):
                warn(line)
            else:
                info("  " + line)
    except Exception as e:
        warn("vibrator HAL dual-instance fix failed (non-fatal): {}".format(e))
        log_write("WARNING: vibrator HAL fix failed: {}".format(e))


def fix_density_tree():
    """Step 6: 把 product/etc/build.prop 的密度 480 改成 shennong 原生 560。

    480 的源头是
    product/etc/build.prop 里的 ro.sf.lcd_density=480 /
    persist.miui.density_v2=480（源机型 madrid 的密度）。init 直接写
    build.prop 属性（绕过 ro.* set-once），且 product 在 vendor 之后加载，
    因此需要改 product 侧。
    实现见 tools/fix_density.py。

    本步骤失败只告警，不阻断移植（product 树缺失时跳过）。
    """
    # product 分区打包自 source_filesystem（源包 madrid，密度 480）。
    # 2026-11-15 实证：原实现指向 target_filesystem/product（底包 shennong
    # 已是 560），fix_density 于是判定 "already patched" 直接 SKIP，
    # 最终产物仍是 480（字体小）。这里对两个候选树都应用一次，
    # 保证无论哪份 product 被打包都是 shennong 原生 560。
    candidates = [SRC_FS / "product", TGT_FS / "product"]
    found = False
    try:
        if str(TOOLS) not in sys.path:
            sys.path.insert(0, str(TOOLS))
        import fix_density
        for product in candidates:
            if not product.is_dir():
                continue
            found = True
            for line in fix_density.apply_to_tree(product):
                log_write("density fix ({}): ".format(product.name) + line)
                if line.startswith("WARN") or line.startswith("SKIP"):
                    warn(line)
                else:
                    info("  " + line)
    except Exception as e:
        warn("density 560 fix failed (non-fatal): {}".format(e))
        log_write("WARNING: density fix failed: {}".format(e))
    if not found:
        warn("product tree not found, skip density 560 fix")


def fix_screen_flicker_tree():
    """Step 6: 处理锁屏临黑偶发闪屏的三处显示配置差异。

    对照基线镜像：vendor/build.prop 的 ro.hwui.use_vulkan 为空、
    product/etc/device_features/shennong.xml 缺 support_aod_fullscreen 且
    fpsList 只有 120/60（缺 90 中间档），三者叠加导致灭屏前最后一帧
    合成/刷新率跨档跳变。实现见 tools/fix_screen_flicker.py。

    vendor 的 Vulkan 开关默认不改动：本组合是
    Android 17 framework + Android 16 vendor GPU 驱动，强开 Vulkan 会让
    SurfaceFlinger/HWUI 起不来、卡开机动画。
    默认只应用 product 侧两项安全配置，Vulkan 需自行验证兼容性后再启用。

    不动 dtbo 与 framework jar（差异属编译副产物 / 换底包风险过高）。
    """
    try:
        if str(TOOLS) not in sys.path:
            sys.path.insert(0, str(TOOLS))
        import fix_screen_flicker
        pairs = []
        vendor = TGT_FS / "vendor"
        product = TGT_FS / "product"
        if vendor.is_dir():
            pairs.extend(fix_screen_flicker.apply_to_vendor(vendor))
        else:
            pairs.append("SKIP: vendor tree not found")
        if product.is_dir():
            pairs.extend(fix_screen_flicker.apply_to_product(product))
        else:
            pairs.append("SKIP: product tree not found")
        for line in pairs:
            log_write("flicker fix: " + line)
            if line.startswith("WARN") or line.startswith("SKIP"):
                warn(line)
            else:
                info("  " + line)
    except Exception as e:
        warn("screen flicker fix failed (non-fatal): {}".format(e))
        log_write("WARNING: flicker fix failed: {}".format(e))


def patch_vbmeta(pack_cfg):
    """用「未签名 + flags=3」的空白禁用 vbmeta 覆盖打包输出。

    2026-11-05 shennong 实机对照实验（A/B/C/D/E 五组）结论：
      官方底包 vbmeta 是已签名镜像 (algo=1, auth>0)。旧实现用 vbmeta_patch.py
      直接在签名后的镜像上改 flags 字节，等于破坏 AVB 签名；ABL 校验顶层
      vbmeta 失败后直接回落 fastboot，连内核都不加载（pstore 为空），
      recovery / fastbootd 也全部无法启动 —— 这解释了为什么换 super 结构、
      清 misc、双槽刷入、清数据全都无效。
      已知可开机包使用未签名 (algo=0, auth=0) 且 flags=3
      (HASHTREE_DISABLED | VERIFICATION_DISABLED) 的空白 vbmeta。
      实测：仅替换 vbmeta 后，我们自己的 boot 链即可正常进 recovery。
    """
    if pack_cfg.get("patch_vbmeta", "true").lower() != "true":
        return True
    log_write("Applying blank disabled vbmeta (AVB verification off)")
    info("Applying blank disabled vbmeta (unsigned, flags=3)...")
    count = 0
    for f in sorted(TGT_UNPACK.glob("vbmeta*.img")):
        try:
            shutil.copy2(f, PACK_OUT / f.name)
            count += 1
            log_write("vbmeta copied: {}".format(f.name))
        except Exception:
            err("failed to copy: {}".format(f.name))
            log_write("ERROR: copy failed: {}".format(f.name))
    if count == 0:
        err("No vbmeta*.img found in target payload")
        log_write("WARNING: no vbmeta*.img in target_payload, skip")
        return True

    blank_dir = ROOT / "tools" / "assets"
    mapping = {"vbmeta.img": "vbmeta_disabled.img",
               "vbmeta_system.img": "vbmeta_system_disabled.img"}
    applied = 0
    for name, asset in mapping.items():
        src, dst = blank_dir / asset, PACK_OUT / name
        if not dst.is_file() or not src.is_file():
            continue
        shutil.copy2(src, dst)
        algo, flags = _vbmeta_header(dst)
        info("blank vbmeta applied: {} (algo={}, flags={})".format(name, algo, flags))
        log_write("blank vbmeta applied: {} -> {}".format(asset, name))
        applied += 1

    if applied:
        algo, flags = _vbmeta_header(PACK_OUT / "vbmeta.img")
        if algo != 0 or flags != 3:
            err("vbmeta.img 不是空白禁用镜像 (algo={}, flags={})，该包会无法开机！".format(algo, flags))
            log_write("ERROR: vbmeta.img invalid (algo={}, flags={})".format(algo, flags))
            return False
        ok("vbmeta 校验通过: 未签名 + flags=3")
        return True

    # 2026-11-16: 资产缺失不再回退字节补丁。字节补丁只能改 flags，algo 仍是 1
    # （签名状态），ABL 校验失败会直接回落 fastboot；build_flashpack 也会因此
    # 拒绝出包（本次 CI 实测：super 发布成功但 flashpack 缺失）。改为硬失败。
    err("缺少 tools/assets/vbmeta_disabled.img 与 vbmeta_system_disabled.img")
    err("空白禁用 vbmeta 是开机的必要条件，不再回退到不安全的字节补丁")
    log_write("ERROR: vbmeta assets missing (tools/assets/vbmeta*_disabled.img)")
    return False


def _lp_slot_suffix(pack_cfg):
    """A/B 设备 (metadata_slots>=2) 的 super 元数据里, 分区/组名必须带槽位后缀
    (system_a / qti_dynamic_partitions_a)。缺后缀时 init 与 fastbootd 找不到
    动态分区, 设备无法启动(2026-11-03 shennong 实机确认)。可启动参照:
    已验证基线镜像仅填充 _a 槽。"""
    try:
        return "_a" if int(pack_cfg.get("metadata_slots", "2")) >= 2 else ""
    except (TypeError, ValueError):
        return "_a"


def _append_lpc_entry(lpc_args, part, img_path, size, pack_cfg):
    # attributes 用 none（不是 readonly）：可开机参照的基线镜像的 LP 元数据
    # Attributes 全为 none，带 readonly 时 virtual-ab 设备映射行为不同。
    # 本地实测: name:none:size:group -> lpdumps 显示 Attributes: none
    slot = _lp_slot_suffix(pack_cfg)
    lpc_args.append("--partition={}:none:{}:{}".format(
        part + slot, size, pack_cfg.get("super_group", "main") + slot))
    lpc_args.append("--image={}={}".format(part + slot, img_path))


# ---------------- Step 6 分区打包辅助 ----------------
def pack_one_partition(part, fs_dir, pack_cfg, lpc_args, counters):
    src = fs_dir / part
    if not src.exists():
        return
    info("Packing partition: {}".format(part))
    log_write("Packing {}...".format(part))
    cmd = [
        PY, str(TOOLS / "pack_partitions.py"),
        pack_cfg["format"],
        "{},{}".format(pack_cfg["compression"], pack_cfg["compression_level"]),
        str(src),
        str(PACK_OUT),
    ]
    rc = run_tool(cmd).returncode
    out_img = PACK_OUT / (part + ".img")
    if rc != 0:
        err("{}: pack_partitions.py failed".format(part))
        log_write("ERROR: {} packing failed".format(part))
        counters["pack_fail"] += 1
        return
    if not out_img.exists():
        err("{}: output image not found".format(part))
        log_write("ERROR: {}.img not generated".format(part))
        counters["pack_fail"] += 1
        return
    size = out_img.stat().st_size
    info("{}.img packed, {} bytes".format(part, size))
    log_write("{}.img packed: {} bytes".format(part, size))
    _append_lpc_entry(lpc_args, part, out_img, size, pack_cfg)
    counters["pack_ok"] += 1


def copy_partition_image(part, src_file, pack_cfg, lpc_args, counters):
    # 从 payload 镜像直接复制到 packed（mi_ext / vendor / vendor_dlkm）
    if not src_file.exists():
        err("{}.img not found in payload".format(part))
        log_write("ERROR: {}.img not found in payload".format(part))
        counters["pack_fail"] += 1
        pause()
        return
    try:
        shutil.copy2(src_file, PACK_OUT / src_file.name)
    except Exception as e:
        err("{}.img copy failed: {}".format(part, e))
        log_write("ERROR: {}.img copy failed: {}".format(part, e))
        counters["pack_fail"] += 1
        pause()
        return
    if not (PACK_OUT / src_file.name).exists():
        err("{}.img copy failed".format(part))
        log_write("ERROR: {}.img copy failed (output missing)".format(part))
        counters["pack_fail"] += 1
        pause()
        return
    size = (PACK_OUT / src_file.name).stat().st_size
    info("{}.img ready, {} bytes".format(part, size))
    log_write("{}.img ready: {} bytes".format(part, size))
    _append_lpc_entry(lpc_args, part, PACK_OUT / src_file.name, size, pack_cfg)
    counters["pack_ok"] += 1


def create_super_img(pack_cfg, lpc_args, pack_ok):
    # lpmake 生成 super.img，返回 0=成功/跳过, 1=空间不足, 2=其他错误
    if pack_cfg.get("pack_super", "false").lower() != "true":
        return 0
    if pack_ok == 0:
        err("No partitions packed, skipping super.img.")
        log_write("WARNING: No partitions packed, super.img skipped")
        return 0
    info("Creating super.img...")
    cmd = [
        str(LPM),
        "--metadata-size", pack_cfg["metadata_size"],
        "--super-name", pack_cfg["super_name"],
        "--metadata-slots", pack_cfg["metadata_slots"],
        "--device", "{}:{}".format(pack_cfg["super_name"], pack_cfg["device_size"]),
        "--group", "{}{}:{}".format(pack_cfg["super_group"],
                                    _lp_slot_suffix(pack_cfg), pack_cfg["device_size"]),
    ]
    cmd += lpc_args
    if pack_cfg.get("virtual_ab", "true").lower() == "true":
        cmd.append("--virtual-ab")
    if pack_cfg.get("sparse", "true").lower() == "true":
        cmd.append("--sparse")
    cmd += ["--output=" + str(PACK_OUT / "super.img")]
    info("lpmake command: " + subprocess.list2cmdline(cmd))
    # lpmake 日志实时滚屏（不吞日志），同时逐行留档用于失败分类；
    # 看门狗防卡死，超时分区总大小放大（12GB 级 super 在慢盘上 10 分钟不够）。
    try:
        total_img = sum(f.stat().st_size for f in PACK_OUT.glob("*.img") if f.is_file())
    except Exception:
        total_img = 0
    lpm_timeout = scaled_timeout(total_img, per_gb_min=3, cap_min=180)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            errors="replace")
    timed_out = False

    def _kill_lpmake():
        nonlocal timed_out
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass

    watchdog = threading.Timer(lpm_timeout, _kill_lpmake)
    watchdog.start()
    output_lines = []
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            output_lines.append(line)
        proc.wait()
    finally:
        watchdog.cancel()
    rc = proc.returncode
    output = "".join(output_lines)
    if timed_out:
        err("lpmake timed out after {} minutes".format(lpm_timeout // 60))
        log_write("ERROR: lpmake timed out after {} seconds".format(lpm_timeout))
        return 2
    if rc != 0:
        lower = output.lower()
        if any(kw in lower for kw in ["exceeds", "not enough", "no space", "too large", "overflow", "size limit"]):
            m = re.search(r"partition\s+(\S+)\s+with\s+size\s+(\d+)", lower)
            if m:
                err("lpmake failed: super space insufficient (partition={}, size={})".format(
                    m.group(1), m.group(2)))
                log_write("ERROR: lpmake failed - super space insufficient (partition={}, size={})".format(
                    m.group(1), m.group(2)))
            else:
                err("lpmake failed: super space insufficient")
                log_write("ERROR: lpmake failed - super space insufficient")
            return 1
        err("lpmake failed (full log above)")
        log_write("ERROR: lpmake failed to create super.img (rc={})".format(rc))
        pause()
        return 2
    log_write("super.img created successfully")
    if pack_cfg.get("sparse", "true").lower() == "true":
        info("sparse super.img created directly by lpmake")
    sup = PACK_OUT / "super.img"
    if sup.exists():
        info("super.img created, {} bytes".format(sup.stat().st_size))
    return 0


# ---------------- super 空间自适应（auto-fit） ----------------
# lpmake 报空间不足时，按缺口从 product 里删最大的非保护应用，重打包重试。
# 保护名单（shallow/keep_apps.conf + 硬编码关键组件）里的目录绝不动。

_AUTOFIT_HARD_KEEP = {
    "settings", "systemui", "miuisystemui", "miuihome", "miuisettings",
    "permissioncontroller", "miuiframework", "framework-res", "miuicamera",
    "packageinstaller", "securitycenter", "securitycore", "telecom",
    "telephonyprovider", "mms", "contacts", "contactsprovider",
    "downloadprovider", "documentsui", "extservices", "shell",
}


def _dir_size_bytes(path):
    total = 0
    try:
        for cur, _dirs, files in os.walk(str(path)):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(cur, f))
                except OSError:
                    pass
    except Exception:
        pass
    return total


def _autofit_keep_names():
    """汇总保护名单（取每条规则第 3 段的目录名，小写）。宁多勿少：只删绝对不在名单里的。"""
    names = set(_AUTOFIT_HARD_KEEP)
    confs = [SHALLOW_DIR / "keep_apps.conf"]
    if TARGET_DEVICE:
        confs.append(DEVICES_DIR / TARGET_DEVICE / "keep_apps.conf")
    for conf in confs:
        try:
            if not conf.is_file():
                continue
            for raw in conf.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                seg = line.split(":")
                if len(seg) < 3:
                    continue
                pat = seg[2].strip().lstrip("=").strip().lower()
                if pat:
                    names.add(pat)
        except Exception:
            pass
    return names


def _autofit_pick_victims(need_raw, max_pick=12):
    """从 product 挑待删目录。顺序：data-app(预装) → app → priv-app；同层按大小降序。"""
    keep = _autofit_keep_names()
    prod = SRC_FS / "product"
    tiers = [prod / "data-app", prod / "data_app", prod / "app", prod / "priv-app"]
    picked, picked_bytes = [], 0
    for base in tiers:
        if not base.is_dir():
            continue
        cands = []
        try:
            for child in base.iterdir():
                if not child.is_dir():
                    continue
                if child.name.lower() in keep:
                    continue
                sz = _dir_size_bytes(child)
                if sz > 0:
                    cands.append((sz, child))
        except Exception:
            continue
        cands.sort(key=lambda x: x[0], reverse=True)
        for sz, child in cands:
            if picked_bytes >= need_raw or len(picked) >= max_pick:
                break
            picked.append((sz, child))
            picked_bytes += sz
        if picked_bytes >= need_raw or len(picked) >= max_pick:
            break
    return picked, picked_bytes


def _repack_product(pack_cfg, lpc_args, counters):
    """删除 product 旧条目并重新打包（带槽位后缀）"""
    slot = _lp_slot_suffix(pack_cfg)
    lpc_args[:] = [a for a in lpc_args
                   if not a.startswith("--partition=product" + slot + ":")
                   and not a.startswith("--image=product" + slot + "=")]
    try:
        (PACK_OUT / "product.img").unlink()
    except Exception:
        pass
    pack_one_partition("product", SRC_FS, pack_cfg, lpc_args, counters)


def auto_fit_super(pack_cfg, lpc_args, counters, max_rounds=None):
    """lpmake 空间不足时迭代删减 product 应用直到能塞进 super 组。返回 lpmake 最终 rc。

    源包越大缺口越大（OS4.0.29 比 27 大约 0.9GB），轮数可通过
    config.ini 的 auto_fit_rounds 调整，默认 8 轮。
    """
    if max_rounds is None:
        try:
            max_rounds = int(pack_cfg.get("auto_fit_rounds", "8"))
        except (TypeError, ValueError):
            max_rounds = 8
        max_rounds = max(1, min(max_rounds, 20))
    group = pack_cfg.get("super_group", "main") + _lp_slot_suffix(pack_cfg)
    device_size = int(pack_cfg["device_size"])
    meta = int(pack_cfg.get("metadata_size", "65536"))
    slots = int(pack_cfg.get("metadata_slots", "3"))
    # 组预算：device_size 减去元数据与对齐余量
    budget = device_size - meta * (slots + 1) - 4 * 1024 * 1024
    removed_all = []

    def group_total():
        total = 0
        for a in lpc_args:
            if not a.startswith("--partition="):
                continue
            seg = a[len("--partition="):].split(":")
            # 兼容四段 name:readonly:size:group 与三段 name:size:group
            if len(seg) >= 3 and seg[-1] == group:
                try:
                    total += int(seg[-2])
                except ValueError:
                    pass
        return total

    for rnd in range(1, max_rounds + 1):
        total = group_total()
        deficit = total - budget
        info("auto-fit round {}: group total {:.0f} MB / budget {:.0f} MB, deficit {:.0f} MB".format(
            rnd, total / 1048576, budget / 1048576, max(deficit, 0) / 1048576))
        log_write("auto-fit round {}: total={} budget={} deficit={}".format(rnd, total, budget, deficit))
        if deficit <= 0:
            rc = create_super_img(pack_cfg, lpc_args, counters["pack_ok"])
            if rc == 0:
                break
            if rc != 1:
                return rc
            deficit = 64 * 1024 * 1024  # 估算偏差，至少再删这些
        # erofs 对 APK 类内容压缩率有限，按 1.35 倍原始量删，循环兜底
        need_raw = int(deficit * 1.35) + 16 * 1024 * 1024
        victims, victims_bytes = _autofit_pick_victims(need_raw)
        if not victims:
            err("auto-fit: product 里已没有可删的非保护应用，仍缺 {:.0f} MB".format(deficit / 1048576))
            log_write("ERROR: auto-fit exhausted candidates, deficit={}".format(deficit))
            _autofit_report_top(keep=_autofit_keep_names())
            return 1
        for sz, child in victims:
            info("  auto-fit remove: {} ({:.1f} MB)".format(child.name, sz / 1048576))
            log_write("AUTO-FIT REMOVE: {} ({} bytes)".format(child, sz))
            try:
                shutil.rmtree(child, ignore_errors=True)
            except Exception as e:
                warn("auto-fit remove failed {}: {}".format(child, e))
            removed_all.append(child.name)
        _repack_product(pack_cfg, lpc_args, counters)
        rc = create_super_img(pack_cfg, lpc_args, counters["pack_ok"])
        if rc == 0:
            break
        if rc != 1:
            return rc
    else:
        rc = 1
    if rc == 0 and removed_all:
        ok("super.img fits after auto-fit; removed apps: {}".format(", ".join(removed_all)))
        log_write("auto-fit success, removed: {}".format(", ".join(removed_all)))
    return rc


def _autofit_report_top(keep, top=10):
    """失败时列出 product 里最大的目录（含被保护的），便于人工决策"""
    prod = SRC_FS / "product"
    entries = []
    for sub in ("data-app", "data_app", "app", "priv-app"):
        base = prod / sub
        if not base.is_dir():
            continue
        try:
            for child in base.iterdir():
                if child.is_dir():
                    entries.append((_dir_size_bytes(child), sub, child.name,
                                    child.name.lower() in keep))
        except Exception:
            pass
    entries.sort(reverse=True)
    warn("product 最大的 {} 个目录（* = 保护名单内，未删）：".format(min(top, len(entries))))
    for sz, sub, name, protected in entries[:top]:
        tag = "*" if protected else " "
        warn("  {} {:<12}{:<36}{:.0f} MB".format(tag, sub, name, sz / 1048576))
    log_write("auto-fit top dirs: " + ", ".join(
        "{}{}={}MB".format(n, "*" if p else "", int(s / 1048576)) for s, _sub, n, p in entries[:top]))


def _abort_port(auto, msg):
    # 流水线中途失败的统一出口：auto 直接返回失败码；交互提示后回菜单
    err(msg)
    log_write("ERROR: {}".format(msg))
    if auto:
        return 1
    pause()
    raise ReturnToMenu()


# ---------------- 一键移植流水线 ----------------
def one_click_port(auto=False):
    global TARGET_DEVICE

    tool_status(pause_after=False)
    if not auto:
        pause_seconds(3)

    shallow_cfg = read_shallow_section()
    shallow_flag = "  {}[on]{}".format(G, N) if shallow_cfg.get("enable", "true").lower() in (
        "true", "1", "yes", "y", "on") else "  {}[off]{}".format(R, N)

    os.system("cls" if os.name == "nt" else "clear")
    log_write("========== XMAport Session Start ==========")
    log_write("Target Device: {}".format(TARGET_DEVICE))
    print()
    print("  {}{}============================================================{}".format(C, BD, N))
    print("  {}{}  One-Click HyperOS Porting{}".format(C, BD, N))
    print("  {}{}============================================================{}".format(C, BD, N))
    print()
    print("  {}  Workflow:   {}移植 -> 精简 -> 打包{}".format(W, BD, N))
    print("  {}    Step 0{}  ROM compatibility precheck".format(G, N))
    print("  {}    Step 1{}  Download ROM packages".format(G, N))
    print("  {}    Step 2{}  Extract archives".format(G, N))
    print("  {}    Step 3{}  Extract payload (payload.bin/.dat)".format(G, N))
    print("  {}    Step 4{}  Unpack partition images".format(G, N))
    print("  {}    Step 5{}  Migrate & patch (make_hyper)".format(G, N))
    print("  {}    Step 6{}  Shallow / slim (shallow_slim){}".format(G, N, shallow_flag))
    print("  {}    Step 7{}  Pack partitions + super.img".format(G, N))
    print("  {}    Step 8{}  Summary & output".format(G, N))
    print()
    print("  " + D + "----------------------------------------------------------" + N)
    print()

    # 读取配置
    try:
        read_config()
    except ReturnToMenu:
        if auto:
            err("config.ini not found, cannot continue in auto mode")
            log_write("ERROR: config.ini missing, auto mode aborted")
            return 1
        raise
    pack_cfg = read_packing_config()

    # 输入/读取目标设备代号
    cfg_txt = WORKSPACE / "config.txt"
    if CLI_DEVICE:
        TARGET_DEVICE = CLI_DEVICE.strip()
        info("Device codename from --device: {}".format(TARGET_DEVICE))
    elif cfg_txt.exists():
        for line in cfg_txt.read_text(encoding="gbk", errors="ignore").splitlines():
            if line.startswith("TARGET_DEVICE="):
                TARGET_DEVICE = line.split("=", 1)[1].strip()
                break
    elif auto:
        err("Target device codename not provided (--device) and config.txt missing")
        log_write("ERROR: no device codename in auto mode")
        return 1
    else:
        print("  {}  Enter target device codename:{}".format(W, N))
        print("  {}  (e.g. sheng, fuxi, cupid, mondrian){}".format(D, N))
        TARGET_DEVICE = prompt("  > ").strip()

    if not is_safe_device_name(TARGET_DEVICE):
        err("Invalid device codename: only A-Z, a-z, 0-9, underscore and hyphen are allowed")
        log_write("ERROR: unsafe device codename rejected: {}".format(TARGET_DEVICE))
        if auto:
            return 1
        pause()
        raise ReturnToMenu()
    cfg_txt.write_text("TARGET_DEVICE={}\n".format(TARGET_DEVICE), encoding="gbk")

    print("  {}  Source URL:   {}{}{}".format(W, C, SRC_URL[:50], N))
    print("  {}  Target URL:   {}{}{}".format(W, C, TGT_URL[:50], N))
    print("  {}  Device:   {}{}{}".format(W, G, TARGET_DEVICE, N))
    print("  {}  Format:    {}{}{}  Compression: {}{} level {}{}  Pack super: {}{}{}".format(
        W, G, pack_cfg["format"], N, G, pack_cfg["compression"], pack_cfg["compression_level"], N,
        G, pack_cfg["pack_super"], N))
    print()
    if not auto:
        confirm = prompt("  {}Are These Right? (Y/N): {}".format(Y, N))
        if confirm.strip().lower() != "y":
            raise ReturnToMenu()
    else:
        info("Auto mode: skipping confirmation, proceeding...")

    # ---------------- Step 0: 兼容性预检 ----------------
    rc0 = run_compat_precheck(auto=auto)
    if rc0 != 0:
        if auto:
            log_write("ERROR: Step 0 aborted, exit 1")
            return 1
        raise ReturnToMenu()

    # ---------------- 磁盘空间预检（大 ROM 防爆满） ----------------
    if not check_disk_space(auto):
        if auto:
            log_write("ERROR: aborted by user (low disk space)")
            return 1
        raise ReturnToMenu()
    disk_report("pipeline start")

    # ---------------- Step 1: 下载（两包并行） ----------------
    info("=== Step 1/8: Download ROM ===")
    log_write("Step 1: Download ROM start")
    log_write("Source URL: {}".format(SRC_URL))
    log_write("Target URL: {}".format(TGT_URL))
    jobs = []
    if SRC_URL:
        jobs.append((SRC_URL, SRC_DL, "SourceROM"))
    if TGT_URL:
        jobs.append((TGT_URL, TGT_DL, "TargetROM"))
    if jobs:
        results = download_roms(jobs)
        failed = [name for name, rc in results.items() if rc != 0]
        if failed:
            err("Step 1 failed: {} download".format(" / ".join(failed)))
            log_write("ERROR: Step 1 download failed: {}".format(", ".join(failed)))
            if auto:
                return 1
            pause()
            raise ReturnToMenu()
        for d in [SRC_DL, TGT_DL]:
            try:
                for f in sorted(d.iterdir()):
                    if f.is_file():
                        info("  {}  {} bytes".format(f.name, f.stat().st_size))
            except Exception:
                pass
    info("Step 1 done")
    log_write("Step 1: Download ROM done")

    # ---------------- Step 2: 解压 ----------------
    # URL 留空 = 复用上次工作区（设计如此）：无归档可解压时跳过而非报错；
    # URL 有值时下载必然产生了新归档，解压失败才算真失败。
    info("=== Step 2/8: Extract archives ===")
    log_write("Step 2: Extract archives start")
    if SRC_URL:
        info("[1/2] Extracting source archive...")
        if extract_archive(SRC_DL, SRC_ROM, "Source") != 0:
            return _abort_port(auto, "Step 2 failed: source archive extraction")
    else:
        info("[1/2] SRC_URL empty, reusing existing source workspace")
        log_write("Step 2: SRC_URL empty, reuse source workspace")
    if TGT_URL:
        info("[2/2] Extracting target archive...")
        if extract_archive(TGT_DL, TGT_ROM, "Target") != 0:
            return _abort_port(auto, "Step 2 failed: target archive extraction")
    else:
        info("[2/2] TGT_URL empty, reusing existing target workspace")
        log_write("Step 2: TGT_URL empty, reuse target workspace")
    info("Step 2 done")
    log_write("Step 2: Extract archives done")
    # 解压完成，下载的 zip 已无用途，删除释放空间（大 ROM 时约 19GB）
    clean_download_archives()
    disk_report("after Step 2")

    # ---------------- Step 3: 解包 payload ----------------
    info("=== Step 3/8: Extract payload ===")
    log_write("Step 3: Extract payload start")
    if not check_payload_extracted(SRC_UNPACK):
        info("[1/2] Extracting source payload...")
        if extract_payload_bin(SRC_ROM, SRC_UNPACK) != 0:
            return _abort_port(auto, "Step 3 failed: source payload extraction")
        log_write("Source payload extracted to: {}".format(SRC_UNPACK))
    if not check_payload_extracted(TGT_UNPACK):
        info("[2/2] Extracting target payload...")
        if extract_payload_bin(TGT_ROM, TGT_UNPACK) != 0:
            return _abort_port(auto, "Step 3 failed: target payload extraction")
        log_write("Target payload extracted to: {}".format(TGT_UNPACK))
    info("Step 3 done")
    log_write("Step 3: Extract payload done")
    # payload 已解出 *.img，原始 payload.bin / *.dat 已无用途，删除释放空间
    clean_payload_bins()
    disk_report("after Step 3")

    # ---------------- Step 4: 解包镜像 ----------------
    info("=== Step 4/8: Unpack IMG ===")
    log_write("Step 4: Unpack IMG start")
    info("Unpacking source images...")
    if unpack_all_img(SRC_UNPACK, SRC_FS, "Source") != 0:
        return _abort_port(auto, "Step 4 failed: source image unpack")
    log_write("Source images unpacked to: {}".format(SRC_FS))
    info("Unpacking target images...")
    if unpack_all_img(TGT_UNPACK, TGT_FS, "Target") != 0:
        return _abort_port(auto, "Step 4 failed: target image unpack")
    log_write("Target images unpacked to: {}".format(TGT_FS))
    info("Step 4 done")
    log_write("Step 4: Unpack IMG done")

    # ---------------- Step 5: 迁移 ----------------
    info("=== Step 5/8: Migrate ===")
    log_write("Step 5: Migrate start")
    migrate_ok = 0
    migrate_fail = 0
    mh = TOOLS / "make_hyper.py"
    rc = run_tool([PY, str(mh), "speed"]).returncode if mh.exists() else 1
    if rc == 0:
        migrate_ok += 1
        log_write("make_hyper.py speed: SUCCESS")
    else:
        migrate_fail += 1
        return _abort_port(auto, "Step 5 failed: make_hyper.py speed returned an error (rc={})".format(rc))
    info("Step 5 done. Success: {} , Fail: {}".format(migrate_ok, migrate_fail))
    log_write("Step 5: Migrate done (OK={}, Fail={})".format(migrate_ok, migrate_fail))

    # ---------------- Step 5.5: base_first（可选，默认关闭）----------------
    # 以 310 底包为骨架重建 SRC_FS 的 system/system_ext/product 内容树。
    # 关键：只重建内容、不换目录名，后续 Step 6 shallow / fix_density /
    # pack_partitions 仍照常操作 source_filesystem，无需改动。
    if pack_cfg.get("base_first", "false").strip().lower() in (
            "true", "1", "yes", "y", "on"):
        info("=== Step 5.5/8: base_first (310 base as skeleton) ===")
        log_write("Step 5.5: base_first start")
        bf = TOOLS / "base_first_merge.py"
        if not bf.is_file():
            return _abort_port(auto, "base_first=true but tools/base_first_merge.py missing")
        rc = run_tool([PY, str(bf), "--device", TARGET_DEVICE or ""]).returncode
        if rc != 0:
            return _abort_port(auto, "base_first_merge.py returned {}".format(rc))
        info("Step 5.5 done")
        log_write("Step 5.5: base_first merge done")

    # ---------------- Step 6: 精简（移植之后、打包之前） ----------------
    info("=== Step 6/8: Shallow (slim) ===")
    log_write("Step 6: Shallow start")
    slim_rc, slim_ran = run_shallow_slim(TARGET_DEVICE, auto)
    if slim_ran:
        if slim_rc == 0:
            info("Step 6 done")
            log_write("Step 6: Shallow done")
        else:
            err("Step 6 shallow reported failures (rc={})".format(slim_rc))
            log_write("WARNING: Step 6 shallow finished with errors (rc={})".format(slim_rc))
            if slim_cfg_strict(shallow_cfg):
                return _abort_port(auto, "Step 6 failed: shallow_slim.py returned {} (strict=true)".format(slim_rc))
    else:
        log_write("Step 6: Shallow skipped (script missing)")

    # 注入 adb debug 属性（精简之后再注入，避免被后续 prop 处理覆盖）
    inject_adb_debug(pack_cfg)
    # 与已验证基线一致的 HyperOS 版本属性（odm + mi_ext 双写）
    inject_os_version_props(pack_cfg)
    # 把源包残留的 madrid 机型标识改成目标机型，避免设置参数缺项 / 自动亮度退化
    inject_product_identity_props(pack_cfg)
    # 补齐 4.0.27 基线的性能调度属性，改善开应用 / 操作卡顿
    inject_golden_perf_props(pack_cfg)
    # Android 17 振动 HAL 双实例配置
    fix_vibrator_hal_tree()
    # shennong 原生密度 560（源头在 product/etc/build.prop）
    fix_density_tree()
    # 锁屏临黑偶发闪屏三处显示配置
    fix_screen_flicker_tree()

    # ---------------- Step 7: 打包分区 + super ----------------
    info("=== Step 7/8: Pack partitions ===")
    info("Format: {} , Compression: {} level {}".format(
        pack_cfg["format"], pack_cfg["compression"], pack_cfg["compression_level"]))
    info("Cleaning packed directory...")
    for old in PACK_OUT.glob("*.img"):
        try:
            old.unlink()
        except Exception:
            pass

    # 传递打包环境变量给 pack_partitions.py
    os.environ["XMAPORT_UTC_STAMP"] = str(pack_cfg.get("utc_stamp", ""))
    os.environ["XMAPORT_EROFS_LEGACY"] = str(pack_cfg.get("erofs_old_kernel", "false"))
    os.environ["XMAPORT_IS_SKIP_APEX"] = str(pack_cfg.get("is_skip_apex", "false"))
    sync_status = WORKSPACE / "sync_apex_changed.txt"
    sync_apex_changed = "false"
    try:
        sync_apex_changed = sync_status.read_text(encoding="ascii").strip().lower()
    except Exception:
        pass
    os.environ["XMAPORT_SYNC_APEX_CHANGED"] = "true" if sync_apex_changed == "true" else "false"
    os.environ["XMAPORT_USE_LEGACY_EROFS"] = detect_legacy_erofs_marker()

    # 打包前校验分区镜像格式（只警告不阻断）
    info("Checking original partition image formats...")
    try:
        subprocess.run([
            PY, str(TOOLS / "check_img_format.py"), pack_cfg["format"],
            str(SRC_UNPACK / "system.img"), str(SRC_UNPACK / "system_ext.img"),
            str(SRC_UNPACK / "product.img"), str(TGT_UNPACK / "odm.img"),
        ])
    except Exception:
        pass
    log_write("Partition image format check done (expected: {})".format(pack_cfg["format"]))

    counters = {"pack_ok": 0, "pack_fail": 0}
    port_failed = False
    lpc_args = []

    # 打包源分区 system / system_ext / product
    log_write("Packing source partitions: system, system_ext, product")
    is_skip_apex = pack_cfg.get("is_skip_apex", "false").lower() == "true"
    for part in PACK_SOURCE_PARTS:
        if part == "system_ext" and is_skip_apex:
            info("is_skip_apex=true: system_ext 跳过重新打包，将直接复制源 payload")
            continue
        pack_one_partition(part, SRC_FS, pack_cfg, lpc_args, counters)

    # is_skip_apex=true 时直接复制源 system_ext.img
    if is_skip_apex:
        log_write("Copying source system_ext.img (is_skip_apex=true)")
        copy_partition_image("system_ext", SRC_UNPACK / "system_ext.img", pack_cfg, lpc_args, counters)

    # 打包目标 odm（始终运行）
    log_write("Packing odm from target filesystem")
    info("Packing partition: odm")
    if (TGT_FS / "odm").exists():
        pack_one_partition("odm", TGT_FS, pack_cfg, lpc_args, counters)
    else:
        err("odm not found in target filesystem")
        log_write("ERROR: odm not found in target filesystem")
        counters["pack_fail"] += 1
        port_failed = True
        pause()

    # mi_ext 必须来自底包(目标 payload)：内含机型专属的 init rc / device_features /
    # framework 扩展。用源包 madrid 的 mi_ext 配 shennong 的 odm/vendor 会导致早期
    # init 崩溃并回落 fastboot。参照 多组已验证基线镜像，
    # mi_ext_a 固定都是 106156032 字节 = 底包 mi_ext，与源 ROM 无关。
    mi_ext_src = TGT_UNPACK / "mi_ext.img"
    if mi_ext_src.is_file():
        log_write("Copying mi_ext from TARGET payload (device-specific)")
        info("Adding mi_ext from target payload (device-specific)...")
    else:
        warn("target payload 无 mi_ext.img, 回退到源包(可能不开机)")
        log_write("WARNING: mi_ext fallback to SOURCE payload (likely unbootable)")
        mi_ext_src = SRC_UNPACK / "mi_ext.img"
    copy_partition_image("mi_ext", mi_ext_src, pack_cfg, lpc_args, counters)

    # 处理 vendor：MTK 从 target filesystem 重新打包；高通直接复制目标 payload
    device_platform = pack_cfg.get("device_platform", "qualcomm").lower()
    if device_platform == "mtk":
        # unpack_all_img 不包含 vendor，需补充解包到 target_filesystem
        if not (TGT_FS / "vendor").exists():
            log_write("MTK: extracting vendor.img to target filesystem")
            info("Extracting vendor.img for MTK vendor repack...")
            img = TGT_UNPACK / "vendor.img"
            if img.exists():
                os.makedirs(TGT_FS / "vendor", exist_ok=True)
                run_tool([PY, str(TOOLS / "extract_img.py"), str(img), str(TGT_FS / "vendor")])
        log_write("Packing vendor from target filesystem (MTK)")
        info("Packing partition: vendor (MTK)")
        if (TGT_FS / "vendor").exists():
            pack_one_partition("vendor", TGT_FS, pack_cfg, lpc_args, counters)
        else:
            err("vendor not found in target filesystem")
            log_write("ERROR: vendor not found in target filesystem")
            counters["pack_fail"] += 1
            port_failed = True
            pause()
    else:
        log_write("Copying vendor from target payload")
        info("Adding vendor from target payload...")
        copy_partition_image("vendor", TGT_UNPACK / "vendor.img", pack_cfg, lpc_args, counters)

    # 复制 vendor_dlkm（目标 payload）
    if (TGT_UNPACK / "vendor_dlkm.img").exists():
        copy_partition_image("vendor_dlkm", TGT_UNPACK / "vendor_dlkm.img", pack_cfg, lpc_args, counters)

    # 复制 system_dlkm（目标 payload；可启动的参照包 super 元数据里含 system_dlkm_a）
    if (TGT_UNPACK / "system_dlkm.img").exists():
        copy_partition_image("system_dlkm", TGT_UNPACK / "system_dlkm.img", pack_cfg, lpc_args, counters)

    # 生成 super.img；空间不足(rc=1)时：extreme 精简重试 → auto-fit 按缺口迭代删减
    log_write("Creating super.img (pack_super={})".format(pack_cfg.get("pack_super", "false")))
    super_rc = create_super_img(pack_cfg, lpc_args, counters["pack_ok"])
    if super_rc == 1:
        info("Triggering extreme slimming mode (make_hyper.py extreme)...")
        log_write("Super space insufficient, running extreme slimming")
        if run_tool([PY, str(TOOLS / "make_hyper.py"), "extreme"]).returncode != 0:
            err("make_hyper.py extreme failed, skip to auto-fit")
            log_write("ERROR: make_hyper.py extreme failed")
        else:
            info("Re-packing product partition after extreme slimming...")
            _repack_product(pack_cfg, lpc_args, counters)
            info("Retrying super.img creation...")
            log_write("Retrying super.img after extreme slimming")
            super_rc = create_super_img(pack_cfg, lpc_args, counters["pack_ok"])
    if super_rc == 1 and pack_cfg.get("auto_fit", "true").lower() == "true":
        info("Triggering auto-fit: remove largest unprotected apps until super fits...")
        log_write("Super still insufficient, running auto-fit")
        super_rc = auto_fit_super(pack_cfg, lpc_args, counters)
    if super_rc != 0:
        port_failed = True
        if super_rc == 1:
            err("super.img failed: still insufficient space after slimming; "
                "see workspace log for auto-fit details")
            log_write("ERROR: super.img failed - space insufficient after all retries")
        else:
            err("super.img creation failed (rc={})".format(super_rc))
            log_write("ERROR: super.img creation failed (rc={})".format(super_rc))
        # 删掉可能残留的不完整 super.img，避免误刷
        try:
            if (PACK_OUT / "super.img").exists():
                (PACK_OUT / "super.img").unlink()
                warn("Removed incomplete super.img to avoid flashing a corrupt image")
                log_write("WARNING: incomplete super.img removed")
        except Exception:
            pass

    # vbmeta 禁验（在 super 打包之后、汇总之前，不受 pack_super 限制）
    if not patch_vbmeta(pack_cfg):
        port_failed = True

    # ---------------- Step 7.5: 线刷+卡刷一体包 ----------------
    # 先回收空间: super.img 已合并全部动态分区, 一体包只需要固件类镜像。
    # 不回收的话 simg2img 转 raw super 时会磁盘爆满(上次 CI 实测)。
    clean_pre_flashpack_imgs()
    disk_report("before flashpack")
    flashpack_failed = False
    if pack_cfg.get("build_flashpack", "true").lower() == "true" \
            and (PACK_OUT / "super.img").exists():
        info("=== Building fastboot+recovery flash pack ===")
        log_write("Building flash pack (build_flashpack.py)")
        if run_tool([PY, str(TOOLS / "build_flashpack.py")]).returncode == 0:
            ok("Flash pack ready: workspace/flashpack/")
            log_write("Flash pack built OK")
        else:
            # 2026-11-16: 本项目核心交付物就是「线卡一体包」，flashpack 失败
            # 不能只 warn 后继续 —— 那样 CI 会显示成功却只发布 super 分卷，
            # 用户拿不到刷机脚本（实机踩过）。改为硬失败，让流水线红。
            flashpack_failed = True
            err("flashpack build FAILED (line-card one-package is required)")
            log_write("ERROR: flashpack build failed")

    # 打包完成，*_payload 下的分区镜像已复制/重打包进 packed，删除释放空间
    clean_unpacked_imgs()
    disk_report("after Step 7")

    # ---------------- Step 8: 汇总 ----------------
    port_ok = (not port_failed) and counters["pack_fail"] == 0 and not flashpack_failed
    log_write("========== Porting {} ==========".format(
        "Complete" if port_ok else "Finished With Errors"))
    log_write("Total packed: {} partitions, {} failed".format(counters["pack_ok"], counters["pack_fail"]))
    log_write("Step 7: Pack partitions done")
    log_write("Pack OK={}, Fail={}".format(counters["pack_ok"], counters["pack_fail"]))
    print()
    print("  {}{}============================================================{}".format(C, BD, N))
    if port_ok:
        print("  {}{}Porting Complete!{}".format(G, BD, N))
    else:
        print("  {}{}Porting Finished With Errors!{}".format(R, BD, N))
    print("  {}{}============================================================{}".format(C, BD, N))
    print()
    if not port_ok:
        print("  {}  Pack OK={}, Fail={} -- check workspace log for details{}".format(
            Y, counters["pack_ok"], counters["pack_fail"], N))
        print()
    print("  {}  Source FS:    {}{}{}".format(W, C, SRC_FS, N))
    print("  {}  Target FS:    {}{}{}".format(W, C, TGT_FS, N))
    if (PACK_OUT / "super.img").exists():
        print("  {}  Output:       {}{}{}".format(W, C, str(PACK_OUT / "super.img"), N))
    else:
        print("  {}  Output:       {}(super.img NOT generated - pack failed){}".format(W, R, N))
    print()
    for part in ALL_OUTPUT_PARTS:
        img = PACK_OUT / (part + ".img")
        if img.exists():
            print("  {}    {}.img  {} bytes{}".format(G, part, img.stat().st_size, N))

    # ROM Info（从源分区 build.prop 读取）
    rom_info_lines = []
    print()
    rom_info_lines.append("---------- ROM Info ----------")
    print("  {}  ---------- ROM Info ----------{}".format(C, N))
    bp_candidates = [
        SRC_FS / "odm" / "etc" / "build.prop",
        SRC_FS / "product" / "etc" / "build.prop",
        SRC_FS / "system" / "system" / "build.prop",
    ]
    bp_path = None
    for cand in bp_candidates:
        if cand.exists():
            bp_path = cand
            break
    if bp_path is None:
        print("  {}  [ERR] build.prop not found{}".format(R, N))
        rom_info_lines.append("[ERR] build.prop not found")
    else:
        try:
            props_text = bp_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            props_text = ""
        for key in [
            "ro.product.odm.device", "ro.product.odm.model",
            "ro.product.odm.marketname", "ro.product.odm.brand",
            "ro.product.odm.name", "ro.product.odm.manufacturer",
        ]:
            label = key.split(".")[-1]
            if label == "manufacturer":
                label = "vendor"
            for line in props_text.splitlines():
                if "=" in line and line.split("=", 1)[0].strip() == key:
                    val = line.split("=", 1)[1].strip()
                    print("  {}  {}: {}{}{}".format(W, label.ljust(10), C, val, N))
                    rom_info_lines.append("{}: {}".format(label, val))
                    break
    print("  {}  ------------------------------{}".format(C, N))
    rom_info_lines.append("-------------------------------")
    if AUTO_MODE:
        try:
            (WORKSPACE / "rom_info.txt").write_text(
                "\n".join(rom_info_lines), encoding="utf-8")
        except Exception:
            pass
    print()
    print("  {}{}============================================================{}".format(C, BD, N))
    print()
    if not auto:
        pause()
    return 0 if port_ok else 1


# ---------------- 全局崩溃报告 ----------------
def crash_report(exc_type, exc, tb):
    # KeyboardInterrupt 不算崩溃
    if exc_type is KeyboardInterrupt:
        print()
        sys.exit(0)
    lineno = "?"
    module = "?"
    if tb is not None:
        while tb.tb_next is not None:
            tb = tb.tb_next
        lineno = tb.tb_lineno
        module = tb.tb_frame.f_globals.get("__name__", "?")
    details = [
        "",
        "  {}============================================================{}".format(R, N),
        "  {}  XMAPORT CRASHED{}".format(R, N),
        "  {}============================================================{}".format(R, N),
        "  {}  Error type:{} {}".format(W, Y, exc_type.__name__ + N),
        "  {}  Message:   {}{}".format(W, C, str(exc) + N),
        "  {}  Location:  {}{}:{}".format(W, D, module, str(lineno) + N),
        "  {}  Platform:  {}{}".format(W, D, platform.platform() + N),
        "  {}  Python:    {}{}".format(W, D, sys.version.split()[0] + N),
        "  {}  Details have been written to the log file{}".format(Y, N),
        "  {}============================================================{}".format(R, N),
        "",
    ]
    for line in details:
        try:
            print(line, flush=True)
        except Exception:
            pass
    log_write("CRASH: {}: {} ({}:{})".format(exc_type.__name__, exc, module, lineno))


sys.excepthook = crash_report


# ---------------- [A] 命令执行器 ----------------
def compose_super():
    # 用 workspace/packed 现有分区镜像合成 super.img：
    # 检测 packed 分区 → 从 config.ini 读取 super 参数 → lpmake 合成。
    os.system("cls" if os.name == "nt" else "clear")
    print()
    print("  {}{}============================================================{}".format(C, BD, N))
    print("  {}{}  Compose super.img{}".format(C, BD, N))
    print("  {}{}============================================================{}".format(C, BD, N))
    print()
    pack_cfg = read_packing_config()
    lpc_args = []
    counters = {"pack_ok": 0, "pack_fail": 0}
    found = []
    for part in ALL_OUTPUT_PARTS:
        img = PACK_OUT / (part + ".img")
        if img.exists():
            _append_lpc_entry(lpc_args, part, img, img.stat().st_size, pack_cfg)
            counters["pack_ok"] += 1
            found.append("{}  {} bytes".format(part + ".img", img.stat().st_size))
    if not found:
        err("No packed partition images found in workspace\\packed")
        err("Run a port first (menu [1]), then compose super.img here")
        log_write("compose_super aborted: no partition images in packed")
        pause()
        return
    print("  {}  Partitions found in packed:{}{}".format(W, G, N))
    for line in found:
        print("  {}    - {}{}".format(W, line, N))
    print("  {}  super: device_size={}  metadata={}/{} slots  group={}  virtual_ab={}  sparse={}".format(
        W, pack_cfg.get("device_size", "?"), pack_cfg.get("metadata_size", "?"),
        pack_cfg.get("metadata_slots", "?"), pack_cfg.get("super_group", "?"),
        pack_cfg.get("virtual_ab", "?"), pack_cfg.get("sparse", "?"), N))
    print()
    try:
        (PACK_OUT / "super.img").unlink()
    except Exception:
        pass
    # create_super_img 内部按 pack_super 配置决定是否跳过，此处强制执行合成
    pack_cfg["pack_super"] = "true"
    rc = create_super_img(pack_cfg, lpc_args, counters["pack_ok"])
    sup = PACK_OUT / "super.img"
    if rc == 0 and sup.exists():
        print()
        print("  {}  super.img ready: {} bytes{}".format(G, sup.stat().st_size, N))
        log_write("compose_super OK: {} bytes".format(sup.stat().st_size))
    elif rc == 1:
        # 单独合成时不自动触发极限精简重试，只提醒用户
        print()
        err("super space insufficient, compose aborted")
        print("  {}  Tip: run extreme slimming (python tools/make_hyper.py extreme),{}"
              .format(Y, N))
        print("  {}  then re-pack and re-run this compose, or enlarge device_size in config.ini{}".format(Y, N))
        log_write("compose_super: super space insufficient (rc=1), no auto retry")
    elif rc != 0:
        log_write("compose_super failed: rc={}".format(rc))
    pause()


def show_executor():
    os.system("cls" if os.name == "nt" else "clear")
    print()
    print("  {}{}============================================================{}".format(C, BD, N))
    print("  {}{}  Command Executor{}".format(C, BD, N))
    print("  {}{}============================================================{}".format(C, BD, N))
    print()
    print("  {}  [1] Compose super.img{}".format(W, N))
    print("  {}      Pack existing partitions in workspace\\packed into super.img{}".format(D, N))
    print("  {}  [0] Back{}".format(W, N))
    print()
    ch = prompt("  {}Select [0-1]: {}".format(Y, N)).strip()
    if ch == "1":
        compose_super()


# ---------------- 主菜单 ----------------
def print_banner():
    print()
    print("  {}{}============================================================{}".format(C, BD, N))
    print("  {}{}    __   __ __  __     _     ____    ___   ____   _____ {}".format(C, BD, N))
    print("  {}{}    \\ \\ / /|  \\/  |   / \\   |  _ \\  / _ \\ |  _ \\ |_   _|{}".format(C, BD, N))
    print("  {}{}     \\ V / | |\\/| |  / _ \\  | |_) || | | || |_) |  | |  {}".format(C, BD, N))
    print("  {}{}     /   \\ | |  | | / ___ \\ |  __/ | |_| ||  _ <   | |  {}".format(C, BD, N))
    print("  {}{}    /_/ \\_\\|_|  |_|/_/   \\_\\|_|     \\___/ |_| \\_\\  |_|  {}".format(C, BD, N))
    print("  {}{}============================================================{}".format(C, BD, N))


def show_menu():
    os.system("cls" if os.name == "nt" else "clear")
    print_banner()
    print()
    print("  {}{}  [1] One-Click Port HyperOS{}     {}下载→移植→精简→打包{}".format(G, BD, N, D, N))
    print()
    print("  {}{}  -- Tools --{}".format(Y, BD, N))
    print("  {}  [S] Run shallow/slim only{}      {}仅执行精简阶段{}".format(W, N, D, N))
    print("  {}  [A] Command Executor{}".format(W, N))
    print("  {}  [C] Bundled Components{}".format(W, N))
    print("  {}  [D] Clean workspace{}".format(W, N))
    print()
    print("  {}{}============================================================{}".format(C, BD, N))
    print()
    choice = prompt("  {}{}Select [1, S, A-D]: {}".format(Y, BD, N))
    return choice.strip().lower()


# ---------------- CLI 参数解析 ----------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="XMAPort - Android ROM porting tool",
    )
    parser.add_argument("--auto", action="store_true",
                        help="Non-interactive mode for CI/GitHub Actions")
    parser.add_argument("--device", default="",
                        help="Target device codename (overrides config.txt)")
    parser.add_argument("--source", default="",
                        help="Source ROM URL (overrides config.ini [source] url)")
    parser.add_argument("--target", default="",
                        help="Target ROM URL (overrides config.ini [target] url)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip confirmation prompts")
    parser.add_argument("--skip-shallow", action="store_true",
                        help="Skip the slim stage (Step 6) even if enabled in config.ini")
    parser.add_argument("--slim-only", action="store_true",
                        help="Run only the slim stage (Step 6) then exit")
    return parser.parse_args()


def main():
    global CLI_SOURCE_URL, CLI_TARGET_URL, CLI_DEVICE, SHALLOW_ENABLED
    args = parse_args()

    # CLI 覆盖
    CLI_SOURCE_URL = args.source
    CLI_TARGET_URL = args.target
    CLI_DEVICE = args.device
    SHALLOW_ENABLED = not args.skip_shallow

    # 初始化控制台与目录
    init_console(auto=args.auto)
    try:
        os.chdir(ROOT)
    except Exception:
        pass
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)

    if args.slim_only:
        # 只跑精简阶段（调试清单 / 已移植完毕想补精简时用）
        log_write("========== Slim-only mode ==========")
        device = CLI_DEVICE or read_saved_device()
        info("Device: {}".format(device or "<unknown>"))
        slim, _ran = run_shallow_slim(device, auto=True)
        sys.exit(slim if slim is not None else 0)

    if args.auto:
        log_write("========== XMAport Auto Mode Start ==========")
        try:
            result = one_click_port(auto=True)
        except ReturnToMenu:
            err("Workflow aborted (ReturnToMenu) in auto mode")
            log_write("ERROR: auto mode aborted via ReturnToMenu")
            result = 1
        sys.exit(result if result is not None else 0)

    while True:
        try:
            ch = show_menu()
        except KeyboardInterrupt:
            print()
            break

        if ch == "1":
            try:
                one_click_port()
            except ReturnToMenu:
                continue
            except KeyboardInterrupt:
                print()
                continue
        elif ch == "s":
            slim_menu()
        elif ch == "a":
            show_executor()
        elif ch == "c":
            show_credits()
        elif ch == "d":
            clean_workspace()
        else:
            print("  {}  Invalid input{}".format(R, N))
            pause_seconds(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(0)
