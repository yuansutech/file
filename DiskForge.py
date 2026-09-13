#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DiskForge - 一个只读的磁盘查看工具
直读物理设备，解析 MBR/GPT，十六进制查看扇区。

运行要求：
  Windows: 管理员权限
  Linux  : root

目前所有操作都是只读，不会碰你的分区表。
"""

import os
import sys
import struct
import ctypes
import tkinter as tk
from tkinter import ttk, messagebox

IS_WIN = sys.platform.startswith('win')
SECTOR_SIZE = 512


# ---------------------------------------------------------------
# Windows 裸设备 API 封装
# ---------------------------------------------------------------
if IS_WIN:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE, ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
    ]
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]

    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C


# ---------------------------------------------------------------
# 裸设备句柄
# ---------------------------------------------------------------
class RawDisk:
    """打开物理磁盘 / 块设备，只提供顺序读和指定偏移读。"""

    def __init__(self, path, size=0, index=-1):
        self.path = path
        self.size = size
        self.index = index
        self._handle = None
        self._fd = None

    def open(self, write=False):
        if IS_WIN:
            access = GENERIC_READ | (GENERIC_WRITE if write else 0)
            handle = kernel32.CreateFileW(
                self.path, access,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None, OPEN_EXISTING, 0, None,
            )
            if not handle or handle == INVALID_HANDLE_VALUE:
                err = ctypes.get_last_error()
                raise OSError(err, f"打开 {self.path} 失败 (WinError={err})")
            self._handle = handle
        else:
            flags = os.O_RDWR if write else os.O_RDONLY
            try:
                self._fd = os.open(self.path, flags)
            except PermissionError:
                raise OSError(13, f"打开 {self.path} 失败，权限不足")
        return self

    def close(self):
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def read(self, offset, length):
        if IS_WIN:
            new_pos = ctypes.c_longlong(0)
            ok = kernel32.SetFilePointerEx(
                self._handle, ctypes.c_longlong(offset),
                ctypes.byref(new_pos), 0,
            )
            if not ok:
                raise OSError(f"Seek 到 0x{offset:X} 失败")
            buf = ctypes.create_string_buffer(length)
            read = wintypes.DWORD(0)
            ok = kernel32.ReadFile(
                self._handle, buf, length, ctypes.byref(read), None,
            )
            if not ok:
                raise OSError(f"读取 0x{offset:X} 失败")
            return buf.raw[:read.value]
        else:
            return os.pread(self._fd, length, offset)

    def __enter__(self):
        return self.open()

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------
# 枚举磁盘
# ---------------------------------------------------------------
def enum_disks():
    """返回所有可访问的物理磁盘列表。"""
    disks = []

    if IS_WIN:
        for i in range(32):
            path = rf"\\.\PhysicalDrive{i}"
            handle = kernel32.CreateFileW(
                path, GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None, OPEN_EXISTING, 0, None,
            )
            if not handle or handle == INVALID_HANDLE_VALUE:
                continue
            size = ctypes.c_ulonglong(0)
            returned = wintypes.DWORD(0)
            ok = kernel32.DeviceIoControl(
                handle, IOCTL_DISK_GET_LENGTH_INFO,
                None, 0, ctypes.byref(size), 8,
                ctypes.byref(returned), None,
            )
            kernel32.CloseHandle(handle)
            if not ok or size.value == 0:
                continue
            disks.append({
                'index': i,
                'path': path,
                'size': size.value,
                'model': f"PhysicalDrive{i}",
            })
    else:
        # Linux: 从 /sys/block 枚举，跳过虚拟设备
        skip = ('loop', 'ram', 'dm-', 'sr', 'zram', 'md')
        try:
            names = sorted(os.listdir('/sys/block'))
        except OSError:
            names = []

        for name in names:
            if name.startswith(skip):
                continue
            dev = f'/dev/{name}'
            if not os.path.exists(dev):
                continue
            try:
                with open(f'/sys/block/{name}/size') as f:
                    sectors = int(f.read().strip())
            except (OSError, ValueError):
                continue
            if sectors == 0:
                continue

            model = name
            model_path = f'/sys/block/{name}/device/model'
            if os.path.exists(model_path):
                try:
                    with open(model_path) as f:
                        model = f.read().strip() or name
                except OSError:
                    pass

            disks.append({
                'index': len(disks),
                'path': dev,
                'size': sectors * SECTOR_SIZE,
                'model': model,
            })

    return disks


# ---------------------------------------------------------------
# 分区表解析
# ---------------------------------------------------------------
MBR_TYPES = {
    0x00: "Empty",          0x01: "FAT12",
    0x04: "FAT16 <32M",     0x05: "Extended",
    0x06: "FAT16",          0x07: "NTFS / exFAT",
    0x0B: "FAT32 (CHS)",    0x0C: "FAT32 (LBA)",
    0x0E: "FAT16 (LBA)",    0x0F: "Extended (LBA)",
    0x27: "Windows Recovery", 0x42: "LDM",
    0x82: "Linux Swap",     0x83: "Linux",
    0x85: "Linux Extended", 0x8E: "Linux LVM",
    0xA5: "FreeBSD",        0xA8: "Mac OS X",
    0xAF: "Mac HFS/HFS+",   0xEE: "GPT Protective",
    0xEF: "EFI System",     0xFD: "Linux RAID",
}

GPT_TYPES = {
    "C12A7328-F81F-11D2-BA4B-00A0C93EC93B": "EFI System",
    "E3C9E316-0B5C-4DB8-817D-F92DF00215AE": "MS Reserved",
    "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7": "MS Basic Data",
    "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC": "Windows Recovery",
    "21686148-6449-6E6F-744E-656564454649": "BIOS Boot",
    "0FC63DAF-8483-4772-8E79-3D69D8477DE4": "Linux Filesystem",
    "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F": "Linux Swap",
    "E6D6D379-F507-44C2-A23C-238F2A3DF928": "Linux LVM",
    "A19D880F-05FC-4D3B-A006-743F0F84911E": "Linux RAID",
    "48465300-0000-11AA-AA11-00306543ECAC": "Apple HFS+",
    "7C3457EF-0000-11AA-AA11-00306543ECAC": "Apple APFS",
}


def guid_to_str(data):
    """把 16 字节的小端 GUID 转成标准字符串。"""
    if len(data) < 16:
        return ""
    d1 = struct.unpack('<I', data[0:4])[0]
    d2 = struct.unpack('<H', data[4:6])[0]
    d3 = struct.unpack('<H', data[6:8])[0]
    d4 = data[8:10].hex().upper()
    d5 = data[10:16].hex().upper()
    return f"{d1:08X}-{d2:04X}-{d3:04X}-{d4}-{d5}"


def parse_mbr(data):
    """从 512 字节 MBR 里解析 4 个主分区条目，空的跳过。"""
    if len(data) < 512 or data[510:512] != b'\x55\xAA':
        return None

    parts = []
    for i in range(4):
        off = 446 + i * 16
        entry = data[off:off + 16]
        ptype = entry[4]
        lba_start = struct.unpack('<I', entry[8:12])[0]
        sectors = struct.unpack('<I', entry[12:16])[0]
        if ptype == 0 and sectors == 0:
            continue
        parts.append({
            'slot': i + 1,
            'bootable': entry[0] == 0x80,
            'type': ptype,
            'type_name': MBR_TYPES.get(ptype, f"Unknown 0x{ptype:02X}"),
            'lba_start': lba_start,
            'lba_end': lba_start + sectors - 1 if sectors else lba_start,
            'sectors': sectors,
            'size': sectors * SECTOR_SIZE,
            'name': '',
        })
    return parts


def parse_gpt_header(data):
    """解析 LBA1 处的 GPT 头。"""
    if len(data) < 92 or data[0:8] != b'EFI PART':
        return None
    return {
        'revision':     struct.unpack('<I', data[8:12])[0],
        'header_size':  struct.unpack('<I', data[12:16])[0],
        'crc32':        struct.unpack('<I', data[16:20])[0],
        'current_lba':  struct.unpack('<Q', data[24:32])[0],
        'backup_lba':   struct.unpack('<Q', data[32:40])[0],
        'first_usable': struct.unpack('<Q', data[40:48])[0],
        'last_usable':  struct.unpack('<Q', data[48:56])[0],
        'disk_guid':    guid_to_str(data[56:72]),
        'entry_lba':    struct.unpack('<Q', data[72:80])[0],
        'num_entries':  struct.unpack('<I', data[80:84])[0],
        'entry_size':   struct.unpack('<I', data[84:88])[0],
    }


def parse_gpt_entries(data, header):
    """遍历 GPT 分区条目区。"""
    parts = []
    entry_size = header['entry_size']
    for i in range(header['num_entries']):
        entry = data[i * entry_size:(i + 1) * entry_size]
        if len(entry) < 128 or entry[0:16] == b'\x00' * 16:
            continue
        type_guid = guid_to_str(entry[0:16])
        unique_guid = guid_to_str(entry[16:32])
        start = struct.unpack('<Q', entry[32:40])[0]
        end = struct.unpack('<Q', entry[40:48])[0]
        attrs = struct.unpack('<Q', entry[48:56])[0]
        try:
            name = entry[56:128].decode('utf-16-le', errors='ignore').rstrip('\x00')
        except Exception:
            name = ''
        parts.append({
            'slot': i + 1,
            'type_guid': type_guid,
            'type_name': GPT_TYPES.get(type_guid, "Unknown GPT Type"),
            'uniq_guid': unique_guid,
            'lba_start': start,
            'lba_end': end,
            'sectors': end - start + 1 if end >= start else 0,
            'size': (end - start + 1) * SECTOR_SIZE if end >= start else 0,
            'attrs': attrs,
            'name': name,
        })
    return parts


def scan_disk(disk):
    """扫描一块磁盘，返回分区方案和分区列表。"""
    result = {
        'scheme': 'RAW',
        'mbr_raw': b'',
        'gpt_header': None,
        'partitions': [],
    }

    try:
        raw = RawDisk(disk['path'], disk['size']).open()
    except OSError:
        return result

    try:
        mbr = raw.read(0, 512)
        result['mbr_raw'] = mbr
        if len(mbr) < 512 or mbr[510:512] != b'\x55\xAA':
            return result

        # 第一分区槽类型是 0xEE 说明是 GPT 保护性 MBR
        if mbr[450] == 0xEE:
            header_data = raw.read(512, 512)
            header = parse_gpt_header(header_data)
            if header and header['num_entries'] and header['entry_size'] >= 128:
                result['scheme'] = 'GPT'
                result['gpt_header'] = header
                total = header['num_entries'] * header['entry_size']
                entries_raw = raw.read(header['entry_lba'] * SECTOR_SIZE, total)
                result['partitions'] = parse_gpt_entries(entries_raw, header)
                return result

        parts = parse_mbr(mbr) or []
        result['partitions'] = parts
        result['scheme'] = 'MBR' if parts else 'RAW'

    except OSError:
        pass
    finally:
        raw.close()

    return result


# ---------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------
def fmt_size(n):
    if n is None:
        return "N/A"
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB', 'PB'):
        if n < 1024 or unit == 'PB':
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def render_hex(data, base_offset):
    """把字节流格式化成 hexdump 文本。"""
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_part = ' '.join(f'{b:02X}' for b in chunk).ljust(47)
        ascii_part = ''.join(
            chr(b) if 32 <= b < 127 else '.' for b in chunk
        )
        lines.append(f"{base_offset + i:012X}  {hex_part}  |{ascii_part}|")
    return '\n'.join(lines)


# ---------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------
class DiskForgeApp:

    BG = '#1e1e22'
    BG_PANEL = '#26262c'
    BG_HEADER = '#2e2e36'
    FG = '#d4d4d8'
    FG_DIM = '#8a8a94'
    ACCENT = '#4fc3f7'
    WARN = '#ff7043'
    OK = '#81c784'

    def __init__(self, root):
        self.root = root
        root.title("DiskForge")
        root.geometry("1200x760")
        root.minsize(900, 560)
        root.configure(bg=self.BG)

        self.disks = []
        self.scan_cache = {}
        self.tree_map = {}
        self.cur_disk = None
        self.cur_part = None
        self.view_offset = 0
        self.view_length = 4096

        self._setup_style()
        self._build_ui()
        self.root.after(100, self.refresh)

    # ----- 样式 -----
    def _setup_style(self):
        style = ttk.Style()
        style.theme_use('clam')

        style.configure('.',
                        background=self.BG,
                        foreground=self.FG,
                        fieldbackground=self.BG_PANEL,
                        bordercolor=self.BG_HEADER,
                        lightcolor=self.BG_HEADER,
                        darkcolor=self.BG_HEADER,
                        focuscolor=self.BG_HEADER)

        style.configure('Treeview',
                        background=self.BG_PANEL,
                        foreground=self.FG,
                        fieldbackground=self.BG_PANEL,
                        rowheight=24,
                        borderwidth=0)
        style.configure('Treeview.Heading',
                        background=self.BG_HEADER,
                        foreground=self.ACCENT,
                        relief='flat',
                        font=('Consolas', 10, 'bold'))
        style.map('Treeview',
                  background=[('selected', '#0f4c63')],
                  foreground=[('selected', '#ffffff')])

        style.configure('TNotebook',
                        background=self.BG, borderwidth=0)
        style.configure('TNotebook.Tab',
                        background=self.BG_HEADER,
                        foreground=self.FG_DIM,
                        padding=[14, 7],
                        font=('Consolas', 10),
                        borderwidth=0)
        style.map('TNotebook.Tab',
                  background=[('selected', self.BG_PANEL)],
                  foreground=[('selected', self.ACCENT)])

        style.configure('TFrame', background=self.BG)
        style.configure('TLabel', background=self.BG, foreground=self.FG)
        style.configure('Dim.TLabel',
                        background=self.BG,
                        foreground=self.FG_DIM,
                        font=('Consolas', 9))
        style.configure('Head.TLabel',
                        background=self.BG,
                        foreground=self.ACCENT,
                        font=('Consolas', 12, 'bold'))

        style.configure('TButton',
                        background=self.BG_HEADER,
                        foreground=self.FG,
                        borderwidth=0,
                        padding=[10, 5],
                        font=('Consolas', 10))
        style.map('TButton',
                  background=[('active', '#3a3a44'),
                              ('pressed', '#0f4c63')],
                  foreground=[('active', self.ACCENT)])

        style.configure('TEntry',
                        fieldbackground=self.BG_PANEL,
                        foreground=self.FG,
                        insertcolor=self.ACCENT,
                        borderwidth=0)

        style.configure('TPanedwindow', background=self.BG)
        style.configure('Sash',
                        sashthickness=4,
                        gripcount=0,
                        background=self.BG_HEADER,
                        bordercolor=self.BG_HEADER)

    # ----- 布局 -----
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        top.pack(fill='x')

        ttk.Label(top, text="DiskForge", style='Head.TLabel').pack(side='left')
        ttk.Label(top, text="  裸设备直读 · 只读模式",
                  style='Dim.TLabel').pack(side='left', padx=(10, 0))

        ttk.Button(top, text="重新扫描",
                   command=self.refresh).pack(side='right')
        self.perm_label = ttk.Label(top, text="", style='Dim.TLabel')
        self.perm_label.pack(side='right', padx=12)

        paned = ttk.PanedWindow(self.root, orient='horizontal')
        paned.pack(fill='both', expand=True, padx=12, pady=(0, 6))

        # 左侧设备树
        left = ttk.Frame(paned)
        paned.add(left, weight=1)

        ttk.Label(left, text="设备 / 分区",
                  style='Dim.TLabel').pack(anchor='w', pady=(0, 4))

        tree_wrap = tk.Frame(left, bg=self.BG_HEADER)
        tree_wrap.pack(fill='both', expand=True)

        self.tree = ttk.Treeview(tree_wrap, show='tree', selectmode='browse')
        vsb = ttk.Scrollbar(tree_wrap, orient='vertical',
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.tree.pack(side='left', fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.on_select)

        # 右侧选项卡
        right = ttk.Frame(paned)
        paned.add(right, weight=3)

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill='both', expand=True)

        self._build_overview_tab()
        self._build_partition_tab()
        self._build_hex_tab()

        self.status_var = tk.StringVar(value="就绪")
        status = tk.Label(self.root, textvariable=self.status_var,
                          bg=self.BG_HEADER, fg=self.FG_DIM, anchor='w',
                          font=('Consolas', 9), padx=12, pady=4)
        status.pack(fill='x', side='bottom')

    def _build_overview_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=' 概览 ')

        self.overview = tk.Text(
            frame, bg=self.BG_PANEL, fg=self.FG, bd=0,
            font=('Consolas', 10), wrap='none',
            insertbackground=self.ACCENT, padx=14, pady=10,
            selectbackground='#0f4c63',
        )
        vsb = ttk.Scrollbar(frame, orient='vertical',
                            command=self.overview.yview)
        self.overview.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.overview.pack(fill='both', expand=True)

        self.overview.tag_configure('key', foreground=self.ACCENT)
        self.overview.tag_configure('value', foreground=self.FG)
        self.overview.tag_configure('warn', foreground=self.WARN)
        self.overview.tag_configure('ok', foreground=self.OK)
        self.overview.tag_configure('section', foreground=self.ACCENT,
                                    font=('Consolas', 10, 'bold'))
        self.overview.configure(state='disabled')

    def _build_partition_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=' 分区表 ')

        columns = ('idx', 'boot', 'type', 'start', 'end',
                   'sectors', 'size', 'name')
        headings = ('#', '标志', '类型', '起始 LBA', '结束 LBA',
                    '扇区数', '大小', '名称')
        widths = (45, 55, 210, 100, 100, 100, 95, 180)

        self.partition_tree = ttk.Treeview(
            frame, columns=columns, show='headings', selectmode='browse')
        for col, head, width in zip(columns, headings, widths):
            self.partition_tree.heading(col, text=head)
            self.partition_tree.column(
                col, width=width, anchor='w',
                stretch=(col in ('type', 'name')))
        self.partition_tree.column('idx', anchor='center')
        self.partition_tree.column('boot', anchor='center')

        vsb = ttk.Scrollbar(frame, orient='vertical',
                            command=self.partition_tree.yview)
        self.partition_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.partition_tree.pack(fill='both', expand=True)

    def _build_hex_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=' 扇区查看 ')

        bar = ttk.Frame(frame, padding=(10, 8))
        bar.pack(fill='x')

        ttk.Label(bar, text="跳转 (LBA 或 0x偏移):",
                  style='Dim.TLabel').pack(side='left')

        self.jump_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.jump_var,
                          width=20, font=('Consolas', 10))
        entry.pack(side='left', padx=8)
        entry.bind('<Return>', lambda e: self.jump())

        ttk.Button(bar, text="跳转", command=self.jump).pack(side='left')
        ttk.Button(bar, text="上一屏",
                   command=lambda: self.scroll(-self.view_length)
                   ).pack(side='left', padx=(16, 4))
        ttk.Button(bar, text="下一屏",
                   command=lambda: self.scroll(self.view_length)
                   ).pack(side='left')

        self.hex_label = ttk.Label(bar, text="", style='Dim.TLabel')
        self.hex_label.pack(side='right')

        wrap = tk.Frame(frame, bg=self.BG_PANEL)
        wrap.pack(fill='both', expand=True, padx=10, pady=(0, 10))

        self.hex_view = tk.Text(
            wrap, bg=self.BG_PANEL, fg=self.FG, bd=0,
            font=('Consolas', 10), wrap='none',
            insertbackground=self.ACCENT, padx=12, pady=8,
        )
        vsb = ttk.Scrollbar(wrap, orient='vertical',
                            command=self.hex_view.yview)
        hsb = ttk.Scrollbar(wrap, orient='horizontal',
                            command=self.hex_view.xview)
        self.hex_view.configure(yscrollcommand=vsb.set,
                                xscrollcommand=hsb.set)
        vsb.pack(side='right', fill='y')
        hsb.pack(side='bottom', fill='x')
        self.hex_view.pack(side='left', fill='both', expand=True)
        self.hex_view.configure(state='disabled')

    # ----- 扫描 -----
    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.tree_map.clear()
        self.scan_cache.clear()
        self.cur_disk = None
        self.cur_part = None

        try:
            self.disks = enum_disks()
        except Exception as e:
            messagebox.showerror("枚举失败", f"枚举磁盘时出错:\n{e}")
            self.disks = []

        if not self.disks:
            self.perm_label.configure(text="没有权限 / 没找到设备",
                                      foreground=self.WARN)
            self.tree.insert('', 'end', text="  未检测到物理磁盘")
            self._set_overview([
                ("提示",
                 "没有扫到任何物理磁盘。\n\n"
                 "Windows 请用管理员身份运行；Linux 请用 sudo 运行。",
                 'warn'),
            ])
            self.status_var.set("扫描完成：0 个磁盘")
            return

        self.perm_label.configure(text="已获取裸设备访问权限",
                                  foreground=self.OK)

        total_parts = 0
        for disk in self.disks:
            scan = scan_disk(disk)
            self.scan_cache[disk['index']] = scan

            scheme = scan['scheme']
            n_parts = len(scan['partitions'])
            total_parts += n_parts

            if scheme == 'RAW':
                tag = "[RAW]"
            else:
                tag = f"[{scheme}] {n_parts} 个分区"

            node = self.tree.insert(
                '', 'end', open=True,
                text=f"物理磁盘 {disk['index']}  "
                     f"{fmt_size(disk['size'])}  {tag}",
            )
            self.tree_map[node] = {'kind': 'disk',
                                   'disk': disk, 'scan': scan}

            for part in scan['partitions']:
                if scheme == 'GPT':
                    label_name = part.get('name') or part['type_name']
                else:
                    flag = "*" if part.get('bootable') else " "
                    label_name = f"{flag} {part['type_name']}"
                child = self.tree.insert(
                    node, 'end',
                    text=f"[{part['slot']}] {label_name}  "
                         f"{fmt_size(part['size'])}",
                )
                self.tree_map[child] = {
                    'kind': 'part', 'disk': disk,
                    'scan': scan, 'part': part,
                }

        self.status_var.set(
            f"扫描完成：{len(self.disks)} 个物理磁盘，{total_parts} 个分区")

    # ----- 选中 -----
    def on_select(self, _event):
        selection = self.tree.selection()
        if not selection:
            return
        info = self.tree_map.get(selection[0])
        if not info:
            return

        if info['kind'] == 'disk':
            self.cur_disk = info['disk']
            self.cur_part = None
            self.view_offset = 0
            self._show_disk_overview(info['disk'], info['scan'])
            self._fill_partition_table(info['disk'], info['scan'])
            self.status_var.set(f"已选中 物理磁盘 {info['disk']['index']}")
        else:
            self.cur_disk = info['disk']
            self.cur_part = info['part']
            self.view_offset = info['part']['lba_start'] * SECTOR_SIZE
            self._show_partition_overview(info['disk'], info['scan'],
                                          info['part'])
            self._fill_partition_table(info['disk'], info['scan'],
                                       highlight=info['part']['slot'])
            self.status_var.set(
                f"已选中 分区 [{info['part']['slot']}]  "
                f"起始 LBA {info['part']['lba_start']}")

        self._refresh_hex()

    # ----- 概览渲染 -----
    def _set_overview(self, rows):
        self.overview.configure(state='normal')
        self.overview.delete('1.0', 'end')
        for key, value, tag in rows:
            if key:
                self.overview.insert('end', f"{key:<24}", 'key')
            self.overview.insert('end', f"{value}\n", tag)
        self.overview.configure(state='disabled')

    def _show_disk_overview(self, disk, scan):
        rows = [
            ("设备路径", disk['path'], 'value'),
            ("型号", disk.get('model', 'N/A'), 'value'),
            ("总容量", f"{fmt_size(disk['size'])}  "
                       f"({disk['size']:,} 字节)", 'value'),
            ("总扇区数", f"{disk['size'] // SECTOR_SIZE:,} x 512B", 'value'),
            ("分区方案", scan['scheme'],
             'ok' if scan['scheme'] != 'RAW' else 'warn'),
        ]

        if scan['scheme'] == 'GPT' and scan['gpt_header']:
            h = scan['gpt_header']
            rows += [
                ("", "", 'value'),
                ("GPT 头部", "", 'section'),
                ("修订版本", f"0x{h['revision']:08X}", 'value'),
                ("头部大小", f"{h['header_size']} 字节", 'value'),
                ("头部 CRC32", f"0x{h['crc32']:08X}", 'value'),
                ("当前 LBA", str(h['current_lba']), 'value'),
                ("备份 LBA", str(h['backup_lba']), 'value'),
                ("首个可用 LBA", str(h['first_usable']), 'value'),
                ("末个可用 LBA", str(h['last_usable']), 'value'),
                ("磁盘 GUID", h['disk_guid'], 'value'),
                ("分区表 LBA", str(h['entry_lba']), 'value'),
                ("条目数", f"{h['num_entries']} x {h['entry_size']} 字节",
                 'value'),
            ]
        elif scan['scheme'] == 'MBR':
            mbr = scan['mbr_raw']
            signature = mbr[510:512] if len(mbr) >= 512 else b''
            disk_sig = (struct.unpack('<I', mbr[440:444])[0]
                        if len(mbr) >= 444 else 0)
            rows += [
                ("", "", 'value'),
                ("MBR 头部", "", 'section'),
                ("结束标志",
                 f"0x{signature.hex().upper()}" if signature else "N/A",
                 'value'),
                ("磁盘签名", f"0x{disk_sig:08X}", 'value'),
            ]

        rows += [
            ("", "", 'value'),
            ("分区数量", str(len(scan['partitions'])), 'value'),
        ]
        self._set_overview(rows)

    def _show_partition_overview(self, disk, scan, part):
        scheme = "GPT" if scan['scheme'] == 'GPT' else "MBR"
        rows = [
            ("所属磁盘", f"物理磁盘 {disk['index']}  ({disk['path']})",
             'value'),
            ("分区表类型", scheme, 'value'),
            ("槽位", str(part['slot']), 'value'),
        ]

        if scan['scheme'] == 'GPT':
            rows += [
                ("分区名称", part.get('name') or '(空)', 'value'),
                ("类型 GUID", part['type_guid'], 'value'),
                ("类型", part['type_name'], 'ok'),
                ("唯一 GUID", part['uniq_guid'], 'value'),
                ("属性", f"0x{part['attrs']:016X}", 'value'),
            ]
        else:
            rows += [
                ("分区类型",
                 f"0x{part['type']:02X}  ({part['type_name']})", 'ok'),
                ("启动标志",
                 "活动 / 可启动" if part.get('bootable') else "无", 'value'),
            ]

        rows += [
            ("", "", 'value'),
            ("起始 LBA", f"{part['lba_start']:,}", 'value'),
            ("结束 LBA", f"{part['lba_end']:,}", 'value'),
            ("扇区总数", f"{part['sectors']:,}", 'value'),
            ("分区大小", fmt_size(part['size']), 'value'),
            ("", "", 'value'),
            ("起始字节偏移",
             f"0x{part['lba_start'] * SECTOR_SIZE:X}  "
             f"({part['lba_start'] * SECTOR_SIZE:,})", 'value'),
        ]
        self._set_overview(rows)

    def _fill_partition_table(self, disk, scan, highlight=None):
        self.partition_tree.delete(*self.partition_tree.get_children())
        scheme = scan['scheme']

        for part in scan['partitions']:
            if scheme == 'GPT':
                boot = 'UEFI' if 'EFI' in part['type_name'] else ''
                name = part.get('name') or ''
                ptype = part['type_name']
            else:
                boot = '*' if part.get('bootable') else ''
                name = ''
                ptype = f"0x{part['type']:02X} {part['type_name']}"

            item = self.partition_tree.insert('', 'end', values=(
                part['slot'], boot, ptype,
                f"{part['lba_start']:,}", f"{part['lba_end']:,}",
                f"{part['sectors']:,}", fmt_size(part['size']), name,
            ))
            if highlight and part['slot'] == highlight:
                self.partition_tree.selection_set(item)
                self.partition_tree.see(item)

    # ----- 十六进制查看 -----
    def _refresh_hex(self):
        self.hex_view.configure(state='normal')
        self.hex_view.delete('1.0', 'end')

        if not self.cur_disk:
            self.hex_view.insert(
                'end',
                "\n  未选择设备。在左侧点一下磁盘或分区。\n")
            self.hex_view.configure(state='disabled')
            self.hex_label.configure(text="")
            return

        try:
            raw = RawDisk(self.cur_disk['path']).open()
            data = raw.read(self.view_offset, self.view_length)
            raw.close()
        except OSError as e:
            self.hex_view.insert('end', f"\n  读取失败: {e}\n")
            self.hex_view.configure(state='disabled')
            return

        header = (
            f"  {self.cur_disk['path']}  "
            f"@ 0x{self.view_offset:012X}  "
            f"(LBA {self.view_offset // SECTOR_SIZE})\n\n"
        )
        self.hex_view.insert('end', header)
        self.hex_view.insert('end', render_hex(data, self.view_offset))
        self.hex_view.configure(state='disabled')

        self.hex_label.configure(
            text=f"偏移 0x{self.view_offset:012X}   "
                 f"LBA {self.view_offset // SECTOR_SIZE}   "
                 f"{len(data)} 字节")

    def scroll(self, delta):
        if not self.cur_disk:
            return
        new_offset = self.view_offset + delta
        new_offset = max(0, new_offset)
        max_offset = max(0, self.cur_disk['size'] - self.view_length)
        new_offset = min(new_offset, max_offset)
        self.view_offset = new_offset & ~(SECTOR_SIZE - 1)
        self._refresh_hex()

    def jump(self):
        if not self.cur_disk:
            messagebox.showinfo("提示", "先选一个磁盘或分区。")
            return

        text = self.jump_var.get().strip()
        if not text:
            return

        try:
            lower = text.lower()
            if lower.startswith('0x'):
                offset = int(text, 16)
            elif lower.startswith('lba'):
                offset = int(text[3:], 10) * SECTOR_SIZE
            else:
                n = int(text, 10)
                # 小数字当 LBA，大数字当字节偏移
                offset = n * SECTOR_SIZE if n < 1_000_000_000 else n
        except ValueError:
            messagebox.showerror(
                "输入错误",
                f"看不懂这个：{text}\n"
                "支持的格式： 2048 (LBA)  /  0x1F4000  /  lba:2048")
            return

        max_offset = max(0, self.cur_disk['size'] - self.view_length)
        self.view_offset = min(offset, max_offset) & ~(SECTOR_SIZE - 1)
        self._refresh_hex()


# ---------------------------------------------------------------
# 入口
# ---------------------------------------------------------------
def is_admin():
    if IS_WIN:
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    return os.geteuid() == 0


def main():
    root = tk.Tk()
    app = DiskForgeApp(root)

    if not is_admin():
        message = (
            "权限不足。\n\n"
            "本工具需要直接访问裸设备：\n"
            "  Windows: 用管理员身份打开 cmd，再运行脚本\n"
            "  Linux  : sudo python3 diskforge.py\n\n"
            "现在也能打开，但很可能看不到任何磁盘。"
        )
        root.after(300, lambda: messagebox.showwarning("权限警告", message))

    root.mainloop()


if __name__ == '__main__':
    main()