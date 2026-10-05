# Archie Windows GUI

Визуальный интерфейс для локального скачивания глав iFreedom через **обычный Google Chrome и обычный профиль пользователя**.

Никакого отдельного Chromium, временного профиля или копирования cookies Archie не создаёт.

## Установка

В PowerShell:

    cd "E:\Ранобэ\Archie"
    git pull
    cd "E:\Ранобэ\Archie\backup_tool\local"
    .\install.bat

## Запуск

Запусти:

    .\start_gui.bat

В окне Archie:

1. Укажи диапазон глав.
2. Нажми «Открыть Chrome и начать».
3. Archie откроет твой обычный Google Chrome, с твоим обычным профилем.
4. В Chrome открой вкладку `chrome://inspect/#remote-debugging`.
5. Включи **Allow remote debugging for this browser instance**.
6. Войди в iFreedom через VK обычным способом.
7. Проверь, что глава открывается полностью.
8. Нажми «Я вошёл в iFreedom» в окне Archie.
9. После этого запускается обычный requests-загрузчик из backup.py. Браузер больше не участвует в скачивании.

Chrome 144+ поддерживает включение Remote Debugging прямо из `chrome://inspect/#remote-debugging` без запуска отдельного профиля. Playwright 1.60+ умеет работать с этим новым способом подключения.

## Архив

    backup\chapters\N.txt
    backup\backup.txt
    backup\manifest.json

## Безопасность

Archie не получает пароль VK и не выполняет вход автоматически. Он использует уже открытую и авторизованную тобой сессию Chrome.

Remote Debugging даёт внешней программе полный доступ к открытой сессии браузера, поэтому включай его только когда Archie запущен и доверяешь этой программе.

Archie не обходит CAPTCHA, платный доступ, DRM или другие технические ограничения.
