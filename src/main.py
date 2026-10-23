#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自律定时关机工具 (SelfShutdown)
================================
单文件实现。PyInstaller --onefile --noconsole 打包后双击即用，无外部脚本依赖。

模式（命令行子命令，供计划任务/提权使用）：
    无参数          弹出设置窗口（GUI）
    --install       创建/覆盖三个计划任务（幂等）
    --uninstall     删除三个计划任务
    --test          创建一次性测试任务（2 分钟后触发）
    --guardian      守护：检查并恢复任务
    --enforce       到点执行关机（读跳过状态）
    --warn          弹倒计时提醒窗口
    --dry-run       演练模式：流程照走，不真正关机
    --status        打印任务状态

安全承诺：关机只调用 shutdown.exe（Windows 原生 ACPI 流程：停服务→卸载驱动→断电），
不做任何硬断电，不影响底层驱动。
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import datetime
import json
import os
import subprocess
import sys
import tempfile
import traceback

APP_NAME = "SelfShutdown"
VERSION = "0.1.0"
TASK_FOLDER = "SelfDisciplineShutdown"
TASK_ENFORCE = "Enforcement"
TASK_WARN = "Warning"
TASK_GUARDIAN = "Guardian"
TASK_TEST = "TestOnce"
ALL_TASKS = (TASK_ENFORCE, TASK_WARN, TASK_GUARDIAN)

CREATE_NO_WINDOW = 0x08000000
ERROR_CANCELLED = 1223

DEFAULT_CONFIG = {
    "shutdown_time": "22:30",            # 每天关机时间 HH:MM
    "shutdown_mode": "force",            # force=强制 / graceful=优雅
    "warn_seconds": 60,                  # 提前提醒秒数
    "warn_message": "自律时间到，请保存工作，即将关机",
    "retry_count": 3,                    # 任务失败重试次数
    "retry_interval_minutes": 1,         # 重试间隔（分钟）
    "guardian_interval_minutes": 60,     # 守护任务周期检查间隔
    "guardian_on_logon": True,           # 登录时也检查
    "run_on_battery": True,              # 电池模式下也运行
    "wake_to_run": True,                 # 到点唤醒睡眠中的电脑
    "task_folder": TASK_FOLDER,
    "warn_user": "",                     # 安装时的用户（提醒任务以此身份运行，守护恢复时也用它）
}


# ---------------------------------------------------------------------------
# 路径 / 配置 / 状态 / 日志
# ---------------------------------------------------------------------------

def app_dir():
    """EXE 所在目录。onefile 下 sys.argv[0] 是真实路径（非 _MEIPASS 临时目录）。"""
    if getattr(sys, "frozen", False):
        p = os.path.abspath(sys.argv[0])
        if os.path.exists(p):
            return os.path.dirname(p)
    return os.path.dirname(os.path.abspath(__file__))


def exe_path():
    """本程序的真实 EXE 绝对路径（供计划任务引用）。"""
    if getattr(sys, "frozen", False):
        p = os.path.abspath(sys.argv[0])
        if os.path.exists(p):
            return p
    return os.path.abspath(sys.executable)


def config_path():
    return os.path.join(app_dir(), "config.json")


def state_path():
    return os.path.join(app_dir(), "state.json")


def log_path():
    return os.path.join(app_dir(), "actions.log")


def log_action(msg):
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (ts, msg))
    except Exception:
        pass


def safe_print(*a, **kw):
    """ShellExecute 启动的进程可能没有控制台（sys.stdout 为 None），避免 print 崩溃。"""
    try:
        if sys.stdout is not None:
            print(*a, **kw)
    except Exception:
        pass


# 提权子进程的结果回传：--result-file 参数优先，环境变量兜底
_RESULT_FILE = None
_RESULT_WRITTEN = False


def set_result_file(path):
    global _RESULT_FILE
    _RESULT_FILE = path or None


def write_result(ok, error=""):
    """把结果写给父进程。任何情况下都不允许抛异常。"""
    global _RESULT_WRITTEN
    p = _RESULT_FILE or os.environ.get("SELFSHUTDOWN_RESULT")
    if not p:
        return
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"ok": bool(ok), "error": str(error)}, f, ensure_ascii=False)
        _RESULT_WRITTEN = True
    except Exception:
        pass


def write_result_missing(rc):
    """进程即将退出但没写过结果时，兜底写一条，保证父进程能拿到真实退出码。"""
    if _RESULT_FILE and not _RESULT_WRITTEN:
        write_result(False, "子进程未返回明细（退出码 %s），请查看 EXE 同目录 actions.log" % rc)


def _coerce_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _coerce_int(v, lo, hi, default):
    try:
        n = int(v)
        return max(lo, min(hi, n))
    except Exception:
        return default


def parse_hhmm(s):
    parts = str(s).strip().split(":")
    if len(parts) != 2:
        raise ValueError("时间格式应为 HH:MM")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("时间超出范围")
    return datetime.time(h, m)


def normalize_time(v):
    try:
        return parse_hhmm(v).strftime("%H:%M")
    except Exception:
        return DEFAULT_CONFIG["shutdown_time"]


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULT_CONFIG:
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    cfg["shutdown_time"] = normalize_time(cfg["shutdown_time"])
    cfg["shutdown_mode"] = cfg["shutdown_mode"] if cfg["shutdown_mode"] in ("force", "graceful") else "force"
    cfg["warn_seconds"] = _coerce_int(cfg["warn_seconds"], 10, 86400, 60)
    cfg["retry_count"] = _coerce_int(cfg["retry_count"], 0, 10, 3)
    cfg["retry_interval_minutes"] = _coerce_int(cfg["retry_interval_minutes"], 1, 60, 1)
    cfg["guardian_interval_minutes"] = _coerce_int(cfg["guardian_interval_minutes"], 5, 1440, 60)
    cfg["guardian_on_logon"] = _coerce_bool(cfg["guardian_on_logon"])
    cfg["run_on_battery"] = _coerce_bool(cfg["run_on_battery"])
    cfg["wake_to_run"] = _coerce_bool(cfg["wake_to_run"])
    cfg["task_folder"] = str(cfg["task_folder"]) or TASK_FOLDER
    cfg["warn_user"] = str(cfg.get("warn_user") or "")
    return cfg


