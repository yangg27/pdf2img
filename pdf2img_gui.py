# -*- coding: utf-8 -*-
"""
PDF 转图片工具（离线版）
------------------------------------------------
功能：
  1. 选择 PDF 文件
  2. 输出模式：单页（每页一张图） / 合并长图（所有页纵向拼接成一张图）
  3. 清晰度：150 / 200 / 300 / 400 / 600 DPI
  4. 图片格式：PNG（无损）/ JPG（体积小）
  5. 输出到 PDF 同目录下的 "<文件名>_图片" 文件夹

特点：
  - 完全离线，不联网
  - 扫描件（整页一张图）自动按原始像素无损提取，不做二次重采样
  - 转换在后台线程执行，界面不卡死
  - 支持命令行模式（--cli），便于批处理与自动化

用法（命令行）：
  python pdf2img_gui.py --cli -i 文件.pdf -o 输出目录 --mode single --dpi 300 --format png --json
  python pdf2img_gui.py --cli -i 文件.pdf --mode merged --dpi 200
"""

import argparse
import io
import json
import os
import queue
import sys
import threading
import traceback

# ----------------------------- 依赖 -----------------------------
try:
    import pymupdf  # PyMuPDF
except ImportError:  # 兼容旧包名
    import fitz as pymupdf

from PIL import Image

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    HAS_TK = True
except ImportError:  # 无 GUI 环境下仅命令行可用
    tk = ttk = filedialog = messagebox = None
    HAS_TK = False

# 解除 PIL 巨图尺寸限制（长图 / 600DPI 会超过默认阈值）
Image.MAX_IMAGE_PIXELS = None

APP_NAME = "PDF 转图片工具"
APP_VERSION = "1.0"

DPI_CHOICES = [
    ("草稿（150 DPI）", 150),
    ("标准（200 DPI）", 200),
    ("高清（300 DPI）", 300),
    ("超清（400 DPI）", 400),
    ("印刷级（600 DPI）", 600),
]
DEFAULT_DPI = 300

FORMAT_CHOICES = [("PNG（无损，推荐）", "png"), ("JPG（体积小）", "jpg")]
DEFAULT_FORMAT = "png"

MODE_SINGLE = "single"
MODE_MERGED = "merged"

# 合并长图总像素上限，超过则提示（约 4 亿像素 ≈ 1.2GB RGB）
MAX_TOTAL_PIXELS = 400_000_000

JPG_QUALITY = 95


# ============================ 核心转换逻辑 ============================
def _single_full_image_xref(page):
    """若整页就是一张图（扫描件），返回该图的 xref，否则返回 None。"""
    try:
        imgs = page.get_images(full=True)
        if len(imgs) != 1:
            return None
        xref = imgs[0][0]
        rects = page.get_image_rects(xref)
        if not rects:
            return None
        r = page.rect
        page_area = r.width * r.height
        if page_area <= 0:
            return None
        ir = rects[0]
        if (abs(ir.width) * abs(ir.height)) / page_area >= 0.95:
            return xref
    except Exception:
        return None
    return None


def _render_page_image(page, dpi):
    """按指定 DPI 渲染整页，返回 PIL.Image（RGB）。"""
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def _save_image(img, path, fmt, jpeg_bytes=None):
    """保存图片。jpeg_bytes 提供时表示扫描件原始 JPEG 字节，可无损直写。"""
    if fmt == "jpg":
        if jpeg_bytes is not None:
            with open(path, "wb") as f:
                f.write(jpeg_bytes)
            return
        img.save(path, "JPEG", quality=JPG_QUALITY, optimize=True, progressive=True)
    else:
        img.save(path, "PNG", optimize=True)


def estimate_total_pixels(doc, dpi):
    """预估合并长图的总像素数（渲染前计算，零成本）。"""
    total = 0
    for page in doc:
        r = page.rect
        w = max(1, round(r.width / 72.0 * dpi))
        h = max(1, round(r.height / 72.0 * dpi))
        total += w * h
    return total


