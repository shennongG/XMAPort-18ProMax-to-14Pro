# 完美包内容基线（shennong / 18 Pro Max → 14 Pro）

本项目把 X-MiPort「完美包」（真机可开机的移植成品）的内容特征固化为可自动校验的基线，
让 CI 在出包后就能拦住「结构全对、真机却卡 HyperOS 二屏」的失败构建。

## 背景

一次全新构建：LP 结构、SELinux 标签统计全部通过，真机却反复重启进 Recovery、卡开机动画。
对比两个可开机基准（X-MiPort 完美包 + 本项目 V5），发现失败包在三项内容上同时偏离。
基线即由这三项 + 密度 + 振动固化而来。

## 两个基准（内容高度一致）

| 基准 | 源包 | 底包 | 状态 |
| --- | --- | --- | --- |
| X-MiPort 完美包（`rom2`，当前） | OS4.0.31.0.XEOCNXM / madrid | OS3.0.310.0.WNBCNXM | 真机可开机 |
| X-MiPort 完美包（早期） | OS4.0.30.0.XEOCNXM / madrid | OS3.0.310.0.WNBCNXM | 真机可开机 |
| X-MiPort 完美包（更早） | OS4.0.29.0.XEOCNXM / madrid | OS3.0.310.0.WNBCNXM | 真机可开机 |
| 本项目 V5（兜底） | OS4.0.29.0.XEOCNXM / madrid | OS3.0.310.0.WNBCNXM | 真机可开机 |

2026-11-21 复核（4.0.31，`--lists-dir --strict-counts` 实跑）：
system 4793 / system_ext 3693 / product 2315 / odm 2321 / vendor 2968 / mi_ext 147 /
vendor_dlkm 328 / system_dlkm 186，关键 SELinux 标签、oat 17 项、无 rearscreen 全部命中，
`[PASS] 全部关键项对齐完美包基线`。与 4.0.29/4.0.30 相比只有 product +6，
因此同一份基线 JSON 同时适用 4.0.29 / 4.0.30 / 4.0.31。

## 可开机特征（基线 JSON 内容）

来源：`devices/shennong/perfect_baseline.json`

### 1. 关键 SELinux 标签（卡二屏头号嫌疑）

| 文件 | 必须有 |
| --- | --- |
| `/system/etc/selinux/mapping/30.0.cil` | `u:object_r:sepolicy_file:s0` |
| `/system/etc/selinux/mapping/30.0.compat.cil` | `u:object_r:sepolicy_file:s0` |
| `/system/bin/bugreporter` | `u:object_r:shell_exec:s0` |

失败包里这些是 `system_file`。根因是 `pack_partitions.py` 补全 file_contexts 时路径带了
重复前缀 `/system/system/...`，特殊名单没命中；已修。

### 2. system_ext 保留 miui-services 预编译 oat

只数 `.odex/.vdex/.art` 本体（不含 `.fsv_meta`）：完美包 17 / V5 19 / 失败包 0，下限 15。
删光会卡二屏；只删 `system/framework/oat` 下的 services.* 是安全的。

### 3. product 不含 madrid 副屏/背屏资源

`media/rearscreen` 目录必须为空（完美包 0 项，失败包 268 项）。
`precust_theme` ≤ 220、`subscreencenter` ≤ 6、`overlay` ≤ 105。

### 4. 密度与振动

- `ro.sf.lcd_density=560` / `persist.miui.density_v2=560`（底包 shennong 原生值；源包 madrid 是 480）
- 振动 HAL 至少 1 个实例，推荐 2 个：`IVibrator/vibratorfeature` + `IVibrator/default`
  （Android 17 只有 feature 实例可能无振动，本项目 fix_vibrator_hal 会自动补 default）

### 5. 动态分区条目数

system 4793 / system_ext 3693 / product 2309 / odm 2321 / vendor 2968 / mi_ext 147 /
vendor_dlkm 328 / system_dlkm 186

## 使用

CI 出包后自动运行（见 `.github/workflows/build.yml`）：

```bash
python tools/verify_perfect_baseline.py --tree workspace/source_filesystem --device shennong
```

本地验一个已解包的成品（用 `extract.erofs -p -s` 导出的清单）：

```bash
python tools/verify_perfect_baseline.py --lists-dir <清单目录> --tag perfect
```

退出码：0 = 通过（可含 WARN），1 = 存在 FAIL，2 = 参数/环境错误。

**CI 通过 ≠ 真机可开机**：静态基线是必要条件，不是充分条件。最终仍需上机验证，
卡机时抓 `adb logcat` 与 `/sys/fs/pstore/console-ramoops` 定位崩点。

## 5. 完美包「补入」的底包原生应用（2026-11-21 新增）

逐文件对比 4.0.29 完美包与源包后，确认完美包不是「只裁剪源包」：

| 项 | 条目数 |
| --- | --- |
| 源包 product | 2533 |
| 完美包删除 | −363 |
| 完美包补入 | +139 |
| 完美包 product | 2309 |

补入项全部是 **shennong（底包 310）原生、madrid 4.0.29 没有** 的系统应用：

| 路径 | 说明 |
| --- | --- |
| `app/WMService` | 窗口管理扩展服务 |
| `app/UPTsmService` | TEE / 可信服务（含 8 个 .so） |
| `app/system/system.apk` | 系统核心应用（曾被 `product:app:=system` 误删） |
| `pangu/system/app/NQNfcNci` | NFC 控制器 |
| `app/talkback` / `app/PaymentService` / `priv-app/AutoRegistration` | 辅助功能 / 支付 / 注册 |

本仓库旧产物只删不补，真机反复卡 HyperOS 二屏。现在：

1. `devices/shennong/perfect_add_paths.txt` 列出 82 个待补文件；
2. `step_align_perfect_product` 在删除之后，从底包 `product` 树复制补齐；
3. `perfect_baseline.json` 的 `product_required_dirs` 由 CI 硬校验，缺一个即 FAIL；
4. `remove_apps.conf` 里与完美包冲突的 21 条已用 `!` 停用。

> 补入文件不随仓库分发（相机 apk 175 MB 等大文件会超 GitHub 限制），
> 全部取自 `config.ini [target]` 下载的底包。
