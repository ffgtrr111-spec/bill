"""Password rotation and bounded inventory rendering with synthetic local data."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Billing_App/app'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtTest import QTest
from PySide6.QtCore import QCoreApplication, QEvent
from access import AccessSession, RoleDatabase, verify_admin_password
from database import Database
from inventory_tab import InventoryTab
from main import PasswordDialog


class PasswordTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'private' / 'admin.json'
        self.session = AccessSession(self.path)

    def test_rotation_persists_and_rejects_old_password_in_every_verifier(self):
        self.assertTrue(self.session.login('1852j'))
        self.session.change_password('1852j', 'private shop password')
        self.assertFalse(verify_admin_password('1852j', self.path))
        self.assertTrue(verify_admin_password('private shop password', self.path))
        self.assertTrue(AccessSession(self.path).login('private shop password'))
        record = json.loads(self.path.read_text())
        self.assertNotIn('private shop password', self.path.read_text())
        self.assertEqual(record['iterations'], 600000)
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.glob('.admin-*')), [])

    def test_failed_replacement_keeps_previous_password(self):
        self.session.login('1852j')
        self.session.change_password('1852j', 'private shop password')
        before = self.path.read_bytes()
        with patch('access.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.session.change_password('private shop password', 'another long password')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('.admin-*')), [])

    def test_rejects_weak_wrong_current_and_logged_out_changes(self):
        with self.assertRaises(PermissionError):
            self.session.change_password('1852j', 'long enough password')
        self.session.login('1852j')
        for current, new in [('wrong', 'long enough password'), ('1852j', 'short')]:
            with self.assertRaises(ValueError):
                self.session.change_password(current, new)
        self.assertFalse(self.path.exists())

    def test_corrupt_credentials_never_fall_back_to_shared_password(self):
        self.path.parent.mkdir()
        for record in ['not json', '{}', '{"salt": 42}', 'null',
                       json.dumps(dict(salt='aa'*32, digest='bb'*32, iterations=10**9))]:
            self.path.write_text(record)
            self.assertFalse(verify_admin_password('1852j', self.path))

    def test_repeated_failures_cool_down_without_sleeping(self):
        with patch('access.time.monotonic', return_value=100):
            for _ in range(5):
                self.assertFalse(self.session.login('wrong'))
            self.assertEqual(self.session.retry_after, 30)
            self.assertFalse(self.session.login('1852j'))
        with patch('access.time.monotonic', return_value=131):
            self.assertTrue(self.session.login('1852j'))
            self.assertEqual(self.session.retry_after, 0)


class InventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Database(str(Path(folder.name) / 'test.db'))
        self.category = self.db.get_categories()[0]['id']
        with self.db._conn() as conn:
            conn.executemany('INSERT INTO items(name,category_id,rate) VALUES (?,?,?)',
                             [('Same name', self.category, 10)] * 405)
        self.session = AccessSession(Path(folder.name) / 'password.json')
        self.session.login('1852j')
        self.access = RoleDatabase(self.db, self.session)

    def dispose_widget(self, widget):
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_paging_covers_duplicate_names_and_clamps_after_delete(self):
        all_ids = []
        for offset in (0, 200, 400):
            total, rows = self.db.inventory_page(self.category, offset=offset)
            self.assertEqual(total, 405)
            self.assertLessEqual(len(rows), 200)
            all_ids.extend(row['id'] for row in rows)
        self.assertEqual(all_ids, sorted(set(all_ids)))
        self.assertEqual(len(all_ids), 405)
        with self.db._conn() as conn:
            conn.execute('DELETE FROM items WHERE id>?', (all_ids[399],))
        total, rows = self.db.inventory_page(self.category, offset=400)
        self.assertEqual(total, 400)
        self.assertEqual(rows[0]['id'], all_ids[200])
        self.assertEqual(len(self.db.get_items()), 400)

    def test_widget_bounds_rows_debounces_search_and_ignores_logout_timer(self):
        tab = InventoryTab(self.access)
        self.addCleanup(self.dispose_widget, tab)
        self.assertEqual(tab.items_table.rowCount(), 200)
        tab.pager.next.click()
        self.assertEqual(tab.pager.offset, 200)
        tab.pager.next.click()
        self.assertEqual(tab.items_table.rowCount(), 5)
        with patch.object(self.db, 'inventory_page', wraps=self.db.inventory_page) as query:
            for text in ('n', 'no', 'no match'):
                tab.item_search_input.setText(text)
            self.assertEqual(query.call_count, 0)
            QTest.qWait(250)
            self.assertEqual(query.call_count, 1)
            self.assertEqual(tab.items_table.rowCount(), 0)
            self.assertEqual(tab.pager.offset, 0)
            tab.item_search_input.setText('Same')
            self.session.logout()
            QTest.qWait(250)
            self.assertEqual(query.call_count, 1)

    def test_password_dialog_writes_only_matching_confirmed_password(self):
        dialog = PasswordDialog(self.session)
        self.addCleanup(self.dispose_widget, dialog)
        dialog.current.setText('1852j')
        dialog.new.setText('private shop password')
        with patch('main.QMessageBox.warning') as warning:
            dialog._save()
        warning.assert_called_once()
        self.assertFalse(Path(self.session.password_path).exists())
        dialog.confirm.setText('private shop password')
        dialog._save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertTrue(verify_admin_password('private shop password', self.session.password_path))


if __name__ == '__main__':
    unittest.main()
