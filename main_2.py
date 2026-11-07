import sys
import os
import json
import uuid
import shutil
import subprocess
import requests
from pathlib import Path

import minecraft_launcher_lib
from minecraft_launcher_lib.exceptions import VersionNotFound

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QUrl
from PyQt6.QtGui import QFont, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QPushButton, QProgressBar,
    QMessageBox, QFrame, QListWidget, QListWidgetItem,
    QSpinBox, QDialog, QDialogButtonBox, QMenu, QFormLayout, QGridLayout
)

from qt_material import QtStyleTools

# ============================================================================
# 配置文件管理
# ============================================================================
CONFIG_FILE = Path(__file__).parent / "launcher_config.json"

DEFAULT_CONFIG = {
    "username": "Player",
    "theme": "dark_teal.xml",
    "java_path": "",
    "memory_mb": 2048,
    "last_version": "",
    "favorite_versions": [],
    "littleskin_account": None,
    "background_image": ""
}

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(data)
            return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def ensure_authlib_injector():
    """自动下载 LittleSkin 外置登录所需的 authlib-injector.jar"""
    jar_path = Path(__file__).parent / "authlib-injector.jar"
    
    # 如果文件不存在，或者文件大小小于 100KB（说明下错了），就重新下载
    if not jar_path.exists() or jar_path.stat().st_size < 100000:
        # 换用官方 GitHub 的直链
        url = "https://github.com/yushijinhun/authlib-injector/releases/download/v1.2.5/authlib-injector-1.2.5.jar"
        try:
            r = requests.get(url, stream=True, timeout=15, allow_redirects=True)
            r.raise_for_status() # 如果 404 会直接报错
            with open(jar_path, 'wb') as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            print("authlib-injector 下载成功")
        except Exception as e:
            print(f"authlib-injector 自动下载失败: {e}")
            if jar_path.exists():
                os.remove(jar_path) # 删掉下错的空文件
    return jar_path

# ============================================================================
# 后台工作线程
# ============================================================================
class FetchVersionsWorker(QThread):
    finished_ok = pyqtSignal(list)
    finished_error = pyqtSignal(str)

    def __init__(self, mc_dir):
        super().__init__()
        self.mc_dir = mc_dir

    def run(self):
        try:
            versions = minecraft_launcher_lib.utils.get_available_versions(self.mc_dir)
            ids = [v["id"] for v in versions if v["type"] == "release"]
            self.finished_ok.emit(ids)
        except Exception as e:
            self.finished_error.emit(str(e))


class InstallWorker(QThread):
    status = pyqtSignal(str)
    progress = pyqtSignal(int)
    max_value = pyqtSignal(int)
    done = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, mode, mc_version, mc_dir):
        super().__init__()
        self.mode = mode
        self.mc_version = mc_version
        self.mc_dir = mc_dir

    def _callback(self):
        return {
            "setStatus": lambda t: self.status.emit(t),
            "setProgress": lambda v: self.progress.emit(v),
            "setMax": lambda v: self.max_value.emit(max(v, 1)),
        }

    def run(self):
        try:
            cb = self._callback()
            if self.mode == "vanilla":
                minecraft_launcher_lib.install.install_minecraft_version(self.mc_version, self.mc_dir, callback=cb)
                installed_id = self.mc_version
            elif self.mode == "forge":
                forge_versions = minecraft_launcher_lib.forge.find_forge_versions(self.mc_version)
                if not forge_versions:
                    self.error.emit(f"Forge 没有适用于 {self.mc_version} 的版本")
                    return
                installed_id = minecraft_launcher_lib.forge.install_forge_version(forge_versions[0], self.mc_dir, callback=cb)
            elif self.mode == "fabric":
                installed_id = minecraft_launcher_lib.fabric.install_fabric(self.mc_version, self.mc_dir, callback=cb)
            elif self.mode == "quilt":
                installed_id = minecraft_launcher_lib.quilt.install_quilt(self.mc_version, self.mc_dir, callback=cb)
            else:
                self.error.emit("未知安装模式")
                return
            self.done.emit(installed_id)
        except Exception as e:
            self.error.emit(str(e))


