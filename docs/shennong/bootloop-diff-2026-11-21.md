# 卡 HyperOS 二屏 · 逐文件对比报告（2026-11-21）

## 结论先行

X-MiPort 完美包**不是「只裁剪源包」**，而是：

```
product     源包 2533 − 删 363 + 补 139 = 2309
system_ext  源包 3661 + 补 34（miui-services oat）= 3695
```

本项目此前只做「删」，产物 product 2533 项，**缺 139 项底包原生系统服务**，
真机表现为反复卡 HyperOS 二屏、机身不热、pstore 为空。

补入项全部是 14 Pro（底包 310）原生、madrid 4.0.x 没有的文件：

| 路径 | 说明 | 缺失后果 |
| --- | --- | --- |
| `app/WMService` | 窗口管理扩展服务 | 窗口/SystemUI 初始化异常 |
| `app/UPTsmService`（含 8 个 .so） | TEE / 可信服务 | 安全服务绑定失败 |
| `app/system/system.apk` | 系统核心应用 | 曾被 `product:app:=system` 误删 |
| `pangu/system/app/NQNfcNci` | NFC 控制器 | NFC 服务崩溃 |
| `app/talkback`、`app/PaymentService`、`priv-app/AutoRegistration` | 辅助/支付/注册 | 对应服务异常 |
| `system_ext/framework/oat/*/miui-services.*` | 框架预编译 oat | system_server dexopt/类加载回退 |

## 对比样本

| 样本 | 源包 | 底包 | 真机 |
| --- | --- | --- | --- |
| `rom2` X-MiPort 完美包 | OS4.0.31.0.XEOCNXM / madrid | OS3.0.310.0.WNBCNXM | **可开机** |
| `rom1` X-MiPort 完美包 | OS4.0.7.0.XPACNXM / nezha（17 Ultra） | OS3.0.310.0.WNBCNXM | **可开机** |
| `perfect` 早期完美包 | OS4.0.29.0.XEOCNXM / madrid | OS3.0.310.0.WNBCNXM | **可开机** |
| `new2` 本项目产物 | OS4.0.29.0.XEOCNXM / madrid | OS3.0.310.0.WNBCNXM | 卡二屏 |
| `super_density560` 更早产物 | OS4.0.29.0.XEOCNXM / madrid | OS3.0.310.0.WNBCNXM | 卡二屏 |

两个不同源机型（madrid / nezha）的可开机包给出**同一组特征**，可排除版本噪音。

## 可开机包共性（交叉验证）

| 特征 | rom2 (4.0.31) | rom1 (4.0.7 nezha) | 旧产物 bad | new2 |
| --- | --- | --- | --- | --- |
| product `media/rearscreen` | **0** | **0** | 268 | 0 |
| product `etc/precust_theme/**/rearscreen` | **0** | **0** | 有 | 0 |
| system_ext miui-services oat | **17** | **17** | 0 | 38 |
| `system/lib64/libxmiport.so` | 有 | 有 | 无 | 无 |
| `system/app/PowerKeeper` | 无 | 无 | 有 | 有 |
| `app/WMService` | 有 | 有 | 有 | **无** |
| `app/UPTsmService` | 有 | 有 | 有 | **无** |
| `pangu/system/app/NQNfcNci` | 有 | 有 | 有 | **无** |

`libxmiport.so` / `PowerKeeper` 非开机必需：前者是 X-MiPort 的定制功能库（去广告、
状态栏开关），后者是省电管理；两者都不参与启动链，暂不处理。

## 同版本逐分区差异（perfect 4.0.29 → new2）

| 分区 | 完美包 | new2 | 缺失 | 多余 | 标签不同 |
| --- | --- | --- | --- | --- | --- |
| system | 4791 | 4794 | 3 | 6 | **0** |
| system_ext | 3693 | 3702 | 0 | 9 | **0** |
| product | 2309 | 2270 | **139** | 100 | **0** |
| odm | 2321 | 2322 | 0 | 1 | **0** |
| vendor | 2959 | 2958 | 1 | 0 | **0** |
| mi_ext | 147 | 146 | 1 | 0 | **0** |
| vendor_dlkm | 328 | 328 | 0 | 0 | **0** |
| system_dlkm | 186 | 186 | 0 | 0 | **0** |

**SELinux 标签差异为 0** —— 2026-11-15 修复的 `pack_partitions` file_contexts
补全逻辑有效，标签不再是卡机原因。

其余零散差异：

- system 缺 `cacerts/cb1b120a.0`（证书，无害）、`services.art.fsv_meta`（我们主动删
  services.art，见 `drop_stale_services_oat`）
- system 多 `PowerKeeper`（省电管理，无害）
- system_ext 多 `MiuiSystemUI/oat`（多比少安全）
- odm 多 `vibratorfeature...service.default`（**故意**：Android 17 需要双实例振动）
- vendor 缺 `fstab.qcom.bak`（备份文件，无害）
- mi_ext 缺 `rustruntime_cfg_v3_v5.xml`（compat 注入项，旧产物构建时该资产尚未入库）

## 修复清单

1. `devices/shennong/perfect_add_paths.txt`：116 条补入清单
   （product 82 + system_ext 34），构建时从底包复制
2. `shallow_slim.py`：`step_align_perfect_product` 增加**补入**，支持多分区，
   缺项汇总告警
3. `remove_apps.conf`：21 条与完美包冲突的条目用 `!` 停用；
   其中 `product:app:=system` 会误删 `/app/system/system.apk`
4. `perfect_remove_paths.txt`：+20 条漏删（`MiAONServiceX`、`data-app/*`、`displayconfig`）
5. `perfect_baseline.json`：新增 `product_required_dirs` 硬校验；
   基线更新到 4.0.31
6. `verify_perfect_baseline.py`：
   - 支持 `extract.erofs -p` 直接输出的 `<part>.txt`
   - product 清单为空时 **FAIL**（此前会静默全部 PASS，属假阳性）
   - 新增必需应用检查
7. `tools/diff_partitions.py`：新增逐分区逐文件对比工具，输出 Markdown 报告

## 复现方式

```bash
# 导出两份清单（-p 会带 SELinux 标签与 uid/gid/mode）
extract.erofs -p -i perfect_product.img -s > perfect/product.txt
extract.erofs -p -i new_product.img     -s > new2/product.txt

# 逐文件对比
python tools/diff_partitions.py --left perfect --right new2 --verbose --out diff.md

# 按基线校验
python tools/verify_perfect_baseline.py --lists-dir perfect
```

> 静态对比只能排除「结构/内容」层面的偏差。真机仍卡二屏时，必须抓
> `adb logcat` / `/sys/fs/pstore/console-ramoops` 才能定位到具体服务。
