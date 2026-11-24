@echo off
cd /d "%~dp0"
set BIN=bin\Windows
if not exist "%BIN%\fastboot.exe" set BIN=bin\platform-tools-windows
if not exist "%BIN%\fastboot.exe" (
	echo.
	echo.刷机工具缺失，请完整解压后再运行，或关闭杀毒软件后重新解压
	pause
	exit /b 1
)
if not exist "%BIN%\cecho.exe" (
	echo.
	echo.cecho.exe 缺失，请关闭杀毒软件或 Windows 安全中心后重新解压
	pause
	exit /b 1
)
setlocal enabledelayedexpansion
set lang=English
chcp 936 |findstr Active >nul
if "!errorlevel!" NEQ "0" set lang=Chinese
if "!lang!" == "Chinese" (
	title XMAPort 线刷脚本 [请勿选中窗口，卡住按右键或回车恢复]
) else (
	title Fastboot flash script
)
echo.
%BIN%\cecho {F0}                        {0F}{\n}
%BIN%\cecho {F0}     Xiaomi Hyper{F9}OS     {0F}{\n}
%BIN%\cecho {F0}        {F2}XMAPort        {0F}{\n}
%BIN%\cecho {F0}                        {0F}{\n}
if "!lang!" == "Chinese" (
	echo.
	echo.-------ROM INFO-------
	echo.加密状态:Data解密
	echo.机型代号:@@DEVICE@@
	echo.机型名称:@@MODEL@@
	echo.底包版本:@@BASE_VER@@
	echo.移植机型:@@PORT_MODEL@@
	echo.移植版本:@@PORT_VER@@
	echo.Recovery:TWRP 第三方，强制刷入
	echo.----------------------
	echo.
	echo.刷机注意事项：
	echo.1. 首次刷入必须清除数据，脚本会进 TWRP 执行 format data
	echo.2. 本包固定使用第三方 TWRP，不会刷回官方 Recovery
) else (
	echo.
	echo.-------ROM INFO-------
	echo.Data State:DataDecryption
	echo.Device Code:@@DEVICE@@
	echo.Device Name:@@MODEL@@
	echo.Base ROM Version:@@BASE_VER@@
	echo.Port From:@@PORT_MODEL@@
	echo.Port Version:@@PORT_VER@@
	echo.Recovery  :TWRP forced, official recovery is NOT restored
	echo.----------------------
	echo.
	echo.Notes:
	echo.1. Format data is required on first flash ^(TWRP format data^)
	echo.2. TWRP is flashed permanently
)
echo.

REM ---- 强制校验：TWRP 必须存在，super 必须完整 ----
if not exist images\recovery_twrp.img (
	echo.[错误] 缺少 images\recovery_twrp.img，本包强制刷入第三方 TWRP，无法继续
	echo.[ERROR] images\recovery_twrp.img is missing. TWRP flash is mandatory.
	pause
	exit /b 1
)
if exist images\super.img.zst (
	if not exist images\super.img (
		echo.
		if "!lang!" == "Chinese" (echo.正在解压 super.img.zst，请稍候...) else (echo.Decompressing super.img.zst...)
		%BIN%\zstd.exe -d -f -q -T0 images/super.img.zst -o images/super.img
		if "!errorlevel!" NEQ "0" (
			if "!lang!" == "Chinese" (
				echo.super.img 解压失败，最常见原因是磁盘空间不足，请释放约 8 GB 后重试
			) else (
				echo.Failed to decompress super. Free about 8 GB and try again.
			)
			pause
			exit /b 1
		)
	)
)
if not exist images\super.img (
	echo.[错误] 缺少 images\super.img，无法刷入
	pause
	exit /b 1
)
set SUPERSZ=0
for %%F in (images\super.img) do set SUPERSZ=%%~zF
if not "!SUPERSZ!"=="@@SUPER_SIZE@@" (
	echo.[错误] super.img 大小不符：实际 !SUPERSZ! 字节，应为 @@SUPER_SIZE@@ 字节
	echo.说明包未完整解压或磁盘空间不足，刷入不完整 super 会直接无法开机。
	echo.请重新完整解压本包后再运行，切勿继续刷入。
	pause
	exit /b 1
)
echo.super.img 校验通过: !SUPERSZ! 字节
echo.

