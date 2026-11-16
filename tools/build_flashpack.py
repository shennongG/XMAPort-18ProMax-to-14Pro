# -*- coding: utf-8 -*-
"""
XMAPort FlashPack —— 线刷(fastboot) + 卡刷(recovery) 一体包构建
打包结构:

    XMAPort_<device>_<version>_<md5前10位>.zip
    ├── XMAPort-Flash.bat                  # Windows 线刷脚本 (fastboot, GBK)
    ├── bin/Windows/...                    # adb / fastboot / zstd.exe (来自 tools/flashtools.zip)
    ├── bin/android/{busybox,zstd}         # recovery 内用 (来自 tools/flashtools.zip)
    ├── images/*.img                       # 固件镜像(底包) + 补丁后的 vbmeta + super.img.zst
    ├── META-INF/com/google/android/update-binary   # 卡刷安装脚本 (自动生成)
    ├── RootManager/                       # 可选: root 管理器 APK
    └── README-FLASH.txt

用法:
    python tools/build_flashpack.py                 # 构建并打 zip
    python tools/build_flashpack.py --keep-raw      # 保留解包后的目录版本
    python tools/build_flashpack.py --patcherror    # 标记打包有错(卡刷脚本会拒绝刷入)
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
WORKSPACE = ROOT / "workspace"
PACK_OUT = WORKSPACE / "packed"
TGT_UNPACK = WORKSPACE / "target_payload"
TGT_ROM = WORKSPACE / "target_rom"
SRC_FS = WORKSPACE / "source_filesystem"
TGT_FS = WORKSPACE / "target_filesystem"
DEVICES_DIR = ROOT / "devices"
FLASH_DIR = WORKSPACE / "flashpack"
FLASHTOOLS_ZIP = TOOLS / "flashtools.zip"
FLASHTOOLS_DIR = TOOLS / "flashtools"
FLASH_BAT_TEMPLATE = TOOLS / "offline_flashpack" / "XMAPort-Flash.template.bat"

R, G, Y, C, N = ("\x1b[91m", "\x1b[92m", "\x1b[93m", "\x1b[96m", "\x1b[0m") \
    if sys.stdout.isatty() and os.name != "nt" else ("", "", "", "", "")


def info(msg):
    print("{}[INFO]{} {}".format(C, N, msg), flush=True)


def ok(msg):
    print("{}[OK]{} {}".format(G, N, msg), flush=True)


def warn(msg):
    print("{}[WARN]{} {}".format(Y, N, msg), flush=True)


def err(msg):
    print("{}[ERROR]{} {}".format(R, N, msg), flush=True)


# super.img 里已包含的动态分区 —— 不单独放进 images/
DYNAMIC_PARTS = {
    "system", "system_ext", "product", "odm", "vendor", "vendor_dlkm",
    "odm_dlkm", "system_dlkm", "mi_ext", "mi_product",
}
# 线刷顺序: 固件类先刷, boot 类其次, vbmeta 禁验, super 最后
BOOT_LIKE = {"boot", "init_boot", "dtbo", "vendor_boot", "recovery"}


def run_cmd(cmd, **kw):
    try:
        return subprocess.run(cmd, **kw).returncode
    except OSError as e:
        err("命令执行失败 {}: {}".format(cmd[0], e))
        return 1


def verify_vbmeta(images_dir):
    """顶层 vbmeta 必须是「未签名 + flags=3」的空白禁用镜像，否则禁止出包。

    2026-11-05 shennong 实机确认：在已签名 vbmeta 上直接改 flags 字节会破坏
    AVB 签名，ABL 校验失败后设备秒回 fastboot（连 recovery / fastbootd 都进不去，
    pstore 为空）。这是「刷机全绿但无法开机」的真正根因。
    """
    p = images_dir / "vbmeta.img"
    if not p.is_file():
        warn("包内缺少 vbmeta.img，跳过 AVB 校验")
        return True
    h = p.read_bytes()[:128]
    if h[:4] != b"AVB0":
        err("images/vbmeta.img 不是合法 AVB 镜像")
        return False
    algo = int.from_bytes(h[28:32], "big")
    flags = int.from_bytes(h[120:124], "big")
    if algo != 0 or flags != 3:
        err("images/vbmeta.img 是签名/未完全禁用的镜像 (algo={}, flags={})".format(algo, flags))
        err("刷入后 ABL 校验失败会直接回落 fastboot，已中止打包。")
        err("建议: 把 tools/assets/vbmeta_disabled.img 复制为 images/vbmeta.img")
        return False
    ok("vbmeta 校验通过: 未签名 + flags=3 (ABL 不会因签名失败拒绝启动)")
    return True


def _target_rom_url():
    """读 config.ini [target] 的底包链接（编码兼容 UTF-8 / GBK）。"""
    ini = ROOT / "config.ini"
    if not ini.is_file():
        return ""
    text = None
    for enc in ("utf-8-sig", "gbk"):
        try:
            text = ini.read_bytes().decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return ""
    section = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip().lower()
        elif section == "target" and s.lower().startswith("url="):
            return s[4:].strip()
    return ""


def _firmware_baseline(device):
    """按底包版本挑固件基线：309 配 4.0.27（真机验证），310 配 4.0.29（自洽校验）。"""
    dev_dir = DEVICES_DIR / (device or "shennong")
    m = re.search(r"/(OS[0-9.]+[A-Za-z0-9.]*)/", _target_rom_url())
    if m:
        cand = dev_dir / "firmware_baseline_{}.md5".format(m.group(1))
        if cand.is_file():
            return cand
    return dev_dir / "firmware_baseline.md5"


def verify_firmware(images_dir, device):
    """底层固件必须与所选底包的基准一致，否则禁止出包。

    2026-11-05 shennong 实机确认：OS3.0.310 底包的 27 个底层固件分区
    (abl/xbl/tz/hyp/modem...) 与 OS3.0.309 基线全不同。混入不同版本
    的固件跑 Android 17 boot 链，会出现刷写全绿但 40-100 秒回 fastboot。

    2026-11-06 更正：310 不是「ABL 拒绝加载」——真机 dmesg 里有完整的
    Linux 6.1.138 启动日志和 init 第二阶段的输出。310 缺的是配套 userspace
    兼容层（VNDK v30~v33 + 30.0 SELinux mapping），见 devices/shennong/compat/。
    """
    baseline = _firmware_baseline(device)
    script = TOOLS / "verify_firmware.py"
    if not baseline.is_file() or not script.is_file():
        warn("固件基准清单/校验脚本缺失，跳过固件校验")
        return True
    if os.environ.get("XMAPORT_SKIP_FIRMWARE_CHECK") == "1":
        warn("XMAPORT_SKIP_FIRMWARE_CHECK=1，已明确跳过固件校验（仅限调试）")
        return True
    info("校验底层固件是否等于底包基准 ({}) ...".format(baseline.name))
    rc = subprocess.run(
        [sys.executable, str(script), str(images_dir), "--baseline", str(baseline)],
        cwd=str(ROOT)).returncode
    if rc != 0:
        err("底层固件校验失败 —— 包里混入了非本底包的固件，刷入后可能回 fastboot。")
        err("建议: 确认 config.ini [target] 的底包链接与 devices/<device>/")
        err("      firmware_baseline_<版本>.md5 一一对应，且打包取的是该 payload 的固件。")
        err("调试时可设 XMAPORT_SKIP_FIRMWARE_CHECK=1 跳过（风险自负）。")
        return False
    ok("底层固件校验通过: 与 {} 逐字节一致".format(baseline.name))
    return True


def port_device_from_config():
    """从 config.ini [source] 的 OTA 直链解析源机型代号。

    用于刷机脚本 ROM INFO 的「源 ROM」一行 —— 版本信息一律来自本次构建
    配置，不写死在脚本里（与离线一体包的构建显示保持一致）。
    """
    try:
        text = (ROOT / "config.ini").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    sec = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            sec = s[1:-1].lower()
            continue
        if sec == "source" and s.startswith("url="):
            m = re.search(r"/([a-z0-9_]+)-ota_full-", s)
            return m.group(1) if m else ""
    return ""


def read_device():
    cfg = WORKSPACE / "config.txt"
    if cfg.is_file():
        try:
            for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip().startswith("TARGET_DEVICE="):
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return ""


def read_prop(base, part, key):
    """从已解包文件系统的 build.prop 里读属性"""
    candidates = [base / part / "etc" / "build.prop", base / part / "build.prop"]
    for bp in candidates:
        try:
            if not bp.is_file():
                continue
            for line in bp.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return ""


def collect_images(device):
    """收集要放进 images/ 的镜像, 返回 {分区名: 源路径}。优先级: packed > payload > firmware-update"""
    result = {}

    def add_from(dirpath, overwrite=False):
        if not dirpath.is_dir():
            return
        for img in sorted(dirpath.glob("*.img")):
            pname = img.stem
            if pname in DYNAMIC_PARTS or pname == "super":
                continue
            if overwrite or pname not in result:
                result[pname] = img

    # 1) 打包产物里的补丁 vbmeta 等优先
    add_from(PACK_OUT, overwrite=True)
    # 2) payload 解包产物 (boot/init_boot/dtbo/vendor_boot/recovery/固件...)
    add_from(TGT_UNPACK)
    # 3) 底包 ROM 解压出来的 firmware-update 目录
    for cand in (TGT_ROM / "firmware-update", TGT_UNPACK / "firmware-update"):
        add_from(cand)

    # 可选: 机型目录里的 TWRP 与 root 变体镜像 / 管理器 APK
    dev_dir = DEVICES_DIR / device if device else None
    if dev_dir:
        if (dev_dir / "recovery_twrp.img").is_file():
            result["recovery_twrp"] = dev_dir / "recovery_twrp.img"
        root_dir = dev_dir / "root"
        if root_dir.is_dir():
            for img in sorted(root_dir.glob("*.img")):
                result[img.stem] = img
    return result


def prepare_bin_tools(staging):
    """优先从 tools/flashtools/ 散文件复制；缺失时回退 flashtools.zip。

    输出为 staging/bin/Windows 与 staging/bin/android，
    与 XMAPort-Flash.template.bat 里的 bin/Windows/ 路径一致。
    """
    extracted = {}
    if FLASHTOOLS_DIR.is_dir():
        for sub in ("Windows", "android"):
            src_dir = FLASHTOOLS_DIR / sub
            if not src_dir.is_dir():
                continue
            for f in sorted(src_dir.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(FLASHTOOLS_DIR)
                dst = staging / "bin" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                if os.name != "nt" and sub == "android":
                    os.chmod(dst, 0o755)
                extracted["bin/" + rel.as_posix()] = dst
        if extracted:
            ok("刷机工具: tools/flashtools/ 散文件 {} 个".format(len(extracted)))
            return extracted
    if not FLASHTOOLS_ZIP.is_file():
        warn("tools/flashtools/ 与 flashtools.zip 都不存在, 跳过刷机工具 —— 线刷脚本将不可用")
        return {}
    extracted = {}
    with zipfile.ZipFile(FLASHTOOLS_ZIP) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            if name.startswith("bin/Windows/") or name.startswith("bin/android/"):
                rel = name[len("bin/"):]
                dst = staging / "bin" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)
                if os.name != "nt" and name.startswith("bin/android/"):
                    os.chmod(dst, 0o755)
                extracted[name] = dst
    return extracted


def find_zstd(staging):
    """按平台选 zstd: Windows 只用 exe, Linux 用 ELF/PATH"""
    win = staging / "bin" / "Windows" / "zstd.exe"
    if os.name == "nt":
        return win if win.is_file() else shutil.which("zstd")
    cand = TOOLS / "linux" / "zstd"
    if cand.is_file():
        return cand
    win2 = staging / "bin" / "Windows" / "zstd.exe"
    return win2 if win2.is_file() else shutil.which("zstd")


def prepare_twrp(staging, device):
    """把第三方 TWRP 放进 images/recovery_twrp.img。

    GitHub 单文件 100 MB 限制：devices/<device>/recovery_twrp.img 以
    .img.zst 归档（约 47 MB），出包时解压回原始 boot 镜像。
    主线刷模板**强制**刷入 TWRP，缺失即拒绝出包。
    """
    dst = staging / "images" / "recovery_twrp.img"
    if dst.is_file():
        return True
    dev = DEVICES_DIR / device if device else None
    src = dev / "recovery_twrp.img" if dev else None
    if src and src.is_file():
        shutil.copy2(src, dst)
        ok("TWRP: devices/{}/recovery_twrp.img".format(device))
        return True
    zst = dev / "recovery_twrp.img.zst" if dev else None
    if not (zst and zst.is_file()):
        err("缺少 devices/{}/recovery_twrp.img(.zst) —— 线刷模板强制刷入 TWRP，拒绝出包".format(device))
        return False
    zstd = find_zstd(staging)
    if not zstd:
        err("找不到 zstd，无法解压 TWRP 镜像")
        return False
    info("解压 TWRP 镜像 ({:.1f} MB) ...".format(zst.stat().st_size / 1048576))
    if run_cmd([str(zstd), "-d", "-f", "-q", "-T0", str(zst), "-o", str(dst)]) != 0:
        err("TWRP 镜像解压失败")
        return False
    with open(dst, "rb") as f:
        magic = f.read(8)
    if magic[:8] != b"ANDROID!":
        err("解压产物不是 Android boot/recovery 镜像 (magic={!r})".format(magic))
        dst.unlink(missing_ok=True)
        return False
    ok("TWRP: 解压完成 ({:.1f} MB)".format(dst.stat().st_size / 1048576))
    return True


def gen_force_twrp_bat(staging):
    """生成 2-Force-TWRP.bat：救砖用，强制双槽刷 TWRP 并进 TWRP 重建 /data。

    线刷 super 后开机闪退 / 卡 LOGO / 回 recovery 时，无需重刷 8GB 的 super，
    直接把 TWRP 刷进两个槽位，进 TWRP 执行 format data 即可恢复。
    """
    L = []
    a = L.append
    a("@echo off")
    a('cd /d "%~dp0"')
    a('set BIN=bin\\Windows')
    a('if not exist "%BIN%\\fastboot.exe" set BIN=bin\\platform-tools-windows')
    a('if not exist "%BIN%\\fastboot.exe" (')
    a("  echo.未找到 fastboot.exe，请完整解压刷机包后再运行")
    a("  pause")
    a("  exit /b 1")
    a(")")
    a('if not exist images\\recovery_twrp.img (')
    a("  echo.未找到 images\\recovery_twrp.img，无法强制刷入 TWRP")
    a("  pause")
    a("  exit /b 1")
    a(")")
    a("setlocal enabledelayedexpansion")
    a("echo.")
    a("echo.  ============================================")
    a("echo.   强制刷入 TWRP 第三方 Recovery + 救砖")
    a("echo.  ============================================")
    a("echo.")
    a("echo.  适用: 刷完 super 后开机闪退 / 卡 LOGO / 回 recovery")
    a("echo.  作用: TWRP 刷入两个槽位并直接进入 TWRP, 可重建 /data")
    a("echo.")
    a("echo.  请关机后按住 音量下 + 电源 进入 fastboot 并连接电脑")
    a("pause")
    a("echo.")
    a('%BIN%\\fastboot devices 2>&1 |findstr /r /c:"fastboot" >nul || (')
    a("  echo.未检测到 fastboot 设备，请检查驱动与数据线")
    a("  pause")
    a("  exit /b 1")
    a(")")
    a("echo.已检测到 fastboot 设备，开始强制刷入 TWRP...")
    a("echo.")
    a("echo.[1/3] 双槽刷入 recovery_twrp.img")
    a("set TWRP_OK=0")
    a('%BIN%\\fastboot flash recovery_ab images/recovery_twrp.img && set TWRP_OK=1')
    a('if "!TWRP_OK!"=="0" (')
    a("  echo.recovery_ab 不可用，改为逐槽刷入")
    a('  %BIN%\\fastboot flash recovery_a images/recovery_twrp.img')
    a('  %BIN%\\fastboot flash recovery_b images/recovery_twrp.img')
    a(")")
    a("echo.")
    a("echo.[2/3] 切到 a 槽并重启进 TWRP")
    a("%BIN%\\fastboot set_active a")
    a("%BIN%\\fastboot reboot recovery")
    a("echo.")
    a("echo.[3/3] 等待 TWRP 的 adb 上线")
    a("set /a N=0")
    a(":WAIT_TWRP")
    a("set /a N+=1")
    a("if !N! GTR 60 goto NO_ADB")
    a('%BIN%\\adb devices 2>&1 |findstr /i "recovery" >nul && goto TWRP_OK')
    a("ping -n 2 127.0.0.1 >nul")
    a("goto WAIT_TWRP")
    a(":TWRP_OK")
    a("echo.")
    a("echo.已进入 TWRP。开机闪退通常是 /data 的 FBE 密钥不匹配，")
    a("echo.需要执行 清除 -^> 格式化 Data -^> 输入 yes。")
    a('set /p DOFMT="是否自动执行 twrp format data 并重启系统？[Y/n]: "')
    a('if /i "!DOFMT!"=="n" (')
    a("  echo.请在 TWRP 里手动格式化 Data 后重启")
    a("  pause")
    a("  exit /b 0")
    a(")")
    a("%BIN%\\adb shell twrp format data")
    a("%BIN%\\adb reboot")
    a("echo.完成。首次开机会跑 dexopt，5-10 分钟内发热属正常。")
    a("echo.若仍无法开机，请重新运行 XMAPort-Flash.bat 完整重刷。")
    a("pause")
    a("exit /b 0")
    a(":NO_ADB")
    a("echo.60 秒内未检测到 TWRP 的 adb，请手动按住 音量上 + 电源 进 recovery，")
    a("echo.在 TWRP 里执行 清除 -^> 格式化 Data -^> 输入 yes 后重启。")
    a("pause")
    a("exit /b 0")
    target = staging / "2-Force-TWRP.bat"
    target.write_bytes(("\r\n".join(L) + "\r\n").encode("gbk", errors="replace"))
    ok("救砖脚本: {}".format(target.name))


def super_to_zstd(staging, level=3):
    """PACK_OUT/super.img → (sparse 转 raw) → zstd 压缩 → images/super.img.zst"""
    sup = PACK_OUT / "super.img"
    if not sup.is_file():
        err("packed/super.img 不存在, 无法构建一体包")
        return None
    images = staging / "images"
    images.mkdir(parents=True, exist_ok=True)
    raw = images / "super.img.raw"

    SPARSE_MAGIC = b"\x3a\xff\x26\xed"
    with open(sup, "rb") as f:
        magic = f.read(4)
    if magic == SPARSE_MAGIC:
        s2i = TOOLS / "simg2img.exe"
        if os.name != "nt":
            s2i = TOOLS / "linux" / "simg2img" if (TOOLS / "linux" / "simg2img").is_file() else s2i
        if not s2i.is_file():
            err("找不到 simg2img, 无法转换 sparse super.img")
            return None
        info("simg2img: sparse → raw ...")
        if run_cmd([str(s2i), str(sup), str(raw)]) != 0:
            err("simg2img 失败")
            return None
    else:
        info("super.img 已是 raw 格式, 直接复制")
        shutil.copy2(sup, raw)

    global _RAW_SUPER_SIZE
    _RAW_SUPER_SIZE = raw.stat().st_size

    zstd = find_zstd(staging)
    if not zstd:
        err("找不到 zstd, 无法压缩 super.img")
        return None
    info("zstd -{} 压缩 super.img (可能需要几分钟)...".format(level))
    zst = images / "super.img.zst"
    cmd = [str(zstd), "-{}".format(level), "-f", "--rm", str(raw), "-o", str(zst)]
    if run_cmd(cmd) != 0 or not zst.is_file():
        err("zstd 压缩失败")
        return None
    ok("super.img.zst: {:.0f} MB".format(zst.stat().st_size / 1048576))
    return zst


def _classify(pnames):
    """把镜像分成 固件 / boot类 / vbmeta / root变体 四组"""
    variants = []
    for p in pnames:
        m = re.match(r"init_boot_(.+)", p) or re.match(r"boot_(?!ab$)(.+)", p)
        if m and not p.endswith("_official"):
            variants.append((p, m.group(1)))
    variant_names = {img for img, _ in variants} | {
        p for p in pnames if p.endswith("_official") and (p.startswith("boot_") or p.startswith("init_boot_"))}
    firmware = [p for p in pnames
                if p not in BOOT_LIKE and not p.startswith("vbmeta") and p not in variant_names]
    bootlike = [p for p in BOOT_LIKE if p in pnames and p not in variant_names]
    return firmware, bootlike, variants


def _bat_escape(s):
    """ROM INFO 位于 if (...) 括号内，字面特殊符号必须转义。"""
    out = str(s)
    for ch in ("^", "&", "|", "<", ">", "(", ")"):
        out = out.replace(ch, "^" + ch)
    return out


_RAW_SUPER_SIZE = 0


def super_raw_size():
    """super.img 解压后的 raw 字节数，注入模板做完整性校验。

    线刷脚本会在刷 super 之前比对文件大小：解包不完整 / 磁盘空间不足导致
    的截断 super 是「刷完 super 直接闪退」最常见的原因，必须拒绝刷入。

    注意：PACK_OUT/super.img 可能是 sparse（lpmake --sparse），其大小小于
    解压后的 raw。必须返回 raw 大小（super_to_zstd 转 raw 后记录），否则
    线刷脚本会把 sparse 大小当期望值，与用户解压出的 raw 大小不符，报
    「super.img 大小不符」。
    """
    global _RAW_SUPER_SIZE
    if _RAW_SUPER_SIZE > 0:
        return _RAW_SUPER_SIZE
    p = PACK_OUT / "super.img"
    return p.stat().st_size if p.is_file() else 0


def gen_flash_bat(staging, device, model, port_ver, base_ver):
    """从静态模板生成线刷脚本 (GBK + CRLF)。

    模板为 tools/offline_flashpack/XMAPort-Flash.template.bat，来自实机验证的
    完整线刷脚本（已去除作者信息）。构建时只注入本次 config.ini 解析出的机型
    与版本，以及 super.img 的字节数；分区清单和 ROOT 菜单由模板固定。
    模板缺失时回退到 _gen_flash_bat_dynamic。
    """
    if not FLASH_BAT_TEMPLATE.is_file():
        warn("线刷脚本模板缺失 —— 回退动态生成")
        return _gen_flash_bat_dynamic(staging, device, model, port_ver, base_ver)
    text = FLASH_BAT_TEMPLATE.read_bytes().decode("gbk")
    size = super_raw_size()
    if size <= 0:
        err("packed/super.img 不存在，无法确定 super 大小，拒绝生成线刷脚本")
        return False
    values = {
        "@@DEVICE@@": (device or "?").upper(),
        "@@MODEL@@": model or "?",
        "@@BASE_VER@@": base_ver or "?",
        "@@PORT_MODEL@@": port_device_from_config() or "?",
        "@@PORT_VER@@": port_ver or "?",
        "@@SUPER_SIZE@@": str(size),
    }
    for key, value in values.items():
        text = text.replace(key, _bat_escape(value))
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    bat = staging / "XMAPort-Flash.bat"
    bat.write_bytes(text.encode("gbk", errors="replace"))
    ok("线刷脚本(静态模板): {} (super {} 字节)".format(bat.name, size))
    return True


def _gen_flash_bat_dynamic(staging, device, model, port_ver, base_ver):
    """生成线刷脚本 (GBK + CRLF)。镜像存在才生成对应行, root 变体自动出现菜单"""
    images = sorted((staging / "images").glob("*.img"))
    pnames = [p.stem for p in images if p.stem != "recovery_twrp"]
    firmware, bootlike, variants = _classify(pnames)
    vbmetas = [p for p in pnames if p.startswith("vbmeta")]

    has_super_zst = (staging / "images" / "super.img.zst").is_file()
    L = []
    a = L.append
    a("@echo off")
    a("setlocal enabledelayedexpansion")
    a("title XMAPort 线刷脚本 [请勿选中窗口, 卡住按右键或回车恢复]")
    a('cd /d %~dp0')
    a('if not exist "bin\\Windows\\fastboot.exe" (')
    a("  echo.刷机工具缺失, 请完整解压后再运行!")
    a("  pause & exit /b 1")
    a(")")
    a("chcp 936 >nul")
    a("echo.")
    a("echo.  ============================================")
    a("echo.     XMAPort  HyperOS 线刷一体包")
    a("echo.  ============================================")
    a("echo.")
    a("echo.  -------ROM INFO-------")
    a("echo.  机型代号 : {}".format(device or "?"))
    a("echo.  机型名称 : {}".format(model or "?"))
    a("echo.  底包版本 : {}".format(base_ver or "?"))
    a("echo.  移植版本 : {}".format(port_ver or "?"))
    # 「根据构建显示」：源 ROM / 底包两行由本次构建配置解析注入，不写死
    a("echo.  源 ROM : {} {}".format(port_device_from_config() or "?", port_ver or "?"))
    a("echo.  底包   : {} {}".format(device or "?", base_ver or "?"))
    a("echo.  ----------------------")
    a("echo.")
    a("echo.  注意: 首次刷入或跨版本刷入建议清除数据!")
    a("echo.")
    # 设备校验
    a('bin\\Windows\\fastboot getvar product 2>&1 | findstr /i "{}" >nul'.format(device))
    a("if errorlevel 1 (")
    a("  echo.[警告] 设备代号校验不通过! 请确认此包对应你的机型 {}".format(device))
    a('  set /p CTN="是否仍要继续刷入? [y/n] "')
    a('  if /i "!CTN!" neq "y" exit /b 1')
    a("  echo.")
    a(")")
    # super 解压
    if has_super_zst:
        a('if exist "images\\super.img" del "images\\super.img"')
        a("echo.正在解压 super.img.zst (需要 8GB 以上临时空间)...")
        a('bin\\Windows\\zstd.exe -d -f "images\\super.img.zst" -o "images\\super.img"')
        a("if errorlevel 1 (")
        a("  echo.super.img 解压失败! 请确认磁盘空间充足后重试.")
        a("  pause & exit /b 1")
        a(")")
        a("echo.")
    a('set /p WIPE="是否清除数据? (首次刷入建议 y) [y/n] "')
    a("echo.")
    a("echo.请将手机进入 fastboot 模式并连接电脑, 按回车开始刷入...")
    a("pause >nul")
    a("set /a ERRSUM=0")
    # 与已验证基线一致: 清 frp, 固件一律 _ab 双槽刷入
    # cust 存运营商/机型定制残留, 跨版本刷机后脏数据会影响启动; 无该分区时静默忽略
    a("bin\\Windows\\fastboot erase frp >nul 2>&1")
    a("bin\\Windows\\fastboot erase cust >nul 2>&1")
    if variants:
        a(":ROOT_MENU")
        a("echo.  -------ROOT 选择-------")
        a("echo.   <1> 不root")
        for i, (img, name) in enumerate(variants, start=2):
            a("echo.   <{}> {}".format(i, name))
        a('set /p RT="请选择 root 方式: "')
        a('if "%RT%"=="" goto ROOT_MENU')
    # 固件分区（双槽 _ab，与基线镜像一致）
    for p in firmware:
        a('bin\\Windows\\fastboot flash {}_ab "images\\{}.img" || set /a ERRSUM+=1'.format(p, p))
    # boot 类
    for p in bootlike:
        if p == "recovery" and "recovery" in pnames:
            a('bin\\Windows\\fastboot flash {}_ab "images\\{}.img" || set /a ERRSUM+=1'.format(p, p))
        elif p in ("boot", "init_boot", "dtbo", "vendor_boot"):
            a('bin\\Windows\\fastboot flash {}_ab "images\\{}.img" || set /a ERRSUM+=1'.format(p, p))
    # root 变体选择
    if variants:
        official_boot = "boot_official" if "boot_official" in pnames else "boot"
        official_init = "init_boot_official" if "init_boot_official" in pnames else "init_boot"
        for i, (img, name) in enumerate(variants, start=2):
            base_part = "init_boot" if img.startswith("init_boot_") else "boot"
            a('if "%RT%" EQU "{}" bin\\Windows\\fastboot flash {}_ab "images\\{}.img"'.format(i, base_part, img))
        a('if "%RT%" EQU "1" (')
        a('  if exist "images\\{}.img" bin\\Windows\\fastboot flash boot_ab "images\\{}.img"'.format(official_boot, official_boot))
        a('  if exist "images\\{}.img" bin\\Windows\\fastboot flash init_boot_ab "images\\{}.img"'.format(official_init, official_init))
        a(")")
    # vbmeta 禁验（顺序对齐可开机包：先 vbmeta_system，后顶层 vbmeta）
    ordered_vbmetas = ([p for p in vbmetas if p.startswith("vbmeta_system")] +
                       [p for p in vbmetas if not p.startswith("vbmeta_system")])
    for p in ordered_vbmetas:
        a('bin\\Windows\\fastboot --disable-verity --disable-verification flash {}_ab "images\\{}.img" || set /a ERRSUM+=1'.format(p, p))
    # TWRP 强制刷入：官方 recovery 一律不刷回（2026-11-06 与离线一体包对齐）
    if "recovery_twrp" in pnames or (staging / "images" / "recovery_twrp.img").is_file():
        a("echo.强制刷入 TWRP 随附 Recovery (不刷回官方 recovery)...")
        a('bin\\Windows\\fastboot flash recovery_ab "images\\recovery_twrp.img" || set /a ERRSUM+=1')
    # super
    if has_super_zst:
        a("echo.正在刷入 super (可能卡住较久, 请耐心等待, 不要重启手机)...")
        a('bin\\Windows\\fastboot flash super "images\\super.img" || set /a ERRSUM+=1')
    # 清数据 + 重启
    # 2026-11-05 真机验证: fastboot erase userdata 只擦除分区内容、不会重建
    # f2fs，而 shennong 的 fstab 带 FBE/inlinecrypt/metadata_encryption，
    # init 会因 "Can't find valid F2FS filesystem" 挂在 post-fs-data，
    # 表现为永久停在 HyperOS 动画、不发热、pstore 空。清数据必须走
    # 3-Format-Data.bat 的 "twrp format data" 重建文件系统。
    a('if /i "!WIPE!"=="y" (')
    a("  echo.  注意: 不要擦除 userdata / metadata (只擦不建 f2fs,")
    a("  echo.        擦 metadata 还会破坏 FBE 密钥，开机回 recovery).")
    a("  echo.        需要清数据请用同目录下的 3-Format-Data.bat (TWRP format data).")
    a(")")
    # misc 里的 bootloader_control 可能残留 "boot once into fastboot" / unbootable
    # 槽位标记, 表现为刷写全 OKAY 但每次重启秒回 fastboot。清掉后再设槽并重启。
    a("bin\\Windows\\fastboot erase misc >nul 2>&1")
    a("bin\\Windows\\fastboot set_active a || set /a ERRSUM+=1")
    a("bin\\Windows\\fastboot reboot")
    a("echo.")
    a('if "!ERRSUM!"=="0" (color 0a & echo.刷入完成, 未出现错误.) else (color 0c & echo.刷入完成, 但有 !ERRSUM! 处错误, 请检查上方日志.)')
    a("echo.若卡在 HyperOS 开机动画超过 20 分钟且不发热: 运行 3-Format-Data.bat 重建 /data 后重启.")
    a("echo.其它无法开机的情况: 同样先重建 /data; 仍不行请核对底包版本 {}.".format(base_ver or "?"))
    a("echo.本包底层固件已按 {} 基线逐字节校验.".format(base_ver or "底包"))
    a("pause")
    bat = staging / "XMAPort-Flash.bat"
    bat.write_bytes(("\r\n".join(L) + "\r\n").encode("gbk", errors="replace"))
    ok("线刷脚本: {}".format(bat.name))


def gen_format_data_bat(staging):
    """生成 3-Format-Data.bat：用 TWRP 重建 /data 的 f2fs 文件系统。

    fastboot erase userdata 只擦除分区内容，**不会重建 f2fs**；而 shennong 的
    fstab 带 fileencryption/inlinecrypt/metadata_encryption，init 挂载 /data
    失败后卡在 post-fs-data，SurfaceFlinger 已在播放开机动画。
    症状：一直停在 HyperOS logo、机身不发热、pstore 空、logcat 无 system_server。
    """
    L = []
    a = L.append
    a("@echo off")
    a("setlocal enabledelayedexpansion")
    a("cd /d %~dp0")
    a('set FB=bin\\Windows\\fastboot.exe')
    a('set ADB=bin\\Windows\\adb.exe')
    a("echo.")
    a("echo.  ============================================")
    a("echo.   重建 /data 文件系统 (避免卡在 HyperOS 动画)")
    a("echo.  ============================================")
    a("echo.  注意: 此操作会清空手机全部用户数据!")
    a("echo.  适用: 刷完一直停在开机动画、机身不发热、")
    a("echo.        等待超过 20 分钟仍无变化 (不是首次编译慢)")
    a("echo.")
    a('if not exist "images\\recovery_twrp.img" (')
    a("  echo.  [提示] 本包未附带 TWRP, 请先手动进入 recovery 再运行本脚本")
    a("  echo.")
    a(") else (")
    a("  echo.  1) 手机进入 fastboot 并连接电脑, 按任意键刷入 TWRP")
    a("  pause >nul")
    a('  %FB% flash recovery_ab "images\\recovery_twrp.img"')
    a("  %FB% reboot recovery")
    a(")")
    a("echo.  等 TWRP 主界面出现后, 按任意键继续")
    a("pause >nul")
    a("set DEV=")
    a('for /f "tokens=1" %%i in (\'%ADB% devices ^| findstr /v "List" ^| findstr /v "emulator"\') do set DEV=%%i')
    a('if "!DEV!"=="" ( echo.  [错误] 未找到 TWRP 设备 & pause & exit /b 1 )')
    a("echo.  设备: !DEV!")
    a("echo.  正在格式化 /data ...")
    a('%ADB% -s !DEV! shell "twrp format data"')
    a("echo.  验证 /data 可挂载 ...")
    a('%ADB% -s !DEV! shell "umount /data 2>/dev/null; mount /data; df -h /data | tail -1"')
    a("echo.  重启进系统 ...")
    a("%ADB% -s !DEV! reboot")
    a("echo.  完成。首次开机会跑 dexopt, 约 10-20 分钟且机身发热, 请勿中断。")
    a("pause")
    target = staging / "3-Format-Data.bat"
    target.write_bytes(("\r\n".join(L) + "\r\n").encode("gbk", errors="replace"))
    ok("data 处理脚本: {}".format(target.name))


def gen_update_binary(staging, device, model, port_ver, base_ver, sec_patch):
    """生成卡刷 update-binary (recovery 侧刷入脚本)"""
    images = sorted((staging / "images").glob("*.img"))
    pnames = [p.stem for p in images if p.stem != "recovery_twrp"]
    firmware, bootlike, variants = _classify(pnames)
    has_twrp = (staging / "images" / "recovery_twrp.img").is_file()
    has_super = (staging / "images" / "super.img.zst").is_file()

    L = []
    a = L.append
    a("#!/sbin/sh")
    a("# XMAPort recovery installer (auto-generated by build_flashpack.py)")
    a("OUTFD=/proc/self/fd/$2")
    a('ZIPFILE="$3"')
    a("ZIPNAME=${ZIPFILE##*/}")
    a("")
    a('ui_print() { echo -e "ui_print $1\\nui_print" >>$OUTFD; }')
    a("")
    a("extract_flash() {")
    a("    # $1=zip内镜像路径  $2=分区名 (自动刷 _a / _b 双槽)")
    a('    ui_print "- $(basename $1) -> $2 (a/b)"')
    a('    unzip -p "$ZIPFILE" "$1" >/dev/block/bootdevice/by-name/${2}_a')
    a('    unzip -p "$ZIPFILE" "$1" >/dev/block/bootdevice/by-name/${2}_b')
    a("}")
    a("")
    a("extract_flash_zstd() {")
    a('    ui_print "- super.img.zst -> super (zstd 流式解压)"')
    a('    unzip -p "$ZIPFILE" "$1" | /tmp/bin/android/zstd -c -d >/dev/block/bootdevice/by-name/super')
    a("}")
    a("")
    a("getVolumeKey() {")
    a('    ui_print "- 按音量[+] = 是, 音量[-] = 否"')
    a("    while true; do")
    a("        key=$(getevent -qlc 1 2>/dev/null | grep KEY_VOLUME) || continue")
    a('        case "$key" in')
    a("            *KEY_VOLUMEUP*) return 0 ;;")
    a("            *KEY_VOLUMEDOWN*) return 1 ;;")
    a("        esac")
    a("    done")
    a("}")
    a("")
    a("[ -d /tmp ] && rm -rf /tmp")
    a("mkdir -p /tmp")
    a('unzip -o "$ZIPFILE" "bin/android/zstd" -d /tmp')
    a("chmod -R 0755 /tmp")
    a('ui_print " "')
    a('ui_print "========================="')
    a('ui_print "    XMAPort 移植一体包"')
    a('ui_print " "')
    a('ui_print "  设备代号: {}"'.format(device or "?"))
    a('ui_print "  机型名称: {}"'.format(model or "?"))
    a('ui_print "  底包版本: {}"'.format(base_ver or "?"))
    a('ui_print "  移植版本: {}"'.format(port_ver or "?"))
    a('ui_print "  源 ROM : {} {}"'.format(port_device_from_config() or "?", port_ver or "?"))
    a('ui_print "  底包   : {} {}"'.format(device or "?", base_ver or "?"))
    a('ui_print "========================="')
    a('ui_print " "')
    a("# 设备代号校验")
    a("myDevice=$(getprop ro.product.device 2>/dev/null || getprop ro.product.name)")
    a('case "$myDevice" in')
    a("    *{}*) ;;".format(device or "___"))
    a("    *)")
    a('        ui_print "- 设备代号校验不通过 (当前: $myDevice, 期望: {})"'.format(device or "?"))
    a('        ui_print "- 刷错包有变砖风险, 音量[+]强制继续 / 音量[-]退出"')
    a("        getVolumeKey || { ui_print \"- 你选择了终止刷入\"; exit 1; }")
    a('        ui_print "- 已选择继续刷入"')
    a("        ;;")
    a("esac")
    a("# 打包错误检测")
    a('if unzip -l "$ZIPFILE" 2>/dev/null | grep -q "patcherror.txt"; then')
    a('    ui_print "- 该包构建时存在错误, 终止刷入"')
    a("    exit 1")
    a("fi")
    a("# MD5 片段校验 (sideload 时自动跳过)")
    a('case "$ZIPFILE" in *sideload*) ;; *)')
    a("""    realHash=$(echo "${ZIPNAME%.zip}" | awk -F_ '{print $NF}')""")
    a('    myHash=$(md5sum "$ZIPFILE" | head -c 10)')
    a('    ui_print "- MD5 片段校验: $myHash / $realHash"')
    a('    if [ "$myHash" != "$realHash" ]; then')
    a('        ui_print "- MD5 校验失败, 终止刷机, 请重新下载"')
    a("        exit 250")
    a("    fi")
    a("    ;;")
    a("esac")
    a("# 可选: 禁用 Magisk 模块")
    a('ui_print "- 音量[+]禁用全部模块 / 音量[-]跳过"')
    a("if getVolumeKey; then")
    a("    for m in /data/adb/modules/*; do")
    a('        [ -d "$m" ] && touch "$m/disable" && ui_print "- 已禁用 $(basename $m)"')
    a("    done")
    a("fi")
    a('ui_print "- 开始刷机"')
    for p in firmware:
        a('extract_flash "images/{}.img" {}'.format(p, p))
    for p in bootlike:
        a('extract_flash "images/{}.img" {}'.format(p, p))
    if variants:
        official_boot = "boot_official" if "boot_official" in pnames else "boot"
        official_init = "init_boot_official" if "init_boot_official" in pnames else "init_boot"
        a('ui_print "- 选择 root 方式: 音量[-]切换, 音量[+]确认 (8秒无操作默认不root)"')
        a('opts="不root {}"'.format(" ".join(n for _, n in variants)))
        a("total={}".format(1 + len(variants)))
        a("sel=1; idle=0")
        a("show_sel() {")
        a("    n=1")
        a("    for o in $opts; do")
        a('        [ $n -eq $sel ] && ui_print "  当前: <$sel> $o"')
        a("        n=$((n+1))")
        a("    done")
        a("}")
        a("show_sel")
        a("while true; do")
        a('    key=$(getevent -qlc 1 2>/dev/null | grep KEY_VOLUME) || { sleep 1; idle=$((idle+1)); [ $idle -ge 8 ] && break; continue; }')
        a('    case "$key" in')
        a("        *KEY_VOLUMEDOWN*) sel=$((sel % total + 1)); idle=0; show_sel;;")
        a("        *KEY_VOLUMEUP*) break;;")
        a("    esac")
        a("done")
        for idx, (img, name) in enumerate(variants, start=2):
            base_part = "init_boot" if img.startswith("init_boot_") else "boot"
            a('if [ $sel -eq {} ]; then extract_flash "images/{}.img" {}; fi'.format(idx, img, base_part))
        a("if [ $sel -eq 1 ]; then")
        a('    [ -f "images/{}.img" ] && extract_flash "images/{}.img" boot'.format(official_boot, official_boot))
        a('    [ -f "images/{}.img" ] && extract_flash "images/{}.img" init_boot'.format(official_init, official_init))
        a("fi")
    for p in pnames:
        if p.startswith("vbmeta"):
            a('extract_flash "images/{}.img" {}'.format(p, p))
    if has_twrp:
        # 强制刷 TWRP，不再询问、不刷官方 recovery（2026-11-06 与离线一体包对齐）
        a('ui_print "- 强制刷入 TWRP 随附 Recovery"')
        a('    extract_flash "images/recovery_twrp.img" recovery')
    if has_super:
        a('extract_flash_zstd "images/super.img.zst"')
        a("# 动态分区 remap (存在 lptools 时)")
        a("for pname in odm mi_ext system vendor product system_ext odm_dlkm vendor_dlkm system_dlkm mi_product; do")
        a("    [ -e /dev/block/mapper/${pname}_a ] && command -v lptools >/dev/null 2>&1 && { lptools unmap ${pname}_a >/dev/null 2>&1; lptools map ${pname}_a >/dev/null 2>&1; }")
        a("done")
    if sec_patch:
        a("mount /data 2>/dev/null")
        a("current_security_patch={}".format(sec_patch))
        a("if [ -f /data/security_patch ]; then")
        a('    last=$(cat /data/security_patch)')
        a('    if [ "$current_security_patch" \\< "$last" ]; then')
        a('        ui_print "- 检测到降级刷入, 可能无法开机, 异常时请刷回上一版本"')
        a("    fi")
        a("fi")
        a('echo "$current_security_patch" >/data/security_patch 2>/dev/null')
    a("rm -rf /data/dalvik-cache/arm/* /data/dalvik-cache/arm64/* /data/system/package_cache/* 2>/dev/null")
    a('ui_print " "')
    a('ui_print "- 刷机完毕!"')
    a('ui_print "- 如选了 root, 请到包内 RootManager 安装对应管理器"')
    a('ui_print "- 若无法开机: 清数据重刷, 或刷回上一版本"')
    a("bootctl set-active-boot-slot a 2>/dev/null")
    a("exit 0")
    ub = staging / "META-INF" / "com" / "google" / "android" / "update-binary"
    ub.parent.mkdir(parents=True, exist_ok=True)
    ub.write_bytes(("\n".join(L) + "\n").encode("utf-8"))
    ok("卡刷脚本: META-INF/com/google/android/update-binary")

def gen_readme(staging, device, model, port_ver, base_ver):
    txt = """XMAPort 线刷 + 卡刷 一体包
