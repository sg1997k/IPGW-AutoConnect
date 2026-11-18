#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPGW-AutoConnect
================
东北大学校园网自动登录 · 托盘常驻版

配色取自东北大学校识色(C100 M70 蓝), 顶部标识为「白山黑水」意象的自绘图形,
非校徽复制。若把官方校徽图片放到 assets/logo_neu.png, 程序会自动替换使用。

功能
----
* 常驻系统托盘, 双击打开设置界面
* 可配置: 账号 / 密码 / 检查间隔 / 开机自启动
* 掉线自动重连, 托盘气泡提示, 图标随状态变色
* 窗口可自由缩放, 默认小巧

用法:  python app.py
"""

import os
import sys
import queue
import socket
import threading
import traceback
from datetime import datetime

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk
import pystray

import config as cfgmod
import sso_core as core

APP_NAME = "IPGW-AutoConnect"
APP_TITLE = "IPGW-AutoConnect"
APP_SUB = "Northeastern University · Campus Network"
APP_VER = "1.1"
SINGLE_PORT = 47321

# ---------------------------------------------------------------- 东大配色
PRIMARY = "#00539C"        # 校识色 C100 M70
PRIMARY_DK = "#003D74"
PRIMARY_LT = "#E8F1FA"
BG = "#F3F6FA"
CARD = "#FFFFFF"
TEXT = "#16222F"
MUTED = "#6C7C8C"
LINE = "#E3E9F0"
OK = "#1E9E6A"
BAD = "#C0392B"
WARN = "#D8912A"

FONT = "Microsoft YaHei UI"


def _base_dir():
    """打包成 exe 后 __file__ 指向临时解压目录, 必须改用 exe 所在目录,
    否则配置和 assets 会在临时目录里, 程序一退出就没了。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


ASSETS = os.path.join(_base_dir(), "assets")


# ================================================================ 图形生成
def make_brand_mark(size=96, bg=PRIMARY, fg="#FFFFFF"):
    """自绘标识: 白山黑水意象 —— 山峰 + 水波, 不复制校徽。"""
    S = size * 4  # 超采样后缩小, 边缘更干净
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.24), fill=bg)

    # 远山(小峰)
    d.polygon([(S * 0.44, S * 0.58), (S * 0.66, S * 0.34), (S * 0.88, S * 0.58)], fill=fg)
    # 主峰
    d.polygon([(S * 0.12, S * 0.60), (S * 0.40, S * 0.28), (S * 0.68, S * 0.60)], fill=fg)
    # 水波三道
    for y in (0.68, 0.77, 0.86):
        w = max(2, int(S * 0.030))
        d.arc([-S * 0.06, S * y, S * 0.56, S * y + S * 0.13], start=200, end=340, fill=fg, width=w)
        d.arc([S * 0.44, S * y, S * 1.06, S * y + S * 0.13], start=200, end=340, fill=fg, width=w)
    return img.resize((size, size), Image.LANCZOS)


def load_brand(size=96, bg="#FFFFFF", fg=PRIMARY):
    """优先用 assets 里的官方校徽; 没有就用自绘标识(白底蓝图, 在深蓝顶栏上更醒目)。"""
    p = os.path.join(ASSETS, "logo_neu.png")
    if os.path.exists(p):
        try:
            im = Image.open(p).convert("RGBA")
            im.thumbnail((size, size), Image.LANCZOS)
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2), im)
            return canvas
        except Exception:
            pass
    return make_brand_mark(size, bg=bg, fg=fg)


def load_lab_logo(size=18):
    p = os.path.join(ASSETS, "logo_sapi.png")
    if os.path.exists(p):
        try:
            im = Image.open(p).convert("RGBA")
            im.thumbnail((size, size), Image.LANCZOS)
            return im
        except Exception:
            pass
    return None


