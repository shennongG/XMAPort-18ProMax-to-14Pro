# 310 + 29 卡 HyperOS 二屏根因与 V5 修复方案

> 适用：源包 `OS4.0.29.0.XEOCNXM / madrid`（小米 18 Pro Max，Android 17）
> → 目标 `OS3.0.310.0.WNBCNXM / shennong`（小米 14 Pro）
>
> **2026-11-14 真机验证通过**：开机正常进桌面、振动正常、字体大小正常。
> 本文是对 V5 修复包的完整复盘，用于让后续 Actions 构建不再复现同类问题。

---

## 一、结论速览

| 项 | 结论 |
| --- | --- |
| 版本组合 | **310 底包 + 29 源包可行**，不需要退回 309 / 27；前提是 compat 注入齐全 |
| 卡二屏根因 | 不是版本、不是固件、不是 boot 链，**是后注入文件缺 SELinux 标签** |
| 字体小根因 | `product/etc/build.prop` 沿用源机 madrid 的密度 480，shennong 原生应为 560 |
| 无振动根因 | odm 只注册 `IVibrator/vibratorfeature`，Android 17 还需要 `IVibrator/default` |
| 修复策略 | 以「已验证可开机」的 V4 super 为底座，**只原地替换 odm_a / product_a 两个动态分区**，其余一字节不动 |

---

## 二、两个包的对比证据

对比对象：

- **V4 包**（`rom1`，`XMAPORT-429-V4`）：能开机，但无振动、字体小。
- **Actions 全新包**（`rom2`）：同为 310 + 29，刷后卡 HyperOS 开机动画、机身发热、超过 20 分钟无变化。

| 对比项 | 结果 |
| --- | --- |
| 25 个固件分区 + `boot` / `vendor_boot` / `dtbo` / `vbmeta` | md5 **逐字节完全一致** |
| `init_boot` | 仅差 91 字节（`build.date` / 309 vs 310 指纹），非阻断项 |
| `recovery_twrp` | TWRP 版本不同，非阻断项 |
| `super.img.zst` | md5 不同；V4 可开机，Actions 卡机 → 差异全部在 super 内容 |

### 关键差异：无 SELinux 标签的文件数

用 `tools/extract.erofs.exe -i <part>.img -p -s` 列出每个分区的
`fsConfig` / `seLabel`，统计 `type=FILE` 且 `seLabel=[]` 的条目：

| 分区 | V4（可开机） | Actions（卡机） |
| --- | --- | --- |
| 全部只读分区 | 4 个（均为无关的 PowerKeeper 残留） | 9 个 |

Actions 多出来的 5 个恰好全是启动关键项：

| 文件 | 应有标签 |
| --- | --- |
| `system/etc/selinux/mapping/30.0.cil` | `u:object_r:sepolicy_file:s0` |
| `system/etc/selinux/mapping/30.0.compat.cil` | `u:object_r:sepolicy_file:s0` |
| `system/etc/keystube.zip` | `u:object_r:system_file:s0` |
| `system/etc/toast.dex` | `u:object_r:system_file:s0` |
| `system/bin/bugreporter` | `u:object_r:shell_exec:s0` |
| `odm/.../vibratorfeature.service.default` | `u:object_r:hal_vibrator_default_exec:s0` |

V4 里这些文件全部带正确标签；Actions 里全为空标签（`unlabeled`）。
在 enforcing 模式下，`init` / `system_server` 读不到 30.0 兼容映射、
HAL 二进制无法 exec → 开机动画反复重启、CPU 空转发热。

### 源码定位

`tools/pack_partitions.py` 的 `file_contexts` 自动补全原先写死：

```python
_FC_ALLOW_PARTS = ("product", "system_ext")
```

而 `tools/shallow_slim.py` 往 **system** 注入 VNDK compat / `30.0*.cil` /
`keystube.zip` / `toast.dex`，`fix_vibrator_hal.py` 往 **odm** 注入第二个
HAL 副本。这些新增文件在原始 `file_contexts` 里没有规则，system / odm
又不在补全白名单 → 打出的镜像里 `seLabel` 为空。

### 本次同步的源码修复

`tools/pack_partitions.py`：

- `_FC_ALLOW_PARTS` 放开到 8 个只读分区：`product / system_ext / system /
  vendor / odm / mi_ext / system_dlkm / vendor_dlkm`。
- 为新增分区补 `_FC_DEFAULT_LABEL`，避免落到 `unlabeled`。
- `_FC_SPECIAL_DEFAULT` 追加 compat 注入文件与振动 HAL 的精确标签规则。