class LaunchWorker(QThread):
    status = pyqtSignal(str)
    success = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, version, mc_dir, options, java_path=""):
        super().__init__()
        self.version = version
        self.mc_dir = mc_dir
        self.options = options
        self.java_path = java_path

    def run(self):
        try:
            self.status.emit(f"正在校验版本 {self.version} ...")
            minecraft_launcher_lib.install.install_minecraft_version(self.version, self.mc_dir)
            
            self.status.emit("正在生成启动命令...")
            command = minecraft_launcher_lib.command.get_minecraft_command(self.version, self.mc_dir, self.options)
            if self.java_path and Path(self.java_path).exists():
                command[0] = self.java_path

            self.status.emit("正在启动 Minecraft，等待游戏进程...")
            
            # 启动并捕获输出
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            
            # 阻塞等待游戏退出（如果游戏正常运行，这里会一直卡住，说明启动成功）
            # 如果闪退，会立刻返回并拿到报错
            output, _ = process.communicate()
            
            if process.returncode != 0:
                error_msg = f"游戏异常退出 (代码: {process.returncode})\n\n可能的错误日志:\n"
                lines = output.strip().split('\n')
                error_msg += "\n".join(lines[-20:])
                self.error.emit(error_msg)
            else:
                # 退出代码为 0，正常关闭
                self.success.emit()
                
        except VersionNotFound:
            self.error.emit(f"版本 {self.version} 不存在或文件损坏")
        except Exception as e:
            self.error.emit(str(e))


class LittleSkinLoginWorker(QThread):
    success = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, email, password):
        super().__init__()
        self.email = email
        self.password = password

    def run(self):
        url = "https://littleskin.cn/api/yggdrasil/authserver/authenticate"
        payload = {
            "agent": {"name": "Minecraft", "version": 1},
            "username": self.email,
            "password": self.password,
            "requestUser": True
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            if resp.status_code == 200 and "accessToken" in data:
                self.success.emit({
                    "username": data["selectedProfile"]["name"],
                    "uuid": data["selectedProfile"]["id"],
                    "accessToken": data["accessToken"],
                    "clientToken": data["clientToken"]
                })
            else:
                self.error.emit(data.get("errorMessage", "登录失败，请检查账号密码"))
        except Exception as e:
            self.error.emit(f"网络请求失败: {e}")


class ModSearchWorker(QThread):
    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, query, mc_version, loader):
        super().__init__()
        self.query = query
        self.mc_version = mc_version
        self.loader = loader.lower()

    def run(self):
        url = "https://api.modrinth.com/v2/search"
        facets = f'[["categories:{self.loader}"],["versions:{self.mc_version}"]]'
        params = {"query": self.query, "facets": facets, "limit": 20}
        try:
            resp = requests.get(url, params=params, timeout=10)
            self.finished.emit(resp.json().get("hits", []))
        except Exception as e:
            self.error.emit(str(e))