================================

机型代号: {device}
机型名称: {model}
底包版本: {base}
移植版本: {port}

【线刷 fastboot】
1. 完整解压本 zip (路径不要含中文可能出现问题的字符)
2. 手机进入 fastboot 模式 (关机后 按住 电源+音量下) 连接电脑
3. 双击运行 XMAPort-Flash.bat, 按提示操作
   - 脚本先校验 super.img 字节数, 大小不符会拒绝刷入 (防止刷入截断的 super)
   - 强制刷入 TWRP 到 recovery 双槽, 不会刷回官方 Recovery
   - 刷完 super 后不直接重启进系统, 而是进入 TWRP 执行 format data
     (重建 FBE 密钥; fastboot erase userdata/metadata 只擦不建, 会回 recovery)
4. 需要已安装小米驱动; BL 已解锁

【开机闪退 / 卡 LOGO 救砖】
不用重刷 8GB 的 super: 双击 2-Force-TWRP.bat
  -> 强制双槽刷 TWRP -> 自动进 TWRP -> 自动 format data -> 重启
仍不行再运行 3-Format-Data.bat 单独重建 /data, 最后才重刷完整包。

【卡刷 recovery】
1. 手机关机, 进入 recovery, 选择 Apply update from ADB (adb sideload)
   或把 zip 放进 U 盘 /data 后从 recovery 选择
