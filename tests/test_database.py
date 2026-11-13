"""Поведенческие проверки SQLite без обращения к пользовательской базе."""

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from app.data import database as db_module
from app.data.database import (
    SCHEMA_VERSION,
    Database,
    SchemaIntegrityError,
    UnsupportedSchemaVersion,
    create_calculation,
    create_document,
    create_tariff,
    create_tenant,
    delete_calculation,
    get_calculation,
    get_database_path,
    get_document,
    get_settings,
    get_tariff,
    get_tenant,
    list_calculations,
    list_documents,
    list_tariffs,
    list_tenants,
    update_calculation,
    update_document,
    update_settings,
    update_tariff,
    update_tenant,
)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "new" / "app.db"
        self.db = Database(self.path)
        self.db.initialize()

    def add_tenant(self, name="Вымышленный арендатор"):
        with self.db.transaction() as conn:
            return create_tenant(conn, name=name)

    def add_calculation(self, tenant_id, **changes):
        values = dict(
            tenant_id=tenant_id, year=2026, month=10,
            period_start="2026-10-01", period_end="2026-10-31",
        )
        values.update(changes)
        with self.db.transaction() as conn:
            return create_calculation(conn, **values)

    def test_creates_all_five_tables_and_user_version_in_new_file(self):
        self.assertTrue(self.path.is_file())
        with self.db.connection() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            self.assertEqual(tables, {"tenants", "tariffs", "calculations", "documents", "settings"})
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(list_tenants(conn), [])
            self.assertEqual(list_tariffs(conn), [])
            self.assertEqual(list_documents(conn), [])
            self.assertEqual(get_settings(conn)["performer_name"], "")

    def test_initialize_twice_preserves_saved_records_and_ids(self):
        tenant = self.add_tenant()
        self.db.initialize()
        self.db.initialize()
        with self.db.connection() as conn:
            self.assertEqual(get_tenant(conn, tenant)["id"], tenant)
            self.assertEqual(len(list_tenants(conn)), 1)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertGreater(self.add_tenant(), tenant)

    def test_tenant_create_update_read_and_archive_restore(self):
        with self.db.transaction() as conn:
            tenant_id = create_tenant(
                conn, name="Организация А (тест)", inn="0000000000", kpp="",
                legal_address="Вымышленный адрес", contract_number="Д-001",
                contract_date="2026-01-10", additional_agreement="Доп. соглашение 1",
                initial_reading="123.125", initial_reading_date="2026-01-10",
            )
            update_tenant(conn, tenant_id, name="Организация Б (тест)", is_archived=True)
        with self.db.connection() as conn:
            item = get_tenant(conn, tenant_id)
            self.assertEqual(item["name"], "Организация Б (тест)")
            self.assertEqual(item["contract_number"], "Д-001")
            self.assertEqual(item["initial_reading"], Decimal("123.125"))
            self.assertEqual(list_tenants(conn), [])
            self.assertEqual(len(list_tenants(conn, include_archived=True)), 1)
        with self.db.transaction() as conn:
            update_tenant(conn, tenant_id, is_archived=False)
        with self.db.connection() as conn:
            self.assertEqual(len(list_tenants(conn)), 1)
            self.assertEqual(get_tenant(conn, -1), None)

    def test_blank_or_invisible_tenant_names_rejected_on_create_and_update(self):
        invalid_names = ("", "   ", "\t", "\n", "\t\n", "\u00a0", "\u2003", "\u200b", "\u2060", "\u0301")
        tenant_id = self.add_tenant("Тестовая компания")
        for invalid in invalid_names:
            with self.subTest(name=repr(invalid)):
                with self.assertRaises(ValueError):
                    with self.db.transaction() as conn:
                        create_tenant(conn, name=invalid)
                with self.assertRaises(ValueError):
                    with self.db.transaction() as conn:
                        update_tenant(conn, tenant_id, name=invalid)
        with self.db.transaction() as conn:
            update_tenant(conn, tenant_id, name="  Компания А  ")
        with self.db.connection() as conn:
            self.assertEqual([tenant["name"] for tenant in list_tenants(conn)], ["  Компания А  "])

    def test_tariff_create_read_update_and_list(self):
        with self.db.transaction() as conn:
            tariff_id = create_tariff(conn, year=2026, month=10, rate=Decimal("6.1234"))
            update_tariff(conn, tariff_id, rate="7.0001")
        with self.db.connection() as conn:
            item = get_tariff(conn, tariff_id)
            self.assertEqual(item["rate"], Decimal("7.0001"))
            self.assertEqual(len(list_tariffs(conn)), 1)
            self.assertIsNone(get_tariff(conn, -1))

    def test_tariffs_have_unique_year_month(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as conn:
                create_tariff(conn, year=2026, month=1, rate="5.1234")
                create_tariff(conn, year=2026, month=1, rate="9.9999")
        with self.db.connection() as conn:
            self.assertEqual(list_tariffs(conn), [])

    def test_calculation_create_update_read_and_filter(self):
        tenant_id = self.add_tenant()
        calculation_id = self.add_calculation(tenant_id, start_reading="100.125", end_reading="101.250")
        with self.db.transaction() as conn:
            update_calculation(
                conn, calculation_id, consumption="1.125", total_amount="6.89",
                manual_consumption=True, is_ready=True, start_reading_source="manual",
            )
        with self.db.connection() as conn:
            item = get_calculation(conn, calculation_id)
            self.assertEqual(item["consumption"], Decimal("1.125"))
            self.assertEqual(item["total_amount"], Decimal("6.89"))
            self.assertEqual(item["manual_consumption"], 1)
            self.assertEqual(item["start_reading_source"], "manual")
            self.assertTrue(item["created_at"])
            self.assertTrue(item["updated_at"])
            self.assertEqual(len(list_calculations(conn, tenant_id=tenant_id, year=2026, month=10)), 1)
            self.assertEqual(list_calculations(conn, year=2025), [])
            self.assertIsNone(get_calculation(conn, -1))

    def test_multiple_calculations_can_share_exactly_the_same_period(self):
        tenant_id = self.add_tenant()
        a = self.add_calculation(tenant_id, end_reading="1.000")
        b = self.add_calculation(tenant_id, end_reading="2.000")
        self.assertNotEqual(a, b)
        with self.db.connection() as conn:
            self.assertEqual({r["id"] for r in list_calculations(conn)}, {a, b})

    def test_latest_calculation_order_breaks_timestamp_ties_by_id(self):
        tenant_id = self.add_tenant()
        a = self.add_calculation(tenant_id)
        b = self.add_calculation(tenant_id)
        with self.db.transaction() as conn:
            conn.execute("UPDATE calculations SET updated_at = ? WHERE id IN (?, ?)",
                         ("2026-10-20T10:00:00.000000+00:00", a, b))
        with self.db.connection() as conn:
            self.assertEqual([r["id"] for r in list_calculations(conn)], [b, a])

    def test_document_create_read_update_and_list(self):
        calculation = self.add_calculation(self.add_tenant())
        with self.db.transaction() as conn:
            doc_id = create_document(conn, calculation_id=calculation, type="act", number=15,
                                     document_date="2026-11-01")
            update_document(conn, doc_id, number=16, is_current=True,
                            last_generated_at="2026-11-02T12:00:00+00:00")
        with self.db.connection() as conn:
            doc = get_document(conn, doc_id)
            self.assertEqual(doc["number"], 16)
            self.assertEqual(doc["document_date"], "2026-11-01")
            self.assertEqual(doc["is_current"], 1)
            self.assertEqual(len(list_documents(conn, calculation_id=calculation)), 1)
            self.assertIsNone(get_document(conn, -1))

    def test_documents_have_unique_type_per_calculation(self):
        calculation = self.add_calculation(self.add_tenant())
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as conn:
                create_document(conn, calculation_id=calculation, type="act", number=1,
                                document_date="2026-11-01")
                create_document(conn, calculation_id=calculation, type="act", number=2,
                                document_date="2026-11-01")
        with self.db.connection() as conn:
            self.assertEqual(list_documents(conn), [])

    def test_document_numbers_are_not_unique_even_with_same_type_and_date(self):
        tenant_id = self.add_tenant()
        first = self.add_calculation(tenant_id)
        second = self.add_calculation(tenant_id)
        with self.db.transaction() as conn:
            create_document(conn, calculation_id=first, type="act", number=1,
                            document_date="2026-11-01")
            create_document(conn, calculation_id=second, type="act", number=1,
                            document_date="2026-11-01")
            create_document(conn, calculation_id=first, type="invoice", number=1,
                            document_date="2026-11-01")
        with self.db.connection() as conn:
            self.assertEqual(len(list_documents(conn)), 3)

    def test_settings_are_singleton_and_persist_edits(self):
        with self.db.transaction() as conn:
            update_settings(conn, performer_name="Тестовый исполнитель", inn="0000000000",
                            bank_name="Вымышленный банк", reuse_manual_start_readings=True)
        with self.db.connection() as conn:
            settings = get_settings(conn)
            self.assertEqual(settings["performer_name"], "Тестовый исполнитель")
            self.assertEqual(settings["reuse_manual_start_readings"], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM settings").fetchone()[0], 1)
        self.db.initialize()
        with self.db.connection() as conn:
            self.assertEqual(get_settings(conn)["bank_name"], "Вымышленный банк")

    def test_foreign_keys_are_on_for_every_connection(self):
        for _ in range(3):
            with self.db.connection() as conn:
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as conn:
                create_calculation(conn, tenant_id=99999, year=2026, month=10,
                                   period_start="2026-10-01", period_end="2026-10-31")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as conn:
                create_document(conn, calculation_id=99999, type="act", number=1,
                                document_date="2026-11-01")

    def test_tenant_cannot_be_deleted_with_calculation_but_document_cascades(self):
        tenant_id = self.add_tenant()
        calculation = self.add_calculation(tenant_id)
        with self.db.transaction() as conn:
            document = create_document(conn, calculation_id=calculation, type="invoice", number=3,
                                       document_date="2026-11-01")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
        with self.db.transaction() as conn:
            delete_calculation(conn, calculation)
        with self.db.connection() as conn:
            self.assertIsNone(get_calculation(conn, calculation))
            self.assertIsNone(get_document(conn, document))
            self.assertIsNotNone(get_tenant(conn, tenant_id))

    def test_decimals_are_exact_text_in_sqlite_and_decimal_in_python(self):
        tenant_id = self.add_tenant()
        with self.db.transaction() as conn:
            tariff = create_tariff(conn, year=2026, month=10, rate="0.0001")
            calc = create_calculation(
                conn, tenant_id=tenant_id, year=2026, month=10,
                period_start="2026-10-01", period_end="2026-10-31",
                start_reading="1234567890123456789.125", end_reading="1234567890123456790.250",
                consumption="1.125", total_amount="9999999999999999999.99",
            )
        with self.db.connection() as conn:
            row = conn.execute("SELECT typeof(start_reading), start_reading, typeof(total_amount), total_amount "
                               "FROM calculations WHERE id = ?", (calc,)).fetchone()
            self.assertEqual(tuple(row), ("text", "1234567890123456789.125",
                                          "text", "9999999999999999999.99"))
            self.assertEqual(get_calculation(conn, calc)["end_reading"], Decimal("1234567890123456790.250"))
            self.assertEqual(get_tariff(conn, tariff)["rate"], Decimal("0.0001"))

    def test_float_and_excessive_fractional_digits_are_rejected(self):
        tenant_id = self.add_tenant()
        with self.assertRaises(TypeError):
            with self.db.transaction() as conn:
                create_tariff(conn, year=2026, month=10, rate=0.1)
        with self.assertRaises(ValueError):
            with self.db.transaction() as conn:
                create_tariff(conn, year=2026, month=10, rate="1.23456")
        with self.assertRaises(ValueError):
            self.add_calculation(tenant_id, end_reading="5.1234")
        with self.assertRaises(ValueError):
            self.add_calculation(tenant_id, total_amount="6.789")
        with self.assertRaises(ValueError):
            with self.db.transaction() as conn:
                create_tariff(conn, year=2026, month=10, rate="NaN")

    def test_period_cannot_cross_month_or_be_reversed(self):
        tenant_id = self.add_tenant()
        for start, end in (("2026-10-01", "2026-11-01"),
                           ("2026-10-31", "2026-10-01"),
                           ("2026-10-32", "2026-10-31")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.add_calculation(tenant_id, period_start=start, period_end=end)

    def test_transaction_rolls_back_multiple_successful_writes_on_exception(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as conn:
                create_tenant(conn, name="Временная запись")
                create_tariff(conn, year=2026, month=10, rate="1.0000")
                raise RuntimeError("Ошибка последующей операции")
        with self.db.connection() as conn:
            self.assertEqual(list_tenants(conn), [])
            self.assertEqual(list_tariffs(conn), [])

    def test_failed_update_rolls_back_earlier_update(self):
        tenant_id = self.add_tenant("Исходное название")
        with self.assertRaises(ValueError):
            with self.db.transaction() as conn:
                update_tenant(conn, tenant_id, name="Новое название")
                update_tenant(conn, tenant_id, non_existing_column="bad")
        with self.db.connection() as conn:
            self.assertEqual(get_tenant(conn, tenant_id)["name"], "Исходное название")

    def test_data_is_saved_after_connections_close_and_new_database_object_opens(self):
        tenant_id = self.add_tenant()
        second = Database(self.path)
        second.initialize()
        with second.connection() as conn:
            self.assertEqual(get_tenant(conn, tenant_id)["id"], tenant_id)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_declared_current_schema_without_tables_is_rejected_without_reset(self):
        path = Path(self.directory.name) / "empty_with_version.db"
        with sqlite3.connect(path) as conn:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        before = path.read_bytes()
        with self.assertRaisesRegex(SchemaIntegrityError, "отсутствуют обязательные таблицы"):
            Database(path).initialize()
        self.assertEqual(path.read_bytes(), before)
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_missing_column_in_current_schema_fails_without_data_loss(self):
        tenant_id = self.add_tenant("Уцелевшая организация")
        with sqlite3.connect(self.path) as conn:
            conn.execute("ALTER TABLE tenants DROP COLUMN postal_address")
        with self.assertRaisesRegex(SchemaIntegrityError, "postal_address"):
            self.db.initialize()
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT name FROM tenants WHERE id = ?", (tenant_id,)).fetchone()[0],
                             "Уцелевшая организация")

    def test_foreign_key_check_rejects_invalid_existing_data_without_deletion(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("""INSERT INTO calculations(
                tenant_id, year, month, period_start, period_end, created_at, updated_at
            ) VALUES (999, 2026, 10, '2026-10-01', '2026-10-31', 'test', 'test')""")
        with self.assertRaisesRegex(SchemaIntegrityError, "внешние ключи"):
            self.db.initialize()
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM calculations WHERE tenant_id = 999").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_existing_schema_missing_required_constraints_is_rejected(self):
        tenant_id = self.add_tenant("Тестовые данные сохраняются")
        calculation_id = self.add_calculation(tenant_id)
        with self.db.transaction() as conn:
            tariff_id = create_tariff(conn, year=2026, month=10, rate="4.1234")
            document_id = create_document(
                conn, calculation_id=calculation_id, type="act",
                number=7, document_date="2026-10-31",
            )

        # Для каждой мутации — независимая копия корректной БД.
        # Восстанавливаем все колонки и существующие данные, но убираем одно
        # обязательное ограничение: quick_check и foreign_key_check остаются OK.
        cases = (
            ("tariffs", ",\n        UNIQUE (year, month)", "", "UNIQUE(year, month)", None),
            ("documents", ",\n        UNIQUE (calculation_id, type)", "", "UNIQUE(calculation_id, type)", None),
            ("calculations", " REFERENCES tenants(id) ON DELETE RESTRICT", "", "внешний ключ", None),
            ("documents", " REFERENCES calculations(id) ON DELETE CASCADE", "", "внешний ключ", None),
            ("documents", "ON DELETE CASCADE", "ON DELETE RESTRICT", "ON DELETE CASCADE", None),
            ("tariffs", ",\n        UNIQUE (year, month)", "", "UNIQUE(year, month)",
             "CREATE UNIQUE INDEX wrong_partial ON tariffs(year, month) WHERE month = 10"),
            ("tariffs", ",\n        UNIQUE (year, month)", "", "UNIQUE(year, month)",
             "CREATE UNIQUE INDEX wrong_extra_column ON tariffs(year, month, rate)"),
            ("documents", "REFERENCES calculations(id)", "REFERENCES settings(id)",
             "внешний ключ", None),
        )
        for case_no, (table, old, new, error, extra_sql) in enumerate(cases):
            with self.subTest(table=table, mutation=old, replacement=new, extra=extra_sql):
                path = Path(self.directory.name) / f"broken_constraints_{case_no}.db"
                shutil.copy2(self.path, path)
                with sqlite3.connect(path) as conn:
                    conn.execute("PRAGMA foreign_keys = OFF")
                    original_sql = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone()[0]
                    self.assertIn(old, original_sql)
                    # Copy-before-drop avoids rewriting unrelated FK declarations.
                    conn.execute(f"CREATE TEMP TABLE source_data AS SELECT * FROM {table}")
                    conn.execute(f"DROP TABLE {table}")
                    conn.execute(original_sql.replace(old, new))
                    conn.execute(f"INSERT INTO {table} SELECT * FROM source_data")
                    conn.execute("DROP TABLE source_data")
                    if extra_sql:
                        conn.execute(extra_sql)
                    self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
                    self.assertIsNone(conn.execute("PRAGMA foreign_key_check").fetchone())
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                before = path.read_bytes()
                with self.assertRaises(SchemaIntegrityError) as caught:
                    Database(path).initialize()
                self.assertIn(error, str(caught.exception))
                self.assertEqual(path.read_bytes(), before)
                with sqlite3.connect(path) as conn:
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                    self.assertEqual(conn.execute("SELECT name FROM tenants WHERE id=?", (tenant_id,)).fetchone()[0],
                                     "Тестовые данные сохраняются")
                    self.assertEqual(conn.execute("SELECT rate FROM tariffs WHERE id=?", (tariff_id,)).fetchone()[0],
                                     "4.1234")
                    self.assertEqual(conn.execute("SELECT number FROM documents WHERE id=?", (document_id,)).fetchone()[0],
                                     7)

    def test_data_persists_between_independent_python_processes(self):
        # Оба запуска происходят вне процесса unittest и из другого cwd.
        env = os.environ.copy()
        root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
        writer = """import sys
from app.data.database import Database, create_tenant, create_tariff
from decimal import Decimal
db = Database(sys.argv[1]); db.initialize()
with db.transaction() as conn:
    tenant = create_tenant(conn, name='Процесс A', inn='0000000000', initial_reading=Decimal('3.125'))
    tariff = create_tariff(conn, year=2026, month=10, rate=Decimal('4.1234'))
print(f'{tenant},{tariff}')
"""
        reader = """import sys
from decimal import Decimal
from app.data.database import Database, get_tenant, get_tariff
db = Database(sys.argv[1]); db.initialize()
with db.connection() as conn:
    tenant = get_tenant(conn, int(sys.argv[2]))
    tariff = get_tariff(conn, int(sys.argv[3]))
    assert tenant['name'] == 'Процесс A' and tenant['inn'] == '0000000000'
    assert tenant['initial_reading'] == Decimal('3.125')
    assert tariff['rate'] == Decimal('4.1234')
print('OK')
"""
        created = subprocess.run(
            [sys.executable, "-c", writer, str(self.path)],
            cwd=self.directory.name, env=env, capture_output=True, text=True, check=True,
        )
        tenant_id, tariff_id = created.stdout.strip().split(",")
        reopened = subprocess.run(
            [sys.executable, "-c", reader, str(self.path), tenant_id, tariff_id],
            cwd=self.directory.name, env=env, capture_output=True, text=True, check=True,
        )
        self.assertEqual(reopened.stdout.strip(), "OK")

    def test_unknown_newer_schema_version_is_never_overwritten(self):
        tenant_id = self.add_tenant()
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA user_version = 999")
        with self.assertRaises(UnsupportedSchemaVersion):
            self.db.initialize()
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 999)
            self.assertEqual(get_tenant(conn, tenant_id)["id"], tenant_id)

    def test_migration_failure_rolls_back_ddl_and_version(self):
        path = Path(self.directory.name) / "broken.db"
        db = Database(path)

        def broken_v2(conn):
            conn.execute("CREATE TABLE temporary_migration_table(id INTEGER)")
            raise RuntimeError("Имитация сбоя миграции")

        with mock.patch.object(db_module, "SCHEMA_VERSION", 2), mock.patch.object(
            db_module, "MIGRATIONS", (db_module._migrate_to_v1, broken_v2)
        ):
            with self.assertRaises(RuntimeError):
                db.initialize()
        with db.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)

    def test_broken_future_migration_keeps_existing_version_and_user_records(self):
        tenant_id = self.add_tenant("Сохранённая тестовая организация")

        def broken_v2(conn):
            conn.execute("CREATE TABLE temporary_migration_table(id INTEGER)")
            conn.execute("UPDATE tenants SET name = 'Недопустимое изменение'")
            raise RuntimeError("Сбой второй миграции")

        with mock.patch.object(db_module, "SCHEMA_VERSION", 2), mock.patch.object(
            db_module, "MIGRATIONS", (db_module._migrate_to_v1, broken_v2)
        ):
            with self.assertRaises(RuntimeError):
                self.db.initialize()
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(get_tenant(conn, tenant_id)["name"], "Сохранённая тестовая организация")
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name='temporary_migration_table'"
            ).fetchone())

    def test_next_migration_is_sequential_and_does_not_recreate_v1(self):
        tenant_id = self.add_tenant()
        def migrate_to_v2(conn):
            conn.execute("CREATE TABLE test_future(id INTEGER PRIMARY KEY)")

        with mock.patch.object(db_module, "SCHEMA_VERSION", 2), mock.patch.object(
            db_module, "MIGRATIONS", (db_module._migrate_to_v1, migrate_to_v2)
        ):
            self.db.initialize()
            self.db.initialize()
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNotNone(get_tenant(conn, tenant_id))
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE name='test_future'").fetchone())

    def test_explicit_test_db_ignores_real_user_data_location(self):
        with mock.patch.object(db_module, "get_database_path", side_effect=AssertionError("Путь не должен вызываться")):
            test_db = Database(self.path)
            test_db.initialize()
        self.assertEqual(test_db.path, self.path)

    def test_first_windows_launch_creates_user_directory_outside_project(self):
        localappdata = Path(self.directory.name) / "FakeUser" / "AppData" / "Local"
        with mock.patch.object(db_module.sys, "platform", "win32"), mock.patch.dict(
            os.environ, {"LOCALAPPDATA": str(localappdata)}
        ):
            default_db = Database()
            self.assertFalse(default_db.path.exists())
            default_db.initialize()
            self.assertEqual(default_db.path, localappdata / "ElectricityReports" / "app.db")
            self.assertTrue(default_db.path.is_file())
            self.assertEqual(default_db.path.parent.name, "ElectricityReports")
        with default_db.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_relative_windows_localappdata_is_rejected_without_creating_database(self):
        with mock.patch.object(db_module.sys, "platform", "win32"), mock.patch.dict(
            os.environ, {"LOCALAPPDATA": "."}
        ):
            with self.assertRaisesRegex(RuntimeError, "абсолютным путём"):
                Database().initialize()
        self.assertFalse((Path.cwd() / "ElectricityReports" / "app.db").exists())

    def test_relative_xdg_directory_is_rejected(self):
        with mock.patch.object(db_module.sys, "platform", "linux"), mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": "relative/path"}
        ):
            with self.assertRaisesRegex(RuntimeError, "абсолютным путём"):
                get_database_path()

    def test_windows_path_uses_localappdata_and_not_working_directory(self):
        fake_user_dir = Path(self.directory.name) / "UserData" / "Local"
        with mock.patch.object(db_module.sys, "platform", "win32"), mock.patch.dict(
            os.environ, {"LOCALAPPDATA": str(fake_user_dir)}
        ):
            self.assertEqual(get_database_path(), fake_user_dir / "ElectricityReports" / "app.db")
            self.assertEqual(Database().path, fake_user_dir / "ElectricityReports" / "app.db")
        with mock.patch.object(db_module.sys, "platform", "win32"), mock.patch.dict(
            os.environ, {"LOCALAPPDATA": ""}
        ):
            with self.assertRaises(RuntimeError):
                get_database_path()


if __name__ == "__main__":
    unittest.main()
