"""Isolated integration probe executed by the actual frozen Windows binary.

This is NOT a normal user command. The required profile guard prevents accidental
use of the customer's database. All records, PDFs and images are synthetic.
"""
from __future__ import annotations

import os
import tempfile
import traceback
import zipfile
from pathlib import Path

from app.core.service import CalculationService
from app.data import database as db
from app.documents import export_bulk_zip, export_document
from app.qa_profile import validate_qa_profile


def _probe() -> str:
    # The caller MUST redirect the default database location to a dedicated
    # absolute test profile. Never open or overwrite a normal user profile.
    root = validate_qa_profile()
    expected = root / "ElectricityReports" / "app.db"

    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    from app.ui.common import import_image
    from app.ui.main_window import MainWindow

    from app.ui.common import appearance_settings, read_appearance

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("контакт")
    first_launch = not expected.exists()
    # This must be INI in the isolated test profile, never Qt's HKCU registry.
    preferences = appearance_settings()
    if Path(preferences.fileName()).resolve() != root / "qa-appearance.ini":
        raise AssertionError("Qt appearance settings escaped the synthetic profile.")
    if first_launch:
        if read_appearance() != (100, 400):
            raise AssertionError("Тестовый профиль не содержит исходных настроек.")
        preferences.setValue("font_size_percent", 125)
        preferences.setValue("font_weight_value", 700)
        preferences.sync()
        if preferences.status() != QSettings.Status.NoError:
            raise AssertionError("Не удалось записать изолированные настройки Qt.")
    elif read_appearance() != (125, 700):
        raise AssertionError("Настройки шрифта не пережили обновление каталога программы.")

    database = db.Database()
    database.initialize()

    with database.connection() as connection:
        tenants = db.list_tenants(connection, include_archived=True)
    if first_launch and tenants:
        raise AssertionError("В первой пустой базе обнаружены арендаторы.")
    if not first_launch and len(tenants) != 1:
        raise AssertionError("После замены папки программы данные арендатора потеряны.")

    service = CalculationService(database)
    if first_launch:
        with database.transaction() as connection:
            db.update_settings(connection, performer_name="ИП Синтетический")
        service.set_tariff(2026, 10, "11.0704")
        tenant = service.create_tenant(name="ООО Вымышленный арендатор")
        calculation = service.create_calculation(
            tenant["id"], 2026, 10, start_reading="100.125", end_reading="109.625"
        )
        with tempfile.TemporaryDirectory(prefix="kontakt-image-") as temp:
            image_path = Path(temp) / "synthetic.png"
            image = QImage(10, 10, QImage.Format.Format_RGB32)
            image.fill(0xFFFFFFFF)
            if not image.save(str(image_path)):
                raise AssertionError("Qt не смог записать синтетическое PNG.")
            stored = import_image(str(image_path), "signature", database=database)
            if stored.parent != expected.parent / "images" or not stored.is_file():
                raise AssertionError("Изображение сохранено не рядом с пользовательской БД.")
            service.update_settings(signature_path=str(stored))
    else:
        tenant = tenants[0]
        with database.connection() as connection:
            calculations = db.list_calculations(connection, tenant_id=tenant["id"])
            settings = db.get_settings(connection)
        if len(calculations) != 1:
            raise AssertionError("После обновления не найден сохранённый расчёт.")
        if settings["performer_name"] != "ИП Синтетический":
            raise AssertionError("Настройки исполнителя не сохранились.")
        signature = Path(settings["signature_path"] or "")
        if not signature.is_file() or signature.parent != expected.parent / "images":
            raise AssertionError("Сохранённая подпись не найдена после обновления.")
        calculation = calculations[0]

    window = MainWindow(database)
    try:
        window.show()
        app.processEvents()
        if not window.isVisible():
            raise AssertionError("Нативное главное окно не открылось.")
    finally:
        window.close()
        app.processEvents()

    with tempfile.TemporaryDirectory(prefix="kontakt-pdfs-") as temp:
        output = Path(temp)
        for kind in ("act", "invoice"):
            path = output / f"{kind}.pdf"
            result = export_document(
                service, calculation["id"], kind, path,
                document_date="2026-11-02", overwrite=False,
            )
            if result.path != path or not path.read_bytes().startswith(b"%PDF-"):
                raise AssertionError(f"Не создан корректный PDF: {kind}.")
            if path.stat().st_size < 1000:
                raise AssertionError(f"Слишком короткий PDF: {kind}.")
        bulk = export_bulk_zip(service, 2026, 10, output / "synthetic.zip")
        if (not bulk.statuses_confirmed or len(bulk.successful) != 2
                or bulk.failed or bulk.path is None):
            raise AssertionError("Не удалось сформировать полный ZIP с двумя PDF.")
        with zipfile.ZipFile(bulk.path) as archive:
            if archive.testzip() is not None or len(archive.namelist()) != 2:
                raise AssertionError("Повреждён ZIP или неверное число документов.")
            if not all(archive.read(name).startswith(b"%PDF-")
                       for name in archive.namelist()):
                raise AssertionError("В ZIP содержится не PDF.")

    # A fresh Database object must see the same durable records.
    reopened = db.Database()
    reopened.initialize()
    with reopened.connection() as connection:
        if len(db.list_tenants(connection)) != 1:
            raise AssertionError("Записи не сохранились после повторного открытия SQLite.")
        if len(db.list_documents(connection, calculation_id=calculation["id"])) != 2:
            raise AssertionError("Не сохранились метаданные акта и счёта.")

    return "fresh" if first_launch else "existing"


def run_release_smoke() -> int:
    """Write a machine-readable result because a windowed EXE has no console."""
    if os.environ.get("KONTAKT_RELEASE_SMOKE") != "1":
        return 2
    result_path = os.environ.get("KONTAKT_SMOKE_RESULT")
    if not result_path:
        return 2
    try:
        phase = _probe()
    except Exception:
        Path(result_path).write_text("FAIL\n" + traceback.format_exc(), encoding="utf-8")
        return 1
    Path(result_path).write_text("PASS " + phase + "\n", encoding="utf-8")
    return 0