2. adb sideload XMAPort_xxx.zip
3. 按音量键按提示选择 (root 方式 / 是否禁用模块); TWRP 为强制刷入

注意事项:
- 首次刷入必须清除数据并重建 /data, 否则开机闪退 / 回 recovery
- vbmeta 已禁用 verification
- 若一直停在 HyperOS 开机动画、机身不发热、超过 20 分钟无变化:
  这不是首次编译慢, 是 /data 无法挂载, 用 3-Format-Data.bat 重建 /data
- 无法开机可尝试: 2-Force-TWRP.bat → 3-Format-Data.bat → 完整重刷
- APatch / FolkPatch 缺少对应 boot 镜像时菜单会拒绝选择, 请改选其它 ROOT 方式
- root 变体镜像仅在包内存在时才会出现选择菜单

由 XMAPort-SHALLOW 自动生成
""".format(device=device or "?", model=model or "?", base=base_ver or "?", port=port_ver or "?")
    (staging / "README-FLASH.txt").write_text(txt, encoding="utf-8")


def build_zip(staging, out_dir, device, port_ver):
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "XMAPort_tmp.zip"
    if tmp.exists():
        tmp.unlink()
    info("打包 zip (镜像用 STORED 不二次压缩)...")
    # 360压缩等解压软件不支持「局部文件头偏移 > 4GB」的 ZIP64 条目：
    # 若把 6~7GB 的 super.img.zst 排在中间，其后所有小条目的 header_offset
    # 都会落在 7GB 之后，被误判为「压缩文件头部数据已经损坏」并跳过。
    # 这里把所有 >4GB 的条目排到最后，保证其余条目偏移恒 < 4GB。
    files = sorted(p for p in staging.rglob("*") if p.is_file())
    zip64_limit = 4 * 1024 ** 3
    small = [p for p in files if p.stat().st_size <= zip64_limit]
    large = [p for p in files if p.stat().st_size > zip64_limit]
    if large:
        info("zip 条目重排: {} 个超大条目置尾 ({})".format(
            len(large), ", ".join(p.name for p in large)))
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path in small + large:
            zf.write(path, path.relative_to(staging).as_posix())
    h = hashlib.md5()
    with open(tmp, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    md510 = h.hexdigest()[:10]
    final = out_dir / "XMAPort_{}_{}_{}.zip".format(device or "device", port_ver or "unknown", md510)
    if final.exists():
        final.unlink()
    tmp.rename(final)
    ok("一体包: {} ({:.0f} MB)".format(final.name, final.stat().st_size / 1048576))
    return final


def main():
    ap = argparse.ArgumentParser(description="Build fastboot+recovery flash pack")
    ap.add_argument("--out-dir", default=str(WORKSPACE / "flashpack"))
    ap.add_argument("--keep-raw", action="store_true", help="保留 staging 目录")
    ap.add_argument("--patcherror", action="store_true", help="写入 patcherror.txt 使卡刷拒绝刷入")
    ap.add_argument("--zstd-level", type=int, default=3)
    args = ap.parse_args()

    device = read_device()
    if not device:
        err("workspace/config.txt 未找到 TARGET_DEVICE, 请先跑完移植流程")
        return 1
    model = read_prop(TGT_FS, "vendor", "ro.product.vendor.model") or \
        read_prop(SRC_FS, "product", "ro.product.product.model")
    port_ver = read_prop(SRC_FS, "product", "ro.build.version.incremental") or "unknown"
    base_ver = read_prop(TGT_FS, "vendor", "ro.vendor.build.version.incremental") or \
        read_prop(TGT_FS, "vendor", "ro.build.version.incremental") or "unknown"
    sec_patch = read_prop(SRC_FS, "product", "ro.build.version.security_patch")

    info("设备: {} | 移植: {} | 底包: {}".format(device, port_ver, base_ver))
    try:
        free_gb = shutil.disk_usage(str(WORKSPACE)).free / (1024 ** 3)
        info("磁盘剩余: {:.1f} GB (一体包构建约需 18GB)".format(free_gb))
    except Exception:
        pass

    # 清理旧 staging
    stamp = args.out_dir
    staging = Path(stamp) / "pack_staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    imgs = collect_images(device)
    if not imgs:
        err("未收集到任何固件镜像 (workspace 不完整?)")
        return 1
    images_dir = staging / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    for pname, src in sorted(imgs.items()):
        shutil.copy2(src, images_dir / (pname + ".img"))
        info("  images/{}.img  ← {}".format(pname, src))
    ok("共 {} 个镜像".format(len(imgs)))

    if not verify_vbmeta(images_dir):
        return 1

    if not verify_firmware(images_dir, device):
        return 1

    if args.patcherror:
        (staging / "patcherror.txt").write_text("build errors marked by --patcherror\n", encoding="utf-8")

    # 抽取刷机工具 bin/Windows (fastboot/zstd/adb) 与 bin/android (卡刷用)
    # 漏掉的话 XMAPort-Flash.bat 会报 "刷机工具缺失"
    extracted = prepare_bin_tools(staging)
    if extracted:
        ok("刷机工具: bin/Windows {}, bin/android {}".format(
            sum(1 for k in extracted if "/Windows/" in k),
            sum(1 for k in extracted if "/android/" in k)))

    # 主线刷模板强制刷入 TWRP：镜像必须随包提供，缺失即拒绝出包
    if not prepare_twrp(staging, device):
        return 1

    zst = super_to_zstd(staging, level=args.zstd_level)
    if zst is None:
        return 1

    if not gen_flash_bat(staging, device, model, port_ver, base_ver):
        return 1
    gen_force_twrp_bat(staging)
    gen_format_data_bat(staging)
    gen_update_binary(staging, device, model, port_ver, base_ver, sec_patch)
    gen_readme(staging, device, model, port_ver, base_ver)

    rm_dir = DEVICES_DIR / device / "RootManager" if device else None
    if rm_dir and rm_dir.is_dir():
        shutil.copytree(rm_dir, staging / "RootManager", dirs_exist_ok=True)
        info("RootManager/ 已包含 ({} 个文件)".format(sum(1 for _ in (staging / "RootManager").rglob("*"))))

    final = build_zip(staging, Path(args.out_dir), device, port_ver)
    if not args.keep_raw:
        shutil.rmtree(staging, ignore_errors=True)
    else:
        ok("staging 目录保留: {}".format(staging))
    print()
    ok("完成: {}".format(final))
    return 0


if __name__ == "__main__":
    sys.exit(main())
