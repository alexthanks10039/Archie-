# Archie Cloud

Облачная версия Archie.

Она использует тот же локальный архиватор, но дополнительно умеет восстанавливать предыдущий архив из Dropbox и загружать обновлённый архив обратно в Dropbox.

## Локальный запуск

    pip install -r requirements.txt
    python backup.py

## Dropbox

Переменные окружения:

    DROPBOX_APP_KEY
    DROPBOX_APP_SECRET
    DROPBOX_REFRESH_TOKEN

Для GitHub Actions они должны храниться в Repository Secrets.

Архив:

    /Archie/latest/archie-backup.zip

Manifest:

    /Archie/latest/manifest.json

## GitHub Actions

Workflow находится в .github/workflows/archie-cloud.yml.

Он:
1. восстанавливает предыдущий архив из Dropbox;
2. докачивает доступные материалы;
3. собирает archie-backup.zip;
4. загружает архив и manifest в Dropbox;
5. сохраняет временный GitHub Actions artifact на 7 дней.

Google Drive в этой версии отсутствует.

Инструмент работает только с обычными публично доступными страницами и не обходит авторизацию, платный доступ, CAPTCHA, DRM или другие технические ограничения.
