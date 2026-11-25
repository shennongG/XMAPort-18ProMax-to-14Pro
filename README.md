# XMAPort-18ProMax-to-14Pro

将 **Xiaomi 18 Pro Max（madrid，HyperOS OS4 / Android 17）** 的 ROM 移植到
**Xiaomi 14 Pro（shennong，HyperOS OS3 / Android 16）** 的一体化流水线。

> 跨 Android 大版本移植（17 → 16）风险较高。项目内置兼容注入与内容基线校验，
> 但静态校验通过不等于真机一定可开机。刷机前请备份，风险自负。

## 移植路线

| | 源包（Port From） | 底包（Base） |
| --- | --- | --- |
| 机型 | Xiaomi 18 Pro Max | Xiaomi 14 Pro |
| 代号 | `madrid` | `shennong` |
| 版本 | OS4.0.31.0.XEOCNXM（默认） | OS3.0.310.0.WNBCNXM |
| Android | 17 | 16 |
| 分区角色 | system / system_ext / product / mi_ext | odm / vendor / boot / 固件 |

`super` 总大小 **8,321,499,136 B（7.75 GiB）**，屏幕密度 **560**。

## 特性

- **9 步流水线**：兼容性预检 → 下载 → 解压 → 解 payload → 解分区镜像 → 迁移打补丁 →
  精简与内容对齐 → 重打包 → 内容基线校验
- **内容基线校验**：把真机验证过的关键 SELinux 标签、oat 保留、资源裁剪、密度和
  振动实例要求固化为 JSON，出包后自动校验
- **跨版本兼容注入**：补齐 VNDK v30~v33 APEX、`30.0*.cil` SELinux mapping、`keystube.zip`
- **SELinux 标签补全**：只读分区补全 file_contexts，特殊路径使用独立标签
- **双路线**：默认「源包为主 + 内容基线对齐」；可选实验性 `base_first`（底包骨架模式）
- **云端构建**：GitHub Actions 出包并自动发布 Release
- **线刷 + 卡刷一体包**：`build_flashpack.py` 自动生成，支持分卷

## 快速开始

### 方式一：GitHub Actions（推荐）

1. Actions → **Build 18ProMax-to-14Pro** → Run workflow
2. `device` 填 `shennong`；源包/底包直链留空即使用 `config.ini` 默认值
3. 完成后在 Release 下载分卷 7z，合并解压：
   - 线刷：解压 `XMAPort_*.zip`，运行 `XMAPort-Flash.bat`
   - 卡刷：进 Recovery sideload 该 zip

### 方式二：本地命令行

```bat
python XMAPort.py --auto --device shennong ^
  --source <madrid-ota_full-....zip 直链> ^
  --target <shennong-ota_full-OS3.0.310.0.WNBCNXM-....zip 直链>
```

留空 `--source` / `--target` 会复用工作区已有数据。

### 方式三：交互式菜单

```bat
python XMAPort.py
```

需要 Windows 10/11、Python 3.8+、约 60GB 可用磁盘。

## 配置

配置集中在 `config.ini`（UTF-8，脚本兼容 UTF-8/GBK）。关键项：

| 项 | 值 | 说明 |
| --- | --- | --- |
| `device_platform` | `Qualcomm` | 必须按真实平台填写 |
| `device_size` | `8321499136` | shennong super 实测大小 |
| `[source] url` | OS4.0.31 madrid 直链 | 可换 4.0.30 / 4.0.29 |
| `[target] url` | OS3.0.310 shennong 直链 | 提供 odm / vendor / 固件 |
| `[packing] base_first` | `false` | 默认源包为主；实验性底包骨架模式 |
| `[shallow] density` | 空 | 由 `fix_density_tree()` 固定为 560 |
| `[shallow] inject_boot_compat` | `true` | 跨版本兼容注入，关闭后可能卡二屏 |
| `[shallow] align_perfect_product` | `true` | 按内容基线裁剪 product |

## 两条移植路线

- **source_first（默认）**：从 4.0.x 源包迁移 system/system_ext/product/mi_ext，
  叠加兼容注入、密度 560、oat 保留和资源裁剪，适用于大多数场景。
- **base_first（实验性）**：以 310 底包为骨架，只替换源包增量，减少机型错配。
  规则见 `devices/shennong/base_first_rules.conf`。

## 目录结构