> 该改动是「构建侧」修复，需重新跑一次 Actions 才能验证；V5 刷机包本身
> 是「产物侧」修复（基于已开机的 V4 super），两者互不依赖。

---

## 三、另外两个已修复的问题

### 字体小：密度 480 → 560

`product/etc/build.prop` 里：

```properties
ro.sf.lcd_density=480
persist.miui.density_v2=480
```

源自 madrid。改为 shennong 原生值，并补一条显示属性：

```properties
ro.sf.lcd_density=560
persist.miui.density_v2=560
vendor.display.lcd_density=560
```

`ro.sf.lcd_density` 是 set-once 属性，MIUI 后续的 `persist.miui.density_v2`
不一定能覆盖，所以必须直接改 `build.prop`；已刷过机的设备还需要清
`/data/property/persist.miui.density_v2`（即 `format data`）才彻底生效。
工具：`tools/fix_density.py <product_tree_dir>`。

### 无振动：IVibrator 双实例

底包 odm 只注册 `IVibrator/vibratorfeature`。Android 17 的
`VibratorManagerService` 找的是 `IVibrator/default`，只留前者 →
`vibratorIds=[]`，触感全部 `IGNORED_UNSUPPORTED`；只改成 `default` 又会
卡在 HyperOS 开机界面（MIUI framework 硬等 `vibratorfeature`）。
**必须两个实例同时存在**（2026-11-09 真机定案）。
工具：`tools/fix_vibrator_hal.py --image odm_a.img --out odm_a_fixed.img`。

---

## 四、V5 修复包构造（可复现步骤）

原则：**不动 super 的 LP 元数据与分区布局**，只把两个逻辑分区的镜像内容
原地写回各自的 extent，因此 `super.img` 仍是 `8,321,499,136` 字节，
线刷脚本里的大小校验值无需改动。

Windows 下 erofs 工具需要禁止 MSYS2 路径转换：

```bash
export MSYS2_ARG_CONV_EXCL='*'
```

1. 从 V4 的一体包取出 raw `super.img`（`images/super.img.zst` 用 `zstd -d` 解压）。
   记录 `lpdumps super.img` 里 `odm_a` / `product_a` 的 extent 偏移与长度
   （单位 sector，乘 512 得字节）。V4 的实测值：

   | 分区 | offset (sector) | size (sector) |
   | --- | --- | --- |
   | `odm_a` | 3713024 | 4516784 |
   | `product_a` | 9500672 | 5275688 |

   > 换任何其它 super 都必须重新用 `lpdumps` 读，不要套用上表。

2. 解出两个逻辑分区并导出打包配置：

   ```bash
   tools/lpunpack.exe -p odm_a -p product_a super.img parts/
   tools/extract.erofs.exe -i parts/product_a.img -x -o prod_work -s
   # 得到 prod_work/product_a/ 与 prod_work/config/product_a_{fs_config,file_contexts,fs_options}
   ```

3. 振动修复（直接对 odm 镜像操作，工具内部会同时补 `file_contexts`）：

   ```bash
   python tools/fix_vibrator_hal.py --image parts/odm_a.img --out odm_a_v5.img
   ```

4. 密度修复 + 用原参数重建 product 镜像：

   ```bash
   python tools/fix_density.py prod_work/product_a
   tools/mkfs.erofs.exe -zlz4hc -T 0 -U <原 UUID> \
     --mount-point=/product_a \
     --fs-config-file=prod_work/config/product_a_fs_config \
     --file-contexts=prod_work/config/product_a_file_contexts \
     product_a_v5.img prod_work/product_a
   ```

   `-U` 与 `--mount-point` 必须与 `extract.erofs` 导出的原值一致，
   否则分区 UUID / 挂载点变化会影响启动。

5. 写回 super（Python，按字节偏移覆盖，剩余空间补 0）：

   ```python
   import shutil
   shutil.copyfile('super.img', 'super_v5.img')
   regions = [('odm',     3713024*512, 4516784*512, 'odm_a_v5.img'),
              ('product', 9500672*512, 5275688*512, 'product_a_v5.img')]
   with open('super_v5.img', 'r+b') as f:
       for name, off, size, img in regions:
           data = open(img, 'rb').read()
           assert len(data) <= size
           f.seek(off); f.write(data)
           rest = size - len(data)
           while rest > 0:
               n = min(rest, 1 << 20); f.write(b'\0' * n); rest -= n
   ```

