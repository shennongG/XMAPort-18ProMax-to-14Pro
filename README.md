# XMAPort-18ProMax-to-14Pro

将 **小米 18 Pro Max（madrid，HyperOS OS4 / Android 17）** 的 ROM 移植到
**小米 14 Pro（shennong，HyperOS OS3 / Android 16）** 的一体化流水线。

以 [NorthStarK-Lvy/XMAPort](https://github.com/NorthStarK-Lvy/XMAPort) 为骨架，
并借鉴 X-MiPort 的**同版本完美包**（真机可开机的移植成品）内容基线，把「移植 + 精简 + 打包 + 校验」
收敛进一条命令。

> ⚠️ 跨 Android 大版本移植（17 → 16）风险高。本项目已内置兼容注入与内容基线校验，
> 但**静态通过不等于真机可开机**，请自行测试并承担风险，刷机前备份。

## 移植路线

| | 源包（Port From） | 底包（Base） |
| --- | --- | --- |
| 机型 | Xiaomi 18 Pro Max | Xiaomi 14 Pro |
| 代号 | `madrid` | `shennong` |
| 版本 | OS4.0.30.0.XEOCNXM（默认，官方直链可用；与完美包同源） | OS3.0.310.0.WNBCNXM |
| Android | 17 | 16 |
| 分区角色 | system / system_ext / product / mi_ext | odm / vendor / boot / 固件 |

`super` 总大小 **8,321,499,136 B（7.75 GiB）**，密度 **560**。

## 特性

- **9 步流水线**：兼容性预检 → 下载 → 解压 → 解 payload → 解分区镜像 → 迁移打补丁 →
  **精简（含完美包对齐）** → 重打包 → 内容基线校验 + 汇总
- **完美包内容基线**：把真机可开机包的关键 SELinux 标签、oat 保留、madrid 资源裁剪、
  密度/振动固化为 JSON，CI 出包后自动校验，拦住「结构对但卡二屏」的失败包
- **跨版本兼容注入**：自动补 VNDK v30~v33 APEX、`30.0*.cil` SELinux mapping、`keystube.zip`
- **SELinux 标签补全**：全只读分区补 file_contexts，特殊路径走独立标签名单
- **双路线**：默认「源包为主 + 完美包对齐」；可选实验性 `base_first`（以 310 底包为骨架）
- **双包并行下载**、失败即中断、云端 GitHub Actions 构建并自动发 Release
- **线刷 + 卡刷一体包**：`build_flashpack.py` 自动生成，含分卷兼容处理

## 快速开始

### 方式一：GitHub Actions（推荐）

1. Actions → **Build 18ProMax-to-14Pro** → Run workflow
2. `device` 填 `shennong`；源/底包直链留空即用 `config.ini` 默认值
3. 完成后在 Release 下载分卷 7z，合并解压后：
   - 线刷：解压 `XMAPort_*.zip` → 跑 `XMAPort-Flash.bat`
   - 卡刷：进 recovery sideload 该 zip

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

全部集中在 `config.ini`（UTF-8，脚本自动识别 UTF-8/GBK）。关键项：

| 项 | 值 | 说明 |
| --- | --- | --- |
| `device_platform` | `Qualcomm` | 填错有变砖风险 |
| `device_size` | `8321499136` | shennong super 实测大小 |
| `[source] url` | OS4.0.30 madrid 直链 | 与完美包同源版本（12.46 GB）；可换其它 madrid 版本 |
| `[target] url` | OS3.0.310 shennong 直链 | 提供 odm / vendor / 固件 |
| `[packing] base_first` | `false` | 默认源包为主；实验性底包骨架模式见下 |
| `[shallow] density` | 空 | 由 `fix_density_tree()` 强制 560 |
| `[shallow] inject_boot_compat` | `true` | 跨版本兼容注入，关掉会卡二屏 |
| `[shallow] align_perfect_product` | `true` | 按完美包清单裁剪 product |

## 两条移植路线

- **source_first（默认，`base_first=false`）**：从 4.0.x 源包迁 system/system_ext/product/mi_ext，
  叠加兼容注入、密度 560、oat 保留、258 个 madrid 资源删除；CI 完美包基线全 PASS。
- **base_first（实验性，`base_first=true`）**：以 310 底包为骨架，只把源包增量替换进去，
  从源头减少机型错配。规则见 `devices/shennong/base_first_rules.conf`，
  详见 [base-first-mode.md](docs/shennong/base-first-mode.md)。

## 目录结构

```
XMAPort.py                       主流水线
config.ini                       配置（源/底包直链、打包、精简开关）
tools/
  make_hyper.py                  分区迁移 + 特性同步
  shallow_slim.py                精简阶段（删应用/机型注入/oat/完美包对齐/兼容注入）
  pack_partitions.py             erofs/ext4 打包 + file_contexts 补全
  base_first_merge.py            底包骨架合并（实验性）
  verify_perfect_baseline.py     完美包内容基线校验
  build_flashpack.py             线刷+卡刷一体包
  fix_density.py                 密度 560
  rom_compat.py                  Step 0 版本预检
  config_codec.py                config.ini 编码兼容
devices/shennong/                机型档案（基线/删除清单/合并规则/机型配置）
shallow/keep_apps.conf           应用保护白名单
docs/shennong/                   移植说明与基线文档
.github/workflows/build.yml      云端构建
```

## 内容基线（关键）

出包后 CI 自动校验以下可开机特征（详见 [perfect-baseline.md](docs/shennong/perfect-baseline.md)）：

- `30.0.cil` / `30.0.compat.cil` = `sepolicy_file`，`bugreporter` = `shell_exec`
- system_ext 保留 miui-services oat（本体 ≥ 15）
- product 无 `media/rearscreen`
- 密度 560、振动 HAL ≥ 1 实例

## 致谢与许可

- 主流水线源自 [NorthStarK-Lvy/XMAPort](https://github.com/NorthStarK-Lvy/XMAPort)（MIT）
- 完美包内容基线逆向自 X-MiPort 的移植成品，仅用于个人学习与研究
- 本项目为学习研究用途，ROM 版权归小米所有；请遵守当地法律，勿用于商业用途