def save_config(cfg):
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_state():
    try:
        with open(state_path(), "r", encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            st.setdefault("history", [])
            return st
    except Exception:
        pass
    return {"skip_until": None, "history": []}


def save_state(st):
    with open(state_path(), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def _today():
    return datetime.date.today()


def is_skipping():
    su = load_state().get("skip_until")
    if not su:
        return False
    try:
        return datetime.date.fromisoformat(str(su)) >= _today()
    except Exception:
        return False


def skip_status_text():
    su = load_state().get("skip_until")
    if not su:
        return "正常运行（未跳过）"
    try:
        if datetime.date.fromisoformat(str(su)) >= _today():
            return "已跳过至 %s" % su
    except Exception:
        pass
    return "正常运行（未跳过）"


def set_skip_days(days):
    days = _coerce_int(days, 0, 365, 1)
    until = (_today() + datetime.timedelta(days=days)).isoformat()
    st = load_state()
    st["skip_until"] = until
    st.setdefault("history", []).append({
        "date": _today().isoformat(), "action": "skip", "days": days, "until": until,
    })
    save_state(st)
    log_action("cancel: 跳过 %d 天，跳过至 %s" % (days, until))
    return until


def clear_skip():
    st = load_state()
    st["skip_until"] = None
    st.setdefault("history", []).append({
        "date": _today().isoformat(), "action": "resume",
    })
    save_state(st)
    log_action("resume: 恢复正常关机")
    return True


# ---------------------------------------------------------------------------
# Windows API 辅助
# ---------------------------------------------------------------------------

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("fMask", wt.ULONG),
        ("hwnd", wt.HWND),
        ("lpVerb", wt.LPCWSTR),
        ("lpFile", wt.LPCWSTR),
        ("lpParameters", wt.LPCWSTR),
        ("lpDirectory", wt.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wt.HINSTANCE),
        ("lpIDList", wt.LPVOID),
        ("lpClass", wt.LPCWSTR),
        ("hkeyClass", wt.HKEY),
        ("dwHotKey", wt.DWORD),
        ("hIcon", wt.HANDLE),
        ("hProcess", wt.HANDLE),
    ]


SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_SHOWNORMAL = 1


def run_elevated_and_wait(argv, timeout_ms=120000):
    """以管理员权限重跑自身（弹 UAC），等待结束并返回退出码。"""
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe_path()
    sei.lpParameters = subprocess.list2cmdline(argv)
    sei.lpDirectory = app_dir()
    sei.nShow = SW_SHOWNORMAL
    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
    if not ok:
        err = ctypes.get_last_error()
        raise OSError("ShellExecuteExW failed: %s" % err)
    try:
        ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, timeout_ms)
        code = wt.DWORD(0)
        ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        if sei.hProcess:
            ctypes.windll.kernel32.CloseHandle(sei.hProcess)


def elevated_do(argv):
    """GUI/CLI 调用的提权执行入口。返回 (ok, error)。"""
    # 结果文件放在 EXE 同目录：无论哪个管理员账户批准的 UAC，发起用户都能读到
    result_file = os.path.join(app_dir(), ".elevate_result.json")
    try:
        os.remove(result_file)
    except OSError:
        pass
    os.environ["SELFSHUTDOWN_RESULT"] = result_file
    argv = list(argv) + ["--result-file", result_file]
    try:
        code = run_elevated_and_wait(argv)
    except OSError as e:
        if getattr(e, "winerror", None) == ERROR_CANCELLED or ERROR_CANCELLED in str(e):
            return False, "已取消提权（UAC 被拒绝），操作未执行"
        return False, "提权失败：%s" % e
    data = {}
    try:
        with open(result_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        pass
    finally:
        try:
            os.remove(result_file)
        except OSError:
            pass
    if data.get("ok"):
        return True, ""
    return False, data.get("error") or ("操作失败（退出码 %s）" % code)


def run_cmd(args):
    return subprocess.run(
        args, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )


def current_user():
    try:
        r = run_cmd(["whoami"])
        out = (r.stdout or "").strip()
        if out:
            return out
    except Exception:
        pass
    return os.environ.get("USERNAME", "")


def abort_shutdown():
    """中止已进入待定状态的关机（优雅模式 /t N 场景）。"""
    try:
        subprocess.run(["shutdown", "/a"], capture_output=True,
                       creationflags=CREATE_NO_WINDOW, timeout=10)
        log_action("shutdown /a 已执行（中止待定关机）")
    except Exception as e:
        log_action("shutdown /a 失败: %s" % e)


def do_shutdown(cfg, dry_run=False):
    """执行关机。只走 shutdown.exe 原生 ACPI 流程。"""
    if cfg["shutdown_mode"] == "graceful":
        args = ["shutdown", "/s", "/t", "10"]
        desc = "优雅关机（/s /t 10，程序可自行保存）"
    else:
        args = ["shutdown", "/s", "/f", "/t", "0"]
        desc = "强制关机（/s /f /t 0，仅跳过未响应程序，不跳过驱动卸载）"
    log_action("enforce: %s dry_run=%s" % (desc, dry_run))
    if dry_run:
        log_action("DRY-RUN: 未真正调用 shutdown.exe")
        return 0
    subprocess.run(args, creationflags=CREATE_NO_WINDOW, timeout=30)
    return 0


# ---------------------------------------------------------------------------
# 计划任务 XML 生成 / schtasks 操作
# ---------------------------------------------------------------------------

def _fmt_dt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def shutdown_dt_today(cfg):
    return datetime.datetime.combine(_today(), parse_hhmm(cfg["shutdown_time"]))


def warn_dt(cfg):
    return shutdown_dt_today(cfg) - datetime.timedelta(seconds=int(cfg["warn_seconds"]))


def guardian_pre_dt(cfg):
    return shutdown_dt_today(cfg) - datetime.timedelta(minutes=5)


def trigger_daily_dt(dt):
    return (
        "    <CalendarTrigger>\n"
        "      <StartBoundary>%s</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <ScheduleByDay>\n"
        "        <DaysInterval>1</DaysInterval>\n"
        "      </ScheduleByDay>\n"
        "    </CalendarTrigger>" % _fmt_dt(dt)
    )


def trigger_once_dt(dt):
    return (
        "    <TimeTrigger>\n"
        "      <StartBoundary>%s</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "    </TimeTrigger>" % _fmt_dt(dt)
    )


def trigger_boot():
    return (
        "    <BootTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        "    </BootTrigger>"
    )


def trigger_logon():
    return (
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        "    </LogonTrigger>"
    )


def trigger_repetition_minutes(minutes):
    start = datetime.datetime.combine(_today(), datetime.time(0, 0))
    return (
        "    <TimeTrigger>\n"
        "      <Repetition>\n"
        "        <Interval>PT%dM</Interval>\n"
        "        <StopAtDurationEnd>false</StopAtDurationEnd>\n"
        "      </Repetition>\n"
        "      <StartBoundary>%s</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "    </TimeTrigger>" % (int(minutes), _fmt_dt(start))
    )


def principal_system():
    return (
        "    <Principal id=\"Author\">\n"
        "      <UserId>S-1-5-18</UserId>\n"
        "      <RunLevel>HighestAvailable</RunLevel>\n"
        "    </Principal>"
    )


def principal_current_user(cfg=None):
    """提醒任务以“安装时的用户”身份运行（守护任务以 SYSTEM 恢复时也复用该用户）。"""
    user = ""
    if isinstance(cfg, dict):
        user = str(cfg.get("warn_user") or "").strip()
    if not user:
        user = current_user()
    return (
        "    <Principal id=\"Author\">\n"
        "      <UserId>%s</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>HighestAvailable</RunLevel>\n"
        "    </Principal>" % user
    )


def task_xml(cfg, triggers_xml, task_args, principal_xml, exec_time_limit="PT30M"):
    on_battery_block = "false" if cfg["run_on_battery"] else "true"
    xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-16\"?>\n"
        "<Task version=\"1.3\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">\n"
        "  <RegistrationInfo>\n"
        "    <Date>%s</Date>\n"
        "    <Author>%s</Author>\n"
        "    <Description>自律定时关机 %s</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n%s\n  </Triggers>\n"
        "  <Principals>\n%s\n  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>%s</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>%s</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings>\n"
        "      <Duration>PT10M</Duration>\n"
        "      <WaitTimeout>PT1H</WaitTimeout>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>%s</WakeToRun>\n"
        "    <ExecutionTimeLimit>%s</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "    <RestartOnFailure>\n"
        "      <Interval>PT%dM</Interval>\n"
        "      <Count>%d</Count>\n"
        "    </RestartOnFailure>\n"
        "  </Settings>\n"
        "  <Actions Context=\"Author\">\n"
        "    <Exec>\n"
        "      <Command>\"%s\"</Command>\n"
        "      <Arguments>%s</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>"
    ) % (
        _fmt_dt(datetime.datetime.now()), APP_NAME, task_args,
        triggers_xml, principal_xml,
        on_battery_block, on_battery_block,
        "true" if cfg["wake_to_run"] else "false",
        exec_time_limit,
        int(cfg["retry_interval_minutes"]), int(cfg["retry_count"]),
        exe_path(), task_args,
    )
    return xml


