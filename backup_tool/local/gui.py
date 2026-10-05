from __future__ import annotations

import os
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
BACKUP_DIR = APP_DIR / "backup"
LOG_FILE = BACKUP_DIR / "archie.log"
MANIFEST_FILE = BACKUP_DIR / "manifest.json"
DEFAULT_START = 1590
DEFAULT_END = 2362


class ArchieGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Archie · iFreedom Backup")
        self.geometry("1000x760")
        self.minsize(860, 620)

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
            text="Скачивание глав iFreedom через твой обычный Google Chrome",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        settings = ttk.LabelFrame(root, text="Параметры", style="Card.TLabelframe")
        settings.pack(fill="x", pady=(0, 10))

        row = ttk.Frame(settings)
        row.pack(fill="x")
        ttk.Label(row, text="С главы").pack(side="left")
        ttk.Entry(row, textvariable=self.start_var, width=10).pack(side="left", padx=(8, 22))
        ttk.Label(row, text="По главу").pack(side="left")
        ttk.Entry(row, textvariable=self.end_var, width=10).pack(side="left", padx=(8, 22))
        ttk.Label(row, text="Задержка, сек").pack(side="left")
        ttk.Entry(row, textvariable=self.delay_var, width=7).pack(side="left", padx=(8, 22))
        ttk.Label(row, text="Таймаут, сек").pack(side="left")
        ttk.Entry(row, textvariable=self.timeout_var, width=7).pack(side="left", padx=(8, 0))

        actions = ttk.LabelFrame(root, text="Управление", style="Card.TLabelframe")
        actions.pack(fill="x", pady=(0, 10))

        self.start_button = ttk.Button(
            actions,
            text="▶ Открыть Chrome и начать",
            style="Accent.TButton",
            command=self.start_download,
        )
        self.start_button.pack(side="left", padx=(0, 8))

        self.login_button = ttk.Button(
            actions,
            text="✓ Я вошёл в iFreedom",
            command=self.confirm_login,
            state="disabled",
        )
        self.login_button.pack(side="left", padx=8)

        self.stop_button = ttk.Button(
            actions,
            text="■ Остановить",
            command=self.stop_download,
            state="disabled",
        )
        self.stop_button.pack(side="left", padx=8)

        ttk.Button(actions, text="Открыть backup", command=self._open_backup).pack(side="right", padx=(8, 0))
        ttk.Button(actions, text="Открыть manifest", command=self._open_manifest).pack(side="right", padx=(8, 0))
        ttk.Button(actions, text="Открыть лог", command=self._open_log).pack(side="right")

        progress = ttk.LabelFrame(root, text="Прогресс", style="Card.TLabelframe")
        progress.pack(fill="x", pady=(0, 10))
        status_row = ttk.Frame(progress)
        status_row.pack(fill="x")
        ttk.Label(status_row, textvariable=self.status_var).pack(side="left")
        self.counter_label = ttk.Label(status_row, text="0 / 0")
        self.counter_label.pack(side="right")
        ttk.Progressbar(
            progress,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
        ).pack(fill="x", pady=(9, 0))

        hint = ttk.LabelFrame(root, text="Авторизация", style="Card.TLabelframe")
        hint.pack(fill="x", pady=(0, 10))
        ttk.Label(
            hint,
            text=(
                "Archie запускает обычный Chrome и не создаёт отдельный профиль. "
                "Войди в iFreedom через VK вручную. "
                "После нажатия «Я вошёл в iFreedom» Archie подключится к этой сессии. "
                "При любой критической ошибке полный traceback записывается в backup\\archie.log."
            ),
            wraplength=920,
        ).pack(anchor="w")

        log_frame = ttk.LabelFrame(root, text="Журнал текущего запуска", style="Card.TLabelframe")
        log_frame.pack(fill="both", expand=True)
        wrap = ttk.Frame(log_frame)
        wrap.pack(fill="both", expand=True)

        self.log = tk.Text(
            wrap,
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
        scrollbar = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(
            root,
            text="Файлы диагностики: backup\\archie.log и backup\\browser.log. Ошибки также записываются в manifest.json.",
        ).pack(anchor="w", pady=(8, 0))

    def _write_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _parse_int(self, value: str, label: str) -> int:
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{label}: укажи целое число") from exc

    def _parse_float(self, value: str, label: str) -> float:
        try:
            return float(value.strip().replace(",", "."))
        except ValueError as exc:
            raise ValueError(f"{label}: укажи число") from exc

    def _validate(self) -> tuple[int, int, float, int]:
        start = self._parse_int(self.start_var.get(), "С главы")
        end = self._parse_int(self.end_var.get(), "По главу")
        delay = self._parse_float(self.delay_var.get(), "Задержка")
        timeout = self._parse_int(self.timeout_var.get(), "Таймаут")
        if start < 1 or end < start:
            raise ValueError("Диапазон: 1 <= начало <= конец")
        if delay < 0:
            raise ValueError("Задержка не может быть отрицательной")
        if timeout < 5:
            raise ValueError("Таймаут должен быть не меньше 5 секунд")
        return start, end, delay, timeout

    def start_download(self) -> None:
        if self.process is not None:
            return

        if not SCRIPT.exists():
            messagebox.showerror(
                "Archie",
                f"Не найден файл:\n{SCRIPT}\n\nОбнови репозиторий через git pull.",
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
        self.status_var.set("Открываю обычный Chrome...")
        self._write_log("")
        self._write_log(f"=== Archie: главы {start}–{end} ===")
        self._write_log(f"Лог: {LOG_FILE}")
        self._write_log(f"Manifest: {MANIFEST_FILE}")

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
        self.status_var.set("Войди в iFreedom через VK, затем нажми «Я вошёл в iFreedom».")
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
            self.status_var.set("Подключаюсь к обычному Chrome...")
            self._write_log('✓ Сигнал "Я вошёл в iFreedom" отправлен.')
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
                else:
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
            self.progress_var.set(100)
            self.status_var.set("Готово")
            messagebox.showinfo("Archie", "Скачивание завершено без ошибок.")
        elif code == 2:
            self.status_var.set("Критическая ошибка")
            self._write_log(f"КРИТИЧЕСКАЯ ОШИБКА. Подробности: {LOG_FILE}")
            messagebox.showerror(
                "Archie: критическая ошибка",
                f"Подробный лог:\n{LOG_FILE}\n\n"
                f"Manifest:\n{MANIFEST_FILE}",
            )
        else:
            self.status_var.set("Завершено с ошибками")
            self._write_log(f"Есть ошибки. Подробности: {LOG_FILE}")
            messagebox.showwarning(
                "Archie",
                f"Загрузка завершилась с ошибками.\n\n"
                f"Подробный лог:\n{LOG_FILE}\n\n"
                f"Manifest:\n{MANIFEST_FILE}",
            )

    def _open_path(self, path: Path) -> None:
        if not path.exists():
            messagebox.showwarning("Archie", f"Файл ещё не создан:\n{path}")
            return
        try:
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror("Ошибка", str(exc))

    def _open_backup(self) -> None:
        BACKUP_DIR.mkdir(exist_ok=True)
        try:
            os.startfile(BACKUP_DIR)
        except OSError as exc:
            messagebox.showerror("Ошибка", str(exc))

    def _open_log(self) -> None:
        self._open_path(LOG_FILE)

    def _open_manifest(self) -> None:
        self._open_path(MANIFEST_FILE)

    def _on_close(self) -> None:
        if self.process is not None:
            if not messagebox.askyesno("Archie", "Остановить скачивание и закрыть программу?"):
                return
            try:
                self.process.terminate()
            except OSError:
                pass
        self.destroy()


if __name__ == "__main__":
    ArchieGui().mainloop()