class ModDownloadWorker(QThread):
    success = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, project_id, mc_version, loader, mc_dir):
        super().__init__()
        self.project_id = project_id
        self.mc_version = mc_version
        self.loader = loader.lower()
        self.mc_dir = mc_dir

    def run(self):
        url = f"https://api.modrinth.com/v2/project/{self.project_id}/version"
        params = {"loaders": f'["{self.loader}"]', "game_versions": f'["{self.mc_version}"]'}
        try:
            resp = requests.get(url, params=params, timeout=10)
            versions = resp.json()
            if not versions:
                self.error.emit("未找到兼容当前版本的下载文件")
                return
                
            file_info = versions[0]["files"][0]
            mods_dir = Path(self.mc_dir) / "mods"
            mods_dir.mkdir(exist_ok=True)
            filepath = mods_dir / file_info["filename"]
            
            r = requests.get(file_info["url"], stream=True, timeout=30)
            with open(filepath, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            self.success.emit(file_info["filename"])
        except Exception as e:
            self.error.emit(str(e))


# ============================================================================
# 弹窗组件
# ============================================================================
class LittleSkinLoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LittleSkin 外置登录")
        self.resize(320, 220)
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel("邮箱:"))
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("请输入 LittleSkin 邮箱")
        layout.addWidget(self.email_edit)
        
        layout.addWidget(QLabel("密码:"))
        self.pass_edit = QLineEdit()
        self.pass_edit.setPlaceholderText("请输入密码")
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.pass_edit)
        
        layout.addSpacing(10)
        self.btn_login = QPushButton("登录")
        self.btn_login.setObjectName("primaryBtn")
        self.btn_login.clicked.connect(self.do_login)
        layout.addWidget(self.btn_login)
        
        self.login_data = None
        self.worker = None

    def do_login(self):
        email = self.email_edit.text().strip()
        password = self.pass_edit.text().strip()
        if not email or not password:
            QMessageBox.warning(self, "提示", "邮箱和密码不能为空")
            return
            
        self.btn_login.setEnabled(False)
        self.btn_login.setText("登录中...")
        
        self.worker = LittleSkinLoginWorker(email, password)
        self.worker.success.connect(self._on_success)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def _on_success(self, data):
        self.login_data = data
        self.accept()

    def _on_error(self, msg):
        self.btn_login.setEnabled(True)
        self.btn_login.setText("登录")
        QMessageBox.critical(self, "登录失败", msg)


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("设置")
        self.setFixedSize(450, 380)
        layout = QVBoxLayout(self)
        
        form = QFormLayout()
        self.java_edit = QLineEdit(config.get("java_path", ""))
        self.java_edit.setPlaceholderText("留空则使用系统默认 Java")
        form.addRow("Java 路径:", self.java_edit)
        
        self.memory_spin = QSpinBox()
        self.memory_spin.setRange(512, 32768)
        self.memory_spin.setSingleStep(512)
        self.memory_spin.setSuffix(" MB")
        self.memory_spin.setValue(config.get("memory_mb", 2048))
        form.addRow("最大内存:", self.memory_spin)
        
        layout.addLayout(form)
        layout.addStretch()
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_config(self):
        cfg = self.config.copy()
        cfg["java_path"] = self.java_edit.text().strip()
        cfg["memory_mb"] = self.memory_spin.value()
        return cfg


