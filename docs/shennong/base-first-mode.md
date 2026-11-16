# base_first：以 310 底包为骨架的移植模式（实验性）

日期：2026-11-15　适用：`shennong` / 底包 `OS3.0.310.0.WNBCNXM` + 源包 `OS4.0.29.0.XEOCNXM`

## 为什么做这个模式

同版本 X-MiPort 完美包（真机可开机）与本项目产物对比发现，两者是**两条不同的移植路线**：

| | 现有路线（source_first，默认） | 完美包 / base_first |
| --- | --- | --- |
| system / system_ext / product 主体 | 4.0.29 源包 madrid | 310 底包 shennong |
| odm / vendor / mi_ext / boot / 固件 | 310 底包 | 310 底包 |
| 密度 | 源包 480，靠脚本改 560 | 底包原生 560 |
| 机型资源 / displayconfig / oat | 源包带来 madrid 背屏/副屏资源，需裁剪 | 底包天然配套 |
| VNDK / 30.0 compat | 需注入并补 SELinux 标签 | 仍需处理（framework 是 Android 17） |

证据：仓库 ` devices/shennong/golden_baseline.json`（4.0.27 的 shennong 底包骨架）的 6 个分区尺寸与完美包**几乎 1:1**（±0.6%），而 source_first 产物偏离 0.8%~8%；`XMAPort.py` 第 2432 行 `PACK_SOURCE_PARTS` 固定从 `SRC_FS` 打包，构建日志的 mkfs 源目录也直接印证。

前面所有补丁（compat 注入、特殊标签、密度 560、保留 miui-services oat、删 258 个 madrid 文件、CI 内容基线）本质上都是在给 source_first 路线打补丁。base_first 让这些差异在源头上就少一大半。

## 实现：只重建内容树，不换目录名

`shallow_slim.py` / `fix_density` / `pack_partitions.py` 全部固定操作 `workspace/source_filesystem`。所以新模式**不引入新目录名**，而是在 Step 5 之后新增 **Step 5.5**，原地重建这三个分区的内容：

1. 用 `target_filesystem/<part>` 覆盖 `source_filesystem/<part>` 的同名文件（底包骨架）
2. `keep` 规则命中的路径跳过覆盖（保留 4.0.29 的 framework / 应用）
3. `base` 规则优先级高于 `keep`，用于给宽泛 keep 开例外（例如 oat 用底包的）
4. `drop` 规则 + `devices/shennong/perfect_remove_paths.txt` 在合并后删除 madrid 专属资源
5. 源包独有、底包没有的文件自然保留 —— 这就是 4.0.29 增量

`system-as-root` 的 `<part>/<part>/` 双层布局由脚本自动探测，源包与底包分别定位内容根后按相对路径合并。合并统计写入 `workspace/base_first_merge.json`。

## 文件

| 路径 | 作用 |
| --- | --- |
| `tools/base_first_merge.py` | 合并实现，支持 `--device` / `--workspace` / `--dry` |
| `devices/shennong/base_first_rules.conf` | `keep` / `base` / `drop` 目录级规则 |
| `devices/shennong/perfect_remove_paths.txt` | 258 个精确删除项（复用已有清单） |
| `XMAPort.py` Step 5.5 | 受 `packing.base_first` 控制调用合并脚本 |
| `config.ini` `[packing] base_first` | 开关，默认 **false** |
| `.github/workflows/build.yml` | 新增 `base_first` 手动输入，默认 false |

## 启用方式

- 本地：把 `config.ini` 的 `base_first` 改为 `true`，照常跑 `python XMAPort.py --auto --device shennong`；也可先单跑 `python tools/base_first_merge.py --device shennong --dry` 看合并统计。
- Actions：Run workflow 时勾选 **base_first**（默认不勾），构建日志会出现 `Step 5.5/8: base_first` 与 `[OK] base_first 内容树重建完成`。
- 现有 `Verify against perfect baseline` 步骤检查的正是重建后的 `source_filesystem`，模式切换后照常生效。

## 默认为什么不切

1. **规则是目录级初始版本，不是成品清单。** 其它作者是逐文件挑选增量；本模式的 `keep`/`base`/`drop` 需要用真机反馈（logcat、功能回归）迭代到文件级。
2. **并集可能变大**：底包文件与源包独有文件会同时存在，靠 `auto_fit=true` 兜底；分区尺寸需重新确认。
3. **跨版本兼容仍需处理**：4.0.29 framework 配 310 vendor，VNDK / 30.0 mapping 注入仍要保留；`product/etc/build.prop` 特意留在源包侧，密度由 `fix_density_tree` 改 560。
4. 当前 `source_first` 路线刚完成基线对齐、CI 全 PASS，只差真机验证；保持默认可避免两个变量同时变化。

## 验证路径（必须真机）

1. 先勾选 `base_first` 跑一次 Actions，确认 Step 5.5 成功、CI 内容基线校验通过、产物 super 布局正常。
2. 出包后按 `docs/shennong/310-29-V5-bootfix.md` 的静态清单验一遍（标签 / oat / rearscreen / 密度 / 振动）。
3. 上机验证；若卡开机，取 `/sys/fs/pstore/console-ramoops` 与 `adb logcat`，把崩点对应到具体文件后**细化 `base_first_rules.conf`**。
4. 迭代到能稳定进桌面、振动与字体正常后，再考虑把 `config.ini` 默认值切成 `true`。

## 回退

把 `base_first` 设回 `false`（或 Actions 不勾选）即完全回到现有路线，本模式不改动任何默认路径下的行为；`base_first=false` 时 Step 5.5 不执行，新增脚本与规则文件不参与构建。