6. 重新压缩并替换一体包里的 `images/super.img.zst`：

   ```bash
   zstd -3 -T0 -f -q super_v5.img -o super_v5.img.zst
   ```

7. **校验后再刷**（本次全部实测通过）：

   - `lpdumps super_v5.img`：8 分区、`virtual_ab`、`device_size` 不变；
   - `lpunpack -p odm_a -p product_a super_v5.img`：解出的两个镜像可正常读取；
   - `extract.erofs -p`：odm 无 `seLabel=[]`，product 路径集合 / 权限 / 标签与原镜像完全一致；
   - 从 product 里取出 `etc/build.prop` 确认 560；
   - 包内 `super.img.zst` 与本地文件 md5 一致，且能完整解压出 8,321,499,136 字节。

> 注意：刷机 bat 是 **GBK 编码**，若需要改其中的数值请按字节替换
> （例如 `b'8312780636' -> b'8321499136'`），不要用文本编辑器另存为 UTF-8，
> 否则中文会变成乱码。

---

## 五、为什么不把 V5 刷机包上传到这里

V5 一体包约 **6.63 GB**，其中 `super.img` 单文件 8.3 GB（raw）。
GitHub 单文件上限 2 GB、仓库容量也不适合存放镜像，且 ROM 版权归小米所有。
因此本仓库只同步**源码修复 + 方案文档 + 可复现步骤**；产物仍按
`.github/workflows/build.yml` 的方式用 7z 分卷发 Release，或按上节本地生成。

---

## 六、刷机与验证要点

1. 完整解压一体包，路径不要有中文和空格。
2. 手机进 fastboot，运行 `XMAPort-Flash-V4.bat` / `XMAPort-Flash-Auto.bat`。
3. ROOT 选 **1（不 ROOT）** 最稳；清数据必须选 **y**（进 TWRP `format data`，
   重建 f2fs，同时让密度 560 彻底生效）。
4. 首次开机 dexopt 会发热 10~25 分钟，属正常，不要中途断电。
5. 已经刷了 V4 且能开机的设备，不必重刷 8 GB super：
   在 `fastboot reboot fastboot`（fastbootd）下单独刷 `odm_a` 与 `product_a`
   两个分区镜像即可。回退时把 `odm_a` 单独刷回 V4 原版。

---

## 七、已知问题 / 后续待办

- `XMAPort.py` 的 Step 6 已调用 `fix_vibrator_hal_tree()` /
  `fix_density_tree()` / `fix_screen_flicker_tree()`，但实测两份产物里
  仍是单实例振动 HAL、密度 480。推测与 `WORKSPACE/target_filesystem` 树路径
  或 `--slim-only` 分支有关，需在下次构建日志里确认这三个步骤是否
  真的执行、是否有 `WARN` / `SKIP`。在此之前，**产物侧仍需按第四节二次修复**。
- `pack_partitions.py` 的 file_contexts 补全修复需在 push 后重新跑一次
  Actions，并用 `extract.erofs -p` 确认 system / odm 无 `seLabel=[]`。
- 已知 `build_flashpack.py` 的 `super_raw_size()` 曾把 sparse 大小注入校验
  （commit `7d4944b` 已修为使用 simg2img 后的 raw 大小），出包后仍建议
  先离线比对再上机。

---

## 八、线刷脚本闪退 `XMAPort-Flash-V4.bat` 的修复（2026-11-14）

### 现象

双击脚本显示到「是否需要清除数据？[y/n]」，输入 `y` 后出现
「请按任意键继续 . . .」，按键后窗口直接关闭，看起来像闪退。
改用非交互的 `XMAPort-Flash-Auto.bat` 可以正常刷入（该脚本预填了
`wipeData` 与 ROOT 类型，绕开了交互段）。

### 根因（bat 写法问题叠加，与 ROM 无关）

1. `set /p wipeData` 被包在 `if "!lang!"=="Chinese" (...) else (...)` 复合块内，
   中文系统下输入后解析容易出错。
2. 刷 super 前的 else 块里 `echo.Flashing super (8.32 GB) ...` 的**英文右括号
   未转义**，会提前闭合语句块，导致后续解析错乱。
3. 设备检测写成 `fastboot devices ... || ( echo + pause + exit /b 1 )`，
   双击运行时一旦检测失败就直接关窗，用户看不到失败原因。
4. 大小校验用 `for %%F in (...) do if %%~zF NEQ ...`，文件缺失时 `%%~zF`
   为空会造成 `if  NEQ  ...` 语法错误；`if "!errorlevel!" NEQ "0"` 也不可靠。

### 修复版