def build_task_xml(cfg, name, extra_args=None):
    """按任务名生成完整 XML。"""
    if name == TASK_ENFORCE:
        args = (extra_args or "--enforce").strip()
        return task_xml(cfg, trigger_daily_dt(shutdown_dt_today(cfg)),
                        args, principal_system(), "PT30M")
    if name == TASK_WARN:
        return task_xml(cfg, trigger_daily_dt(warn_dt(cfg)),
                        "--warn", principal_current_user(cfg), "PT6H")
    if name == TASK_GUARDIAN:
        trigs = trigger_boot()
        if cfg["guardian_on_logon"]:
            trigs += "\n" + trigger_logon()
        trigs += "\n" + trigger_repetition_minutes(cfg["guardian_interval_minutes"])
        trigs += "\n" + trigger_daily_dt(guardian_pre_dt(cfg))
        return task_xml(cfg, trigs, "--guardian", principal_system(), "PT1H")
    if name == TASK_TEST:
        args = (extra_args or "--enforce").strip()
        when = datetime.datetime.now() + datetime.timedelta(minutes=2)
        return task_xml(cfg, trigger_once_dt(when), args, principal_system(), "PT10M")
    raise ValueError("未知任务: %s" % name)


def _schtasks_create(cfg, name, xml):
    folder = cfg["task_folder"]
    full = "%s\\%s" % (folder, name)
    fd, tmp = tempfile.mkstemp(prefix="selfshutdown_", suffix=".xml")
    os.close(fd)
    try:
        # schtasks /Create /XML 只接受 UTF-16（带 BOM）
        with open(tmp, "w", encoding="utf-16") as f:
            f.write(xml)
        r = run_cmd(["schtasks", "/Create", "/TN", full, "/XML", tmp, "/F"])
        if r.returncode != 0:
            raise RuntimeError("schtasks 创建任务失败: %s %s" % (r.returncode, (r.stderr or r.stdout or "").strip()))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _schtasks_delete(cfg, name):
    full = "%s\\%s" % (cfg["task_folder"], name)
    r = run_cmd(["schtasks", "/Delete", "/TN", full, "/F"])
    return r.returncode == 0


def install_one(cfg, name, extra_args=None):
    _schtasks_create(cfg, name, build_task_xml(cfg, name, extra_args))


def install_tasks(cfg):
    # 记录安装时的用户：提醒任务与 SYSTEM 守护任务恢复时都以该身份运行
    user = current_user()
    if user:
        cfg["warn_user"] = user
        try:
            save_config(cfg)
        except Exception:
            pass
    install_one(cfg, TASK_ENFORCE)
    install_one(cfg, TASK_WARN)
    install_one(cfg, TASK_GUARDIAN)
    log_action("install: 三个计划任务已创建/更新（提醒任务用户：%s）" % user)


def uninstall_tasks(cfg):
    ok = True
    for name in ALL_TASKS + (TASK_TEST,):
        try:
            r = _schtasks_delete(cfg, name)
            ok = ok and (r or name == TASK_TEST)
        except Exception as e:
            log_action("uninstall: 删除 %s 失败: %s" % (name, e))
            ok = False
    log_action("uninstall: 计划任务已删除")
    return ok


def task_state(cfg, name):
    """返回 enabled / disabled / missing。用 PowerShell 避免本地化解析问题。"""
    folder = cfg["task_folder"]
    ps = (
        "try { (Get-ScheduledTask -TaskName '%s' -TaskPath '\\%s\\' "
        "-ErrorAction Stop).State } catch { 'NotFound' }" % (name, folder)
    )
    try:
        r = run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
        out = (r.stdout or "").strip()
    except Exception:
        return "missing"
    if out == "Ready":
        return "enabled"
    if out == "Disabled":
        return "disabled"
    return "missing"


