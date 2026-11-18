#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置读写 + 开机自启动(注册表 Run 键, 无需管理员权限)。"""

import os
import sys
import json
import winreg

if getattr(sys, "frozen", False):
    # 打包成 exe 后, 配置必须放在 exe 同目录, 不能放临时解压目录
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "IPGW-AutoConnect"

DEFAULT = {
    "username": "",
    "password": "",
    "ac_id": 1,
    "interval_minutes": 3,
    "autostart": True,
    "minimize_to_tray": True,
}


def load() -> dict:
    cfg = dict(DEFAULT)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg.update({k: v for k, v in data.items() if k in DEFAULT})
        except Exception:
            pass
    return cfg


def save(cfg: dict) -> None:
    data = {k: cfg.get(k, DEFAULT[k]) for k in DEFAULT}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


# ------------------------------------------------------------------ 开机自启
def _launcher_command() -> str:
    """构造登录后启动托盘的命令行。带 --minimized, 开机只进托盘不弹窗。"""
    if getattr(sys, "frozen", False):
        return '"%s" --minimized' % sys.executable
    script = os.path.join(BASE_DIR, "app.py")
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return '"%s" "%s" --minimized' % (pyw, script)


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except OSError:
        return False


def autostart_enable() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY,
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _launcher_command())
        return True
    except OSError:
        return False


def autostart_disable() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY,
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_NAME)
        return True
    except OSError:
        return False


def autostart_set(enabled: bool) -> bool:
    return autostart_enable() if enabled else autostart_disable()


# ------------------------------------------------------------------ 开机联网(免登录)
# 注册表 Run 键只在用户登录后才触发, 机器重启停在锁屏时不会联网。
# 用计划任务 + SYSTEM 身份才能在开机瞬间、任何用户登录前就把认证做掉。
# 创建这类任务需要管理员权限, 由界面触发 UAC 提权。
TASK_NAME = "IPGW-AutoConnect-AutoLogin"


def _run(args: list) -> tuple:
    """执行外部命令, 返回 (退出码, 输出文本)。"""
    import subprocess
    try:
        r = subprocess.run(args, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = (r.stdout or b"") + (r.stderr or b"")
        try:
            text = raw.decode("gbk", "ignore")
        except Exception:
            text = raw.decode("utf-8", "ignore")
        return r.returncode, text.strip()
    except Exception as ex:
        return -1, repr(ex)


def _task_command() -> str:
    """计划任务要执行的命令行: 同一程序以 --headless 方式跑。"""
    if getattr(sys, "frozen", False):
        return '"%s" --headless' % sys.executable
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return '"%s" "%s" --headless' % (pyw, os.path.join(BASE_DIR, "app.py"))


def task_exists() -> bool:
    """判断开机任务是否已创建。

    这里踩过两次坑, 最终改用文件系统判断:
    · `schtasks /query` 和 `Get-ScheduledTask` 在普通权限下读不到 SYSTEM 任务的
      ACL, 会返回"拒绝访问"/空结果, 于是被误判成"任务不存在", 界面开关莫名其妙弹回去。
    · 但任务文件本身是实体存在的。os.stat 能区分:
        拒绝访问(PermissionError) -> 文件在, 只是无权读 -> 任务存在
        文件不存在(FileNotFoundError) -> 任务确实没有
    这比调计划任务模块快得多(毫秒级), 也不会拖慢启动。
    """
    root = os.environ.get("SystemRoot", r"C:\Windows")
    path = os.path.join(root, "System32", "Tasks", TASK_NAME)
    try:
        os.stat(path)
        return True
    except PermissionError:
        return True          # 存在但无权读
    except FileNotFoundError:
        return False
    except Exception:
        # 兜底: 退回计划任务模块
        ps = ("$t = Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue; "
              "if ($t) { 'TASK_YES' } else { 'TASK_NO' }" % TASK_NAME)
        _, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
        return "TASK_YES" in out


def task_install() -> tuple:
    """创建开机自动联网任务(SYSTEM 身份, 无需登录)。返回 (成功?, 说明)。

    改用计划任务模块而不是 schtasks, 因为 schtasks 的默认值有几个坑:
    · 电池供电时默认不启动(笔记本拔了电源就白搭)
    · 运行超过 3 天自动结束
    这里逐个显式打开。
    """
    if getattr(sys, "frozen", False):
        exe, wd, arg = sys.executable, os.path.dirname(sys.executable), "--headless"
    else:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        wd = BASE_DIR
        arg = '"%s" --headless' % os.path.join(BASE_DIR, "app.py")

    # 注意: 必须 try/catch + 事后验证。PowerShell 遇到非终止错误默认继续往后跑,
    # 原来的写法在"拒绝访问"时依然会输出成功标记, 导致上层误以为建成了。
    ps = (
        "$ErrorActionPreference='Stop'; "
        "try { "
        "$a = New-ScheduledTaskAction -Execute '%s' -Argument '%s' -WorkingDirectory '%s'; "
        "$t = New-ScheduledTaskTrigger -AtStartup; "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable "
        "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); "
        "$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; "
        "Register-ScheduledTask -TaskName '%s' -Action $a -Trigger $t -Settings $s "
        "-Principal $p -Force | Out-Null; "
        "if (Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue) { 'TASK_INSTALLED' } "
        "else { 'TASK_NOT_CREATED' } "
        "} catch { 'TASK_ERROR: ' + $_.Exception.Message }"
    ) % (exe, arg, wd, TASK_NAME, TASK_NAME)

    code, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    if "TASK_INSTALLED" in out:
        return True, "ok"
    low = out.lower()
    if ("拒绝访问" in out or "access is denied" in low
            or "denied" in low or "administrator" in low):
        return False, "需要管理员权限"
    return False, (out.strip() or "创建失败")


def task_remove() -> tuple:
    ps = ("Unregister-ScheduledTask -TaskName '%s' -Confirm:$false "
          "-ErrorAction SilentlyContinue; 'TASK_REMOVED'" % TASK_NAME)
    code, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    return ("TASK_REMOVED" in out), out


def task_run_now() -> tuple:
    code, out = _run(["schtasks", "/run", "/tn", TASK_NAME])
    return code == 0, out


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(extra_arg: str) -> bool:
    """以管理员身份重新启动自己, 用于创建计划任务。"""
    try:
        import ctypes
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, extra_arg
        else:
            exe = sys.executable
            params = '"%s" %s' % (os.path.join(BASE_DIR, "app.py"), extra_arg)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return rc > 32
    except Exception:
        return False