def make_tray_icon(color):
    S = 128
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, S - 2, S - 2], fill=color)
    w = max(3, S // 20)
    cx = S // 2
    for rad in (S * 0.13, S * 0.23, S * 0.33):
        d.arc([cx - rad, cx - rad + S * 0.14, cx + rad, cx + rad + S * 0.14],
              start=210, end=330, fill="white", width=w)
    d.ellipse([cx - w, S * 0.63 - w, cx + w, S * 0.63 + w], fill="white")
    return img


ICON_ON = make_tray_icon(OK)
ICON_OFF = make_tray_icon(BAD)
ICON_WARN = make_tray_icon(WARN)


def make_eye_icon(opened=True, size=48, color="#4E5C6A"):
    """眼睛图标。opened=True 画睁眼(密码隐藏中, 点击可查看);
    opened=False 画「睁眼+斜杠」(密码已明文显示, 点击可藏起)。"""
    S = size * 6
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = max(4, int(S * 0.100))
    box = [S * 0.10, S * 0.28, S * 0.90, S * 0.72]
    d.arc(box, start=185, end=355, fill=color, width=w)
    d.arc(box, start=5, end=175, fill=color, width=w)
    r = S * 0.125
    cx = cy = S / 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    if not opened:
        d.line([(S * 0.15, S * 0.20), (S * 0.85, S * 0.80)],
               fill=color, width=int(w * 0.95))
    return img.resize((size, size), Image.LANCZOS)


# ================================================================ 单实例
def acquire_single_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def _set_app_id():
    """设置 AppUserModelID。不设的话, Windows 任务栏会按宿主进程(exe/python.exe)
    的图标显示, 设置后才会用窗口自己的图标。"""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("IPGW.AutoConnect")
    except Exception:
        pass


# ================================================================ 后台监控
def check_configured_connection(cfg):
    if not cfg.get("username") or not cfg.get("password"):
        return core.Connection("idle", "尚未填写账号密码")
    try:
        return core.check_connection(cfg["username"], cfg["password"], int(cfg.get("ac_id", 1)))
    except Exception as ex:
        return core.Connection("unknown", "状态检查暂时失败（%s）" % type(ex).__name__)


def next_check(state, failures, interval):
    if state in ("failed", "pending", "unknown"):
        failures += 1
        first = 15 if state == "unknown" else 60
        cap = min(900, interval * 60) if state == "unknown" else 900
        return min(first * 2 ** min(failures - 1, 6), cap), failures
    return interval * 60, 0


class Monitor(threading.Thread):
    def __init__(self, out: "queue.Queue"):
        super().__init__(daemon=True)
        self.out = out
        self._stopping = threading.Event()
        self._wake = threading.Event()

    def stop(self):
        self._stopping.set()
        self._wake.set()

    def trigger(self):
        self._wake.set()

    def run(self):
        failures = 0
        last_state = None
        while not self._stopping.is_set():
            self._wake.clear()
            cfg = cfgmod.load()
            interval = max(1, int(cfg.get("interval_minutes", 3)))
            result = check_configured_connection(cfg)
            self.out.put((result.state, result.info if result.state == "online" else result.message))
            if last_state != result.state or result.restored:
                self.out.put(("log", result.message))
                if result.restored:
                    self.out.put(("notify", ("已重新连上", result.message)))
                elif result.state == "failed":
                    self.out.put(("notify", ("校园网认证失败", result.message)))
            last_state = result.state
            wait, failures = next_check(result.state, failures, interval)
            if not self._stopping.is_set():
                self._wake.wait(wait)


# ================================================================ 界面
class App:
    def __init__(self, minimized=False):
        self.cfg = cfgmod.load()
        self.out = queue.Queue()
        self.monitor = Monitor(self.out)
        self.icon = None
        self._img_brand = None
        self.minimized = minimized

        self._build_window()
        self._build_tray()
        self.monitor.start()
        self._poll()

        if not self.cfg.get("username"):
            self.log("欢迎使用。填写账号密码后点「保存设置」即可开始守护。")
        elif minimized:
            # 开机自启时只待在托盘里, 不弹窗打扰
            self.root.after(300, self.root.withdraw)

    # ------------------------------------------------------------ 窗口
    def _build_window(self):
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk(fg_color=BG)
        self.root.title("%s — 校园网自动登录" % APP_TITLE)
        self.root.geometry("420x600")
        self.root.minsize(400, 550)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        try:
            self._tk_icon = ImageTk.PhotoImage(load_brand(64))
            self.root.iconphoto(True, self._tk_icon)
        except Exception:
            pass

        self.root.update_idletasks()
        # 高 DPI + 多显示器下, 直接用屏幕宽算居中会偏。改为取主屏工作区, 用窗口实际尺寸算。
        try:
            import ctypes
            from ctypes import wintypes
            wa = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)
            w, h = self.root.winfo_width(), self.root.winfo_height()
            x = wa.left + max(0, (wa.right - wa.left - w) // 2)
            y = wa.top + max(20, (wa.bottom - wa.top - h) // 2 - 40)
            self.root.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

        self._build_header()
        self._build_body()
        self._build_footer()

    # ------------------------------------------------------------ 顶栏
    def _build_header(self):
        hd = ctk.CTkFrame(self.root, fg_color=PRIMARY, corner_radius=0, height=64)
        hd.grid(row=0, column=0, sticky="ew")
        hd.grid_propagate(False)
        hd.grid_columnconfigure(2, weight=1)

        # 校徽
        mark = load_brand(100)
        self._img_brand = ctk.CTkImage(light_image=mark, dark_image=mark, size=(42, 42))
        ctk.CTkLabel(hd, image=self._img_brand, text="").grid(
            row=0, column=0, padx=(18, 10), pady=11)

        # 实验室徽标 —— 图片背景已处理成顶栏同色, 直接放即可无缝融合
        lab = load_lab_logo(84)
        if lab:
            self._img_lab = ctk.CTkImage(light_image=lab, dark_image=lab, size=(42, 42))
            ctk.CTkLabel(hd, image=self._img_lab, text="").grid(
                row=0, column=1, sticky="w", pady=11)

        # 状态
        self.badge = ctk.CTkLabel(hd, text=" 就绪 ", text_color="#FFFFFF",
                                  fg_color="#3C7AB8", corner_radius=10, height=24,
                                  font=ctk.CTkFont(family=FONT, size=11, weight="bold"))
        self.badge.grid(row=0, column=2, sticky="e", padx=(8, 18))

    # ------------------------------------------------------------ 主体
    def _build_body(self):
        wrap = ctk.CTkFrame(self.root, fg_color="transparent")
        wrap.grid(row=1, column=0, sticky="nsew", padx=16, pady=(14, 0))
        wrap.grid_columnconfigure(0, weight=1)
        wrap.grid_rowconfigure(2, weight=1, minsize=150)

        # --- 账号密码
        card = ctk.CTkFrame(wrap, fg_color=CARD, corner_radius=12,
                            border_width=1, border_color=LINE)
        card.grid(row=0, column=0, sticky="ew")
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(card, text="账号", text_color="#5A6976",
                     font=ctk.CTkFont(family=FONT, size=12),
                     anchor="w").grid(row=0, column=0, columnspan=2,
                                      sticky="w", padx=20, pady=(16, 6))
        self.ent_user = ctk.CTkEntry(card, height=38, corner_radius=8,
                                     border_color="#D7DEE7", fg_color="#FBFCFE",
                                     font=ctk.CTkFont(family=FONT, size=14),
                                     placeholder_text="学号 / 工号")
        self.ent_user.grid(row=1, column=0, columnspan=2, sticky="ew", padx=20)
        self.ent_user.insert(0, self.cfg.get("username", ""))

        ctk.CTkLabel(card, text="密码", text_color="#5A6976",
                     font=ctk.CTkFont(family=FONT, size=12),
                     anchor="w").grid(row=2, column=0, columnspan=2,
                                      sticky="w", padx=20, pady=(14, 6))

        # 密码格子: 外框 + 内嵌输入 + 右侧眼睛, 整体是一个"格子"
        pw = ctk.CTkFrame(card, fg_color="#FBFCFE", corner_radius=8,
                          border_width=1, border_color="#D7DEE7", height=40)
        pw.grid(row=3, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 18))
        pw.grid_propagate(False)
        pw.grid_columnconfigure(0, weight=1)

        self.ent_pass = ctk.CTkEntry(pw, height=34, border_width=0, show="●",
                                     fg_color="transparent",
                                     font=ctk.CTkFont(family=FONT, size=14),
                                     placeholder_text="统一身份认证密码")
        self.ent_pass.grid(row=0, column=0, sticky="ew", padx=(10, 0), pady=3)
        self.ent_pass.insert(0, self.cfg.get("password", ""))

        self._eye_open = ctk.CTkImage(light_image=make_eye_icon(True, 48),
                                      dark_image=make_eye_icon(True, 48), size=(22, 22))
        self._eye_shut = ctk.CTkImage(light_image=make_eye_icon(False, 48),
                                      dark_image=make_eye_icon(False, 48), size=(22, 22))
        self.btn_eye = ctk.CTkButton(pw, image=self._eye_open, text="", width=34, height=34,
                                     fg_color="transparent", hover_color="#E8EEF5",
                                     corner_radius=6, command=self.toggle_password)
        self.btn_eye.grid(row=0, column=1, padx=(2, 4), pady=3)

        # --- 设置
        card2 = ctk.CTkFrame(wrap, fg_color=CARD, corner_radius=12,
                             border_width=1, border_color=LINE)
        card2.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        card2.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(card2, text="检查间隔", text_color=TEXT,
                     font=ctk.CTkFont(family=FONT, size=12),
                     anchor="w").grid(row=0, column=0, sticky="w",
                                      padx=(20, 8), pady=(16, 0))
        right = ctk.CTkFrame(card2, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e", padx=(0, 20), pady=(16, 0))
        self.ent_interval = ctk.CTkEntry(right, width=64, height=30, corner_radius=8,
                                         justify="center", border_color=LINE,
                                         fg_color="#FBFCFE",
                                         font=ctk.CTkFont(family=FONT, size=12))
        self.ent_interval.pack(side="left")
        self.ent_interval.insert(0, str(self.cfg.get("interval_minutes", 3)))
        self.cmb_unit = ctk.CTkComboBox(right, values=["分钟", "小时"], width=80,
                                        height=30, corner_radius=8, state="readonly",
                                        border_color="#D7DEE7", fg_color="#FBFCFE",
                                        text_color=TEXT,
                                        button_color="#DCE8F5", button_hover_color="#CBDBEC",
                                        dropdown_fg_color=CARD,
                                        dropdown_text_color=TEXT,
                                        font=ctk.CTkFont(family=FONT, size=12),
                                        command=self._unit_changed)
        self.cmb_unit.pack(side="left", padx=(6, 0))
        self.cmb_unit.set("分钟")
        self._unit_cache = "分钟"

        ctk.CTkLabel(card2, text="开机自动联网", text_color=TEXT,
                     font=ctk.CTkFont(family=FONT, size=12),
                     anchor="w").grid(row=1, column=0, sticky="w",
                                      padx=(20, 8), pady=(14, 2))
        self.sw_autostart = ctk.CTkSwitch(card2, text="", width=44,
                                          progress_color=PRIMARY,
                                          command=self._autostart_toggled)
        self.sw_autostart.grid(row=1, column=1, sticky="e", padx=(0, 20), pady=(14, 2))
        ctk.CTkLabel(card2, text="开启后重启电脑，停在锁屏界面也会自动连上网（需要一次管理员授权）",
                     text_color="#8A97A4", font=ctk.CTkFont(family=FONT, size=10),
                     anchor="w", justify="left", wraplength=340).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 16))
        # 任务状态查询要走 PowerShell(约 1 秒), 放后台, 别卡住窗口出现
        self.sw_autostart.deselect()
        threading.Thread(target=self._initial_autostart_check, daemon=True).start()

        # --- 状态(可拉伸区)
        self.card_status = ctk.CTkFrame(wrap, fg_color=CARD, corner_radius=12,
                                        border_width=1, border_color=LINE)
        self.card_status.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        self.card_status.grid_columnconfigure(0, weight=1)
        self.card_status.grid_rowconfigure(2, weight=1)

        top = ctk.CTkFrame(self.card_status, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 0))
        self.lbl_status = ctk.CTkLabel(top, text="● 未检测", text_color=MUTED,
                                       font=ctk.CTkFont(family=FONT, size=13, weight="bold"),
                                       anchor="w")
        self.lbl_status.pack(side="left")
        self.lbl_detail = ctk.CTkLabel(top, text="", text_color=MUTED,
                                       font=ctk.CTkFont(family=FONT, size=11), anchor="e")
        self.lbl_detail.pack(side="right")

        ctk.CTkFrame(self.card_status, height=1, fg_color=LINE).grid(
            row=1, column=0, sticky="ew", padx=20, pady=(10, 0))

        self.txt_log = ctk.CTkTextbox(self.card_status, fg_color="transparent",
                                      text_color="#54636F", border_width=0,
                                      font=ctk.CTkFont(family="Consolas", size=11),
                                      wrap="word")
        self.txt_log.grid(row=2, column=0, sticky="nsew", padx=14, pady=(6, 10))
        self.txt_log.configure(state="disabled")

    # ------------------------------------------------------------ 底栏
    def _build_footer(self):
        ft = ctk.CTkFrame(self.root, fg_color="transparent")
        ft.grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 14))
        ft.grid_columnconfigure((0, 1, 2), weight=1)

        ctk.CTkButton(ft, text="保存设置", height=38, corner_radius=9,
                      fg_color=PRIMARY, hover_color=PRIMARY_DK,
                      font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                      command=self.save_settings).grid(row=0, column=0, sticky="ew", padx=(0, 5))

        ctk.CTkButton(ft, text="立即重连", height=38, corner_radius=9,
                      fg_color="#2F80D8", hover_color="#2569B5",
                      font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                      command=self.reconnect_now).grid(row=0, column=1, sticky="ew", padx=5)

        ctk.CTkButton(ft, text="注销", height=38, corner_radius=9,
                      fg_color=CARD, hover_color="#FBEEEC", text_color=BAD,
                      border_width=1, border_color=LINE,
                      font=ctk.CTkFont(family=FONT, size=12),
                      command=self.do_logout).grid(row=0, column=2, sticky="ew", padx=(5, 0))

    # ------------------------------------------------------------ 托盘
    def _build_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("打开设置界面", self.show_window, default=True),
            pystray.MenuItem("立即重连", self.tray_reconnect),
            pystray.MenuItem("刷新状态", lambda *a: self.monitor.trigger()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自动启动", self.tray_toggle_autostart,
                             checked=lambda item: cfgmod.autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self.quit),
        )
        self.icon = pystray.Icon("ipgw-autoconnect", ICON_ON,
                                 "%s · 校园网守护" % APP_TITLE, menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    # ------------------------------------------------------------ 动作
    def toggle_password(self):
        if self.ent_pass.cget("show") == "●":
            self.ent_pass.configure(show="")
            self.btn_eye.configure(image=self._eye_shut)
        else:
            self.ent_pass.configure(show="●")
            self.btn_eye.configure(image=self._eye_open)

    def _unit_changed(self, value):
        try:
            cur = float(self.ent_interval.get() or "0")
        except ValueError:
            cur = 0.0
        if value == "小时" and self._unit_cache == "分钟":
            new = cur / 60.0
        elif value == "分钟" and self._unit_cache == "小时":
            new = cur * 60.0
        else:
            new = cur
        self.ent_interval.delete(0, "end")
        self.ent_interval.insert(0, "%g" % round(new, 2))
        self._unit_cache = value

    def _autostart_toggled(self):
        on = bool(self.sw_autostart.get())
        if on:
            if cfgmod.is_admin():
                ok, msg = cfgmod.task_install()
                if ok:
                    cfgmod.autostart_enable()   # 登录后静默进托盘
                    self.log("开机自动联网已开启（登录前也会连，开机不弹窗）")
                    self.cfg["autostart"] = True
                    cfgmod.save(self.cfg)
                else:
                    self.log("开启失败: %s" % msg)
                    self.sw_autostart.deselect()
            else:
                if cfgmod.relaunch_as_admin("--install-task"):
                    self.log("已请求管理员权限, 请在弹窗点「是」")
                    self.root.after(1500, self._sync_autostart)
                else:
                    self.log("提权被取消, 无法创建开机任务")
                    self.sw_autostart.deselect()
        else:
            if not self._confirm("确定关闭开机自动联网？\n\n"
                                 "关闭后重启电脑不会自动认证，\n"
                                 "需要自己打开网页登录。"):
                self.sw_autostart.select()
                return
            if cfgmod.is_admin():
                ok, msg = cfgmod.task_remove()
                cfgmod.autostart_disable()
                self.log("已关闭开机自动联网" if ok else "关闭失败: %s" % msg)
            else:
                if cfgmod.relaunch_as_admin("--remove-task"):
                    self.log("已请求管理员权限, 请在弹窗点「是」")
                self.root.after(3500, self._sync_autostart)
            self.cfg["autostart"] = False
            cfgmod.save(self.cfg)

    def _initial_autostart_check(self):
        """启动时查一次任务状态(后台线程), 不重试。"""
        try:
            exists = cfgmod.task_exists()
        except Exception:
            exists = False

        def _apply():
            if exists:
                self.sw_autostart.select()
            else:
                self.sw_autostart.deselect()
        self.root.after(0, _apply)

    def _sync_autostart(self, tries=0):
        """提权是异步的, 而且用户点 UAC 的时间不确定。
        所以放后台线程反复查(不卡界面), 最多等约 15 秒再判失败。"""
        def worker():
            try:
                exists = cfgmod.task_exists()
            except Exception:
                exists = False
            self.root.after(0, lambda: self._apply_autostart(exists, tries))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_autostart(self, exists, tries):
        if exists:
            self.sw_autostart.select()
            self.log("开机自动联网: 已启用")
            return
        if tries < 4:
            self.root.after(3000, lambda: self._sync_autostart(tries + 1))
        else:
            self.sw_autostart.deselect()
            self.log("开机自动联网: 未启用(任务没建成, 可能是授权被取消)")

    def save_settings(self):
        self.cfg["username"] = self.ent_user.get().strip()
        self.cfg["password"] = self.ent_pass.get()
        try:
            val = float(self.ent_interval.get().strip() or "3")
        except ValueError:
            val = 3
        if self._unit_cache == "小时":
            val *= 60
        self.cfg["interval_minutes"] = max(1, int(val))
        self.cfg["autostart"] = bool(self.sw_autostart.get())
        cfgmod.save(self.cfg)
        self.log("设置已保存 · 每 %d 分钟检查一次" % self.cfg["interval_minutes"])
        self.monitor.trigger()

    def reconnect_now(self):
        # save_settings 已唤醒监控线程，避免重复提交认证。
        self.save_settings()

    def do_logout(self):
        if not self._confirm("确定断开当前校园网连接？\n断开后网络会立刻不可用，直到重新登录。"):
            return
        threading.Thread(target=self._logout_worker, daemon=True).start()

    def _logout_worker(self):
        core.logout(int(self.cfg.get("ac_id", 1)))
        self.out.put(("offline", {}))
        self.out.put(("log", "已注销当前连接"))

    # ------------------------------------------------------------ 托盘动作
    def show_window(self, *a):
        self.root.after(0, self._show)

    def _show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_to_tray(self):
        self.root.withdraw()
        self._notify("仍在后台运行", "关闭窗口只是收进托盘")

    def tray_reconnect(self, *a):
        self.monitor.trigger()

    def tray_toggle_autostart(self, *a):
        cur = cfgmod.autostart_enabled()
        cfgmod.autostart_set(not cur)
        self.root.after(0, self.sw_autostart.select if not cur
                        else self.sw_autostart.deselect)

    def quit(self, *a):
        try:
            self.monitor.stop()
            if self.icon:
                self.icon.stop()
        except Exception:
            pass
        self.root.after(0, self.root.destroy)

    # ------------------------------------------------------------ 更新
    def _confirm(self, text):
        from tkinter import messagebox
        return messagebox.askyesno(APP_TITLE, text)

    def log(self, msg):
        stamp = datetime.now().strftime("%H:%M:%S")

        def _w():
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", "%s  %s\n" % (stamp, msg))
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        self.root.after(0, _w)

    def _badge(self, text, color):
        self.root.after(0, lambda: self.badge.configure(text=text, fg_color=color))

    def _status(self, online, info=None):
        if online:
            self.lbl_status.configure(text="● 已认证在线", text_color=OK)
        else:
            self.lbl_status.configure(text="● 未连接 / 已掉线", text_color=BAD)
        if info:
            add = info.get("add_time")
            t = datetime.fromtimestamp(int(add)).strftime("%m-%d %H:%M") if add else "—"
            dev = info.get("online_device_total", "—")
            rb = info.get("remain_bytes")
            rem = "%.2f GB" % (int(rb) / 1024 ** 3) if rb is not None else "—"
            line = "上线 %s · 设备 %s · 余 %s" % (t, dev, rem)
            self.lbl_detail.configure(text=line)
        else:
            self.lbl_detail.configure(text="")

    def _notify(self, title, msg):
        if self.icon:
            try:
                self.icon.notify(msg, title)
            except Exception:
                pass

    def _poll(self):
        try:
            while True:
                kind, data = self.out.get_nowait()
                if kind == "log":
                    self.log(data)
                elif kind == "online":
                    self._badge(" 在线 ", OK)
                    self._status(True, data)
                    if self.icon:
                        self.icon.icon = ICON_ON
                elif kind == "offline":
                    self._badge(" 掉线 ", BAD)
                    self._status(False)
                    if self.icon:
                        self.icon.icon = ICON_OFF
                elif kind in ("unknown", "reachable", "pending", "other", "failed", "idle"):
                    labels = {"unknown": "待确认", "reachable": "网络可用", "pending": "待确认",
                              "other": "其他账号在线", "failed": "认证失败", "idle": "待配置"}
                    color = BAD if kind == "failed" else WARN
                    self._badge(labels[kind], color)
                    self.lbl_status.configure(text="● " + labels[kind], text_color=color)
                    self.lbl_detail.configure(text=data)
                    if self.icon:
                        self.icon.icon = ICON_OFF if kind == "failed" else ICON_WARN
                elif kind == "notify":
                    t, m = data
                    self._notify(t, m)
        except queue.Empty:
            pass
        self.root.after(800, self._poll)

    def run(self):
        self.root.mainloop()


# ================================================================ 无界面模式
def run_headless():
    """供开机计划任务调用: 不开窗口、不要托盘, 只闷头把网连上。
    这样机器重启后停在锁屏界面、没人登录也能自动认证。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as guard:
        try:
            guard.bind(("127.0.0.1", SINGLE_PORT + 1))
            guard.listen(1)
        except OSError:
            return
        _run_headless_loop()


def _run_headless_loop():
    import time as _t

    logdir = os.path.join(_base_dir(), "logs")
    try:
        os.makedirs(logdir, exist_ok=True)
    except Exception:
        pass
    logfile = os.path.join(logdir, "auto.log")

    def _log(msg):
        line = "%s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            if os.path.exists(logfile) and os.path.getsize(logfile) > 512 * 1024:
                open(logfile, "w", encoding="utf-8").close()
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    _log("headless 启动")
    failures = 0
    while True:
        cfg = cfgmod.load()
        interval = max(1, int(cfg.get("interval_minutes", 3)))
        result = check_configured_connection(cfg)
        wait, failures = next_check(result.state, failures, interval)
        if result.state == "idle":
            wait = 30
        _log("%s · %d 秒后检查" % (result.message, wait))
        _t.sleep(wait)


# ================================================================ 入口
def _listen_show_requests(lock, app):
    """监听单实例端口: 用户再次双击程序时, 把已在托盘里的窗口亮出来。"""
    def loop():
        while True:
            try:
                conn, _ = lock.accept()
                try:
                    data = conn.recv(32)
                    if b"show" in data:
                        app.root.after(0, app._show)
                finally:
                    conn.close()
            except Exception:
                break
    threading.Thread(target=loop, daemon=True).start()


def main():
    args = sys.argv[1:]

    if "--headless" in args:
        run_headless()
        return

    if "--install-task" in args:
        ok, msg = cfgmod.task_install()
        if ok:
            cfgmod.autostart_enable()
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            ("开机自动联网已启用。\n\n"
             "以后重启电脑, 即使停在登录界面、没人解锁, 也会自动认证上网。\n"
             "登录后程序只待在托盘里, 不会弹窗。") if ok
            else ("创建计划任务失败:\n%s\n\n请右键程序选「以管理员身份运行」再试。" % msg),
            "IPGW-AutoConnect", 0x40 if ok else 0x30)
        return

    if "--remove-task" in args:
        ok, msg = cfgmod.task_remove()
        cfgmod.autostart_disable()
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            "开机自动联网已关闭。" if ok else ("关闭失败:\n%s" % msg),
            "IPGW-AutoConnect", 0x40 if ok else 0x30)
        return

    lock = acquire_single_instance()
    if lock is None:
        # 已经有实例在跑: 让它把窗口亮出来, 然后自己退出
        try:
            s = socket.create_connection(("127.0.0.1", SINGLE_PORT), timeout=2)
            s.sendall(b"show")
            s.close()
        except Exception:
            print("%s 已在运行，请查看系统托盘。" % APP_TITLE)
        return
    os.makedirs(ASSETS, exist_ok=True)
    _set_app_id()

    app = App(minimized=("--minimized" in args))
    _listen_show_requests(lock, app)
    app.run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        input("程序异常退出，按回车关闭…")
