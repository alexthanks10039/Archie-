# Archie backup tool

Локальный загрузчик публично доступных глав с iFreedom для личного резервного копирования.

По умолчанию используется диапазон 1590–2362.

Источник: https://ifreedom.su/ranobe/ohota-demonicheskogo-korolya-na-svoju-zhenu-buntujushhaya-ni-na-chto-ne-godnaya-miss-2/

## Что делает

- открывает страницу книги;
- собирает ссылки на главы и страницы пагинации;
- определяет номер главы;
- сохраняет главы выбранного диапазона отдельными TXT-файлами;
- умеет продолжать работу после остановки;
- создаёт manifest.json с URL, статусом и SHA-256;
- создаёт общий backup.txt.

Инструмент работает только с теми страницами, которые доступны обычным HTTP-запросом. Он не обходит авторизацию, платный доступ, CAPTCHA или другие ограничения.

## Установка

```bash
python -m venv .venv
```

Windows:

```powershell
.venv\\Scripts\\activate
```

Linux/macOS:

```bash
source .venv/bin/activate
```

```bash
pip install -r backup_tool/requirements.txt
```

## Запуск

```bash
python backup_tool/backup.py
```

Явный диапазон:

```bash
python backup_tool/backup.py --start 1590 --end 2362
```

Другая папка:

```bash
python backup_tool/backup.py --output ./backup
```

## Результат

```text
backup/
  chapters/
    1590.txt
    1591.txt
    ...
    2362.txt
  backup.txt
  manifest.json
```

Папка backup/ исключена из Git, поэтому тексты глав не коммитятся в репозиторий автоматически.