echo.请将手机进入 fastboot 模式并用数据线连接电脑，刷机过程中不要操作窗口
echo.
echo.检测设备 BL 解锁状态
%BIN%\fastboot getvar unlocked 2>&1 |findstr yes ||echo.未检测到 BL 解锁，刷写可能失败
echo.
if "!lang!" == "Chinese" goto ASK_WIPE_CN
set /p wipeData="Format data and rebuild FBE keys? [Y/n]: "
goto ASK_WIPE_DONE
:ASK_WIPE_CN
set /p wipeData="是否需要清除数据并重建 /data？首次刷入必须，Y/n: "
:ASK_WIPE_DONE
echo.
%BIN%\fastboot devices 2>&1 |findstr /r /c:"fastboot" >nul
if errorlevel 1 goto ERR_NO_DEVICE
%BIN%\fastboot getvar product 2>&1 |findstr /i "@@DEVICE@@" >nul
if "!errorlevel!" neq "0" (
	for /f %%i in ('%BIN%\fastboot getvar product 2^>^&1 ^|findstr product ^|%BIN%\cut -d ":" -f 2') do set readproduct=%%i
	color 0c
	if "!lang!" == "Chinese" (
		echo.机型代号检验不通过，当前为 !readproduct!，本包指定 @@DEVICE@@
		set /p ctnflash="是否仍要继续刷入？[y/n] "
	) else (
		echo.The product test failed. Current: !readproduct!, expected: @@DEVICE@@
		set /p ctnflash="Continue anyway? [y/n] "
	)
	if /i "!ctnflash!" NEQ "y" exit /b 1
	color 0f
)

set errorsum=0
:ROOT_SELECTER
if "!lang!" == "Chinese" (
	echo.
	echo.-------ROOT 选择-------
	echo.
	echo.  ^<1^> 无需ROOT
	echo.  ^<2^> Magisk
	echo.  ^<3^> APatch
	echo.  ^<4^> FolkPatch
	echo.  ^<5^> KernelSU
	echo.  ^<6^> SukiSU-Ultra
	echo.  ^<7^> KernelSU-Next
	echo.  ^<8^> Kitesune Mask
	echo.
	echo.------------------------
	set /p rootType="请选择你的ROOT方式: "
) else (
	echo.
	echo.-------ROOT Selecter-------
	echo.
	echo.  ^<1^> NoROOT
	echo.  ^<2^> Magisk
	echo.  ^<3^> APatch
	echo.  ^<4^> FolkPatch
	echo.  ^<5^> KernelSU
	echo.  ^<6^> SukiSU-Ultra
	echo.  ^<7^> KernelSU-Next
	echo.  ^<8^> Kitesune Mask
	echo.
	echo.---------------------------
	set /p rootType="Select a root type: "
)
echo.
if "!rootType!" EQU "3" if not exist images\boot_apatch.img (
	echo.缺少 images\boot_apatch.img，APatch 在此包中不可用，请重选
	goto ROOT_SELECTER
)
if "!rootType!" EQU "4" if not exist images\boot_folkpatch.img (
	echo.缺少 images\boot_folkpatch.img，FolkPatch 在此包中不可用，请重选
	goto ROOT_SELECTER
)
if "!rootType!" EQU "1" (set "BOOT=boot.img" & set "IB=init_boot_official.img" & goto NEXT_FLASHER)
if "!rootType!" EQU "2" (set "BOOT=boot.img" & set "IB=init_boot_magisk.img" & goto NEXT_FLASHER)
if "!rootType!" EQU "3" (set "BOOT=boot_apatch.img" & set "IB=init_boot_official.img" & goto NEXT_FLASHER)
if "!rootType!" EQU "4" (set "BOOT=boot_folkpatch.img" & set "IB=init_boot_official.img" & goto NEXT_FLASHER)
if "!rootType!" EQU "5" (set "BOOT=boot.img" & set "IB=init_boot_ksu.img" & goto NEXT_FLASHER)
if "!rootType!" EQU "6" (set "BOOT=boot.img" & set "IB=init_boot_sukisu.img" & goto NEXT_FLASHER)
if "!rootType!" EQU "7" (set "BOOT=boot.img" & set "IB=init_boot_ksu-next.img" & goto NEXT_FLASHER)
if "!rootType!" EQU "8" (set "BOOT=boot.img" & set "IB=init_boot_kitsune-mask.img" & goto NEXT_FLASHER)
goto ROOT_SELECTER

:NEXT_FLASHER
if not exist "images\!BOOT!" (
	echo.缺少 images\!BOOT!，请重新完整解压本包
	pause
	exit /b 1
)
if not exist "images\!IB!" (
	echo.缺少 images\!IB!，请重新完整解压本包
	pause
	exit /b 1
)
echo.ROOT: !IB!
echo.
echo.强制刷入 TWRP 第三方 Recovery（recovery_ab 双槽，失败则逐槽刷入）
set TWRP_OK=0
%BIN%\fastboot flash recovery_ab images/recovery_twrp.img && set TWRP_OK=1
if "!TWRP_OK!"=="0" (
	%BIN%\fastboot flash recovery_a images/recovery_twrp.img ||call :ERROR_FLASH recovery_a
	%BIN%\fastboot flash recovery_b images/recovery_twrp.img ||call :ERROR_FLASH recovery_b
)
echo.

