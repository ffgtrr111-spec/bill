"""Local desktop session permissions, shared by dashboards and billing commands."""
import hashlib
import hmac
import inspect
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from datetime import date

from money import money, line_amount, sum_money
from offers import offer_taxes


# Store a verifier rather than the password. This is an application access gate;
# users who can edit the program/database need separate OS account restrictions.
_ADMIN_DIGEST = "2a28a012069a3b1157afe78c07537a6416f628b3987d2ae2d57f4c7d9c93764c"


def credential_path():
    return Path.home() / ".cloth_shop_billing" / "admin-credentials.json"


def verify_admin_password(password, path=None):
    path = Path(path) if path is not None else credential_path()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["digest"])
        iterations = record["iterations"]
        if (type(iterations) is not int or not 200000 <= iterations <= 1000000
                or len(salt) != 32 or len(expected) != 32):
            return False
    except FileNotFoundError:
        salt, expected, iterations = b"billing-desktop-admin-v1", bytes.fromhex(_ADMIN_DIGEST), 200000
    except (OSError, ValueError, KeyError, TypeError):
        # A damaged credential file must never re-enable the shared password.
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def change_admin_password(current, new, path=None):
    path = Path(path) if path is not None else credential_path()
    if not verify_admin_password(current, path):
        raise ValueError("The current password is incorrect.")
    if not 12 <= len(new) <= 1024:
        raise ValueError("Use a password with 12 to 1024 characters.")
    if current == new:
        raise ValueError("Choose a different password.")
    salt = secrets.token_bytes(32)
    record = dict(salt=salt.hex(), iterations=600000,
                  digest=hashlib.pbkdf2_hmac("sha256", new.encode("utf-8"), salt, 600000).hex())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".admin-", delete=False) as stream:
            temporary = stream.name
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


class AccessSession:
    def __init__(self, password_path=None):
        self.password_path = password_path
        self._failures = 0
        self._retry_at = 0
        self._admin = False
        self.receipt_ids = set()

    @property
    def is_admin(self):
        return self._admin

    @property
    def retry_after(self):
        return max(0, int(self._retry_at - time.monotonic() + 0.999))

    def login(self, password):
        if self.retry_after:
            return False
        if not verify_admin_password(password, self.password_path):
            self._failures += 1
            if self._failures >= 5:
                self._retry_at = time.monotonic() + 30
                self._failures = 0
            return False
        self._failures = 0
        self._retry_at = 0
        self._admin = True
        self.receipt_ids.clear()
        return True

    def change_password(self, current, new):
        if not self.is_admin:
            raise PermissionError("Admin access is required.")
        change_admin_password(current, new, self.password_path)

    def logout(self):
        self._admin = False
        self.receipt_ids.clear()


class RoleDatabase:
    """Check permissions when commands run, including callbacks saved before logout."""
    _EMPLOYEE_METHODS = frozenset({
        "get_categories", "get_subtypes", "get_items", "get_item_by_barcode",
        "get_item_by_id", "find_item_by_name", "get_customer_by_phone",
        "add_customer", "quote_offers", "get_customer_phone_suggestions",
    })

    def __init__(self, database, session):
        self._database = database
        self.session = session

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        target = getattr(self._database, name)
        if not callable(target):
            if not self.session.is_admin:
                raise PermissionError("Admin access is required.")
            return target

        def checked(*args, **kwargs):
            if not self.session.is_admin and name not in self._EMPLOYEE_METHODS:
                raise PermissionError("Admin access is required.")
            return target(*args, **kwargs)
        return checked

    def validate_employee_cart(self, cart, paid_now, bill_date):
        if self.session.is_admin:
            return
        lines = []
        net_lines = []
        active_categories = {c["id"] for c in self._database.get_categories()}
        offers = self._database.quote_offers(cart)
        for row, offer in zip(cart, offers):
            item = self._database.get_item_by_id(row["item_id"])
            if not item or not item["active"] or item["category_id"] not in active_categories:
                raise PermissionError("Employees can only bill active catalog items.")
            qty = row["qty"]
            if qty <= 0 or int(qty) != qty:
                raise PermissionError("Enter a whole quantity greater than zero.")
            amount = line_amount(qty, money(item["rate"]))
            if (money(row["rate"]) != money(item["rate"])
                    or money(row["amount"]) != amount
                    or float(row["gst_rate"]) != float(item["gst_rate"])
                    or money(row["discount"]) != 0):
                raise PermissionError("Only admins can change prices, GST or discounts. Refresh the bill if catalog prices changed.")
            if (money(row.get('offer_discount',0)) != offer['offer_discount']
                    or row.get('offer_id') != offer['offer_id']):
                raise PermissionError('Offers changed. Click Refresh offers and check the new total.')
            net = money(amount-offer['offer_discount'])
            lines.append(net)
            net_lines.append(dict(net=net,gst_rate=item["gst_rate"],offer_id=offer["offer_id"]))
        total = sum_money(lines + offer_taxes(net_lines))
        if money(paid_now) != total:
            raise PermissionError("Employees must collect full payment. Partial payment and credit require an admin.")
        if bill_date != date.today().isoformat():
            raise PermissionError("Only admins can change the bill date.")

    def save_bill(self, *args, **kwargs):
        if not self.session.is_admin:
            bound = inspect.signature(self._database.save_bill).bind(*args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            # Enforce canonical pricing in the same transaction that saves the bill.
            values['employee_pricing'] = True
            try:
                result = self._database.save_bill(**values)
            except ValueError as exc:
                raise PermissionError(str(exc)) from exc
            self.session.receipt_ids.add(result[0])
            return result
        return self._database.save_bill(*args, **kwargs)

    def get_bill(self, bill_id):
        if not self.session.is_admin and bill_id not in self.session.receipt_ids:
            raise PermissionError("Sales history requires admin access.")
        return self._database.get_bill(bill_id)
