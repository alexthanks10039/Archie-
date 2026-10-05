from __future__ import annotations

import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


APP_DIR = Path(__file__).resolve().parent
SCRIPT = APP_DIR / "browser_backup.py"
DEFAULT_START = 1590
DEFAULT_END = 2362


class ArchieGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Archie · iFreedom Backup")
        self.geometry("980x700")
        self.minsize(820, 580)

        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.done_count = 0
        self.total_count = 0

        self.start_var = tk.StringVar(value=str(DEFAULT_START))
        self.end_var = tk.StringVar(value=str(DEFAULT_END))
        self.delay_var = tk.StringVar(value="1")
        self.timeout_var = tk.StringVar(value="25")
        self.status_var = tk.StringVar(value="Готово")
        self.progress_var = tk.DoubleVar(value=0)

        self._configure_style()
        self._build_ui()
        self.after(100, self._drain_output)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10))
        style.configure("Card.TLabelframe", padding=12)
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="Archie", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Резервное скачивание глав iFreedom через авторизованный браузер",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        settings = ttk.LabelFrame(root, text="1. Параметры", style="Card.TLabelframe")
        settings.pack(fill="x", pady=(0, 10))

        row1 = ttk.Frame(settings)
        row1.pack(fill="x")
        ttk.Label(row1, text="С главы").pack(side="left")
        ttk.Entry(row1, textvariable=self.start_var, width=10).pack(side="left", padx=(8, 24))
        ttk.Label(row1, text="По главу").pack(side="left")
        ttk.Entry(row1, textvariable=self.end_var, width=10).pack(side="left", padx=(8, 24))
        ttk.Label(row1, text="Задержка, сек").pack(side="left")
        ttk.Entry(row1, textvariable=self.delay_var, width=8).pack(side="left", padx=(8, 24))
        ttk.Label(row1, text="Таймаут, сек").pack(side="left")
        ttk.Entry(row1, textvariable=self.timeout_var, width=8).pack(side="left", padx=(8, 0))

        row2 = ttk.Frame(settings)
        row2.pack(fill="x", pady=(12, 0))
        ttk.Label(
            row2,
            text=f"Архив: {APP_DIR / 'backup'}",
        ).pack(side="left")
        ttk.Button(row2, text="Открыть папку", command=self._open_backup).pack(side="right")

        actions = ttk.LabelFrame(root, text="2. Управление", style="Card.TLabelframe")
        actions.pack(fill="x", pady=(0, 10))

        self.start_button = ttk.Button(
            actions, text="▶ Запустить скачивание", style="Accent.TButton", command=self.start_download
        )
        self.start_button.pack(side="left", padx=(0, 8))

        self.login_button = ttk.Button(
            actions, text="✓ Я вошёл в iFreedom", command=self.confirm_login, state="disabled"
        )
        self.login_button.pack(side="left", padx=8)

        self.stop_button = ttk.Button(
            actions, text="■ Остановить", command=self.stop_download, state="disabled"
        )
        self.stop_button.pack(side="left", padx=8)

        ttk.Button(actions, text="Тест 1 главы", command=self.test_one).pack(side="right")

        progress_frame = ttk.LabelFrame(root, text="3. Прогресс", style="Card.TLabelframe")
        progress_frame.pack(fill="x", pady=(0, 10))
        status_row = ttk.Frame(progress_frame)
        status_row.pack(fill="x")
        ttk.Label(status_row, textvariable=self.status_var).pack(side="left")
        self.counter_label = ttk.Label(status_row, text="0 / 0")
        self.counter_label.pack(side="right")
        ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
        ).pack(fill="x", pady=(9, 0))

        log_frame = ttk.LabelFrame(root, text="Журнал", style="Card.TLabelframe")
        log_frame.pack(fill="both", expand=True)

        text_wrap = ttk.Frame(log_frame)
        text_wrap.pack(fill="both", expand=True)
        self.log = tk.Text(
            text_wrap,
            wrap="word",
            font=("Cascadia Mono", 9),
            state="disabled",
            background="#111827",
            foreground="#e5e7eb",
            insertbackground="#e5e7eb",
            relief="flat",
            padx=10,
            pady=10,
        )
        scrollbar = ttk.Scrollbar(text_wrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        footer = ttk.Frame(root)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(
            footer,
            text="Вход выполняется вручную в открывшемся Chromium. Archie не обходит CAPTCHA или ограничения доступа.",
        ).pack(side="left")

    def _write_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "
")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_inputs_state(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in self.winfo_children():
            self._set_children_state(widget, state)

    def _set_children_state(self, parent: tk.Misc, state: str) -> None:
        for child in parent.winfo_children():
            if child in {self.start_button, self.login_button, self.stop_button}:
                continue
            try:
                if isinstance(child, (ttk.Entry, ttk.Button)):
                    child.configure(state=state)
            except tk.TclError:
                pass
            self._set_children_state(child, state)

    def _parse_int(self, var: tk.StringVar, label: str) -> int:
        try:
            value = int(var.get().strip())
        except ValueError as exc:
            raise ValueError(f"{label}: укажи целое число") from exc
        return value

    def _parse_float(self, var: tk.StringVar, label: str) -> float:
        try:
            value = float(var.get().strip().replace(",", "."))
        except ValueError as exc:
            raise ValueError(f"{label}: укажи число") from exc
        return value

    def _validate(self) -> tuple[int, int, float, int]:
        start = self._parse_int(self.start_var, "С главы")
        end = self._parse_int(self.end_var, "По главу")
        delay = self._parse_float(self.delay_var, "Задержка")
        timeout = self._parse_int(self.timeout_var, "Таймаут")
        if start < 1 or end < start:
            raise ValueError("Диапазон глав должен быть: 1 <= начало <= конец")
        if delay < 0:
            raise ValueError("Задержка не может быть отрицательной")
        if timeout < 5:
            raise ValueError("Таймаут должен быть не меньше 5 секунд")
        return start, end, delay, timeout

    def test_one(self) -> None:
        self.start_var.set(self.start_var.get().strip() or str(DEFAULT_START))
        self.end_var.set(self.start_var.get())
        self.start_download()

    def start_download(self) -> None:
        if self.process is not None:
            return
        if not SCRIPT.exists():
            messagebox.showerror(
                "Archie",
                f"Не найден файл:\n{SCRIPT}\n\nСначала обнови Archie через git pull.",
            )
            return

        try:
            start, end, delay, timeout = self._validate()
        except ValueError as exc:
            messagebox.showerror("Проверь параметры", str(exc))
            return

        self.total_count = end - start + 1
        self.done_count = 0
        self.progress_var.set(0)
        self.counter_label.configure(text=f"0 / {self.total_count}")
        self.status_var.set("Запускаю...")
        self._write_log("")
        self._write_log(f"=== Archie: главы {start}–{end} ===")

        cmd = [
            sys.executable,
            "-u",
            str(SCRIPT),
            "--start",
            str(start),
            "--end",
            str(end),
            "--delay",
            str(delay),
            "--timeout",
            str(timeout),
        ]

        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=str(APP_DIR),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self.process = None
            messagebox.showerror("Не удалось запустить", str(exc))
            return

        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.login_button.configure(state="normal")
        self.status_var.set("Открылся браузер. Войди в iFreedom.")
        threading.Thread(target=self._read_process_output, daemon=True).start()

    def _read_process_output(self) -> None:
        assert self.process is not None
        process = self.process
        if process.stdout is not None:
            for line in process.stdout:
                self.output_queue.put(("log", line.rstrip()))
        code = process.wait()
        self.output_queue.put(("exit", str(code)))

    def confirm_login(self) -> None:
        if not self.process or not self.process.stdin:
            return
        try:
            self.process.stdin.write("\n")
            self.process.stdin.flush()
            self.login_button.configure(state="disabled")
            self.status_var.set("Скачиваю главы...")
            self._write_log("✓ Сигнал "Я вошёл" отправлен.")
        except (BrokenPipeError, OSError) as exc:
            self._write_log(f"[ERR] Не удалось продолжить: {exc}")

    def stop_download(self) -> None:
        if not self.process:
            return
        try:
            self.process.terminate()
        except OSError:
            pass
        self.status_var.set("Останавливаю...")
        self._write_log("■ Остановка по запросу пользователя.")

    def _drain_output(self) -> None:
        try:
            while True:
                kind, payload = self.output_queue.get_nowait()
                if kind == "log":
                    self._write_log(payload)
                    self._update_progress_from_line(payload)
                elif kind == "exit":
                    self._finish_process(int(payload))
        except queue.Empty:
            pass
        self.after(100, self._drain_output)

    def _update_progress_from_line(self, line: str) -> None:
        if re.search(r"\[(OK\s*|ERR|MISS)", line):
            self.done_count += 1
            self.counter_label.configure(text=f"{self.done_count} / {self.total_count}")
            if self.total_count:
                self.progress_var.set(min(100, self.done_count * 100 / self.total_count))
        if "[OK" in line:
            self.status_var.set("Глава сохранена")
        elif "[ERR" in line:
            self.status_var.set("Есть ошибка. Смотри журнал.")
        elif "Готово." in line:
            self.status_var.set("Загрузка завершена")

    def _finish_process(self, code: int) -> None:
        self.process = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.login_button.configure(state="disabled")
        if code == 0:
            self.status_var.set("Готово")
            messagebox.showinfo("Archie", "Скачивание завершено без ошибок.")
        elif code == 2:
            self.status_var.set("Остановлено / нужна авторизация")
            messagebox.showwarning(
                "Archie",
                "Загрузка не продолжилась. Проверь вход в iFreedom и повтори запуск.",
            )
        else:
            self.status_var.set("Завершено с ошибками")
            messagebox.showwarning(
                "Archie",
                "Загрузка завершилась с ошибками. Открой manifest.json и журнал.",
            )

    def _open_backup(self) -> None:
        backup_dir = APP_DIR / "backup"
        backup_dir.mkdir(exist_ok=True)
        try:
            subprocess.Popen(["explorer", str(backup_dir)])
        except OSError as exc:
            messagebox.showerror("Ошибка", str(exc))

    def _on_close(self) -> None:
        if self.process is not None:
            if not messagebox.askyesno("Archie", "Остановить текущее скачивание и закрыть программу?"):
                return
            try:
                self.process.terminate()
            except OSError:
                pass
        self.destroy()


if __name__ == "__main__":
    app = ArchieGui()
    app.mainloop()