%BIN%\fastboot erase frp
%BIN%\fastboot getvar all 2>&1 |findstr "partition-type:cust" && %BIN%\fastboot erase cust
%BIN%\fastboot flash xbl_ramdump_ab images/xbl_ramdump.img ||call :ERROR_FLASH xbl_ramdump_ab
%BIN%\fastboot flash xbl_config_ab images/xbl_config.img ||call :ERROR_FLASH xbl_config_ab
%BIN%\fastboot flash xbl_ab images/xbl.img ||call :ERROR_FLASH xbl_ab
%BIN%\fastboot flash vm-bootsys_ab images/vm-bootsys.img ||call :ERROR_FLASH vm-bootsys_ab
%BIN%\fastboot flash vendor_boot_ab images/vendor_boot.img ||call :ERROR_FLASH vendor_boot_ab
%BIN%\fastboot --disable-verity --disable-verification flash vbmeta_system_ab images/vbmeta_system.img ||call :ERROR_FLASH vbmeta_system_ab
%BIN%\fastboot --disable-verity --disable-verification flash vbmeta_ab images/vbmeta.img ||call :ERROR_FLASH vbmeta_ab
%BIN%\fastboot flash uefisecapp_ab images/uefisecapp.img ||call :ERROR_FLASH uefisecapp_ab
%BIN%\fastboot flash uefi_ab images/uefi.img ||call :ERROR_FLASH uefi_ab
%BIN%\fastboot flash tz_ab images/tz.img ||call :ERROR_FLASH tz_ab
%BIN%\fastboot flash spuservice_ab images/spuservice.img ||call :ERROR_FLASH spuservice_ab
%BIN%\fastboot flash shrm_ab images/shrm.img ||call :ERROR_FLASH shrm_ab
%BIN%\fastboot flash qupfw_ab images/qupfw.img ||call :ERROR_FLASH qupfw_ab
%BIN%\fastboot flash multiimgqti_ab images/multiimgqti.img ||call :ERROR_FLASH multiimgqti_ab
%BIN%\fastboot flash modemfirmware_ab images/modemfirmware.img ||call :ERROR_FLASH modemfirmware_ab
%BIN%\fastboot flash modem_ab images/modem.img ||call :ERROR_FLASH modem_ab
%BIN%\fastboot flash keymaster_ab images/keymaster.img ||call :ERROR_FLASH keymaster_ab
%BIN%\fastboot flash imagefv_ab images/imagefv.img ||call :ERROR_FLASH imagefv_ab
%BIN%\fastboot flash hyp_ab images/hyp.img ||call :ERROR_FLASH hyp_ab
%BIN%\fastboot flash featenabler_ab images/featenabler.img ||call :ERROR_FLASH featenabler_ab
%BIN%\fastboot flash dtbo_ab images/dtbo.img ||call :ERROR_FLASH dtbo_ab
%BIN%\fastboot flash dsp_ab images/dsp.img ||call :ERROR_FLASH dsp_ab
%BIN%\fastboot flash devcfg_ab images/devcfg.img ||call :ERROR_FLASH devcfg_ab
%BIN%\fastboot flash cpucp_dtb_ab images/cpucp_dtb.img ||call :ERROR_FLASH cpucp_dtb_ab
%BIN%\fastboot flash cpucp_ab images/cpucp.img ||call :ERROR_FLASH cpucp_ab
%BIN%\fastboot flash bluetooth_ab images/bluetooth.img ||call :ERROR_FLASH bluetooth_ab
%BIN%\fastboot flash aop_config_ab images/aop_config.img ||call :ERROR_FLASH aop_config_ab
%BIN%\fastboot flash aop_ab images/aop.img ||call :ERROR_FLASH aop_ab
%BIN%\fastboot flash abl_ab images/abl.img ||call :ERROR_FLASH abl_ab
%BIN%\fastboot flash boot_ab images/!BOOT! ||call :ERROR_FLASH boot_ab
%BIN%\fastboot flash init_boot_ab images/!IB! ||call :ERROR_FLASH init_boot_ab
echo.
echo.若出现 invalid sparse file format at header magic 属正常探测信息，可忽略
echo.正在刷写 super，请耐心等待，不要拔线或手动重启
echo.
%BIN%\fastboot flash super images/super.img ||call :ERROR_FLASH super
echo.
if not "!errorsum!"=="0" (
	color 0c
	echo.刷写过程中出现 !errorsum! 个错误，请勿重启手机，检查连接后重跑本脚本
	pause
	exit /b 1
)
echo.super 刷写完成，未出现错误。
echo.

