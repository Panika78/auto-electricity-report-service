# auto-electricity-report-service

Настольное офлайн-приложение «Контакт» для учёта электроэнергии и подготовки актов и счетов.

## Реализовано в issue #1

- Запуск минимального окна на **PySide6 / Qt Widgets** и сообщение об ошибке инициализации базы.
- Создание SQLite-базы с таблицами `tenants`, `tariffs`, `calculations`, `documents`, `settings`.
- Транзакции, внешние ключи, миграции через `PRAGMA user_version`. При повторном запуске проверяется целостность и полнота схемы: повреждённая база не перезаписывается и приводит к явной ошибке.
- Функции создания, чтения, просмотра списка и обновления сущностей в `app/data/database.py`.
- Сохранение базы **вне приложения**, при повторном запуске записи не сбрасываются.

Экран арендаторов, GUI для расчётов, генерация PDF/ZIP и Windows-сборка **ещё не реализованы** и относятся к следующим issues.

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


## Реализовано в issue #2: бизнес-логика

- Независимые от GUI расчёты `Decimal`: до 3 знаков показаний/расхода, 4 у тарифа, 2 у суммы с `ROUND_HALF_UP`, сумма прописью.
- Готовность к документам допускает денежную сумму **строго меньше 10^18 рублей** (максимум 999 999 999 999 999 999,99 ₽), которую поддерживает функция перевода суммы прописью; бо́льшие суммы сохраняются как неготовые расчёты.
- `CalculationService` в `app/core/service.py`: создание и автосохранение черновиков, начальные показания, пересекающиеся/неполные периоды, ручные корректировки, пересчёт тарифов, проверка готовности и массовый выбор.
- `ConfirmationRequired` с `code` и `affected_ids` возвращается до записи при пересечении, исправлении истории, ручной корректировке, изменении использованного тарифа, удалении, дублирующемся номере или нулевой выгрузке. После подтверждения вызов повторяется с соответствующим `confirm_*`.
- Документы: `next_document_number`, `reserve_document`, `prepare_document`, `document_status`. Только **после успешного сохранения PDF** генератор должен вызвать `mark_document_generated(document_id, expected_revision=snapshot["revision"])`; при ошибке записи PDF этот метод не вызывается. Сами PDF в этом issue не формируются.
- Все операции записи сервиса выполняются в транзакциях `Database.transaction()`; низкоуровневые функции `app.data.database` предназначены для слоя данных. GUI должен использовать именно сервис, иначе можно обойти проверки и отметку актуальности.
- Автоподстановка ручного начала без конечного показания допускается лишь при включённой настройке и явном `confirm_manual_reuse=True`.

- Изменение тарифа пересчитывает значения и помечает документы устаревшими, но **не меняет `updated_at` расчётов**, чтобы не менять порядок выбора источника/массовой выгрузки.
- После `confirm_history=True` изменённые автоматически унаследованные (`start_reading_source='previous_month'`) начала последующих месяцев согласуются с предыдущим месяцем, расход/сумма пересчитываются, документы помечаются для обновления. Ручные начала не заменяются. При удалении единственного источника унаследованное начало очищается и остаётся привязанным к прошлому месяцу для возможной синхронизации в будущем.
- При создании нового завершённого расчёта задним числом с последующими записями сервис запрашивает `confirm_history=True`, а при согласии синхронизирует автоматические начала и документы в одной транзакции. При переносе расчёта в другой месяц сервис отдельно пересчитывает автоматически унаследованное начало **самого перенесённого расчёта** по новому предыдущему месяцу; ручное начало сохраняется.
- Для первичных показаний используйте `CalculationService.create_tenant` / `update_tenant` (отрицательные значения и `float` запрещены).
- `bulk_candidates()` возвращает `skipped` со структурированным `reason` (`no_calculation`, `missing_tariff`, `incomplete_readings`, `invalid_readings`, `not_ready`, `zero_consumption`); для неготового расчёта также возвращается `errors` валидатора.
- **Политика для незавершённых расчётов (FR-06/FR-07, уточнена в PRD в этом PR):** пропускаем записи предыдущего месяца без конечного показания и используем последнюю заполненную по `updated_at DESC, id DESC`. Сам Issue #2 это правило не детализирует. Если заполненных записей нет — ручное начало или явно подтверждённое резервное; в более ранние месяцы не заглядываем.
- `prepare_document()` возвращает `revision` со снимком значений расчёта: при `mark_document_generated()` проверяется и снимок данных, и времена правок; не переиспользуйте снимок после исправления истории.


Пример программного интерфейса:

```python
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from app.core.service import CalculationService
from app.data.database import Database, create_tenant

with TemporaryDirectory() as directory:
    database = Database(Path(directory) / "demo.db")
    database.initialize()
    with database.transaction() as conn:
        tenant_id = create_tenant(conn, name="Демонстрационный арендатор")
    service = CalculationService(database)
    service.set_tariff(2026, 10, Decimal("11.07"))
    draft = service.create_calculation(tenant_id=tenant_id, year=2026, month=10,
                                       start_reading=Decimal("193630"),
                                       end_reading=Decimal("197030"))
    print(draft["consumption"], draft["total_amount"])  # 3400 37638.00
```

Расширенные тесты расчётов и метаданных документов находятся в `tests/test_core.py`, запуск вместе с существующими тестами:

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
python -m compileall -q app tests
```

Экран ввода, формирование PDF и ZIP не относятся к issue #2.
