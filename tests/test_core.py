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
            self.assertEqual(
                amount_in_words(D('123456789012345.67')),
                'Сто двадцать три триллиона четыреста пятьдесят шесть миллиардов '
                'семьсот восемьдесят девять миллионов двенадцать тысяч '
                'триста сорок пять рублей 67 копеек',
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
        self.assertEqual(self.service.get_calculation(later['id'])['start_reading'], D('21'))

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


    def test_explicit_zero_tariff_is_not_missing(self):
        self.tariff(rate='0')
        c = self.make(start_reading='1', end_reading='2')
        self.assertTrue(c['is_ready'])
        self.assertEqual(c['total_amount'], D('0.00'))
        self.assertFalse(self.service.validate(c['id']).zero_consumption)
        self.service.reserve_document(c['id'], 'invoice', document_date='2026-12-01')

    def test_manual_numbers_duplicate_after_confirmation_and_year_reset(self):
        self.tariff()
        a = self.make(start_reading='1', end_reading='2')
        b = self.make(start_reading='3', end_reading='4', confirm_overlap=True)
        doc = self.service.reserve_document(a['id'], 'act', document_date='2027-01-01', number=19)
        self.assertEqual(self.service.next_document_number('act', '2027-12-31'), 20)
        with self.assertRaises(ConfirmationRequired) as ex:
            self.service.reserve_document(b['id'], 'act', document_date='2027-01-03', number=19)
        self.assertEqual(ex.exception.code, 'duplicate_document_number')
        duplicate = self.service.reserve_document(b['id'], 'act', document_date='2027-01-03',
                                                  number=19, confirm_duplicate=True)
        self.assertEqual(self.service.document_number_duplicates('act', 19, '2027-01-01',
                                                                  exclude_id=duplicate['id']), (doc['id'],))
        self.assertEqual(self.service.reserve_document(b['id'], 'act')['number'], 19)
        self.assertEqual(self.service.next_document_number('act', '2028-01-01'), 1)

    def test_bulk_uses_previous_ready_when_latest_draft_and_skips_latest_ready_zero(self):
        self.tariff()
        first = self.make(start_reading='0', end_reading='3')
        self.make(start_reading='4', confirm_overlap=True)
        selected = self.service.bulk_candidates(2026, 10)['selected']
        self.assertEqual([c['id'] for c in selected], [first['id']])
        zero = self.make(start_reading='5', end_reading='5', confirm_overlap=True)
        result = self.service.bulk_candidates(2026, 10)
        self.assertEqual(result['selected'], [])
        self.assertEqual(result['skipped'][0]['calculation_id'], zero['id'])

    def test_invalid_edit_rolls_back_and_manual_amount_cannot_be_negative(self):
        self.tariff()
        c = self.make(start_reading='10', end_reading='20')
        with self.assertRaises(ValueError):
            self.service.update_calculation(c['id'], end_reading=-1)
        with self.assertRaises(ValueError):
            self.service.update_calculation(c['id'], total_amount='-5', confirm_manual=True)
        with self.assertRaises(ValueError):
            self.service.update_calculation(c['id'], end_reading='20.0001')
        unchanged = self.service.get_calculation(c['id'])
        self.assertEqual(unchanged['end_reading'], D('20'))
        self.assertEqual(unchanged['total_amount'], D('110.70'))


    def test_tariff_recalculation_keeps_user_edit_order_for_autofill_and_bulk(self):
        self.tariff(rate='1')
        first = self.make(start_reading='100', end_reading='110')
        second = self.make(start_reading='200', end_reading='220', confirm_overlap=True)
        original_times = {c['id']: c['updated_at'] for c in (first, second)}
        self.assertEqual(self.service.suggest_start_reading(self.tenant_id, 2026, 11).value, D('220'))
        self.assertEqual(self.service.bulk_candidates(2026, 10)['selected'][0]['id'], second['id'])
        with self.assertRaises(ConfirmationRequired):
            self.tariff(rate='2')
        self.tariff(rate='2', confirm_recalculation=True)
        for calc in (first, second):
            updated = self.service.get_calculation(calc['id'])
            self.assertEqual(updated['updated_at'], original_times[calc['id']])
        self.assertEqual(self.service.suggest_start_reading(self.tenant_id, 2026, 11).value, D('220'))
        self.assertEqual(self.service.bulk_candidates(2026, 10)['selected'][0]['id'], second['id'])
        self.assertEqual(self.make(2026, 11)['start_reading'], D('220'))

    def test_validation_uses_exact_decimal_arithmetic_under_low_precision(self):
        from decimal import localcontext
        self.tariff(rate='1')
        with localcontext() as ctx:
            ctx.prec = 6
            calc = self.make(start_reading='0.125', end_reading='1234567.126')
            self.assertEqual(calc['consumption'], D('1234567.001'))
            self.assertEqual(calc['total_amount'], D('1234567.00'))
            result = self.service.validate(calc['id'])
            self.assertTrue(result.ready, result.errors)
            self.assertTrue(result.can_export())
            self.service.reserve_document(calc['id'], 'act', document_date='2026-11-01')

    def test_history_edit_recalculates_automatic_start_and_invalidates_document(self):
        self.tariff(rate='1')
        self.tariff(year=2026, month=11, rate='1')
        october = self.make(start_reading='100', end_reading='110')
        november = self.make(2026, 11, end_reading='120')
        self.assertEqual(november['start_reading_source'], 'previous_month')
        doc = self.service.reserve_document(november['id'], 'act')
        prepared = self.service.prepare_document(november['id'], 'act')
        self.service.mark_document_generated(doc['id'], expected_revision=prepared['revision'])
        # Создаём свежий снимок уже после успешной отметки генерации:
        # document.updated_at не изменится от последующей инвалидации.
        prepared = self.service.prepare_document(november['id'], 'act')
        self.assertEqual(self.service.document_status(november['id'], 'act'), 'current')
        with self.assertRaises(ConfirmationRequired) as exc:
            self.service.update_calculation(october['id'], end_reading='115')
        self.assertEqual(exc.exception.affected_ids, (november['id'],))
        self.assertEqual(self.service.get_calculation(october['id'])['end_reading'], D('110'))
        self.service.update_calculation(october['id'], end_reading='115', confirm_history=True)
        updated = self.service.get_calculation(november['id'])
        self.assertEqual(updated['start_reading'], D('115'))
        self.assertEqual(updated['consumption'], D('5'))
        self.assertEqual(updated['total_amount'], D('5.00'))
        self.assertEqual(updated['updated_at'], november['updated_at'])
        self.assertTrue(updated['is_ready'])
        self.assertEqual(self.service.document_status(november['id'], 'act'), 'needs_update')
        # Автокоррекция сохраняет updated_at, но должна отклонять PDF,
        # подготовленный до изменения источника данных.
        with self.assertRaises(ValueError):
            self.service.mark_document_generated(doc['id'], expected_revision=prepared['revision'])
        self.assertEqual(CalculationService(Database(self.db.path)).get_calculation(
            november['id'])['start_reading'], D('115'))

    def test_history_edit_does_not_replace_manual_start(self):
        self.tariff(rate='1')
        self.tariff(year=2026, month=11, rate='1')
        october = self.make(start_reading='100', end_reading='110')
        november = self.make(2026, 11, start_reading='110', end_reading='125')
        self.assertEqual(november['start_reading_source'], 'manual')
        doc = self.service.reserve_document(november['id'], 'act')
        prepared = self.service.prepare_document(november['id'], 'act')
        self.service.mark_document_generated(doc['id'], expected_revision=prepared['revision'])
        self.service.update_calculation(october['id'], end_reading='115', confirm_history=True)
        updated = self.service.get_calculation(november['id'])
        self.assertEqual(updated['start_reading'], D('110'))
        self.assertEqual(updated['total_amount'], D('15.00'))
        self.assertEqual(self.service.document_status(november['id'], 'act'), 'current')

    def test_deleting_historical_source_clears_auto_start_not_manual(self):
        self.tariff(rate='1')
        self.tariff(year=2026, month=11, rate='1')
        october = self.make(start_reading='100', end_reading='110')
        auto = self.make(2026, 11, end_reading='120')
        manual = self.make(2026, 11, start_reading='110', end_reading='120',
                           confirm_overlap=True)
        self.service.delete_calculation(october['id'], confirm_delete=True, confirm_history=True)
        updated = self.service.get_calculation(auto['id'])
        self.assertIsNone(updated['start_reading'])
        self.assertEqual(updated['start_reading_source'], 'previous_month')
        self.assertIsNone(updated['consumption'])
        self.assertFalse(updated['is_ready'])
        self.assertEqual(self.service.get_calculation(manual['id'])['start_reading'], D('110'))

    def test_tenant_initial_reading_validation_keeps_original_on_error(self):
        self.service.update_tenant(self.tenant_id, initial_reading='5.125',
                                   initial_reading_date='2026-10-01')
        original = self.service.create_calculation(self.tenant_id, 2026, 10)
        self.assertEqual(original['start_reading'], D('5.125'))
        with self.assertRaises(ValueError):
            self.service.update_tenant(self.tenant_id, initial_reading='-10')
        with self.assertRaises(ValueError):
            self.service.update_tenant(self.tenant_id, initial_reading='NaN')
        with self.assertRaises((ValueError, TypeError)):
            self.service.update_tenant(self.tenant_id, initial_reading=0.1)
        with self.db.connection() as conn:
            from app.data.database import get_tenant
            self.assertEqual(get_tenant(conn, self.tenant_id)['initial_reading'], D('5.125'))
        self.service.update_tenant(self.tenant_id, initial_reading='0')
        with self.db.connection() as conn:
            from app.data.database import get_tenant
            self.assertEqual(get_tenant(conn, self.tenant_id)['initial_reading'], D('0'))
        with self.assertRaises(ValueError):
            self.service.create_tenant(name='Тест', initial_reading='-1')
        self.assertEqual(self.service.create_tenant(name='Тест', initial_reading='0.125')['initial_reading'],
                         D('0.125'))

    def test_bulk_skip_reasons_distinguish_missing_record_tariff_and_readings(self):
        with self.db.transaction() as conn:
            no_calculation = create_tenant(conn, name='Нет расчётов')
            incomplete = create_tenant(conn, name='Неполные показания')
        no_tariff = self.make(start_reading='10', end_reading='20')
        result = self.service.bulk_candidates(2026, 10)
        by_id = {entry['tenant_id']: entry for entry in result['skipped']}
        self.assertEqual(by_id[no_calculation]['reason'], 'no_calculation')
        self.assertEqual(by_id[self.tenant_id]['reason'], 'missing_tariff')
        self.assertIn('Тариф', ' '.join(by_id[self.tenant_id]['errors']))
        self.assertEqual(by_id[self.tenant_id]['calculation_id'], no_tariff['id'])
        self.tariff(rate='1')
        self.service.create_calculation(incomplete, 2026, 10, start_reading='10')
        result = self.service.bulk_candidates(2026, 10)
        by_id = {entry['tenant_id']: entry for entry in result['skipped']}
        self.assertEqual(by_id[incomplete]['reason'], 'incomplete_readings')
        self.assertTrue(any('Конечное' in e for e in by_id[incomplete]['errors']))
        self.assertEqual(by_id[no_calculation]['reason'], 'no_calculation')


if __name__ == '__main__':
    unittest.main()
