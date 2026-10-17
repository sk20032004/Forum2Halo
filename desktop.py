"""论坛转 Halo —— 桌面版主程序（tkinter，无浏览器）。

布局仿照网页版：
  ┌─────────────────────────────────────┬──────────────┐
  │ 待导入链接                           │ Halo 设置     │
  │ [链接输入框]                         │  地址/令牌    │
  │ □直接发布 □允许重复导入              │  [测试连接]   │
  │ [开始导入] [只测试解析]  [打开后台]   │  分类         │
  │ 运行日志     [存档文件夹] [清空]     ├──────────────┤
  │ [进度条]                             │ 抓取设置      │
  │ ┌运行日志┬导入结果┐                 │  代理/Cookie  │
  │ │日志内容│         │                 │  并发/上限    │
  │ └───────┴─────────┘                 │  [保存设置]   │
  └─────────────────────────────────────┴──────────────┘
"""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
import traceback
import webbrowser

import tkinter as tk
from tkinter import messagebox, ttk

from halo_client import HaloClient, HaloConfig
from importer import Importer, Settings
from updater import APP_VERSION, DEFAULT_REPO, check_async

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
WORK_ROOT = os.path.join(APP_DIR, "work")

FONT = ("Microsoft YaHei UI", 9)
FONT_MONO = ("Consolas", 9)
BG = "#f5f6f8"
CARD = "#ffffff"
GREEN = "#1db877"   # 统一成功绿：日志与连接状态共用
ICON_PATH = os.path.join(APP_DIR, "app.ico")


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("论坛转 Halo · 导入工具")
        self.geometry("1080x820")
        self.minsize(940, 720)
        self.configure(bg=BG)
        try:
            if os.path.exists(ICON_PATH):
                self.iconbitmap(ICON_PATH)
        except tk.TclError:
            pass

        self.settings = Settings.from_file(CONFIG_PATH)
        self.ui_queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.results: list[dict] = []

        self._style()
        self._build()
        self._bind_right_click_all()
        self._load_to_form()
        self.after(100, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 样式
    def _style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("vista")
        except tk.TclError:
            pass
        st.configure(".", font=FONT)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD, relief="flat")
        st.configure("TLabel", background=BG, foreground="#1c2128")
        st.configure("Card.TLabel", background=CARD, foreground="#1c2128")
        st.configure("Hint.TLabel", background=CARD, foreground="#68717d",
                     font=("Microsoft YaHei UI", 8))
        st.configure("Title.TLabel", background=CARD, foreground="#1c2128",
                     font=("Microsoft YaHei UI", 10, "bold"))
        st.configure("Head.TLabel", background=BG, foreground="#1c2128",
                     font=("Microsoft YaHei UI", 13, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground="#68717d",
                     font=("Microsoft YaHei UI", 9))
        st.configure("TButton", padding=(12, 6))
        st.configure("Primary.TButton", padding=(16, 7))
        st.configure("Card.TCheckbutton", background=CARD, focuscolor=CARD)
        st.configure("Link.TButton", padding=(2, 2))

    # ------------------------------------------------------------ 布局
    def _build(self) -> None:
        # 页头
        head = ttk.Frame(self, style="TFrame")
        head.pack(fill="x", padx=16, pady=(14, 4))
        ttk.Label(head, text="论坛转 Halo · 导入工具", style="Head.TLabel").pack(anchor="w")
        ttk.Label(head, text="批量粘贴帖子链接，自动下载图片转存到 Halo 附件库，并把正文排版成文章。默认存草稿，确认无误后再发布。",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

        # 主体两栏
        body = ttk.Frame(self, style="TFrame")
        body.pack(fill="both", expand=True, padx=16, pady=(8, 14))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0)
        body.rowconfigure(0, weight=1)

        # ================= 左栏 =================
        left = ttk.Frame(body, style="Card.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        li = ttk.Frame(left, style="Card.TFrame")
        li.pack(fill="both", expand=True, padx=14, pady=12)
        li.rowconfigure(5, weight=1)

        ttk.Label(li, text="待导入链接（一行一个）", style="Title.TLabel").grid(row=0, column=0, sticky="w")

        txt_wrap = ttk.Frame(li, style="Card.TFrame")
        txt_wrap.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        li.columnconfigure(0, weight=1)
        self.txt_urls = tk.Text(txt_wrap, height=6, font=FONT_MONO, wrap="none",
                                relief="solid", bd=1, highlightthickness=0,
                                bg="#ffffff", fg="#1c2128", insertbackground="#1c2128")
        self.txt_urls.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(txt_wrap, orient="vertical", command=self.txt_urls.yview)
        sb.pack(side="right", fill="y")
        self.txt_urls.configure(yscrollcommand=sb.set)

        opt = ttk.Frame(li, style="Card.TFrame")
        opt.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.var_publish = tk.BooleanVar(value=False)
        self.var_dup = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="直接发布（不勾选则存为草稿）", variable=self.var_publish,
                        style="Card.TCheckbutton").pack(side="left")
        ttk.Checkbutton(opt, text="允许重复导入", variable=self.var_dup,
                        style="Card.TCheckbutton").pack(side="left", padx=(16, 0))

        btns = ttk.Frame(li, style="Card.TFrame")
        btns.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.btn_start = ttk.Button(btns, text="开始导入", style="Primary.TButton", command=self.on_start)
        self.btn_start.pack(side="left")
        self.btn_test_forum = ttk.Button(btns, text="只测试解析（不上传）", command=self.on_test_forum)
        self.btn_test_forum.pack(side="left", padx=(8, 0))
        self.btn_open_console = ttk.Button(
            btns, text="打开 Halo 后台",
            command=lambda: webbrowser.open(self.var_url.get().rstrip("/") + "/console/"))
        self.btn_open_console.pack(side="right")

        # 日志区
        log_head = ttk.Frame(li, style="Card.TFrame")
        log_head.grid(row=4, column=0, sticky="ew", pady=(14, 6))
        ttk.Label(log_head, text="运行日志", style="Title.TLabel").pack(side="left")
        self.lbl_status = ttk.Label(log_head, text="就绪", style="Hint.TLabel")
        self.lbl_status.pack(side="left", padx=(10, 0))
        ttk.Button(log_head, text="清空", command=self._clear_log, width=6).pack(side="right")
        ttk.Button(log_head, text="打开存档文件夹",
                   command=lambda: os.startfile(WORK_ROOT) if os.path.isdir(WORK_ROOT) else None,
                   width=14).pack(side="right", padx=(0, 6))

        self.pbar = ttk.Progressbar(li, mode="determinate", maximum=100)
        self.pbar.grid(row=5, column=0, sticky="ew", pady=(0, 6))
        li.rowconfigure(6, weight=1)

        nb = ttk.Notebook(li)
        nb.grid(row=6, column=0, sticky="nsew")

        log_frame = ttk.Frame(nb)
        nb.add(log_frame, text="运行日志")
        self.txt_log = tk.Text(log_frame, font=FONT_MONO, wrap="word",
                               bg="#12161c", fg="#d6e0ea", insertbackground="#fff",
                               relief="flat", bd=0, padx=10, pady=8)
        self.txt_log.pack(side="left", fill="both", expand=True)
        lsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt_log.yview)
        lsb.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=lsb.set)
        self.txt_log.tag_configure("ok", foreground=GREEN)
        self.txt_log.tag_configure("err", foreground="#ff8f8f")
        self.txt_log.tag_configure("warn", foreground="#ffc978")
        self.txt_log.tag_configure("muted", foreground="#8a96a3")

        res_frame = ttk.Frame(nb)
        nb.add(res_frame, text="导入结果")
        cols = ("title", "status", "images", "msg")
        self.tree = ttk.Treeview(res_frame, columns=cols, show="headings", height=8)
        self.tree.heading("title", text="帖子标题")
        self.tree.heading("status", text="结果")
        self.tree.heading("images", text="图片")
        self.tree.heading("msg", text="说明")
        self.tree.column("title", width=360, anchor="w")
        self.tree.column("status", width=80, anchor="center")
        self.tree.column("images", width=70, anchor="center")
        self.tree.column("msg", width=280, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        tsb = ttk.Scrollbar(res_frame, orient="vertical", command=self.tree.yview)
        tsb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.tag_configure("ok", foreground="#1a9c5b")
        self.tree.tag_configure("err", foreground="#d33")
        self.tree.tag_configure("warn", foreground="#d98300")
        self.tree.bind("<Double-1>", self._open_selected_post)

        # ================= 右栏 =================
        RIGHT_W = 300
        right_wrap = ttk.Frame(body, style="TFrame", width=RIGHT_W)
        right_wrap.grid(row=0, column=1, sticky="nsew")
        right_wrap.grid_propagate(False)
        right_wrap.columnconfigure(0, weight=1)

        # 卡片1：Halo 设置
        c1 = ttk.Frame(right_wrap, style="Card.TFrame")
        c1.grid(row=0, column=0, sticky="nsew")
        r1 = ttk.Frame(c1, style="Card.TFrame")
        r1.pack(fill="both", expand=True, padx=14, pady=12)

        ttk.Label(r1, text="Halo 设置", style="Title.TLabel").pack(anchor="w")
        ttk.Label(r1, text="博客地址", style="Card.TLabel").pack(anchor="w", pady=(10, 2))
        self.var_url = tk.StringVar()
        self.ent_url = ttk.Entry(r1, textvariable=self.var_url)
        self.ent_url.pack(fill="x")

        ttk.Label(r1, text="个人令牌（pat_ 开头）", style="Card.TLabel").pack(anchor="w", pady=(10, 2))
        self.var_token = tk.StringVar()
        self.ent_token = ttk.Entry(r1, textvariable=self.var_token, show="•")
        self.ent_token.pack(fill="x")
        self.lbl_token_hint = ttk.Label(r1, text="后台 → 右上角头像 → 个人中心 → 个人令牌 → 新建",
                                        style="Hint.TLabel")
        self.lbl_token_hint.pack(anchor="w", pady=(4, 0))

        trow = ttk.Frame(r1, style="Card.TFrame")
        trow.pack(fill="x", pady=(12, 0))
        self.btn_test = ttk.Button(trow, text="测试连接", command=self.on_test_halo, width=10)
        self.btn_test.pack(side="left")
        self.lbl_halo = ttk.Label(trow, text="", style="Hint.TLabel", wraplength=RIGHT_W - 130)
        self.lbl_halo.pack(side="left", padx=(10, 0), fill="x", expand=True)

        ttk.Label(r1, text="分类（留空则不分类）", style="Card.TLabel").pack(anchor="w", pady=(14, 2))
        self.var_category = tk.StringVar()
        self.cmb_category = ttk.Combobox(r1, textvariable=self.var_category)
        self.cmb_category.pack(fill="x")

        # 卡片2：抓取设置
        c2 = ttk.Frame(right_wrap, style="Card.TFrame")
        c2.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        r2 = ttk.Frame(c2, style="Card.TFrame")
        r2.pack(fill="both", expand=True, padx=14, pady=12)

        ttk.Label(r2, text="抓取设置", style="Title.TLabel").pack(anchor="w")

        ttk.Label(r2, text="代理（访问论坛需要）", style="Card.TLabel").pack(anchor="w", pady=(10, 2))
        self.var_proxy = tk.StringVar()
        ttk.Entry(r2, textvariable=self.var_proxy).pack(fill="x")

        ttk.Label(r2, text="论坛 Cookie（可选，需要登录的帖子才填）", style="Card.TLabel").pack(anchor="w", pady=(10, 2))
        self.txt_cookie = tk.Text(r2, height=3, font=FONT, wrap="word",
                                  relief="solid", bd=1, highlightthickness=0)
        self.txt_cookie.pack(fill="x")

        ttk.Label(r2, text="附件分组", style="Card.TLabel").pack(anchor="w", pady=(10, 2))
        self.var_group = tk.StringVar()
        ttk.Entry(r2, textvariable=self.var_group).pack(fill="x")

        adv = ttk.Frame(r2, style="Card.TFrame")
        adv.pack(fill="x", pady=(10, 0))
        ttk.Label(adv, text="并发数", style="Card.TLabel").pack(side="left")
        self.var_threads = tk.StringVar(value="3")
        ttk.Spinbox(adv, from_=1, to=8, textvariable=self.var_threads, width=5).pack(side="left", padx=(4, 0))
        ttk.Label(adv, text="单图上限 MB", style="Card.TLabel").pack(side="left", padx=(16, 0))
        self.var_maxmb = tk.StringVar(value="50")
        ttk.Spinbox(adv, from_=1, to=200, textvariable=self.var_maxmb, width=5).pack(side="left", padx=(4, 0))

        self.var_source_block = tk.BooleanVar(value=True)
        self.var_tag = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="文末附来源信息块", variable=self.var_source_block,
                        style="Card.TCheckbutton").pack(anchor="w", pady=(10, 0))
        ttk.Checkbutton(r2, text="从标题提取番号作标签", variable=self.var_tag,
                        style="Card.TCheckbutton").pack(anchor="w", pady=(4, 0))

        ttk.Button(r2, text="保存设置", style="Primary.TButton",
                   command=self.on_save).pack(anchor="w", pady=(14, 0))

        # 底部：GitHub 点赞 + 清除用户数据
        foot = ttk.Frame(r2, style="Card.TFrame")
        foot.pack(fill="x", pady=(10, 0))
        ttk.Button(foot, text="⭐ GitHub 点赞", style="Link.TButton",
                   command=lambda: webbrowser.open(f"https://github.com/{DEFAULT_REPO}")).pack(side="left")
        ttk.Button(foot, text="清除用户数据", style="Link.TButton",
                   command=self.on_clear_data).pack(side="right")
        self.lbl_ver = ttk.Label(r2, text=f"v{APP_VERSION}", style="Hint.TLabel")
        self.lbl_ver.pack(anchor="e", pady=(2, 0))

        # 静默检查更新（有新版才弹提示，否则安静）
        self.after(2500, lambda: check_async(self._notify_update))

    # ------------------------------------------------------------ 右键菜单
    def _bind_edit_menu(self, widget) -> None:
        menu = tk.Menu(widget, tearoff=0)

        def is_text() -> bool:
            return isinstance(widget, tk.Text)

        def cut(_e=None):
            widget.event_generate("<<Cut>>")

        def copy(_e=None):
            widget.event_generate("<<Copy>>")

        def paste(_e=None):
            try:
                widget.event_generate("<<Paste>>")
            except tk.TclError:
                pass
            return "break"

        def select_all(_e=None):
            if is_text():
                widget.tag_add("sel", "1.0", "end-1c")
            else:
                widget.select_range(0, "end")

        menu.add_command(label="剪切", command=cut)
        menu.add_command(label="复制", command=copy)
        menu.add_command(label="粘贴", command=paste)
        menu.add_separator()
        menu.add_command(label="全选", command=select_all)

        def popup(e):
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()
            return "break"

        widget.bind("<Button-3>", popup)
        widget.bind("<Control-v>", paste)

    def _bind_right_click_all(self) -> None:
        targets: list = [self.txt_urls, self.txt_cookie]
        for w in self.winfo_children():
            for c in self._walk(w):
                if isinstance(c, (tk.Entry, ttk.Entry, ttk.Combobox, ttk.Spinbox)) or (
                        isinstance(c, tk.Text) and c not in (self.txt_log,)):
                    if c not in targets:
                        targets.append(c)
        for w in targets:
            self._bind_edit_menu(w)

    @staticmethod
    def _walk(w):
        yield w
        for child in w.winfo_children():
            yield from App._walk(child)

    # ------------------------------------------------------------ 数据
    def _load_to_form(self) -> None:
        s = self.settings
        self.var_url.set(s.halo_url)
        self.var_token.set(s.halo_token)
        self.var_category.set(s.category)
        self.var_group.set(s.upload_group)
        self.var_proxy.set(s.proxy)
        self.var_threads.set(str(s.threads))
        self.var_maxmb.set(str(s.max_image_mb))
        self.var_source_block.set(s.add_source_block)
        self.var_tag.set(s.tag_from_title)
        self.var_publish.set(s.publish)
        self.txt_cookie.delete("1.0", "end")
        self.txt_cookie.insert("1.0", s.cookie)
        if s.halo_token:
            self.lbl_token_hint.configure(text="已保存令牌，留空输入框则不修改")

    def _form_to_settings(self) -> Settings:
        s = self.settings
        s.halo_url = self.var_url.get().strip()
        token = self.var_token.get().strip()
        if token:
            s.halo_token = token
        s.category = self.var_category.get().strip()
        s.upload_group = self.var_group.get().strip()
        s.proxy = self.var_proxy.get().strip()
        s.cookie = self.txt_cookie.get("1.0", "end").strip()
        try:
            s.threads = max(1, min(8, int(self.var_threads.get())))
        except ValueError:
            s.threads = 3
        try:
            s.max_image_mb = max(1, min(200, int(self.var_maxmb.get())))
        except ValueError:
            s.max_image_mb = 50
        s.add_source_block = self.var_source_block.get()
        s.tag_from_title = self.var_tag.get()
        s.publish = self.var_publish.get()
        return s

    # ------------------------------------------------------------ 事件
    def _notify_update(self, info: dict) -> None:
        """有新版本时由后台线程调用（在 UI 线程执行）。"""
        def show():
            if messagebox.askyesno(
                    "发现新版本",
                    f"当前版本 v{APP_VERSION}，最新版本 v{info['version']}。\n\n"
                    f"{(info.get('notes') or '')[:400]}\n\n"
                    "要现在打开下载页面吗？"):
                webbrowser.open(info["url"])
        self.after(0, show)

    def on_clear_data(self) -> None:
        if not messagebox.askyesno(
                "清除用户数据",
                "将删除以下内容，且无法恢复：\n\n"
                f"· 配置文件（含 Halo 令牌）\n· work/ 下的导入存档与已下载图片\n\n"
                "Halo 博客上的文章和附件不受影响。\n\n确定继续吗？"):
            return
        import shutil
        removed = []
        for p in (CONFIG_PATH, WORK_ROOT):
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    removed.append(os.path.basename(p))
                elif os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                    removed.append(os.path.basename(p) + "/")
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("失败", f"清除 {p} 时出错：{e}")
                return
        self._log(f"已清除用户数据：{', '.join(removed) or '无'}", "warn")
        self._load_to_form()
        self.var_token.set("")
        self.lbl_token_hint.configure(text="已清除，请重新填写令牌")
        messagebox.showinfo("完成", "用户数据已清除。")

    def on_save(self) -> None:
        self._form_to_settings().save(CONFIG_PATH)
        self._log("设置已保存", "ok")

    def on_test_halo(self) -> None:
        s = self._form_to_settings()
        if not s.halo_token:
            self.lbl_halo.configure(text="请先填写个人令牌（pat_ 开头）", foreground="#d33")
            return
        self.lbl_halo.configure(text="连接中…", foreground="#68717d")
        self.btn_test.state(["disabled"])

        def work():
            try:
                c = HaloClient(HaloConfig(base_url=s.halo_url, token=s.halo_token))
                info = c.ping()
                user = ""
                try:
                    user = (c.me().get("spec") or {}).get("displayName", "")
                except Exception:
                    pass
                self.ui_queue.put(("halo_ok", f"✅ 连接成功，现有文章 {info['total']} 篇"
                                              + (f"（{user}）" if user else "")))
                try:
                    cats = [x.get("spec", {}).get("displayName") for x in c.list_categories()]
                    self.ui_queue.put(("categories", [x for x in cats if x]))
                except Exception:
                    pass
            except Exception as e:  # noqa: BLE001
                self.ui_queue.put(("halo_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def on_test_forum(self) -> None:
        urls = self._urls()
        if not urls:
            messagebox.showwarning("提示", "请先粘贴至少一个帖子链接")
            return
        s = self._form_to_settings()
        self._log(f"解析测试：{urls[0]}", "muted")
        self.btn_test_forum.state(["disabled"])

        def work():
            try:
                imp = Importer(s)
                data, _ = imp.fetch_thread(urls[0])
                self.ui_queue.put(("log", (
                    f"✓ 解析成功\n"
                    f"  标题：{data.title}\n"
                    f"  作者：{data.author or '-'}   时间：{data.posted_at or '-'}   版块：{data.forum_name or '-'}\n"
                    f"  图片：{len(data.images)} 张   标签：{', '.join(data.tags) or '-'}", "ok")))
            except Exception as e:  # noqa: BLE001
                self.ui_queue.put(("log", f"✗ 解析失败：{e}", "err"))
            finally:
                self.ui_queue.put(("enable_test_forum", None))

        threading.Thread(target=work, daemon=True).start()

    def on_start(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "正在导入中，请等待完成")
            return
        urls = self._urls()
        if not urls:
            messagebox.showwarning("提示", "请先粘贴至少一个帖子链接")
            return
        s = self._form_to_settings()
        if not s.halo_token:
            messagebox.showwarning("提示", "请先填写 Halo 个人令牌")
            return
        s.save(CONFIG_PATH)

        self.results.clear()
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._clear_log()
        self.stop_flag.clear()
        self.pbar.configure(value=0, maximum=len(urls))
        self._set_running(True)
        self._log(f"开始导入 {len(urls)} 个链接…\n", "muted")

        def work():
            ok = fail = 0
            for i, url in enumerate(urls, 1):
                if self.stop_flag.is_set():
                    self.ui_queue.put(("log", "已停止", "warn"))
                    break
                self.ui_queue.put(("log", f"\n━━━ [{i}/{len(urls)}] {url}", "muted"))
                work_dir = os.path.join(WORK_ROOT, _safe_name(url))
                try:
                    imp = Importer(s, log=lambda m: self.ui_queue.put(("log", "  " + m, None)))
                    res = imp.run(url, work_dir, skip_duplicate=not self.var_dup.get())
                    if res.ok:
                        ok += 1
                        tag = "warn" if res.skipped_duplicate else "ok"
                        self.ui_queue.put(("log",
                                           f"  ✓ {res.message}（图片 {res.images_ok}/{res.images_total}）", tag))
                        self.ui_queue.put(("row", {
                            "title": res.title or url, "status": "已存在" if res.skipped_duplicate else "成功",
                            "tag": tag, "images": f"{res.images_ok}/{res.images_total}",
                            "msg": res.message, "url": res.post_url,
                        }))
                    else:
                        fail += 1
                        self.ui_queue.put(("log", f"  ✗ {res.message}", "err"))
                        self.ui_queue.put(("row", {"title": url, "status": "失败", "tag": "err",
                                                   "images": "-", "msg": res.message, "url": ""}))
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    traceback.print_exc()
                    self.ui_queue.put(("log", f"  ✗ {e}", "err"))
                    self.ui_queue.put(("row", {"title": url, "status": "失败", "tag": "err",
                                               "images": "-", "msg": str(e), "url": ""}))
                self.ui_queue.put(("progress", i))

            self.ui_queue.put(("log", f"\n全部完成：成功 {ok}，失败 {fail}", "ok" if not fail else "warn"))
            self.ui_queue.put(("done", None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _open_selected_post(self, _evt) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        row = self.results[self.tree.index(sel[0])]
        if row.get("url"):
            webbrowser.open(row["url"])

    # ------------------------------------------------------------ 队列
    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    if isinstance(payload, tuple):
                        self._log(payload[0], payload[1])
                    else:
                        self._log(payload)
                elif kind == "progress":
                    self.pbar.configure(value=payload)
                    self.lbl_status.configure(text=f"已处理 {payload}/{int(self.pbar['maximum'])}")
                elif kind == "row":
                    self.results.append(payload)
                    self.tree.insert("", "end", values=(
                        payload["title"], payload["status"], payload["images"], payload["msg"]),
                        tags=(payload["tag"],))
                    self.tree.yview_moveto(1)
                elif kind == "halo_ok":
                    self.lbl_halo.configure(text=payload, foreground=GREEN)
                    self.btn_test.state(["!disabled"])
                    self._log(payload, "ok")
                elif kind == "halo_err":
                    self.lbl_halo.configure(text="❌ " + payload, foreground="#d33")
                    self.btn_test.state(["!disabled"])
                    self._log(payload, "err")
                elif kind == "categories":
                    self.cmb_category["values"] = payload
                elif kind == "enable_test_forum":
                    self.btn_test_forum.state(["!disabled"])
                elif kind == "done":
                    self._set_running(False)
                    self.lbl_status.configure(text="全部完成")
                    self.bell()
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    # ------------------------------------------------------------ 小工具
    def _urls(self) -> list[str]:
        raw = self.txt_urls.get("1.0", "end")
        return [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]

    def _log(self, msg: str, tag: str | None = None) -> None:
        self.txt_log.insert("end", msg + "\n", tag or ())
        self.txt_log.see("end")

    def _clear_log(self) -> None:
        self.txt_log.delete("1.0", "end")

    def _set_running(self, running: bool) -> None:
        if running:
            self.btn_start.state(["disabled"])
            self.btn_test_forum.state(["disabled"])
            self.lbl_status.configure(text="导入中…")
        else:
            self.btn_start.state(["!disabled"])
            self.btn_test_forum.state(["!disabled"])

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("确认", "正在导入中，确定退出吗？未完成的帖子会中断。"):
                return
            self.stop_flag.set()
        try:
            self._form_to_settings().save(CONFIG_PATH)
        except Exception:
            pass
        self.destroy()


def _safe_name(url: str) -> str:
    tid = re.search(r"tid=(\d+)", url)
    tag = tid.group(1) if tid else f"{int(time.time())}"
    host = re.sub(r"[^\w.]", "", url.split("//")[-1].split("/")[0])
    return f"{host}_{tag}"


def main() -> None:
    os.makedirs(WORK_ROOT, exist_ok=True)
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
