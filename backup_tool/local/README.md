# Archie Local

Локальная версия Archie без облачной синхронизации.

Она скачивает публично доступные материалы в локальную папку и ничего не отправляет в облако.

## Запуск

    pip install -r requirements.txt
    python backup.py

Явный диапазон:

    python backup.py --start 1590 --end 2362

По умолчанию результат создаётся в backup/.

## Структура результата

    backup/
      chapters/
        1590.txt
        1591.txt
        ...
      backup.txt
      manifest.json

Инструмент работает только с обычными публично доступными страницами и не обходит авторизацию, платный доступ, CAPTCHA, DRM или другие технические ограничения.


## Авторизация iFreedom

Основной загрузчик остаётся обычным последовательным CLI-загрузчиком. Для страниц, требующих входа, можно передать cookies:

    python backup.py --start 1590 --end 2362 --cookies-file cookies.json

Файл cookies.json может быть JSON-массивом cookies Playwright или объектом с полем "cookies". Также поддерживается --cookie-header и переменная IFREEDOM_COOKIE_HEADER.