def pdf_to_images(pdf_path, out_dir, mode=MODE_SINGLE, dpi=DEFAULT_DPI,
                  fmt=DEFAULT_FORMAT, progress=None, confirm_large=None):
    """
    执行转换。

    progress:   callable(done:int, total:int, message:str) 进度回调
    confirm_large: callable(total_pixels:int) -> bool 超大图确认回调（返回 False 则中止）
    返回：dict
    """
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError("找不到 PDF 文件：%s" % pdf_path)

    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    ext = "png" if fmt == "png" else "jpg"
    results = []
    lossless_count = 0

    doc = pymupdf.open(pdf_path)
    try:
        total_pages = doc.page_count
        if total_pages <= 0:
            raise ValueError("该 PDF 没有任何页面。")

        # ---------------- 合并长图 ----------------
        if mode == MODE_MERGED:
            est = estimate_total_pixels(doc, dpi)
            if est > MAX_TOTAL_PIXELS:
                if confirm_large is not None and not confirm_large(est):
                    return {"ok": False, "aborted": True, "reason": "用户取消了超大图转换"}

            if progress:
                progress(0, total_pages, "正在渲染页面…")

            # 单页若是整页扫描图，用原始像素（避免重采样损失）
            page_imgs = []
            widths = []
            for i, page in enumerate(doc):
                xref = _single_full_image_xref(page)
                if xref is not None:
                    info = doc.extract_image(xref)
                    img = Image.open(io.BytesIO(info["image"])).convert("RGB")
                else:
                    img = _render_page_image(page, dpi)
                page_imgs.append(img)
                widths.append(img.width)
                if progress:
                    progress(i + 1, total_pages, "已渲染 %d/%d 页" % (i + 1, total_pages))

            canvas_w = max(widths)
            canvas_h = sum(img.height for img in page_imgs)
            if progress:
                progress(total_pages, total_pages, "正在拼接长图（%d×%d）…" % (canvas_w, canvas_h))
            canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
            y = 0
            for img in page_imgs:
                canvas.paste(img, ((canvas_w - img.width) // 2, y))
                y += img.height
                img.close()

            out_path = os.path.join(out_dir, "%s_长图.%s" % (base, ext))
            _save_image(canvas, out_path, fmt)
            size = canvas.size
            canvas.close()
            results.append(out_path)
            return {
                "ok": True, "mode": mode, "dpi": dpi, "format": fmt,
                "output_dir": out_dir, "files": results,
                "pages": total_pages, "merged_size": list(size),
                "lossless": False, "ext": ext,
            }

        # ---------------- 单页模式 ----------------
        for i, page in enumerate(doc):
            if progress:
                progress(i, total_pages, "正在处理第 %d/%d 页…" % (i + 1, total_pages))
            out_path = os.path.join(out_dir, "%s_p%d.%s" % (base, i + 1, ext))

            xref = _single_full_image_xref(page)
            if xref is not None:
                info = doc.extract_image(xref)
                img = Image.open(io.BytesIO(info["image"])).convert("RGB")
                jb = info["image"] if info.get("ext") in ("jpg", "jpeg") else None
                _save_image(img, out_path, fmt, jpeg_bytes=jb)
                lossless_count += 1
                img.close()
            else:
                img = _render_page_image(page, dpi)
                _save_image(img, out_path, fmt)
                img.close()
            results.append(out_path)

        if progress:
            progress(total_pages, total_pages, "完成")
        return {
            "ok": True, "mode": mode, "dpi": dpi, "format": fmt,
            "output_dir": out_dir, "files": results, "pages": total_pages,
            "lossless": lossless_count > 0, "lossless_pages": lossless_count,
            "ext": ext,
        }
    finally:
        doc.close()


# ============================ GUI ============================
class Pdf2ImgApp:
    def __init__(self, root):
        self.root = root
        self.pdf_path = tk.StringVar()
        self.out_dir = tk.StringVar()
        self.mode = tk.StringVar(value=MODE_SINGLE)
        self.dpi_label = tk.StringVar(value="高清（300 DPI）")
        self.fmt_label = tk.StringVar(value="PNG（无损，推荐）")
        self.status = tk.StringVar(value="请选择要转换的 PDF 文件")
        self._q = queue.Queue()
        self._busy = False
        self._last_out = None

        self._build_ui()
        self.root.after(120, self._drain_queue)

    # ---------- 界面 ----------
    def _build_ui(self):
        self.root.title("%s  v%s" % (APP_NAME, APP_VERSION))
        self.root.geometry("640x430")
        self.root.minsize(600, 410)
        try:
            self.root.iconbitmap(default=self._icon_path())
        except Exception:
            pass

        style = ttk.Style()
        for theme in ("vista", "clam", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        pad = {"padx": 10, "pady": 6}
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        # ---- 选择文件 ----
        box1 = ttk.LabelFrame(main, text=" 1. 选择 PDF 文件 ", padding=10)
        box1.pack(fill="x")
        row = ttk.Frame(box1)
        row.pack(fill="x")
        self.entry_pdf = ttk.Entry(row, textvariable=self.pdf_path)
        self.entry_pdf.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(row, text="浏览…", width=10, command=self.pick_pdf).pack(side="left")

        # ---- 设置 ----
        box2 = ttk.LabelFrame(main, text=" 2. 转换设置 ", padding=10)
        box2.pack(fill="x", pady=(10, 0))

        r1 = ttk.Frame(box2)
        r1.pack(fill="x", pady=3)
        ttk.Label(r1, text="输出模式：", width=10).pack(side="left")
        ttk.Radiobutton(r1, text="单页（每页一张图）", value=MODE_SINGLE,
                        variable=self.mode, command=self._on_mode_change).pack(side="left")
        ttk.Radiobutton(r1, text="合并长图（所有页拼成一张）", value=MODE_MERGED,
                        variable=self.mode, command=self._on_mode_change).pack(side="left", padx=(14, 0))

        r2 = ttk.Frame(box2)
        r2.pack(fill="x", pady=3)
        ttk.Label(r2, text="清晰度：", width=10).pack(side="left")
        self.cmb_dpi = ttk.Combobox(r2, textvariable=self.dpi_label, state="readonly",
                                    width=20, values=[c[0] for c in DPI_CHOICES])
        self.cmb_dpi.pack(side="left")
        ttk.Label(r2, text="图片格式：").pack(side="left", padx=(18, 0))
        self.cmb_fmt = ttk.Combobox(r2, textvariable=self.fmt_label, state="readonly",
                                    width=18, values=[c[0] for c in FORMAT_CHOICES])
        self.cmb_fmt.pack(side="left")

        r3 = ttk.Frame(box2)
        r3.pack(fill="x", pady=3)
        ttk.Label(r3, text="输出位置：", width=10).pack(side="left")
        ttk.Entry(r3, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(r3, text="浏览…", width=10, command=self.pick_out).pack(side="left")

        self.hint = ttk.Label(box2, text="留空则自动输出到 PDF 同目录下的「文件名_图片」文件夹",
                              foreground="#6b7280", font=("", 8))
        self.hint.pack(anchor="w", padx=(78, 0))

        # ---- 执行 ----
        box3 = ttk.Frame(main)
        box3.pack(fill="x", pady=(12, 0))
        self.btn_run = ttk.Button(box3, text="开始转换", width=16, command=self.start)
        self.btn_run.pack(side="left")
        self.btn_open = ttk.Button(box3, text="打开输出文件夹", width=16,
                                   command=self.open_out, state="disabled")
        self.btn_open.pack(side="left", padx=(8, 0))

        self.bar = ttk.Progressbar(main, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(12, 4))
        ttk.Label(main, textvariable=self.status, foreground="#111827",
                  wraplength=590, justify="left").pack(anchor="w")

    def _icon_path(self):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(base, "app.ico")
        return p if os.path.exists(p) else ""

    # ---------- 事件 ----------
    def _on_mode_change(self):
        pass

    def pick_pdf(self):
        p = filedialog.askopenfilename(
            title="选择 PDF 文件",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
        if p:
            self.pdf_path.set(p)
            if not self.out_dir.get().strip():
                self.status.set("已选择：%s" % os.path.basename(p))
            self._auto_out()

    def _auto_out(self):
        p = self.pdf_path.get().strip()
        if p and not getattr(self, "_out_manual", False):
            d = os.path.join(os.path.dirname(p),
                             os.path.splitext(os.path.basename(p))[0] + "_图片")
            self.out_dir.set(d)

    def pick_out(self):
        d = filedialog.askdirectory(title="选择输出文件夹")
        if d:
            self.out_dir.set(d)
            self._out_manual = True

    def _current_dpi(self):
        for label, v in DPI_CHOICES:
            if label == self.dpi_label.get():
                return v
        return DEFAULT_DPI

    def _current_fmt(self):
        for label, v in FORMAT_CHOICES:
            if label == self.fmt_label.get():
                return v
        return DEFAULT_FORMAT

    def start(self):
        if self._busy:
            return
        pdf = self.pdf_path.get().strip().strip('"')
        if not pdf:
            messagebox.showwarning(APP_NAME, "请先选择要转换的 PDF 文件。")
            return
        if not os.path.isfile(pdf):
            messagebox.showerror(APP_NAME, "文件不存在：\n%s" % pdf)
            return
        out = self.out_dir.get().strip().strip('"')
        if not out:
            out = os.path.join(os.path.dirname(pdf),
                               os.path.splitext(os.path.basename(pdf))[0] + "_图片")
        self._last_out = out

        self._busy = True
        self.btn_run.config(state="disabled")
        self.btn_open.config(state="disabled")
        self.bar.config(value=0)
        self.status.set("准备中…")

        t = threading.Thread(target=self._worker,
                             args=(pdf, out, self.mode.get(),
                                   self._current_dpi(), self._current_fmt()),
                             daemon=True)
        t.start()

    def _worker(self, pdf, out, mode, dpi, fmt):
        def prog(done, total, msg):
            self._q.put(("prog", (done, total, msg)))

        def confirm_large(total_pixels):
            ev = threading.Event()
            holder = {}
            self._q.put(("confirm", (total_pixels, ev, holder)))
            ev.wait()
            return bool(holder.get("value", True))

        try:
            res = pdf_to_images(pdf, out, mode=mode, dpi=dpi, fmt=fmt,
                                progress=prog, confirm_large=confirm_large)
            self._q.put(("done", res))
        except Exception as e:
            self._q.put(("error", (str(e), traceback.format_exc())))

    # ---------- 队列 ----------
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "prog":
                    done, total, msg = payload
                    self.status.set(msg)
                    if total > 0:
                        self.bar.config(value=min(100, done * 100.0 / total))
                elif kind == "confirm":
                    total_pixels, ev, holder = payload
                    mp = total_pixels / 1_000_000.0
                    ok = messagebox.askyesno(
                        APP_NAME,
                        "合并长图预计约 %.0f 百万像素（需较大内存）。\n\n"
                        "建议降低清晰度或减少页数。是否仍要继续？" % mp)
                    holder["value"] = ok
                    ev.set()
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "error":
                    self._on_error(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._drain_queue)

    def _on_done(self, res):
        self._busy = False
        self.btn_run.config(state="normal")
        self.bar.config(value=100)
        if not res.get("ok"):
            self.status.set("已取消。" if res.get("aborted") else "未完成。")
            return
        n = len(res["files"])
        if res["mode"] == MODE_MERGED:
            w, h = res.get("merged_size", (0, 0))
            msg = "转换完成：合并长图 %d×%d，已输出 1 个文件。" % (w, h)
        else:
            extra = "（扫描件已按原始像素无损导出）" if res.get("lossless") else ""
            msg = "转换完成：共 %d 页，输出 %d 张图片%s。" % (res["pages"], n, extra)
        self.status.set(msg + "\n输出目录：" + res["output_dir"])
        self._last_out = res["output_dir"]
        self.btn_open.config(state="normal")
        if messagebox.askyesno(APP_NAME, msg + "\n\n是否立即打开输出文件夹？"):
            self.open_out()

    def _on_error(self, payload):
        self._busy = False
        self.btn_run.config(state="normal")
        self.bar.config(value=0)
        msg, tb = payload
        self.status.set("转换失败：" + msg)
        messagebox.showerror(APP_NAME, "转换失败：\n%s" % msg)
        try:
            log = os.path.join(os.path.expanduser("~"), "pdf2img_error.log")
            with open(log, "w", encoding="utf-8") as f:
                f.write(tb)
        except Exception:
            pass

    def open_out(self):
        d = self._last_out or self.out_dir.get().strip()
        if d and os.path.isdir(d):
            try:
                os.startfile(d)  # Windows
            except AttributeError:
                import subprocess
                subprocess.Popen(["xdg-open", d])
        else:
            messagebox.showinfo(APP_NAME, "输出文件夹还不存在。")


# ============================ 命令行模式 ============================
def _safe_write(stream, text):
    """在 pythonw / --windowed 打包环境下 stdout/stderr 可能为 None。"""
    try:
        if stream is not None:
            stream.write(text)
            stream.flush()
    except Exception:
        pass


def run_cli(argv):
    ap = argparse.ArgumentParser(prog="pdf2img", description="PDF 转图片工具（命令行模式）")
    ap.add_argument("--cli", action="store_true", help="启用命令行模式")
    ap.add_argument("-i", "--input", required=True, help="输入 PDF 路径")
    ap.add_argument("-o", "--output", default="", help="输出目录（默认 PDF 同目录下 <文件名>_图片）")
    ap.add_argument("--mode", choices=[MODE_SINGLE, MODE_MERGED], default=MODE_SINGLE,
                    help="single=单页，merged=合并长图")
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="清晰度 DPI，默认 300")
    ap.add_argument("--format", choices=["png", "jpg"], default=DEFAULT_FORMAT, help="图片格式")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = ap.parse_args(argv)

    pdf = os.path.abspath(args.input)
    out = args.output.strip() or os.path.join(
        os.path.dirname(pdf), os.path.splitext(os.path.basename(pdf))[0] + "_图片")

    def prog(done, total, msg):
        if not args.json:
            _safe_write(sys.stderr, "\r[%d/%d] %s        " % (done, total, msg))

    def confirm(total_pixels):
        # 命令行模式不做交互，默认继续
        return True

    res = pdf_to_images(pdf, out, mode=args.mode, dpi=args.dpi,
                        fmt=args.format, progress=prog, confirm_large=confirm)

    # 结果同时落盘，保证无控制台环境下也能取到结果
    try:
        with open(os.path.join(out, "_转换结果.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    if args.json:
        _safe_write(sys.stdout, json.dumps(res, ensure_ascii=False, indent=2) + "\n")
    else:
        _safe_write(sys.stderr, "\n")
        _safe_write(sys.stdout, "输出目录：%s\n" % res.get("output_dir"))
        for f in res.get("files", []):
            _safe_write(sys.stdout, "  - %s\n" % f)
    return 0 if res.get("ok") else 1


def main():
    # 命令行模式
    if "--cli" in sys.argv or "-i" in sys.argv or "--input" in sys.argv:
        sys.exit(run_cli([a for a in sys.argv[1:] if a != "--cli"]))
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        run_cli(["--help"])

    # GUI 模式
    if not HAS_TK:
        sys.stderr.write("当前环境缺少 tkinter，仅支持命令行模式。\n")
        sys.exit(2)
    root = tk.Tk()
    Pdf2ImgApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()