`tools/offline_flashpack/XMAPort-Flash-V4-Fixed.bat`（GBK + CRLF，与真机刷入
所用版本一致）：

- 标签式交互，所有 `set /p` 移出复合块；
- 统一 `:FAIL` 出口：显示**具体原因** + `pause` + `exit 1` 彻底终止，
  避免 `call` 返回后继续刷写（这点很关键，否则设备异常时会继续刷）；
- 大小校验改成 `for %%F in (...) do set "SZ=%%~zF"` + `if not "!SZ!"==...`；
- `if errorlevel 1` 取代 `if "!errorlevel!" NEQ "0"`；
- 刷写分区清单、ROOT 选择、TWRP `format data` 流程与原版完全一致。

仓库模板 `tools/offline_flashpack/XMAPort-Flash.template.bat` 已同步修掉
「wipe 交互在复合块内」和「设备检测无提示直接退出」两处，保留全部
`@@DEVICE@@` / `@@SUPER_SIZE@@` 占位符，不影响 Actions 出包。

### 离线验证方法（不需要插手机）

拔掉数据线直接双击修复版脚本：应走到设备检测并停住，显示
`[已中止] 未检测到 fastboot 设备 ...`，而不是窗口消失。
看到这句就说明交互段与错误出口都正常。

### 解压旧包时请注意

V5 一体包里原来的 `XMAPort-Flash-V4.bat` 仍是旧版，包内另追加了
`XMAPort-Flash-V4-Fixed.bat`。重新解压后请用 **Fixed** 那个，
或把它改名覆盖同名文件（旧版建议先留 `.orig.bak` 备份）。


---

## 九、2026-11-15 真机验证：Actions 全新构建包不能开机

### 现象

刷入 `6f4239e` 之后重新 Actions 构建的 310+29 包（run `43592334771` / head `b002a0a`）后：
反复重启两次进入 RE 模式，无法自动启动；手动格式化 data 后重启仍卡在 Xiaomi HyperOS 二屏。

### 结论修正

**「8 个动态分区 0 个无 SELinux 标签文件」是必要条件，不是充分条件。**
`pack_partitions.py` 的修复确实消除了「后注入文件无标签」这个卡机根因，
但全新构建产物与真机验证过的 V5 之间仍有实质差异，静态验收全通过仍可能开不了机。
此前把 `30.0*.cil` → `system_file` 判为「低风险」的结论作废。

### 排除项：boot 链与固件完全一致

新包与 V5 逐字节 md5 对比：`boot / vendor_boot / dtbo / vbmeta / vbmeta_system`
以及 modem、modemfirmware、bluetooth、dsp、abl、xbl*、tz、uefi、vm-bootsys 等
13 项固件 **全部 SAME**；仅 `init_boot_official.img`（构建指纹）、
`recovery_twrp.img`（TWRP 版本）与 `super.img` 不同。问题在 super 内容。

### super 差异（V5 可开机 vs 新包卡机）