```
XMAPort.py                       主流水线
config.ini                       配置（源/底包直链、打包、精简开关）
tools/
  make_hyper.py                  分区迁移 + 特性同步
  shallow_slim.py                精简阶段（应用裁剪/机型注入/oat/内容对齐）
  pack_partitions.py             erofs/ext4 打包 + file_contexts 补全
  base_first_merge.py            底包骨架合并（实验性）
  verify_perfect_baseline.py     内容基线校验
  build_flashpack.py             线刷+卡刷一体包
  fix_density.py                 密度 560
  rom_compat.py                  版本预检
  config_codec.py                config.ini 编码兼容
devices/shennong/                机型档案（基线/裁剪清单/合并规则/配置）
shallow/keep_apps.conf           应用保护白名单
.github/workflows/build.yml      云端构建
```

## 线刷 / 卡刷一体包

`build_flashpack.py` 生成一个 zip，同时支持两种刷法：

- **线刷**：解压后运行 `XMAPort-Flash.bat`，fastboot 刷入固件 + `super.img`
- **卡刷**：进第三方 Recovery（TWRP）sideload 同一 zip

刷机脚本固定刷入 TWRP（不会恢复官方 Recovery），并提供：

| 脚本 | 用途 |
| --- | --- |
| `XMAPort-Flash.bat` | 主线刷：机型校验 → ROOT 方式选择 → 固件/super/boot 全刷 → 自动重建 data → 重启 |
| `2-Force-TWRP.bat` | 救砖：双槽刷 TWRP 并重建 `/data` |
| `3-Format-Data.bat` | 单独在 TWRP 里重建 `/data` |

`/data` 使用 `make_f2fs` 并开启 FBE 所需特性；`/metadata` 保留，避免首次开机丢失
元数据加密密钥。脚本会在首次启动失败时自动再重启一次。

### 打包资源

| 路径 | 用途 |
| --- | --- |
| `tools/flashtools/Windows/` | adb / fastboot / zstd 等线刷工具，构建时复制到 `bin/Windows/` |
| `tools/flashtools/android/` | Recovery 内使用的 `zstd` / `7zzs` |
| `devices/shennong/recovery_twrp.img.zst.part*` | TWRP 镜像分片，出包时拼回并解压 |
| `devices/shennong/root/init_boot_*.img` | ROOT 变体（Magisk / KernelSU / SukiSU / Kitsune 等） |
| `tools/assets/vbmeta_disabled.img` | 关闭 AVB 校验所需的空白 vbmeta |

> 更换底包机型时，需要提供对应机型的 TWRP 与 flashtools；缺少 TWRP 时脚本会拒绝出包。

### 为什么卡刷使用包内 7zzs

`super.img.zst` 约 6.4 GB，超过 4 GB，只能以 ZIP64 条目存放。Recovery 自带的
`unzip` 多数不支持 ZIP64，可能报错或静默截断。因此 `update-binary` 统一使用随包的
`bin/android/7zzs e -so`，仅在 7zzs 缺失时退回 `unzip -p`。

### 出包自检

发布前会运行 `tools/verify_flashpack.py`：

- `META-INF/com/google/android/update-binary` 存在
- `bin/android/7zzs`、`bin/android/zstd` 随包
- `images/super.img.zst` > 4 GB 且 zstd 帧头正确
- `images/recovery_twrp.img` 与 `XMAPort-Flash.bat` 存在
- 所有 ≤4 GB 条目的 `header_offset` < 4 GB

任一不通过即中断构建，不发布半成品。

## 内容基线

出包后自动校验以下可开机特征：

- `30.0.cil` / `30.0.compat.cil` = `sepolicy_file`，`bugreporter` = `shell_exec`
- system_ext 保留 miui-services oat（本体 ≥ 15）
- product 不含 `media/rearscreen`
- product 包含 `app/WMService`、`app/UPTsmService`、`app/system`、
  `pangu/system/app/NQNfcNci` 等底包原生服务
- 密度 560、振动 HAL 至少 1 个实例

## 来源与声明

- 主流水线基于 XMAPort 项目骨架，相关权利归原作者所有。上游仓库未附带许可证，
  使用或再分发前请自行确认授权。
- 本项目仅用于个人学习与研究；ROM 版权归 Xiaomi 所有，请勿用于商业用途或非法分发。
- 刷机有风险，操作前请备份并自行承担后果。
