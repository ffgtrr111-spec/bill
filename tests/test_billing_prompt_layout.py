"""Exercise payment confirmation and small-screen customer/expense navigation."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Billing_App/app'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QDate, QEvent, QTimer, QRect, QPoint
from PySide6.QtWidgets import QApplication, QComboBox, QCompleter, QInputDialog, QMessageBox, QScrollArea
from PySide6.QtTest import QTest
from access import AccessSession, RoleDatabase
from billing_tab import BillingTab
from database import Database
from exchanges_tab import ExchangeCart
from receipt import _qr_amount, _upi_payment_uri, build_receipt_html
from theme import STYLESHEET
import main


class PromptLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(STYLESHEET)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Database(str(Path(folder.name) / 'shop.db'))
        self.category = self.db.get_categories()[0]
        self.item = self.db.add_item('Everyday shirt', self.category['id'], None, 'EX123', '', '', 100, 50)
        self.session = AccessSession(Path(folder.name) / 'password.json')
        self.role = RoleDatabase(self.db, self.session)
        for name in ('warning', 'critical', 'information'):
            mocked = patch.object(QMessageBox, name)
            mocked.start()
            self.addCleanup(mocked.stop)

    def make_bill(self, admin=False):
        if admin:
            self.assertTrue(self.session.login('1852j'))
        tab = BillingTab(self.role)
        self.addCleanup(tab.close)
        tab._add_to_cart(self.item, 'Everyday shirt', self.category['name'], 1, 100, 5)
        return tab

    def test_cancel_keeps_draft_and_does_not_save(self):
        tab = self.make_bill()
        tab.payment_mode_combo.setCurrentText('Card')
        with patch('billing_tab.QInputDialog.getItem', return_value=('UPI', False)), \
             patch.object(self.role, 'save_bill', wraps=self.role.save_bill) as save:
            tab.complete_btn.click()
        save.assert_not_called()
        self.assertEqual(tab.payment_mode_combo.currentText(), 'Card')
        self.assertEqual(len(tab.cart), 1)
        with self.db._conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM bills').fetchone()[0], 0)

    def test_real_prompt_saves_selected_payment_for_employee(self):
        tab = self.make_bill()
        def select_upi():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, QInputDialog)
            dialog.findChild(QComboBox).setCurrentText('UPI')
            dialog.accept()
        QTimer.singleShot(0, select_upi)
        with patch('billing_tab.ReceiptDialog') as receipt:
            tab.complete_btn.click()
        receipt.assert_called_once()
        with self.db._conn() as conn:
            bill = conn.execute('SELECT payment_mode,total FROM bills').fetchone()
            payment = conn.execute('SELECT payment_mode,amount FROM payments').fetchone()
        self.assertEqual((bill['payment_mode'], bill['total']), ('UPI', 105))
        self.assertEqual((payment['payment_mode'], payment['amount']), ('UPI', 105))
        self.assertEqual(tab.cart, [])

    def test_admin_partial_and_credit_keep_selected_mode(self):
        tab = self.make_bill(admin=True)
        for mode, amount in [('Card', 30), ('Other', 0)]:
            with self.subTest(mode=mode):
                if not tab.cart:
                    tab._add_to_cart(self.item, 'Everyday shirt', self.category['name'], 1, 100, 5)
                tab.payment_amount_input.setValue(amount)
                with patch('billing_tab.QInputDialog.getItem', return_value=(mode, True)), \
                     patch('billing_tab.ReceiptDialog'):
                    tab.complete_btn.click()
        with self.db._conn() as conn:
            bills = conn.execute('SELECT id,payment_mode,total FROM bills ORDER BY id').fetchall()
            payments = conn.execute('SELECT bill_id,payment_mode,amount FROM payments').fetchall()
        self.assertEqual([(b['payment_mode'], b['total']) for b in bills], [('Card', 105), ('Other', 105)])
        self.assertEqual([(p['bill_id'], p['payment_mode'], p['amount']) for p in payments], [(bills[0]['id'], 'Card', 30)])

    def test_split_payment_records_both_tenders_and_qr_uses_only_upi(self):
        tab = self.make_bill()
        with patch('billing_tab.QInputDialog.getItem', return_value=('Cash + UPI', True)), \
             patch('billing_tab.QInputDialog.getDouble', return_value=(40.12, True)), \
             patch.object(tab, '_confirm_split_upi', side_effect=[False, True]) as confirm, \
             patch('billing_tab.ReceiptDialog') as receipt:
            tab.complete_btn.click()
            self.assertTrue(tab.cart)
            self.assertEqual(self.db.get_item_by_id(self.item)['stock_qty'], 50)
            receipt.assert_not_called()
            tab.complete_btn.click()
        confirm.assert_called_with(40.12, 64.88)
        bill, lines = receipt.call_args.args[:2]
        self.assertEqual((bill['payment_mode'], bill['paid_amount'], bill['balance']),
                         ('Cash + UPI', 105, 0))
        with self.db._conn() as conn:
            payments = conn.execute('SELECT payment_mode, amount FROM payments ORDER BY id').fetchall()
        self.assertEqual([tuple(payment) for payment in payments], [('Cash', 40.12), ('UPI', 64.88)])
        reopened, _ = self.db.get_bill(bill['id'])
        self.assertIn('am=64.88', _upi_payment_uri(reopened, _qr_amount(reopened)))
        self.assertIn('₹64.88', build_receipt_html(reopened, lines))
        self.assertEqual(self.db.get_item_by_id(self.item)['stock_qty'], 49)

    def test_credit_choice_leaves_the_bill_unpaid(self):
        tab = self.make_bill(admin=True)
        with patch('billing_tab.QInputDialog.getItem', return_value=('Credit', True)), \
             patch('billing_tab.ReceiptDialog') as receipt:
            tab.complete_btn.click()
        bill = receipt.call_args.args[0]
        self.assertEqual((bill['payment_mode'], bill['paid_amount'], bill['balance']), ('Credit', 0, 105))
        self.assertEqual(_qr_amount(bill), 0)

    def test_today_rolls_forward_and_manual_backdate_is_preserved(self):
        with patch('billing_tab.QDate', wraps=QDate) as clock:
            clock.currentDate.return_value = QDate(2026, 11, 20)
            tab = self.make_bill(admin=True)
            self.assertEqual(tab.bill_date_input.date(), QDate(2026, 11, 20))
            clock.currentDate.return_value = QDate(2026, 11, 21)
            with patch('billing_tab.ReceiptDialog') as receipt:
                tab._complete_bill()
            self.assertEqual(receipt.call_args.args[0]['bill_date'], '2026-11-21')

            tab.bill_date_input.setDate(QDate(2026, 11, 19))
            clock.currentDate.return_value = QDate(2026, 11, 22)
            tab._add_to_cart(self.item, 'Everyday shirt', self.category['name'], 1, 100, 5)
            with patch('billing_tab.ReceiptDialog') as receipt:
                tab._complete_bill()
            self.assertEqual(receipt.call_args.args[0]['bill_date'], '2026-11-19')

            tab.today_btn.click()
            self.assertEqual(tab.bill_date_input.date(), QDate(2026, 11, 22))
            self.assertEqual(tab.bill_date_input.maximumDate(), QDate(2026, 11, 22))

    def test_repeated_suggestion_refresh_releases_old_models(self):
        tab = self.make_bill()
        self.db.add_customer('Sample', '5551234567')
        original_count = len(tab.findChildren(QCompleter))
        for _ in range(30):
            tab._refresh_phone_completer()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertEqual(len(tab.findChildren(QCompleter)), original_count)
        self.assertEqual(tab.phone_input.completer().model().rowCount(), 1)

    def test_exchange_button_uses_confirmed_payment_type(self):
        self.assertTrue(self.session.login('1852j'))
        bid, _ = self.db.save_bill(None, [dict(item_id=self.item, name='Everyday shirt',
            category=self.category['name'], quantity=1, rate=100, subtotal=100,
            gst_rate=5, gst_amount=5)], 100, 0, 0, 105, 'Cash',
            taxable_amount=100, gst_rate=5, gst_amount=5, initial_payment_amount=105)
        line_id = self.db.get_bill(bid)[1][0]['id']
        replacement = self.db.add_item('Replacement', self.category['id'], None, '', '', '', 200, 10)
        quote = self.db.quote_exchange(bid, {line_id: 1})
        done = []
        cart = ExchangeCart(self.role, bid, {line_id: 1}, quote, lambda: done.append(True))
        self.addCleanup(cart.close)
        cart._add_to_cart(replacement, 'Replacement', self.category['name'], 1, 200, 5)
        with patch('billing_tab.QInputDialog.getItem', return_value=('Card', False)), \
             patch.object(self.role, 'save_exchange', wraps=self.role.save_exchange) as save:
            cart.complete_btn.click()
            save.assert_not_called()
        with patch('billing_tab.QInputDialog.getItem', return_value=('UPI', True)), \
             patch('exchanges_tab.ReceiptDialog'):
            cart.complete_btn.click()
        self.assertEqual(done, [True])
        with self.db._conn() as conn:
            bill = conn.execute('SELECT id, payment_mode FROM bills ORDER BY id DESC LIMIT 1').fetchone()
            payment = conn.execute('SELECT payment_mode, amount FROM payments WHERE bill_id=?', (bill['id'],)).fetchone()
        self.assertEqual((bill['payment_mode'], payment['payment_mode'], payment['amount']), ('UPI', 'UPI', 105))

    def test_empty_cart_shows_original_warning_without_prompt(self):
        tab = BillingTab(self.role)
        self.addCleanup(tab.close)
        with patch('billing_tab.QInputDialog.getItem') as prompt, patch.object(QMessageBox, 'warning') as warning:
            tab.complete_btn.click()
        prompt.assert_not_called()
        warning.assert_called_once()

    def test_customer_and_expenses_tables_remain_accessible_on_small_screens(self):
        cid = self.db.add_customer('Sample Customer', '123456')
        for i in range(12):
            self.db.save_bill(cid, [dict(name='Everyday shirt', quantity=1, rate=100, subtotal=100)],
                              100, 0, 0, 100, 'Cash')
            self.db.add_expense('Rent', 10 + i, description='Sample expense')
        with patch.object(main, 'Database', return_value=self.db):
            window = main.MainWindow()
        self.addCleanup(window.close)
        self.assertTrue(window.session.login('1852j'))
        window._build_dashboard()
        window.show()
        customer = window.customers_tab
        expense = window.expenses_tab
        for width, height in ((640, 480), (800, 600), (1366, 768)):
            with self.subTest(size=(width, height)):
                window.resize(width, height)
                window.tabs.setCurrentWidget(customer)
                customer._load_customer(cid)
                QTest.qWait(50)
                area = customer.findChild(QScrollArea)
                self.assertGreaterEqual(customer.history_table.height(), 190)
                self.assertGreaterEqual(customer.wishlist_table.height(), 150)
                self.assertGreaterEqual(customer.history_table.columnWidth(0), 140)
                self.assertGreaterEqual(customer.history_table.columnWidth(1), 110)
                self.assertGreaterEqual(customer.history_table.rowHeight(0), customer.history_table.cellWidget(0, 4).sizeHint().height())
                area.ensureWidgetVisible(customer.history_table.cellWidget(0, 4), 5, 5)
                self.assertTrue(customer.history_table.cellWidget(0, 4).isVisibleTo(area))
                customer.history_table.horizontalScrollBar().setValue(customer.history_table.horizontalScrollBar().maximum())
                view_button = customer.history_table.cellWidget(0, 4)
                button_rect = QRect(view_button.mapTo(customer.history_table.viewport(), QPoint()), view_button.size())
                self.assertTrue(customer.history_table.viewport().rect().contains(button_rect))
                self.assertGreater(customer.history_table.verticalScrollBar().maximum(), 0)
                if width == 640:
                    self.assertGreater(area.verticalScrollBar().maximum(), 0)
                    self.assertGreater(area.horizontalScrollBar().maximum(), 0)
                window.tabs.setCurrentWidget(expense)
                expense.period.preset.setCurrentText('All time')
                expense.refresh()
                QTest.qWait(50)
                area = expense.findChild(QScrollArea)
                self.assertGreaterEqual(expense.expense_table.height(), 220)
                self.assertGreaterEqual(expense.expense_table.columnWidth(4), 220)
                self.assertGreaterEqual(expense.expense_table.rowHeight(0), expense.expense_table.cellWidget(0, 7).sizeHint().height())
                area.ensureWidgetVisible(expense.expense_table, 5, 5)
                self.assertTrue(expense.expense_table.isVisibleTo(area))
                expense.expense_table.horizontalScrollBar().setValue(expense.expense_table.horizontalScrollBar().maximum())
                delete_button = expense.expense_table.cellWidget(0, 7)
                button_rect = QRect(delete_button.mapTo(expense.expense_table.viewport(), QPoint()), delete_button.size())
                self.assertTrue(expense.expense_table.viewport().rect().contains(button_rect))
                self.assertGreater(expense.expense_table.verticalScrollBar().maximum(), 0)
                if width <= 800:
                    self.assertGreater(expense.expense_table.horizontalScrollBar().maximum(), 0)
                if height == 480:
                    self.assertGreater(area.verticalScrollBar().maximum(), 0)
        self.assertEqual(customer.history_table.rowCount(), 12)
        self.assertEqual(expense.expense_table.rowCount(), 12)
        with patch.object(customer, '_view_bill_by_id') as view:
            customer.history_table.cellWidget(0, 4).click()
        view.assert_called_once_with(customer._history_bill_ids[0])
        with patch('expenses_tab.confirm_delete_password', return_value=False) as confirm:
            expense.expense_table.cellWidget(0, 7).click()
        confirm.assert_called_once()
        self.assertEqual(self.db.expense_page(None, None)['count'], 12)


if __name__ == '__main__':
    unittest.main()