STATE_LABELS = {"enabled": "已安装·启用", "disabled": "已安装·已禁用", "missing": "未安装"}

# 关机方式：界面显示中文，配置存储英文值
MODE_LABELS = {"force": "强制关机", "graceful": "优雅关机"}
MODE_VALUES = {v: k for k, v in MODE_LABELS.items()}


def guardian_main(cfg, dry_run=False):
    results = {}
    for name in ALL_TASKS:
        st = task_state(cfg, name)
        results[name] = st
        if st != "enabled":
            log_action("guardian: 任务 %s 状态=%s，执行恢复" % (name, st))
            try:
                install_one(cfg, name)
            except Exception as e:
                log_action("guardian: 恢复 %s 失败: %s" % (name, e))
    log_action("guardian: 检查完成 %s" % json.dumps(results, ensure_ascii=False))
    return 0


def enforce_main(cfg, dry_run=False):
    if is_skipping():
        log_action("enforce: 跳过生效期内，今日不关机")
        return 0
    return do_shutdown(cfg, dry_run=dry_run)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def system_is_light():
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return val == 1
    except Exception:
        return True


FONT_MAIN = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 9, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 14, "bold")
FONT_COUNT = ("Microsoft YaHei UI", 28, "bold")

# 主题配色（跟随系统深浅色）
THEME = {
    "light": {
        "bg": "#f3f4f6", "fg": "#1f2328", "sub": "#6b7280",
        "field": "#ffffff", "btn": "#e5e7eb", "accent": "#0067c0",
        "accent_fg": "#ffffff", "border": "#d1d5db", "ok": "#137333", "warn": "#b06000",
    },
    "dark": {
        "bg": "#1f2226", "fg": "#e6e8eb", "sub": "#9aa0a6",
        "field": "#2a2e33", "btn": "#30353b", "accent": "#4da3ff",
        "accent_fg": "#10202f", "border": "#3c4046", "ok": "#5bb974", "warn": "#e8a33d",
    },
}


def apply_theme(root):
    import tkinter as tk
    import tkinter.ttk as ttk
    pal = THEME["light"] if system_is_light() else THEME["dark"]
    global _CURRENT_PAL
    _CURRENT_PAL = pal
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    bg, fg, sub = pal["bg"], pal["fg"], pal["sub"]
    field, btn, accent = pal["field"], pal["btn"], pal["accent"]
    style.configure(".", background=bg, foreground=fg, font=FONT_MAIN)
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=fg, font=FONT_MAIN)
    style.configure("Title.TLabel", background=bg, foreground=fg, font=FONT_TITLE)
    style.configure("Sub.TLabel", background=bg, foreground=sub, font=FONT_MAIN)
    style.configure("Hint.TLabel", background=bg, foreground=sub, font=("Microsoft YaHei UI", 8))
    style.configure("TButton", background=btn, foreground=fg, font=FONT_MAIN,
                    borderwidth=1, bordercolor=pal["border"], padding=6)
    style.map("TButton",
              background=[("active", pal["border"]), ("pressed", accent)],
              foreground=[("pressed", pal["accent_fg"])])
    style.configure("Accent.TButton", background=accent, foreground=pal["accent_fg"],
                    font=FONT_BOLD, borderwidth=0, padding=6)
    style.map("Accent.TButton", background=[("active", accent)])
    style.configure("TLabelframe", background=bg, foreground=fg, borderwidth=1,
                    bordercolor=pal["border"])
    style.configure("TLabelframe.Label", background=bg, foreground=sub, font=FONT_BOLD)
    style.configure("TEntry", fieldbackground=field, foreground=fg,
                    borderwidth=1, insertcolor=fg, padding=4)
    style.configure("TCombobox", fieldbackground=field, background=btn, foreground=fg,
                    borderwidth=1, arrowcolor=fg, padding=4)
    style.map("TCombobox", fieldbackground=[("readonly", field)],
              background=[("readonly", btn)])
    # 数字调节框：clam 默认白底字段，必须显式配色，否则深色主题下露白
    style.configure("TSpinbox", fieldbackground=field, foreground=fg, background=btn,
                    borderwidth=1, bordercolor=pal["border"], insertcolor=fg,
                    arrowcolor=fg, padding=2)
    style.map("TSpinbox",
              fieldbackground=[("readonly", field), ("disabled", field)],
              background=[("active", btn), ("pressed", field)])
    style.configure("TCheckbutton", background=bg, foreground=fg, font=FONT_MAIN)
    style.configure("Horizontal.TProgressbar", background=accent, troughcolor=pal["border"],
                    borderwidth=0)
    root.configure(background=bg)
    return pal


def _spinbox_class():
    import tkinter.ttk as ttk
    if hasattr(ttk, "Spinbox"):
        return ttk.Spinbox
    import tkinter as tk
    return tk.Spinbox


# apply_theme 设置的当前调色板（供经典控件回退路径直接配色）
_CURRENT_PAL = None


def make_spin(parent, var, from_, to, width=6):
    """数字调节框。ttk.Spinbox 用 TSpinbox 样式；经典回退则直接配色防止露白。"""
    import tkinter.ttk as ttk
    cls = _spinbox_class()
    if cls is ttk.Spinbox:
        return cls(parent, from_=from_, to=to, textvariable=var, width=width,
                   style="TSpinbox")
    sp = cls(parent, from_=from_, to=to, textvariable=var, width=width)
    pal = _CURRENT_PAL or THEME["light"]
    try:
        sp.configure(background=pal["field"], foreground=pal["fg"],
                     insertbackground=pal["fg"], buttonbackground=pal["btn"],
                     highlightbackground=pal["border"], highlightcolor=pal["border"],
                     relief="flat", bd=1)
    except Exception:
        pass
    return sp


def make_entry(parent, textvar, width=10):
    import tkinter.ttk as ttk
    return ttk.Entry(parent, textvariable=textvar, width=width)