| 分区 | 差异 |
| --- | --- |
| product | 2176 → 2533 项，多 357 项：precust_theme 232、media/rearscreen 83（背屏资源）、app/subscreencenter（副屏服务）、多个 overlay 与 MIUI data-app，均为源机 madrid 专属且 shallow 未精简 |
| system | 多 40 项 priv-app；少 framework/oat/arm64/services.art/odex/vdex 6 个预编译文件 |
| system_ext | 少 41 项 framework/oat/*/miui-services.*.odex/vdex |
| vendor | 完全一致 |
| odm | 仅多已验证无害的双实例振动 HAL |
| 标签 | 30.0.cil / 30.0.compat.cil = system_file（V5 是 sepolicy_file）；bugreporter = system_file（V5 是 shell_exec） |

### 嫌疑排序（未抓 logcat，静态推断）

1. 30.0*.cil 标成 system_file → init 加载 split sepolicy / mapping 失败 → system_server 起不来，最吻合「卡动画 + 反复重启进 RE」。07479dd8 已补双前缀特殊名单，但要下次构建才生效。
2. product 里 madrid 的副屏 / 背屏 / 主题资源与未精简应用，在 shennong 上可能让系统服务崩溃循环。
3. services / miui-services 的 oat 被删，主要拖长首次 dexopt，单独不太会触发 rescue。

### 处置

- **不要用 310+29 的 Actions 全新构建包上机**；`XMAPort_shennong_OS4.0.29.0.XEOCNXM_V5_bootfix.zip`
  仍是唯一真机验证可用（进桌面 / 振动正常 / 字体正常）的包。
- 回退：boot 链与固件已一致，只需重刷 V5 的 `super.img`（md5 `ea451cc6ac1eb62b66067cb71bde0839`）、
  `init_boot_official.img`、`recovery_twrp.img`，再进 TWRP `format data`（必须，FBE 密钥不匹配会回 recovery）。
- 下次构建后先确认 `30.0*.cil` 的 seLabel 已是 `sepolicy_file`，并**真机验证**再推广；
  若要定位确切崩溃点，需在卡机时抓 `adb logcat` 或 TWRP 里取 `/sys/fs/pstore/console-ramoops`。
- 更稳的路线仍是「以 V5 super 为底座做最小改动」，而非从源码全新构建。


---

## 十、按同版本完美包内容对齐（2026-11-15 构建侧落地）

上一章定位到「全新构建偏离已验证基线」后，把 X-MiPort 同版本完美包的
内容特征固化进仓库，让每次构建都朝该基准收敛，而不是靠上机试错。

### 新增文件

| 路径 | 作用 |
| --- | --- |
| `devices/shennong/perfect_baseline.json` | 完美包内容基线：8 个分区文件数、3 个关键 seLabel、system_ext miui-services oat 下限、product 禁用目录与条目上限、密度 560、振动实例要求 |
| `devices/shennong/perfect_remove_paths.txt` | product 删除清单，258 个文件。由三方清单求差生成：仅取「全新构建独有、且完美包与 V5 都不含」的路径，避免误删有效资源 |
| `tools/verify_perfect_baseline.py` | 校验工具，支持 `--tree`（CI 查内容树 + file_contexts）与 `--lists-dir`（本地查 `extract.erofs -p -s` 清单）两种模式 |

### 构建流程改动

- `tools/shallow_slim.py` 新增第 8 步 `align_perfect_product`（`python tools/shallow_slim.py align` 可单跑）：按删除清单裁剪 `source_filesystem/product`。开关 `config.ini -> align_perfect_product=true`。
- `config.ini` 新增该开关；`drop_stale_services_oat` 的注释已更新为「只删 system 的 services oat，保留 system_ext」。
- `.github/workflows/build.yml` 在 `Run XMAPort` 之后、`Pack/split output` 之前新增 **Verify against perfect baseline** 步骤，校验不通过直接让构建失败，阻止同类卡机包流出。
- 之前的 `07479dd8`（密度 + 特殊标签）、`6accfc014bf1`（保留 system_ext oat + 删 rearscreen）与本轮改动共同覆盖三项偏离。

### 校验项与实测结果

| 检查 | 卡机的全新构建 | 完美包 | V5 |
| --- | --- | --- | --- |
| `30.0.cil` / `30.0.compat.cil` / `bugreporter` 标签 | FAIL x3 | PASS | PASS |
| system_ext miui-services oat（>=15 本体文件） | 0 -> FAIL | 17 -> PASS | 19 -> PASS |
| `product/media/rearscreen` | 83 项 -> FAIL | 0 | 0 |
| `precust_theme` / `subscreencenter` / `overlay` 上限 | 428 / 9 / 111 -> FAIL x3 | 196 / 3 / 98 | 196 / 3 / 98 |
| **总判定** | **8 项 FAIL** | **PASS**（3 项 WARN） | **PASS**（3 项 WARN） |

> 计数口径：oat 只算 `.odex/.vdex/.art` 本体，不含 `.fsv_meta`；清单模式下密度与 VINTF 无法读取，输出 WARN 不影响退出码。

### 本地验包

```bash
# 从 super 解出分区并导出清单
tools/lpunpack.exe -p system_a -p system_ext_a -p product_a -p odm_a super.img outdir
for f in outdir/*_a.img; do tools/extract.erofs.exe -i "$f" -p -s > list_new_$(basename "$f" .img).txt; done

# 校验
python tools/verify_perfect_baseline.py --lists-dir . --tag new
```

CI 侧等价命令：`python tools/verify_perfect_baseline.py --tree workspace/source_filesystem --device shennong`。

### 边界与注意

- 完美包 odm 只有 `IVibrator/vibratorfeature` 单实例；本项目 V5 用双实例并已真机验证，因此基线只要求「至少 1 个实例」，不强制退化成单实例。
- 删除清单是**内容差异**而非「完美包全量允许清单」，保留本项目后续注入文件的能力；若更换源包版本，需重新用三方清单求差生成。
- 校验通过只代表「内容对齐已验证基线」，**不能替代真机验证**；V5 与完美包仍是当前两个已验证可开机的基准。
