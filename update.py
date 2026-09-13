import json
import threading
import webbrowser
import urllib.request
import tkinter as tk
from tkinter import ttk, messagebox

# ============ 配置区 ============
DEFAULT_VERSION = "请输入..."
UPDATE_URL = "https://lar160212.github.io/version/version.json"
# 如果 JSON 里没有下载地址，默认打开这个页面
FALLBACK_DOWNLOAD = "https://lar160212.github.io/version/"
# ================================


def parse_version(v):
    """把 '1.2.3' 或 'v1.2.3' 转成 (1, 2, 3) 方便比较"""
    v = v.strip().lstrip("vV")
    return tuple(int(x) for x in v.split("."))


def check_update(current_version):
    """status: newer / older / same / error"""
    result = {
        "status": "error",
        "latest": None,
        "current": current_version,
        "notes": "",
        "download_url": "",
        "error": None,
    }

    try:
        req = urllib.request.Request(
            UPDATE_URL,
            headers={"User-Agent": "Mozilla/5.0 (UpdateChecker)"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        latest = data.get("version") or data.get("latest") or data.get("tag") or ""
        notes = data.get("notes") or data.get("changelog") or data.get("message") or ""
        # 兼容更多字段名
        download_url = (
            data.get("download_url")
            or data.get("download")
            or data.get("url")
            or data.get("link")
            or data.get("download_link")
            or ""
        )

        result["latest"] = latest
        result["notes"] = notes
        result["download_url"] = download_url or FALLBACK_DOWNLOAD

        if not latest:
            result["error"] = "服务器未返回版本号"
            return result

        cur = parse_version(current_version)
        srv = parse_version(latest)

        if srv > cur:
            result["status"] = "newer"
        elif srv < cur:
            result["status"] = "older"
        else:
            result["status"] = "same"

    except Exception as e:
        result["error"] = str(e)

    return result


# ============ 你的版本对照提示（原样保留） ============
VERSION_HINT = """alpha 0.1 输入 0.0.1
alpha 0.2 输入 0.0.2
alpha 0.3 输入 0.0.3
beta 0.1 输入 0.1.0
beta 0.2 输入 0.2.0
beta 0.3(特供粉丝版) 输入 0.3.0
cyber 0.1 输入 0.1.1
cyber 0.2 输入 0.2.1"""
# =====================================================


class UpdateCheckerApp:
    # 配色
    BG = "#f5f6fa"
    CARD_BG = "#ffffff"
    PRIMARY = "#4a6cf7"
    PRIMARY_HOVER = "#3b5bdb"
    SUCCESS = "#2f9e44"
    DANGER = "#e03131"
    GRAY = "#868e96"
    TEXT = "#212529"
    BORDER = "#dee2e6"

    def __init__(self, root):
        self.root = root
        self.root.title("检查更新")
        self.root.geometry("500x760")   # 加高
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        self._setup_style()

        # ---- 顶部标题 ----
        header = tk.Frame(root, bg=self.BG)
        header.pack(fill="x", padx=25, pady=(18, 5))
        tk.Label(
            header, text="检查更新", font=("微软雅黑", 16, "bold"),
            bg=self.BG, fg=self.TEXT
        ).pack(anchor="w")
        tk.Label(
            header, text="输入当前版本号，检查是否有新版本",
            font=("微软雅黑", 9), bg=self.BG, fg=self.GRAY
        ).pack(anchor="w", pady=(2, 0))

        # ---- 版本对照提示 ----
        hint_frame = tk.Frame(root, bg="#fff9db",
                              highlightbackground="#ffe066",
                              highlightthickness=1)
        hint_frame.pack(fill="x", padx=25, pady=(8, 5))
        tk.Label(
            hint_frame, text="版本对照表", font=("微软雅黑", 9, "bold"),
            bg="#fff9db", fg="#b8860b"
        ).pack(anchor="w", padx=12, pady=(8, 2))
        tk.Label(
            hint_frame, text=VERSION_HINT, font=("Consolas", 9),
            bg="#fff9db", fg="#5c3d00", justify="left"
        ).pack(anchor="w", padx=12, pady=(0, 8))

        # ---- 版本输入卡片 ----
        card = tk.Frame(root, bg=self.CARD_BG, highlightbackground=self.BORDER,
                        highlightthickness=1)
        card.pack(fill="x", padx=25, pady=8)

        tk.Label(
            card, text="当前版本", font=("微软雅黑", 9),
            bg=self.CARD_BG, fg=self.GRAY
        ).pack(anchor="w", padx=15, pady=(10, 4))

        self.var_version = tk.StringVar(value=DEFAULT_VERSION)
        self.entry_version = tk.Entry(
            card, textvariable=self.var_version,
            font=("微软雅黑", 13), relief="flat",
            bg="#f1f3f5", fg=self.TEXT, insertbackground=self.TEXT
        )
        self.entry_version.pack(fill="x", padx=15, ipady=8, pady=(0, 12))

        # ---- 检查按钮 ----
        self.btn_check = tk.Button(
            root, text="检查更新", command=self.on_check,
            font=("微软雅黑", 11, "bold"),
            bg=self.PRIMARY, fg="white",
            activebackground=self.PRIMARY_HOVER, activeforeground="white",
            relief="flat", cursor="hand2", height=2
        )
        self.btn_check.pack(fill="x", padx=25, pady=(4, 8))
        self.btn_check.bind("<Enter>", lambda e: self.btn_check.config(bg=self.PRIMARY_HOVER))
        self.btn_check.bind("<Leave>", lambda e: self.btn_check.config(bg=self.PRIMARY))

        # ---- 状态提示 ----
        self.status_var = tk.StringVar(value="点击上方按钮检查更新")
        self.status_label = tk.Label(
            root, textvariable=self.status_var,
            font=("微软雅黑", 10), bg=self.BG, fg=self.GRAY,
            wraplength=450, justify="center"
        )
        self.status_label.pack(pady=(0, 6))

        # ---- 结果显示 ----
        result_frame = tk.Frame(root, bg=self.CARD_BG,
                                highlightbackground=self.BORDER,
                                highlightthickness=1)
        result_frame.pack(fill="both", expand=True, padx=25, pady=(0, 12))

        tk.Label(
            result_frame, text="结果", font=("微软雅黑", 9),
            bg=self.CARD_BG, fg=self.GRAY
        ).pack(anchor="w", padx=15, pady=(10, 4))

        self.result_text = tk.Text(
            result_frame, height=10, font=("微软雅黑", 10),
            bg=self.CARD_BG, fg=self.TEXT, relief="flat",
            wrap="word", state="disabled", padx=12, pady=8
        )
        self.result_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # ---- 前往下载按钮（默认隐藏，始终可用） ----
        self.download_url = None
        self.btn_download = tk.Button(
            root, text="前往下载", command=self.on_download,
            font=("微软雅黑", 11, "bold"),
            bg=self.SUCCESS, fg="white",
            activebackground="#2b8a3e", activeforeground="white",
            relief="flat", cursor="hand2", height=2
        )

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

    def set_result(self, text):
        self.result_text.config(state="normal")
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert(tk.END, text)
        self.result_text.config(state="disabled")

    def set_status(self, text, color=None):
        if color is None:
            color = self.GRAY
        self.status_var.set(text)
        self.status_label.config(fg=color)

    def on_check(self):
        current_version = self.var_version.get().strip()

        if not current_version:
            messagebox.showwarning("提示", "请填写当前版本号")
            return

        try:
            parse_version(current_version)
        except ValueError:
            messagebox.showwarning("提示", "版本号格式错误，应为 1.0.0 这种格式")
            return

        self.btn_check.config(state="disabled", bg="#adb5bd")
        self.set_status("正在检查更新，请稍候...", self.GRAY)
        self.set_result("")
        self.btn_download.pack_forget()
        self.download_url = None

        threading.Thread(
            target=self._do_check,
            args=(current_version,),
            daemon=True
        ).start()

    def _do_check(self, current_version):
        result = check_update(current_version)
        self.root.after(0, lambda: self._show_result(result))

    def _show_result(self, result):
        self.btn_check.config(state="normal", bg=self.PRIMARY)

        if result["error"]:
            self.set_status("检查失败", self.DANGER)
            self.set_result(f"错误信息：\n{result['error']}")
            return

        status = result["status"]

        if status == "newer":
            self.set_status("发现新版本！", self.SUCCESS)
            self.set_result(
                f"最新版本：{result['latest']}\n"
                f"当前版本：{result['current']}\n\n"
                f"更新说明：\n{result['notes'] or '无'}\n\n"
                f"下载地址：\n{result['download_url']}"
            )
            # 只要不是 error，就显示下载按钮
            self.download_url = result["download_url"]
            self.btn_download.pack(fill="x", padx=25, pady=(0, 18))
            self.btn_download.bind("<Enter>", lambda e: self.btn_download.config(bg="#2b8a3e"))
            self.btn_download.bind("<Leave>", lambda e: self.btn_download.config(bg=self.SUCCESS))

        elif status == "older":
            self.set_status("⚠ 可能被篡改", self.DANGER)
            self.set_result(
                f"本地版本：{result['current']}\n"
                f"服务器版本：{result['latest']}\n\n"
                f"本地版本高于服务器版本，程序可能已被篡改，\n"
                f"请从官方渠道重新获取。"
            )

        else:
            self.set_status("已是最新版本", self.GRAY)
            self.set_result(
                f"当前版本：{result['current']}\n"
                f"最新版本：{result['latest']}\n\n"
                f"无需更新。"
            )

    def on_download(self):
        if self.download_url:
            webbrowser.open(self.download_url)


if __name__ == "__main__":
    root = tk.Tk()
    UpdateCheckerApp(root)
    root.mainloop()