class CountdownWindow:
    """--warn 倒计时提醒窗口。"""

    def __init__(self, cfg, dry_run=False, on_closed=None):
        import tkinter as tk
        self.cfg = cfg
        self.dry_run = dry_run
        self.on_closed = on_closed
        self.remaining = int(cfg["warn_seconds"])
        self.root = tk.Tk()
        self.pal = apply_theme(self.root)
        self.root.title("自律提醒 - 即将关机")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        self._center()
        self._tick()

    def _build(self):
        import tkinter as tk
        import tkinter.ttk as ttk
        pal = self.pal
        outer = ttk.Frame(self.root, padding=18)
        outer.grid(row=0, column=0, sticky="nsew")
        ttk.Label(outer, text=self.cfg["warn_message"], style="TLabel",
                  wraplength=380, justify="center").grid(
            row=0, column=0, columnspan=2, pady=(0, 10))
        self.count_label = ttk.Label(outer, text="%d 秒" % self.remaining,
                                     style="Title.TLabel", justify="center")
        self.count_label.grid(row=1, column=0, columnspan=2, pady=4)
        self.progress = ttk.Progressbar(outer, length=380, mode="determinate",
                                        maximum=max(1, self.remaining),
                                        style="Horizontal.TProgressbar")
        self.progress.grid(row=2, column=0, columnspan=2, pady=(0, 12))
        if self.dry_run:
            ttk.Label(outer, text="【演练模式】不会真正关机", style="Sub.TLabel",
                      justify="center").grid(row=3, column=0, columnspan=2, pady=(0, 8))
        ttk.Button(outer, text="立即关机", style="Accent.TButton",
                   command=self._shutdown_now).grid(row=4, column=0, padx=4, sticky="ew")
        ttk.Button(outer, text="取消本次关机", command=self._cancel).grid(
            row=4, column=1, padx=4, sticky="ew")
        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)

    def _center(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry("+%d+%d" % (max(0, (sw - w) // 2), max(0, int((sh - h) * 0.28))))

    def _tick(self):
        self.count_label.configure(text="%d 秒" % self.remaining)
        self.progress.configure(value=max(1, self.remaining) - self.remaining)
        if self.remaining <= 0:
            log_action("warn: 倒计时结束，由 Enforcement 任务执行关机")
            self._destroy()
            return
        self.remaining -= 1
        self.root.after(1000, self._tick)

    def _shutdown_now(self):
        do_shutdown(self.cfg, dry_run=self.dry_run)
        self._destroy()

    def _cancel(self):
        CancelDialog(self.root, self.cfg, on_done=self._after_cancel)

    def _after_cancel(self, closed_software):
        # closed_software: True=关闭软件 / False=跳过 / None=返回（继续倒计时）
        if closed_software is None:
            return
        self._destroy()

    def _on_close(self):
        # 点标题栏关闭 = 不做取消操作，仅关闭窗口（不影响 Enforcement）
        self._destroy()

    def _destroy(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        if self.on_closed:
            self.on_closed()

    def run(self):
        self.root.mainloop()


class CancelDialog:
    """取消方式选择：跳过今天 / 跳过 N 天 / 关闭软件。"""

    def __init__(self, parent, cfg, on_done=None):
        import tkinter as tk
        import tkinter.ttk as ttk
        from tkinter import messagebox
        self.messagebox = messagebox
        self.cfg = cfg
        self.on_done = on_done
        self.win = tk.Toplevel(parent)
        self.win.title("取消本次关机")
        self.win.attributes("-topmost", True)
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.grab_set()
        pal = apply_theme(self.win)
        outer = ttk.Frame(self.win, padding=16)
        outer.grid(row=0, column=0, sticky="nsew")
        ttk.Label(outer, text="选择取消方式：", style="TLabel").grid(
            row=0, column=0, columnspan=3, pady=(0, 10), sticky="w")
        ttk.Button(outer, text="跳过今天", style="Accent.TButton",
                   command=self._skip_today).grid(row=1, column=0, columnspan=3,
                                                  padx=2, pady=4, sticky="ew")
        ttk.Label(outer, text="跳过", style="TLabel").grid(row=2, column=0, pady=4)
        self.days_var = tk.StringVar(value="1")
        make_spin(outer, self.days_var, 0, 365, width=5).grid(row=2, column=1, pady=4)
        ttk.Label(outer, text="天", style="TLabel").grid(row=2, column=2, pady=4)
        ttk.Button(outer, text="跳过 N 天", command=self._skip_days).grid(
            row=3, column=0, columnspan=3, padx=2, pady=4, sticky="ew")
        ttk.Button(outer, text="关闭软件（退出自律，删除计划任务）",
                   command=self._close_software).grid(
            row=4, column=0, columnspan=3, padx=2, pady=4, sticky="ew")
        ttk.Button(outer, text="返回", command=self._back).grid(
            row=5, column=0, columnspan=3, padx=2, pady=(8, 0), sticky="ew")
        for i in range(3):
            outer.columnconfigure(i, weight=1)
        self._center(parent)

    def _center(self, parent):
        self.win.update_idletasks()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            x, y = px + (pw - w) // 2, py + (ph - h) // 2
        except Exception:
            x = (self.win.winfo_screenwidth() - w) // 2
            y = (self.win.winfo_screenheight() - h) // 3
        self.win.geometry("+%d+%d" % (max(0, x), max(0, y)))

    def _skip_today(self):
        until = set_skip_days(0)
        self.messagebox.showinfo("已跳过", "今天的关机已取消，明天照常。\n（跳过至 %s）" % until,
                                 parent=self.win)
        self._finish(False)

    def _skip_days(self):
        try:
            days = int(self.days_var.get())
        except Exception:
            days = 1
        days = max(0, min(365, days))
        until = set_skip_days(days)
        self.messagebox.showinfo("已跳过", "已跳过 %d 天，%s 起恢复自动关机。\n（跳过至 %s）"
                                % (days, "明天" if days == 0 else "%d 天后" % (days + 1), until),
                                 parent=self.win)
        self._finish(False)

    def _close_software(self):
        if not self.messagebox.askyesno(
                "确认关闭软件",
                "关闭软件将删除全部计划任务，之后不再自动关机。\n\n"
                "以后想重新开启，双击 SelfShutdown.exe 再点“安装”即可。\n\n确定关闭吗？",
                parent=self.win):
            return
        abort_shutdown()
        cfg = load_config()
        try:
            if is_admin():
                uninstall_tasks(cfg)
            else:
                ok, err = elevated_do(["--uninstall"])
                if not ok:
                    self.messagebox.showerror("关闭失败", err or "删除计划任务失败",
                                              parent=self.win)
                    return
        except Exception as e:
            log_action("close_software 失败: %s" % e)
            self.messagebox.showerror("关闭失败", str(e), parent=self.win)
            return
        log_action("close_software: 用户选择关闭软件，任务已删除")
        self.messagebox.showinfo("已关闭", "计划任务已删除，自律定时关机已关闭。", parent=self.win)
        self._finish(True)

    def _back(self):
        self._finish(None)

    def _finish(self, closed_software):
        try:
            self.win.grab_release()
            self.win.destroy()
        except Exception:
            pass
        if self.on_done:
            self.on_done(closed_software)


class SettingsApp:
    """设置窗口（无参数双击时启动）。"""

    def __init__(self):
        import tkinter as tk
        import tkinter.ttk as ttk
        self.tk = tk
        self.ttk = ttk
        self.messagebox = None
        self.cfg = load_config()
        self.root = tk.Tk()
        self.pal = apply_theme(self.root)
        from tkinter import messagebox
        self.messagebox = messagebox
        self.root.title("自律定时关机 - 设置 v%s" % VERSION)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self._vars = {}
        self._build()
        self._center()
        self.refresh_status()

    # ---------------- UI 构建 ----------------
    def _build(self):
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=16)
        outer.grid(row=0, column=0, sticky="nsew")
        ttk.Label(outer, text="自律定时关机", style="Title.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(outer, text="到点强制关机 · 走 Windows 原生关机流程 · 不影响驱动",
                  style="Sub.TLabel").grid(row=1, column=0, columnspan=4, sticky="w",
                                           pady=(0, 10))

        # 关机设置
        f1 = ttk.Labelframe(outer, text="关机设置", padding=10)
        f1.grid(row=2, column=0, columnspan=4, sticky="ew", pady=4)
        ttk.Label(f1, text="每天关机时间").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self._var("shutdown_time", tk.StringVar(value=self.cfg["shutdown_time"]))
        make_entry(f1, self._vars["shutdown_time"], width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(f1, text="（HH:MM，24 小时制）", style="Hint.TLabel").grid(
            row=0, column=2, columnspan=2, sticky="w", padx=(8, 0))
        ttk.Label(f1, text="关机方式").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        self._var("shutdown_mode",
                  tk.StringVar(value=MODE_LABELS.get(self.cfg["shutdown_mode"], "强制关机")))
        cb = ttk.Combobox(f1, textvariable=self._vars["shutdown_mode"], width=14,
                          state="readonly",
                          values=[MODE_LABELS["force"], MODE_LABELS["graceful"]])
        cb.grid(row=1, column=1, sticky="w", pady=(6, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self._update_mode_hint())
        self._mode_label = ttk.Label(f1, text="", style="Hint.TLabel")
        self._mode_label.grid(row=1, column=2, columnspan=2, sticky="w", padx=(8, 0), pady=(6, 0))
        self._update_mode_hint()

        # 提醒设置
        f2 = ttk.Labelframe(outer, text="提醒设置", padding=10)
        f2.grid(row=3, column=0, columnspan=4, sticky="ew", pady=4)
        ttk.Label(f2, text="提前提醒（秒）").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self._var("warn_seconds", tk.StringVar(value=str(self.cfg["warn_seconds"])))
        make_spin(f2, self._vars["warn_seconds"], 10, 3600, width=6).grid(row=0, column=1, sticky="w")
        ttk.Label(f2, text="提醒文案").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        self._var("warn_message", tk.StringVar(value=self.cfg["warn_message"]))
        make_entry(f2, self._vars["warn_message"], width=34).grid(
            row=1, column=1, columnspan=3, sticky="w", pady=(6, 0))

        # 任务可靠性
        f3 = ttk.Labelframe(outer, text="任务可靠性", padding=10)
        f3.grid(row=4, column=0, columnspan=4, sticky="ew", pady=4)
        ttk.Label(f3, text="失败重试次数").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self._var("retry_count", tk.StringVar(value=str(self.cfg["retry_count"])))
        make_spin(f3, self._vars["retry_count"], 0, 10, width=5).grid(row=0, column=1, sticky="w")
        ttk.Label(f3, text="重试间隔（分钟）").grid(row=0, column=2, sticky="w", padx=(16, 8))
        self._var("retry_interval_minutes",
                  tk.StringVar(value=str(self.cfg["retry_interval_minutes"])))
        make_spin(f3, self._vars["retry_interval_minutes"], 1, 60, width=5).grid(
            row=0, column=3, sticky="w")
        self._var("run_on_battery", tk.BooleanVar(value=self.cfg["run_on_battery"]))
        ttk.Checkbutton(f3, text="电池模式下也运行", variable=self._vars["run_on_battery"]).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._var("wake_to_run", tk.BooleanVar(value=self.cfg["wake_to_run"]))
        ttk.Checkbutton(f3, text="到点唤醒睡眠中的电脑", variable=self._vars["wake_to_run"]).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))

        # 守护任务
        f4 = ttk.Labelframe(outer, text="守护任务", padding=10)
        f4.grid(row=5, column=0, columnspan=4, sticky="ew", pady=4)
        ttk.Label(f4, text="检查间隔（分钟）").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self._var("guardian_interval_minutes",
                  tk.StringVar(value=str(self.cfg["guardian_interval_minutes"])))
        make_spin(f4, self._vars["guardian_interval_minutes"], 5, 1440, width=6).grid(
            row=0, column=1, sticky="w")
        self._var("guardian_on_logon", tk.BooleanVar(value=self.cfg["guardian_on_logon"]))
        ttk.Checkbutton(f4, text="登录时也检查（任务被删会自动恢复）",
                        variable=self._vars["guardian_on_logon"]).grid(
            row=0, column=2, columnspan=2, sticky="w", padx=(16, 0))

        # 状态区
        f5 = ttk.Labelframe(outer, text="当前状态", padding=10)
        f5.grid(row=6, column=0, columnspan=4, sticky="ew", pady=4)
        self.status_labels = {}
        rows = [("关机任务", TASK_ENFORCE), ("提醒任务", TASK_WARN), ("守护任务", TASK_GUARDIAN)]
        for i, (label, name) in enumerate(rows):
            ttk.Label(f5, text=label).grid(row=i, column=0, sticky="w", padx=(0, 8))
            lb = ttk.Label(f5, text="查询中…", style="TLabel")
            lb.grid(row=i, column=1, sticky="w")
            self.status_labels[name] = lb
        ttk.Label(f5, text="跳过状态").grid(row=3, column=0, sticky="w", padx=(0, 8))
        self.skip_label = ttk.Label(f5, text=skip_status_text(), style="TLabel")
        self.skip_label.grid(row=3, column=1, sticky="w")
        ttk.Button(f5, text="刷新状态", command=self.refresh_status).grid(
            row=0, column=2, rowspan=2, padx=(16, 0), sticky="e")

        # 按钮区
        btns = ttk.Frame(outer, padding=(0, 12, 0, 0))
        btns.grid(row=7, column=0, columnspan=4, sticky="ew")
        ttk.Button(btns, text="保存设置", command=self._save).grid(
            row=0, column=0, padx=(0, 6))
        ttk.Button(btns, text="安装 / 重新安装", style="Accent.TButton",
                   command=self._install).grid(row=0, column=1, padx=6)
        ttk.Button(btns, text="卸载", command=self._uninstall).grid(row=0, column=2, padx=6)
        ttk.Button(btns, text="测试：2 分钟后关机", command=self._test).grid(
            row=0, column=3, padx=6)
        ttk.Button(btns, text="恢复正常关机", command=self._resume).grid(
            row=0, column=4, padx=6)
        ttk.Button(btns, text="退出", command=self._quit).grid(row=0, column=5, padx=6)

        ttk.Label(outer, style="Hint.TLabel", wraplength=520, justify="left",
                  text="提示：卸载也可运行 SelfShutdown.exe --uninstall；关机只调用 Windows 原生 "
                       "shutdown.exe（完整 ACPI 流程，驱动正常卸载），不会损伤硬件；"
                       "如遇安全软件拦截，请将本程序加入信任列表。").grid(
            row=8, column=0, columnspan=4, sticky="w", pady=(10, 0))

    def _var(self, name, var):
        self._vars[name] = var
        return var

    def _update_mode_hint(self):
        label = self._vars["shutdown_mode"].get()
        if label == MODE_LABELS["graceful"]:
            self._mode_label.configure(text="优雅：给程序 10 秒自行保存，卡死的由系统超时强杀")
        else:
            self._mode_label.configure(text="强制：立即关机，仅跳过未响应程序，不影响驱动")

    def _center(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry("+%d+%d" % (max(0, (sw - w) // 2), max(0, int((sh - h) * 0.15))))

    # ---------------- 逻辑 ----------------
    def _collect(self):
        """从界面收集配置（含校验）。"""
        cfg = dict(self.cfg)
        cfg["shutdown_time"] = self._vars["shutdown_time"].get().strip()
        cfg["shutdown_mode"] = MODE_VALUES.get(self._vars["shutdown_mode"].get(), "force")
        cfg["warn_message"] = self._vars["warn_message"].get().strip() or DEFAULT_CONFIG["warn_message"]
        cfg["retry_count"] = self._vars["retry_count"].get()
        cfg["retry_interval_minutes"] = self._vars["retry_interval_minutes"].get()
        cfg["guardian_interval_minutes"] = self._vars["guardian_interval_minutes"].get()
        cfg["warn_seconds"] = self._vars["warn_seconds"].get()
        cfg["run_on_battery"] = self._vars["run_on_battery"].get()
        cfg["wake_to_run"] = self._vars["wake_to_run"].get()
        cfg["guardian_on_logon"] = self._vars["guardian_on_logon"].get()
        return cfg

    def _save(self, silent=False):
        try:
            cfg = self._collect()
            parse_hhmm(cfg["shutdown_time"])  # 校验时间格式
        except Exception as e:
            self.messagebox.showerror("保存失败", "设置项不合法：%s" % e)
            return None
        # 合并 + 规范化（复用 load_config 的强制转换）
        merged = dict(DEFAULT_CONFIG)
        merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
        save_config(merged)
        self.cfg = load_config()
        self._sync_vars()
        self._update_mode_hint()
        if not silent:
            self.messagebox.showinfo("已保存", "设置已写入 config.json")
        return self.cfg

    def _sync_vars(self):
        self._vars["shutdown_time"].set(self.cfg["shutdown_time"])
        self._vars["shutdown_mode"].set(
            MODE_LABELS.get(self.cfg["shutdown_mode"], "强制关机"))
        self._vars["warn_seconds"].set(str(self.cfg["warn_seconds"]))
        self._vars["warn_message"].set(self.cfg["warn_message"])
        self._vars["retry_count"].set(str(self.cfg["retry_count"]))
        self._vars["retry_interval_minutes"].set(str(self.cfg["retry_interval_minutes"]))
        self._vars["guardian_interval_minutes"].set(str(self.cfg["guardian_interval_minutes"]))
        self._vars["run_on_battery"].set(self.cfg["run_on_battery"])
        self._vars["wake_to_run"].set(self.cfg["wake_to_run"])
        self._vars["guardian_on_logon"].set(self.cfg["guardian_on_logon"])

    def _install(self):
        cfg = self._save(silent=True)
        if cfg is None:
            return
        try:
            if is_admin():
                install_tasks(cfg)
            else:
                ok, err = elevated_do(["--install"])
                if not ok:
                    self.messagebox.showerror(
                        "安装失败", "%s\n\n排查建议：\n"
                        "1. 查看 EXE 同目录 actions.log 的最后几行\n"
                        "2. 如装有 360/电脑管家，请将本程序加入信任后重试\n"
                        "3. 确认当前 Windows 账户是管理员" % err)
                    return
            self.messagebox.showinfo("安装完成",
                                     "计划任务已创建：\n"
                                     "· 关机任务（SYSTEM，每天 %s）\n"
                                     "· 提醒任务（提前 %d 秒）\n"
                                     "· 守护任务（开机/周期检查，自动恢复）"
                                     % (cfg["shutdown_time"], cfg["warn_seconds"]))
        except Exception as e:
            log_action("install 失败: %s" % traceback.format_exc())
            self.messagebox.showerror("安装失败", str(e))
        self.refresh_status()

    def _uninstall(self):
        if not self.messagebox.askyesno(
                "确认卸载", "将删除全部计划任务（关机/提醒/守护），之后不再自动关机。\n确定吗？"):
            return
        cfg = self.cfg
        try:
            if is_admin():
                uninstall_tasks(cfg)
            else:
                ok, err = elevated_do(["--uninstall"])
                if not ok:
                    self.messagebox.showerror(
                        "卸载失败", "%s\n\n可手动删除：schtasks /Delete /TN \\SelfDisciplineShutdown\\Enforcement /F\n"
                        "（另两个任务名：Warning、Guardian）" % err)
                    return
            self.messagebox.showinfo("已卸载", "计划任务已删除。程序目录可自行删除。")
        except Exception as e:
            log_action("uninstall 失败: %s" % traceback.format_exc())
            self.messagebox.showerror("卸载失败", str(e))
        self.refresh_status()

    def _test(self):
        win = self.tk.Toplevel(self.root)
        win.title("测试：2 分钟后关机")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        apply_theme(win)
        ttk = self.ttk
        frame = ttk.Frame(win, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frame, wraplength=360, justify="left",
                  text="将创建一个一次性任务：2 分钟后触发关机流程。\n"
                       "建议先保存手头工作。").grid(row=0, column=0, columnspan=2, pady=(0, 10))
        dry_var = self.tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="演练模式（只走流程，不真正关机）",
                        variable=dry_var).grid(row=1, column=0, columnspan=2,
                                               sticky="w", pady=(0, 12))

        def _start():
            cfg = self._save(silent=True)
            if cfg is None:
                return
            args = ["--enforce"] + (["--dry-run"] if dry_var.get() else [])
            try:
                if is_admin():
                    install_one(cfg, TASK_TEST, extra_args=" ".join(args))
                else:
                    ok, err = elevated_do(["--test"] + (["--dry-run"] if dry_var.get() else []))
                    if not ok:
                        self.messagebox.showerror(
                            "创建失败",
                            "%s\n\n排查建议：\n"
                            "1. 查看 EXE 同目录 actions.log 的最后几行\n"
                            "2. 如装有 360/电脑管家，请将本程序加入信任后重试" % err,
                            parent=win)
                        return
            except Exception as e:
                log_action("test 失败: %s" % traceback.format_exc())
                self.messagebox.showerror("创建失败", str(e), parent=win)
                return
            win.grab_release()
            win.destroy()
            self.messagebox.showinfo(
                "已创建测试任务",
                "2 分钟后将触发%s。\n倒计时提醒窗口会先出现。"
                % ("（演练，不会真关机）" if dry_var.get() else "真实关机，请保存好工作"))
            self.refresh_status()

        ttk.Button(frame, text="创建测试任务", style="Accent.TButton",
                   command=_start).grid(row=2, column=0, padx=4, sticky="ew")
        ttk.Button(frame, text="取消", command=lambda: (win.grab_release(), win.destroy())).grid(
            row=2, column=1, padx=4, sticky="ew")
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
        win.geometry("+%d+%d" % (max(0, x), max(0, y)))

    def _resume(self):
        clear_skip()
        self.messagebox.showinfo("已恢复", "已恢复正常自动关机。")
        self.refresh_status()

    def refresh_status(self):
        cfg = self.cfg
        for name, lb in self.status_labels.items():
            try:
                st = task_state(cfg, name)
                lb.configure(text=STATE_LABELS.get(st, st))
            except Exception as e:
                lb.configure(text="查询失败")
        self.skip_label.configure(text=skip_status_text())

    def _quit(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# CLI 模式
# ---------------------------------------------------------------------------

def cli_install():
    cfg = load_config()
    if not is_admin():
        ok, err = elevated_do(["--install"])
        safe_print(err or "安装完成")
        return 0 if ok else 1
    try:
        install_tasks(cfg)
        write_result(True)
        safe_print("OK: 计划任务已创建/更新")
        return 0
    except Exception as e:
        write_result(False, str(e))
        safe_print("FAIL: %s" % e)
        return 1


def cli_uninstall():
    cfg = load_config()
    if not is_admin():
        ok, err = elevated_do(["--uninstall"])
        safe_print(err or "已卸载")
        return 0 if ok else 1
    try:
        uninstall_tasks(cfg)
        write_result(True)
        safe_print("OK: 计划任务已删除")
        return 0
    except Exception as e:
        write_result(False, str(e))
        safe_print("FAIL: %s" % e)
        return 1


def cli_test(dry_run):
    cfg = load_config()
    if not is_admin():
        argv = ["--test"] + (["--dry-run"] if dry_run else [])
        ok, err = elevated_do(argv)
        safe_print(err or "测试任务已创建")
        return 0 if ok else 1
    try:
        args = "--enforce" + (" --dry-run" if dry_run else "")
        install_one(cfg, TASK_TEST, extra_args=args)
        write_result(True)
        safe_print("OK: 测试任务已创建（2 分钟后触发，%s）"
                   % ("演练模式" if dry_run else "真实关机"))
        return 0
    except Exception as e:
        write_result(False, str(e))
        safe_print("FAIL: %s" % e)
        return 1


def cli_status():
    cfg = load_config()
    safe_print("程序路径: %s" % exe_path())
    safe_print("跳过状态: %s" % skip_status_text())
    for name in ALL_TASKS:
        safe_print("%-10s %s" % (name, STATE_LABELS.get(task_state(cfg, name), "?")))
    return 0


def warn_main(dry_run=False):
    if is_skipping():
        log_action("warn: 跳过生效期内，不弹窗")
        return 0
    win = CountdownWindow(load_config(), dry_run=dry_run)
    win.run()
    return 0


def gui_main():
    set_dpi_aware()
    # 单实例防护
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "SelfShutdown_SingleInstance")
    ERROR_ALREADY_EXISTS = 183
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, "自律定时关机 - 设置 v%s" % VERSION)
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        return 0
    app = SettingsApp()
    app.run()
    return 0


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--guardian", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument("--warn", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    parser.add_argument("--result-file", dest="result_file", default=None)
    args, _unknown = parser.parse_known_args()

    set_result_file(args.result_file)
    log_action("startup: argv=%s admin=%s result_file=%s"
               % (" ".join(sys.argv[1:]), is_admin(),
                  bool(args.result_file or os.environ.get("SELFSHUTDOWN_RESULT"))))

    cfg = load_config()
    rc = 0
    try:
        if args.install:
            rc = cli_install()
        elif args.uninstall:
            rc = cli_uninstall()
        elif args.test:
            rc = cli_test(args.dry_run)
        elif args.guardian:
            rc = guardian_main(cfg, dry_run=args.dry_run)
        elif args.enforce:
            rc = enforce_main(cfg, dry_run=args.dry_run)
        elif args.warn:
            rc = warn_main(dry_run=args.dry_run)
        elif args.status:
            rc = cli_status()
        else:
            rc = gui_main()
    except Exception:
        log_action("FATAL: " + traceback.format_exc())
        rc = 1
    finally:
        write_result_missing(rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
