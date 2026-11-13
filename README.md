# auto-electricity-report-service

Настольное офлайн-приложение «Контакт» для учёта электроэнергии и подготовки актов и счетов.

## Реализовано в issue #1

- Запуск минимального окна на **PySide6 / Qt Widgets** и сообщение об ошибке инициализации базы.
- Создание SQLite-базы с таблицами `tenants`, `tariffs`, `calculations`, `documents`, `settings`.
- Транзакции, внешние ключи, миграции через `PRAGMA user_version`. При повторном запуске проверяется целостность и полнота схемы: повреждённая база не перезаписывается и приводит к явной ошибке.
- Функции создания, чтения, просмотра списка и обновления сущностей в `app/data/database.py`.
- Сохранение базы **вне приложения**, при повторном запуске записи не сбрасываются.

Экран арендаторов, автоматические расчёты, генерация документов и Windows-сборка **ещё не реализованы** и относятся к следующим issues.

## Установка и запуск (Python 3.11+)

В PowerShell из корня проекта:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m app.main
```

На Linux/macOS для разработки:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m app.main
```

Для установки PySide6 требуется доступ к индексу пакетов один раз при подготовке среды. После установки приложению и локальной БД интернет не нужен.

## Где хранятся данные

На Windows: `%LOCALAPPDATA%\ElectricityReports\app.db`.

Для разработки на Linux/macOS: `$XDG_DATA_HOME/ElectricityReports/app.db` либо `~/.local/share/ElectricityReports/app.db`.

Невалидные относительные системные пути (`LOCALAPPDATA`, `XDG_DATA_HOME`) приводят к ошибке, а не к записи рядом с программой. База не хранится в каталоге исходников или рядом с исполняемым файлом. Путь к тестовой БД задаётся явно через `Database(path)`; пользовательская БД в тестах не используется.

## Работа со слоем данных

Используйте `Database` и отдельные функции `create_tenant`, `get_tenant`, `list_tenants`, `update_tenant` и аналогичные для тарифов, расчётов и документов. Настройки исполнителя представлены одной записью (`get_settings`, `update_settings`); она создаётся при миграции. Десятичные значения передаются строками или `decimal.Decimal`, при чтении возвращаются как `Decimal`. `float` для показаний/сумм не допускается.

```python
from decimal import Decimal
from tempfile import TemporaryDirectory
from pathlib import Path
from app.data.database import Database, create_tenant, get_tenant

with TemporaryDirectory() as directory:
    db = Database(Path(directory) / "app.db")
    db.initialize()
    with db.transaction() as conn:
        tenant_id = create_tenant(conn, name="Тестовый арендатор", initial_reading=Decimal("0.125"))
    with db.connection() as conn:
        print(get_tenant(conn, tenant_id))
```

Все операции записи выполняются внутри `with db.transaction() as conn:`; при ошибке изменения этой транзакции откатываются. Чтение — в `with db.connection() as conn:`. SQL находится только в модуле `app.data.database`.

## Тестирование

Нужен Python 3.11+; для проверок SQLite дополнительные зависимости не требуются.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

или в установленном Python-окружении:

```bash
python -m unittest discover -s tests -v
```

Тесты SQLite включают сценарий с двумя независимыми Python-процессами, проверки повреждённой схемы и неверных системных путей. GUI-тесты требуют установленного PySide6 и используют `QT_QPA_PLATFORM=offscreen`; без него они помечаются как пропущенные. Для ручной проверки GUI на Windows запустите `python -m app.main`, убедитесь в появлении окна и возможности его закрыть. Проверка Windows 10/11 без Python и сборка PyInstaller — отдельный этап MVP.
