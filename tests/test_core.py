"""Интеграционные тесты Issue #2; работают на реальной app.data.database в репозитории."""
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.core.calculations import (amount_in_words, calculate_consumption,
                                   calculate_total, decimal_value)
from app.core.service import CalculationService, ConfirmationRequired
from app.data.database import Database, create_tenant

D = Decimal


class DecimalTests(unittest.TestCase):
    def test_precise_example_and_round_half_up(self):
        self.assertEqual(calculate_consumption(D('193630'), D('197030')), D('3400'))
        self.assertEqual(calculate_total(D('3400'), D('11.07')), D('37638.00'))
        self.assertEqual(calculate_total(D('1'), D('0.005')), D('0.01'))
        self.assertEqual(calculate_total(D('0.5'), D('0.01')), D('0.01'))
        self.assertEqual(calculate_total(D('1.001'), D('1.0005')), D('1.00'))

    def test_large_values_and_low_global_decimal_precision(self):
        from decimal import localcontext

        with localcontext() as context:
            context.prec = 6
            self.assertEqual(
                calculate_consumption(D('1000000000000000000000000000000.125'),
                                      D('1000000000000000000000000000001.126')),
                D('1.001'),
            )
            self.assertEqual(
                calculate_total(D('1E+30'), D('0.0001')),
                D('100000000000000000000000000.00'),
            )
            self.assertEqual(
                calculate_total(D('0.005'), D('1')),
                D('0.01'),
            )

    def test_rejects_float_negative_and_non_finite(self):
        for bad in (0.5, True, D('NaN'), D('Infinity'), D('-Infinity'), '-1', '1.0001'):
            with self.subTest(bad=bad), self.assertRaises((TypeError, ValueError)):
                decimal_value(bad, 3)
        with self.assertRaises(ValueError):
            calculate_total(D('-1'), D('1'))
        with self.assertRaises(ValueError):
            calculate_total(D('1'), D('-1'))
        self.assertEqual(calculate_total(D('10'), D('0')), D('0.00'))

    def test_words_and_declensions(self):
        expected = {D('37638.00'): 'Тридцать семь тысяч шестьсот тридцать восемь рублей 00 копеек',
                    D('0'): 'Ноль рублей 00 копеек',
                    D('1.01'): 'Один рубль 01 копейка',
                    D('2.12'): 'Два рубля 12 копеек',
                    D('5.25'): 'Пять рублей 25 копеек',
                    D('21001.02'): 'Двадцать одна тысяча один рубль 02 копейки',
                    D('1000001.11'): 'Один миллион один рубль 11 копеек'}
        for amount, words in expected.items():
            with self.subTest(amount=amount):
                self.assertEqual(amount_in_words(amount), words)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'test.db')
        self.db.initialize()
        self.service = CalculationService(self.db)
        with self.db.transaction() as c:
            self.tenant_id = create_tenant(c, name='Вымышленный арендатор')

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, year=2026, month=10, **fields):
        return self.service.create_calculation(self.tenant_id, year, month, **fields)

    def tariff(self, year=2026, month=10, rate='11.07', **options):
        return self.service.set_tariff(year, month, D(rate), **options)

    def test_draft_and_autosave_with_no_tariff(self):
        c = self.make(start_reading='100', end_reading='110')
        self.assertEqual(c['consumption'], D('10'))
        self.assertIsNone(c['total_amount'])
        self.assertFalse(c['is_ready'])
        self.assertIn('Тариф', ' '.join(self.service.validate(c['id']).errors))
        reopened = CalculationService(Database(self.db.path)).get_calculation(c['id'])
        self.assertEqual(reopened['end_reading'], D('110'))
        self.tariff()
        updated = self.service.get_calculation(c['id'])
        self.assertEqual(updated['total_amount'], D('110.70'))
        self.assertTrue(updated['is_ready'])

    def test_invalid_order_persisted_but_export_blocked(self):
        self.tariff()
        c = self.make(start_reading='200', end_reading='100')
        self.assertEqual(c['consumption'], D('-100'))
        self.assertFalse(c['is_ready'])
        self.assertIsNone(c['total_amount'])
        with self.assertRaises(ValueError):
            self.service.reserve_document(c['id'], 'act')
        with self.assertRaises(ValueError):
            self.service.update_calculation(c['id'], consumption=D('-1'), confirm_manual=True)

    def test_manual_overrides_reset_and_confirm(self):
        self.tariff()
        c = self.make(start_reading='100', end_reading='120')
        with self.assertRaises(ConfirmationRequired):
            self.service.update_calculation(c['id'], consumption='15')
        c = self.service.update_calculation(c['id'], consumption='15', confirm_manual=True)
        self.assertTrue(c['manual_consumption'])
        self.assertEqual(c['total_amount'], D('166.05'))
        c = self.service.update_calculation(c['id'], total_amount='200', confirm_manual=True)
        self.assertTrue(c['manual_amount'])
        c = self.service.update_calculation(c['id'], end_reading='130')
        self.assertFalse(c['manual_consumption'])
        self.assertFalse(c['manual_amount'])
        self.assertEqual(c['consumption'], D('30'))
        self.assertEqual(c['total_amount'], D('332.10'))

    def test_tariff_change_resets_manual_amount_but_preserves_manual_consumption(self):
        self.tariff()
        c = self.make(start_reading='10', end_reading='20')
        c = self.service.update_calculation(c['id'], consumption='13', confirm_manual=True)
        c = self.service.update_calculation(c['id'], total_amount='100', confirm_manual=True)
        with self.assertRaises(ConfirmationRequired) as ex:
            self.tariff(rate='2')
        self.assertEqual(ex.exception.code, 'tariff_recalculation')
        self.assertEqual(self.service.get_calculation(c['id'])['total_amount'], D('100'))
        self.tariff(rate='2', confirm_recalculation=True)
        c = self.service.get_calculation(c['id'])
        self.assertEqual(c['consumption'], D('13'))
        self.assertEqual(c['total_amount'], D('26.00'))
        self.assertTrue(c['manual_consumption'])
        self.assertFalse(c['manual_amount'])

    def test_previous_month_skips_missing_end_and_does_not_look_back_further(self):
        self.tariff()
        self.make(start_reading='50', end_reading='60')
        with self.assertRaises(ConfirmationRequired):
            self.make(start_reading='60')
        self.make(start_reading='60', confirm_overlap=True)
        november = self.make(2026, 11)
        self.assertEqual(november['start_reading'], D('60'))
        self.assertEqual(november['start_reading_source'], 'previous_month')
        december = self.make(2026, 12)
        self.assertIsNone(december['start_reading'])

    def test_initial_reading_and_manual_persisted(self):
        with self.db.transaction() as c:
            id2 = create_tenant(c, name='Другой', initial_reading=D('5.250'),
                                initial_reading_date=date(2026, 10, 1))
        c = self.service.create_calculation(id2, 2026, 10)
        self.assertEqual(c['start_reading_source'], 'initial')
        c = self.service.update_calculation(c['id'], start_reading='7')
        self.assertEqual(c['start_reading_source'], 'manual')
        self.assertEqual(self.service.get_calculation(c['id'])['start_reading'], D('7'))

    def test_reuse_manual_fallback_opt_in_and_explicit_confirmation(self):
        self.make(start_reading='10')
        self.assertIsNone(self.service.suggest_start_reading(self.tenant_id, 2026, 11).value)
        self.service.update_settings(reuse_manual_start_readings=True)
        proposal = self.service.suggest_start_reading(self.tenant_id, 2026, 11)
        self.assertEqual(proposal.value, D('10'))
        self.assertTrue(proposal.requires_confirmation)
        with self.assertRaises(ConfirmationRequired):
            self.make(2026, 11)
        c = self.make(2026, 11, confirm_manual_reuse=True)
        self.assertEqual(c['start_reading'], D('10'))

    def test_part_month_overlap_and_historical_protection(self):
        self.tariff()
        c = self.make(period_start='2026-10-15', start_reading='10', end_reading='20')
        with self.assertRaises(ConfirmationRequired):
            self.make(period_start='2026-10-16', start_reading='30')
        self.make(period_start='2026-10-16', start_reading='30', confirm_overlap=True)
        with self.assertRaises(ValueError):
            self.make(period_start='2026-10-15', period_end='2026-11-05')
        later = self.make(2026, 11)
        with self.assertRaises(ConfirmationRequired) as ex:
            self.service.update_calculation(c['id'], end_reading='21')
        self.assertEqual(ex.exception.code, 'historical_readings')
        self.assertIn(later['id'], ex.exception.affected_ids)
        self.service.update_calculation(c['id'], end_reading='21', confirm_history=True)
        self.assertEqual(self.service.get_calculation(later['id'])['start_reading'], D('20'))

    def test_document_sequences_status_revisions_duplicate_and_deletion(self):
        self.tariff()
        c = self.make(start_reading='1', end_reading='2')
        act = self.service.reserve_document(c['id'], 'act', document_date='2027-01-01')
        invoice = self.service.reserve_document(c['id'], 'invoice', document_date='2027-01-01')
        self.assertEqual((act['number'], invoice['number']), (1, 1))
        self.assertEqual(self.service.document_status(c['id'], 'act'), 'not_created')
        snapshot = self.service.prepare_document(c['id'], 'act')
        self.service.mark_document_generated(act['id'], expected_revision=snapshot['revision'])
        self.assertEqual(self.service.document_status(c['id'], 'act'), 'current')
        self.service.update_calculation(c['id'], end_reading='3')
        self.assertEqual(self.service.document_status(c['id'], 'act'), 'needs_update')
        self.assertEqual(self.service.reserve_document(c['id'], 'act')['number'], 1)
        self.assertEqual(self.service.next_document_number('act', '2027-01-01'), 2)
        self.assertEqual(self.service.next_document_number('invoice', '2026-01-01'), 1)
        with self.assertRaises(ConfirmationRequired):
            self.service.delete_calculation(c['id'])
        self.service.delete_calculation(c['id'], confirm_delete=True)
        self.assertEqual(self.service.next_document_number('act', '2027-01-01'), 1)

    def test_zero_export_and_bulk_last_ready(self):
        self.tariff()
        zero = self.make(start_reading='0', end_reading='0')
        self.assertFalse(self.service.validate(zero['id']).can_export(bulk=True))
        with self.assertRaises(ConfirmationRequired):
            self.service.reserve_document(zero['id'], 'act')
        self.service.reserve_document(zero['id'], 'act', confirm_zero=True)
        with self.db.transaction() as conn:
            id2 = create_tenant(conn, name='Ещё один')
        self.service.create_calculation(id2, 2026, 10, start_reading='1', end_reading='2')
        candidates = self.service.bulk_candidates(2026, 10)
        self.assertEqual(len(candidates['selected']), 1)
        self.assertIn({'tenant_id': self.tenant_id, 'reason': 'zero_consumption',
                       'calculation_id': zero['id']}, candidates['skipped'])

    def test_snapshot_guard_and_settings_invalidate(self):
        self.tariff()
        c = self.make(start_reading='1', end_reading='2')
        act = self.service.reserve_document(c['id'], 'act')
        snapshot = self.service.prepare_document(c['id'], 'act')
        self.service.update_settings(performer_name='ИП Тест')
        with self.assertRaises(ValueError):
            self.service.mark_document_generated(act['id'], expected_revision=snapshot['revision'])
        updated = self.service.prepare_document(c['id'], 'act')
        self.service.mark_document_generated(act['id'], expected_revision=updated['revision'])
        self.service.update_tenant(self.tenant_id, name='Новое имя')
        self.assertEqual(self.service.document_status(c['id'], 'act'), 'needs_update')


if __name__ == '__main__':
    unittest.main()