# ============================================================================
# 主窗口
# ============================================================================
class LauncherWindow(QMainWindow, QtStyleTools):
    THEMES = {
        "深色 · Teal": "dark_teal.xml",
        "深色 · Blue": "dark_blue.xml",
        "深色 · Purple": "dark_purple.xml",
        "深色 · Red": "dark_red.xml",
        "浅色 · Teal": "light_teal.xml",
        "浅色 · Blue": "light_blue.xml",
    }

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.setWindowTitle("Yuansu Minecraft Launcher - Pro")
        self.resize(980, 680)

        self.minecraft_dir = str(Path(__file__).parent / ".minecraft")
        os.makedirs(self.minecraft_dir, exist_ok=True)
        os.makedirs(Path(self.minecraft_dir) / "mods", exist_ok=True)

        self._fetch_worker = None
        self._install_worker = None
        self._launch_worker = None
        self._mod_search_worker = None
        self._mod_download_worker = None

        self._build_ui()
        self._apply_theme(self.config["theme"])
        self._center()
        
        self._fetch_versions_async()
        self.load_local_versions()
        self._update_account_ui()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 左侧导航栏 ----
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(200)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(15, 20, 15, 20)
        side_layout.setSpacing(8)

        logo = QLabel("⛏  Yuansu Launcher")
        logo.setObjectName("logo")
        side_layout.addWidget(logo)
        side_layout.addSpacing(20)

        nav_buttons = [
            ("🚀  启动游戏", self._show_page_launch),
            ("📦  版本管理", self._show_page_version),
            ("🧩  模组下载", self._show_page_mod),
            ("🧰  百宝箱", self._show_page_toolbox),
            ("⚙️  设置", self._open_settings)
        ]
        for text, slot in nav_buttons:
            btn = QPushButton(text)
            btn.setObjectName("navBtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(slot)
            side_layout.addWidget(btn)

        side_layout.addStretch()

        side_layout.addWidget(QLabel("主题"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(list(self.THEMES.keys()))
        self.theme_combo.currentTextChanged.connect(self._on_theme_changed)
        side_layout.addWidget(self.theme_combo)

        root.addWidget(sidebar)

        # ---- 右侧内容区 ----
        self.content_stack = QWidget()
        self.content_layout = QVBoxLayout(self.content_stack)
        self.content_layout.setContentsMargins(0, 0, 0, 0)

        self.page_launch = self._build_page_launch()
        self.page_version = self._build_page_version()
        self.page_mod = self._build_page_mod()
        self.page_toolbox = self._build_page_toolbox()

        for p in [self.page_launch, self.page_version, self.page_mod, self.page_toolbox]:
            p.hide()
            self.content_layout.addWidget(p)
            
        self.page_launch.show()
        root.addWidget(self.content_stack, 1)

    def _build_page_launch(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 25, 30, 25)
        layout.setSpacing(15)
        
        layout.addWidget(self._make_title("启动游戏"))

        # 账户卡片
        acc_card = QFrame()
        acc_card.setObjectName("card")
        acc_layout = QHBoxLayout(acc_card)
        acc_layout.setContentsMargins(20, 15, 20, 15)
        
        self.account_label = QLabel("👤  离线 · Player")
        self.account_label.setObjectName("accountLabel")
        acc_layout.addWidget(self.account_label, 1)
        
        login_btn = QPushButton("LittleSkin 登录")
        login_btn.setObjectName("secondaryBtn")
        login_btn.clicked.connect(self._littleskin_login)
        acc_layout.addWidget(login_btn)
        layout.addWidget(acc_card)

        # 启动卡片
        launch_card = QFrame()
        launch_card.setObjectName("card")
        lc_layout = QVBoxLayout(launch_card)
        lc_layout.setContentsMargins(20, 20, 20, 20)
        
        form = QFormLayout()
        self.username_input = QLineEdit(self.config.get("username", "Player"))
        form.addRow("离线用户名:", self.username_input)
        
        self.version_combo = QComboBox()
        self.version_combo.setPlaceholderText("请先在版本管理中安装")
        form.addRow("游戏版本:", self.version_combo)
        lc_layout.addLayout(form)
        lc_layout.addSpacing(10)
        
        self.launch_btn = QPushButton("▶   启动 Minecraft")
        self.launch_btn.setObjectName("primaryBtn")
        self.launch_btn.setMinimumHeight(45)
        self.launch_btn.clicked.connect(self._start_launch)
        lc_layout.addWidget(self.launch_btn)
        layout.addWidget(launch_card)
        
        layout.addStretch()
        self.status_label = QLabel("就绪")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)
        
        return page

    def _build_page_version(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 25, 30, 25)
        layout.addWidget(self._make_title("版本管理"))

        ins_card = QFrame()
        ins_card.setObjectName("card")
        il = QVBoxLayout(ins_card)
        il.addWidget(QLabel("安装新版本"))
        
        ins_form = QFormLayout()
        self.install_version_combo = QComboBox()
        ins_form.addRow("游戏版本:", self.install_version_combo)
        self.loader_combo = QComboBox()
        self.loader_combo.addItems(["原版 (Vanilla)", "Forge", "Fabric", "Quilt"])
        ins_form.addRow("加载器:", self.loader_combo)
        il.addLayout(ins_form)
        
        self.install_btn = QPushButton("开始安装")
        self.install_btn.setObjectName("secondaryBtn")
        self.install_btn.clicked.connect(self._start_install)
        il.addWidget(self.install_btn, alignment=Qt.AlignmentFlag.AlignRight)
        
        self.install_progress = QProgressBar()
        self.install_progress.setRange(0, 100)
        il.addWidget(self.install_progress)
        self.install_status = QLabel("")
        il.addWidget(self.install_status)
        layout.addWidget(ins_card)

        list_card = QFrame()
        list_card.setObjectName("card")
        ll = QVBoxLayout(list_card)
        ll.addWidget(QLabel("已安装版本 (右键操作)"))
        self.version_list = QListWidget()
        self.version_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.version_list.customContextMenuRequested.connect(self._version_context_menu)
        ll.addWidget(self.version_list)
        layout.addWidget(list_card, 1)
        
        return page

    def _build_page_mod(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 25, 30, 25)
        layout.addWidget(self._make_title("在线 Mod 下载 (Modrinth)"))

        search_card = QFrame()
        search_card.setObjectName("card")
        sl = QHBoxLayout(search_card)
        
        self.mod_search_input = QLineEdit()
        self.mod_search_input.setPlaceholderText("输入 Mod 名称 (如: Sodium, JEI)...")
        self.mod_search_btn = QPushButton("搜索")
        self.mod_search_btn.setObjectName("secondaryBtn")
        self.mod_search_btn.clicked.connect(self._search_mods)
        
        sl.addWidget(self.mod_search_input, 1)
        sl.addWidget(self.mod_search_btn)
        layout.addWidget(search_card)

        self.mod_list = QListWidget()
        self.mod_list.itemDoubleClicked.connect(self._download_mod)
        layout.addWidget(self.mod_list, 1)
        
        hint = QLabel("提示: 双击列表项即可下载至 .minecraft/mods 目录")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)
        
        return page

    def _build_page_toolbox(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 25, 30, 25)
        layout.addWidget(self._make_title("🧰 百宝箱"))

        grid = QGridLayout()
        grid.setSpacing(15)
        
        btn_clean = QPushButton("🧹 一键清理日志与崩溃报告")
        btn_clean.setObjectName("toolBtn")
        btn_clean.clicked.connect(self._clean_garbage)
        grid.addWidget(btn_clean, 0, 0)
        
        btn_open_mods = QPushButton("📂 打开 Mods 文件夹")
        btn_open_mods.setObjectName("toolBtn")
        btn_open_mods.clicked.connect(lambda: self._open_folder("mods"))
        grid.addWidget(btn_open_mods, 0, 1)
        
        btn_open_root = QPushButton("📁 打开 .minecraft 目录")
        btn_open_root.setObjectName("toolBtn")
        btn_open_root.clicked.connect(lambda: self._open_folder(""))
        grid.addWidget(btn_open_root, 1, 0)
        
        btn_check_java = QPushButton("☕ 检测 Java 环境")
        btn_check_java.setObjectName("toolBtn")
        btn_check_java.clicked.connect(self._check_java)
        grid.addWidget(btn_check_java, 1, 1)
        
        layout.addLayout(grid)
        layout.addStretch()
        return page

    def _make_title(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("pageTitle")
        return lbl

    # ------------------------------------------------------------------
    # 页面切换
    # ------------------------------------------------------------------
    def _show_page_launch(self):
        for p in [self.page_version, self.page_mod, self.page_toolbox]: p.hide()
        self.page_launch.show()

    def _show_page_version(self):
        for p in [self.page_launch, self.page_mod, self.page_toolbox]: p.hide()
        self.page_version.show()
        self.load_local_versions()

    def _show_page_mod(self):
        for p in [self.page_launch, self.page_version, self.page_toolbox]: p.hide()
        self.page_mod.show()

    def _show_page_toolbox(self):
        for p in [self.page_launch, self.page_version, self.page_mod]: p.hide()
        self.page_toolbox.show()

    # ------------------------------------------------------------------
    # 主题与设置
    # ------------------------------------------------------------------
    def _apply_theme(self, theme_file):
        try:
            invert = theme_file.startswith("light")
            self.apply_stylesheet(QApplication.instance(), theme=theme_file, invert_secondary=invert)
        except Exception:
            pass
        self.setStyleSheet(self._extra_qss())

    def _on_theme_changed(self, label):
        theme_file = self.THEMES.get(label, "dark_teal.xml")
        self.config["theme"] = theme_file
        save_config(self.config)
        self._apply_theme(theme_file)

    def _extra_qss(self):
        return """
        QFrame#sidebar { background-color: rgba(0, 0, 0, 0.15); border-right: 1px solid rgba(255,255,255,0.05); }
        QLabel#logo { font-size: 18px; font-weight: bold; }
        QLabel#pageTitle { font-size: 22px; font-weight: bold; margin-bottom: 10px; }
        QLabel#accountLabel { font-size: 14px; font-weight: bold; }
        QPushButton#navBtn { text-align: left; padding: 10px; border: none; background: transparent; font-size: 13px; border-radius: 8px;}
        QPushButton#navBtn:hover { background: rgba(255, 255, 255, 0.1); }
        QFrame#card { background-color: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 12px; padding: 10px;}
        QPushButton#primaryBtn { background-color: #89b4fa; color: #11111b; font-weight: bold; border-radius: 8px; }
        QPushButton#primaryBtn:hover { background-color: #b4befe; }
        QPushButton#secondaryBtn { background-color: rgba(255,255,255,0.1); border-radius: 6px; padding: 5px 15px;}
        QPushButton#secondaryBtn:hover { background-color: rgba(255,255,255,0.2); }
        QPushButton#toolBtn { padding: 15px; font-size: 14px; border-radius: 8px; background-color: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); }
        QPushButton#toolBtn:hover { background-color: rgba(255,255,255,0.15); }
        QProgressBar { border-radius: 4px; height: 8px; text-align: center; }
        QProgressBar::chunk { background-color: #a6e3a1; border-radius: 4px; }
        QListWidget { border-radius: 8px; background-color: rgba(0,0,0,0.1); }
        """

    def _center(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move((screen.width() - self.width()) // 2, (screen.height() - self.height()) // 2)

    def set_status(self, text):
        self.status_label.setText(text)

    def _open_settings(self):
        dlg = SettingsDialog(self.config, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.config = dlg.get_config()
            save_config(self.config)
            QMessageBox.information(self, "提示", "设置已保存")

    # ------------------------------------------------------------------
    # 版本列表与安装
    # ------------------------------------------------------------------
    def _fetch_versions_async(self):
        self._fetch_worker = FetchVersionsWorker(self.minecraft_dir)
        self._fetch_worker.finished_ok.connect(self._on_versions_fetched)
        self._fetch_worker.start()

    def _on_versions_fetched(self, ids):
        self.install_version_combo.clear()
        self.install_version_combo.addItems(ids)
        if ids: self.install_version_combo.setCurrentIndex(0)

    def load_local_versions(self):
        try:
            installed = minecraft_launcher_lib.utils.get_installed_versions(self.minecraft_dir)
            ids = [v["id"] for v in installed]
        except Exception:
            ids = []

        favs = set(self.config.get("favorite_versions", []))
        ids.sort(key=lambda x: (x not in favs, x))

        self.version_combo.clear()
        self.version_list.clear()
        for vid in ids:
            prefix = "⭐ " if vid in favs else "📦 "
            self.version_combo.addItem(prefix + vid, vid)
            item = QListWidgetItem(prefix + vid)
            item.setData(Qt.ItemDataRole.UserRole, vid)
            self.version_list.addItem(item)

    def _version_context_menu(self, pos):
        item = self.version_list.itemAt(pos)
        if not item: return
        vid = item.data(Qt.ItemDataRole.UserRole)
        favs = self.config.get("favorite_versions", [])
        is_fav = vid in favs

        menu = QMenu(self)
        fav_act = menu.addAction("取消收藏" if is_fav else "收藏版本")
        open_act = menu.addAction("打开版本文件夹")
        del_act = menu.addAction("删除版本")
        action = menu.exec(self.version_list.mapToGlobal(pos))

        if action == fav_act:
            if is_fav: favs.remove(vid)
            else: favs.append(vid)
            self.config["favorite_versions"] = favs
            save_config(self.config)
            self.load_local_versions()
        elif action == open_act:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.minecraft_dir) / "versions" / vid)))
        elif action == del_act:
            if QMessageBox.question(self, "确认删除", f"确定删除 {vid} 吗？不可恢复！") == QMessageBox.StandardButton.Yes:
                shutil.rmtree(Path(self.minecraft_dir) / "versions" / vid, ignore_errors=True)
                self.load_local_versions()

    def _start_install(self):
        mc_version = self.install_version_combo.currentText()
        mode = ["vanilla", "forge", "fabric", "quilt"][self.loader_combo.currentIndex()]
        if not mc_version: return

        self.install_btn.setEnabled(False)
        self.install_progress.setValue(0)
        self.install_status.setText("准备安装...")

        self._install_worker = InstallWorker(mode, mc_version, self.minecraft_dir)
        self._install_worker.status.connect(self.install_status.setText)
        self._install_worker.progress.connect(self.install_progress.setValue)
        self._install_worker.max_value.connect(lambda v: self.install_progress.setMaximum(max(v, 1)))
        self._install_worker.done.connect(self._on_install_done)
        self._install_worker.error.connect(lambda msg: QMessageBox.critical(self, "安装失败", msg))
        self._install_worker.start()

    def _on_install_done(self, installed_id):
        self.install_btn.setEnabled(True)
        self.install_status.setText(f"安装完成: {installed_id}")
        self.load_local_versions()

    # ------------------------------------------------------------------
    # 账户与启动逻辑
    # ------------------------------------------------------------------
    def _littleskin_login(self):
        dlg = LittleSkinLoginDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.login_data:
            self.config["littleskin_account"] = dlg.login_data
            save_config(self.config)
            self._update_account_ui()

    def _update_account_ui(self):
        acc = self.config.get("littleskin_account")
        if acc:
            self.account_label.setText(f"👤  外置登录 · {acc['username']}")
            self.username_input.setEnabled(False)
        else:
            self.account_label.setText("👤  离线模式")
            self.username_input.setEnabled(True)

    def _start_launch(self):
        version = self.version_combo.currentData()
        if not version:
            QMessageBox.warning(self, "提示", "请先安装并选择版本")
            return

        self.launch_btn.setEnabled(False)
        self.set_status("正在准备启动...")

        options = {}
        jvm_args = [f"-Xmx{self.config['memory_mb']}M"]
        
        acc = self.config.get("littleskin_account")
        if acc:
            options = {
                "username": acc["username"],
                "uuid": acc["uuid"],
                "token": acc["accessToken"],
                "userType": "mojang",
            }
            jar_path = ensure_authlib_injector()
            if jar_path.exists():
                jar_path_str = str(jar_path).replace("\\", "/")
                jvm_args.append(f"-javaagent:{jar_path_str}=https://littleskin.cn/api/yggdrasil")
        else:
            username = self.username_input.text().strip() or "Player"
            offline_uuid = str(uuid.uuid3(uuid.NAMESPACE_DNS, username))
            options = {
                "username": username,
                "uuid": offline_uuid,
                "token": "0",
                "userType": "legacy",
            }

        options["jvmArguments"] = jvm_args

        self._launch_worker = LaunchWorker(version, self.minecraft_dir, options, self.config.get("java_path", ""))
        self._launch_worker.status.connect(self.set_status)
        self._launch_worker.success.connect(self._on_launch_success)
        self._launch_worker.error.connect(self._on_launch_error)
        self._launch_worker.start()

    def _on_launch_success(self):
        # 游戏正常关闭后恢复按钮
        self.launch_btn.setEnabled(True)
        self.set_status("游戏已退出")

    def _on_launch_error(self, msg):
        self.launch_btn.setEnabled(True)
        self.set_status("启动失败，请查看日志")
        # 弹出一个详细的日志窗口
        dlg = QDialog(self)
        dlg.setWindowTitle("启动失败日志")
        dlg.resize(600, 400)
        layout = QVBoxLayout(dlg)
        
        text = QLabel(msg)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(text)
        
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    # ------------------------------------------------------------------
    # Mod 检索与下载
    # ------------------------------------------------------------------
    def _search_mods(self):
        query = self.mod_search_input.text().strip()
        version = self.version_combo.currentData()
        if not query: return
        if not version:
            QMessageBox.warning(self, "提示", "请先选择一个游戏版本，以便筛选兼容的 Mod")
            return

        loader = "fabric"
        if "forge" in version.lower(): loader = "forge"
        elif "quilt" in version.lower(): loader = "quilt"
        mc_ver = version.split("-")[0]

        self.mod_list.clear()
        self.mod_list.addItem("搜索中，请稍候...")
        self.mod_search_btn.setEnabled(False)

        self._mod_search_worker = ModSearchWorker(query, mc_ver, loader)
        self._mod_search_worker.finished.connect(self._on_mod_searched)
        self._mod_search_worker.error.connect(lambda e: self.mod_list.addItem(f"出错: {e}"))
        self._mod_search_worker.start()

    def _on_mod_searched(self, hits):
        self.mod_search_btn.setEnabled(True)
        self.mod_list.clear()
        if not hits:
            self.mod_list.addItem("未找到相关 Mod")
            return
        for mod in hits:
            item = QListWidgetItem(f"【{mod['title']}】 {mod['description'][:40]}...")
            item.setData(Qt.ItemDataRole.UserRole, mod["project_id"])
            self.mod_list.addItem(item)

    def _download_mod(self, item):
        project_id = item.data(Qt.ItemDataRole.UserRole)
        version = self.version_combo.currentData()
        if not project_id or not version: return
        
        loader = "fabric"
        if "forge" in version.lower(): loader = "forge"
        mc_ver = version.split("-")[0]

        QMessageBox.information(self, "下载", "开始下载，完成后会弹出提示。")
        
        self._mod_download_worker = ModDownloadWorker(project_id, mc_ver, loader, self.minecraft_dir)
        self._mod_download_worker.success.connect(lambda fn: QMessageBox.information(self, "成功", f"下载完成:\n{fn}"))
        self._mod_download_worker.error.connect(lambda e: QMessageBox.critical(self, "失败", e))
        self._mod_download_worker.start()

    # ------------------------------------------------------------------
    # 百宝箱与工具
    # ------------------------------------------------------------------
    def _clean_garbage(self):
        dirs_to_clean = ["logs", "crash-reports"]
        cleaned = []
        for d in dirs_to_clean:
            target = Path(self.minecraft_dir) / d
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                cleaned.append(d)
        
        versions_dir = Path(self.minecraft_dir) / "versions"
        if versions_dir.exists():
            for v in versions_dir.iterdir():
                if v.is_dir() and (v / "logs").exists():
                    shutil.rmtree(v / "logs", ignore_errors=True)
        
        QMessageBox.information(self, "清理完成", f"已清理: {', '.join(cleaned) if cleaned else '未发现垃圾文件'}")

    def _open_folder(self, sub):
        path = Path(self.minecraft_dir) / sub
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _check_java(self):
        try:
            res = subprocess.run(["java", "-version"], capture_output=True, text=True, check=True)
            info = res.stderr or res.stdout
            QMessageBox.information(self, "Java 环境", f"当前系统默认 Java:\n{info}")
        except Exception:
            QMessageBox.warning(self, "未找到", "未在系统 PATH 中找到 java，请前往设置手动指定。")


# ============================================================================
# 入口
# ============================================================================
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    if sys.platform == "win32":
        app.setFont(QFont("Microsoft YaHei UI", 10))
    else:
        app.setFont(QFont("Noto Sans CJK SC", 10))

    window = LauncherWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()