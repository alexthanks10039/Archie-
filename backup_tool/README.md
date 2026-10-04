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


## ☁️ Облачное архивирование

Archie может работать как автономный архиватор через GitHub Actions.

После настройки облачных ключей один workflow:

1. восстанавливает предыдущий архив;
2. проверяет источники и докачивает новые доступные материалы;
3. собирает единый архив;
4. отправляет его одновременно в **Google Drive** и **Dropbox**;
5. сохраняет manifest отдельно;
6. оставляет временную копию в GitHub Actions.

Workflow запускается вручную и по расписанию.

### Google Drive

Для автоматической загрузки используется Google service account.

Нужно создать папку в Google Drive и предоставить ей доступ сервисному аккаунту. ID этой папки передаётся в secret:

GOOGLE_DRIVE_FOLDER_ID

JSON ключ сервисного аккаунта хранится в:

GOOGLE_SERVICE_ACCOUNT_JSON

После этого Archie загружает в указанную папку:

- archie-backup.zip
- manifest.json

Google Drive поддерживает resumable upload, поэтому архив можно передавать частями. Подробнее: https://developers.google.com/workspace/drive/api/guides/manage-uploads

### Dropbox

Для Dropbox используются три secrets:

DROPBOX_APP_KEY
DROPBOX_APP_SECRET
DROPBOX_REFRESH_TOKEN

Архив сохраняется в:

/Archie/latest/archie-backup.zip

Manifest:

/Archie/latest/manifest.json

### Автоматический режим

Файл:

.github/workflows/archie-cloud.yml

По умолчанию workflow проверяет диапазон 1–10000. Это сделано специально, чтобы не привязывать облачную архивацию к конкретному диапазону глав: Archie собирает все главы, которые удаётся обнаружить на источнике.

Если нужный источник предоставляет только часть контента, сохраняется именно доступная часть.

### Важно

Секреты не должны находиться в исходниках. GitHub Actions предоставляет encrypted secrets специально для API-ключей и других чувствительных данных. Никогда не добавляй JSON-ключ Google, Dropbox refresh token или другие credentials в Git. Подробнее: https://docs.github.com/en/actions/reference/security/secrets