%BIN%\fastboot set_active a ||call :ERROR_OPRATE set_active_a
echo.强制进入 TWRP 第三方 Recovery，不直接重启进系统
%BIN%\fastboot reboot recovery ||call :ERROR_OPRATE reboot_recovery
if /i "!wipeData!"=="n" goto TWRP_MANUAL
echo.等待 TWRP 的 adb 上线（最多 180 秒）...
set /a N=0
:WAIT_TWRP
set /a N+=1
if !N! GTR 180 goto TWRP_TIMEOUT
%BIN%\adb devices 2>&1 |findstr /i "recovery" >nul && goto TWRP_OK
ping -n 2 127.0.0.1 >nul
goto WAIT_TWRP
:TWRP_OK
echo.卸载 /sdcard、/data 与 /metadata（TWRP 常把 /data 挂在 dm 映射上）
%BIN%\adb shell "umount /sdcard 2>/dev/null; umount /data 2>/dev/null; umount /metadata 2>/dev/null"
%BIN%\adb shell "dmctl delete userdata 2>/dev/null"
echo.正在用 make_f2fs 重建 /data（启用 FBE 加密特性，兼容 OS4）
%BIN%\adb shell "make_f2fs -f -O encrypt,extra_attr,project_quota,compression /dev/block/bootdevice/by-name/userdata" > "%TEMP%\xm_fmt.log" 2>&1
type "%TEMP%\xm_fmt.log"
findstr /i "format successful" "%TEMP%\xm_fmt.log" >nul
if errorlevel 1 (
	color 0c
	echo.[错误] /data 格式化失败，请把上面的日志发出来
	pause
	goto DONE
)
echo.保留 /metadata（首次启动会自动沿用或重建加密密钥）
echo.格式化完成，正在重启进系统
%BIN%\adb reboot
set /a RETRY=0
set /a WAIT=0
:WAIT_SYSTEM
timeout /t 5 /nobreak >nul
set /a WAIT+=5
if !WAIT! GTR 300 goto BOOT_TIMEOUT
set "MODE="
for /f "skip=1 tokens=1,2" %%A in ('%BIN%\adb devices 2^>nul') do (
	if /i "%%B"=="recovery" set "MODE=recovery"
	if /i "%%B"=="device" set "MODE=device"
)
if /i "!MODE!"=="recovery" (
	set /a RETRY+=1
	if !RETRY! GTR 2 goto BOOT_TIMEOUT
	echo.首次启动未完成（正在生成加密密钥），自动再重启一次...
	%BIN%\adb reboot
	set /a WAIT=0
	goto WAIT_SYSTEM
)
if /i "!MODE!"=="device" (
	set "BC="
	for /f "delims=" %%C in ('%BIN%\adb shell getprop sys.boot_completed 2^>nul') do set "BC=%%C"
	if "!BC!"=="1" goto BOOT_OK
)
goto WAIT_SYSTEM
:BOOT_OK
echo.已进入系统。首次开机 dexopt 需要 5-10 分钟，发热属正常
goto DONE
:BOOT_TIMEOUT
echo.[警告] 5 分钟内未检测到系统启动完成
echo.请手动重启一次；若仍回 recovery，请把屏幕拍照发出来
pause
goto DONE
:TWRP_TIMEOUT
echo.180 秒内未检测到 TWRP 的 adb（可能 TWRP 未开 ADB 调试）
echo.请运行同目录的 3-Format-Data.bat 重建 /data，再重启系统
echo.不格式化 Data 将无法开机
pause
goto DONE
:TWRP_MANUAL
echo.你选择不自动清除数据。请运行同目录的 3-Format-Data.bat
echo.重建 /data 后再重启系统
echo.注意：首次刷入或从加密 Data 的包互刷，不格式化 Data 将无法开机
pause
goto DONE

:DONE
echo.
if "!errorsum!"=="0" (
	color 0a
	if "!lang!" == "Chinese" (
		echo.刷机脚本执行完毕，刷机过程未出现异常
	) else (
		echo.Flash done, no error
	)
) else (
	color 0c
	echo.刷机脚本执行完毕，出现 !errorsum! 个异常，请检查后重刷
)
echo.
echo.Recovery 已固定为 TWRP；如需官方 Recovery 请自行单独刷入
echo.若选了 ROOT，请到包内 RootManager 文件夹安装对应管理器
echo.若仍无法开机，请运行 3-Format-Data.bat 重建 /data 后重启
echo.
pause
exit /b 0

:ERR_NO_DEVICE
echo.
echo.[已中止] 未检测到 fastboot 设备，请检查驱动与数据线、确认手机处于 fastboot 模式
echo.
pause
exit /b 1

:ERROR_FLASH
set /a errorsum+=1
echo.
if "!lang!" == "Chinese" (
	echo.刷写分区 %* 时出错
) else (
	echo.An error occurred while flash partition %*
)
echo.
exit /b

:ERROR_OPRATE
set /a errorsum+=1
color 0c
echo.
echo.Fastboot 指令执行出错: %*
echo.
exit /b
