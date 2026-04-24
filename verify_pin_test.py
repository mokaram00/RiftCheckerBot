"""
اختبار سريع لـ Epic verify-pin فقط.

يستخدم ``epic_auth.verify_epic_content_control_pin_sync`` — طلب HTTP عبر **curl_cffi**
(متصفح مزيف) لتقليل حظر Cloudflare على store.epicgames.com.

1) انسخ من users/<telegram_id>.json حقول device_auth (account_id, device_id, secret).
2) ضع الـ PIN المكوّن من 6 أرقام.
3) من مجلد المشروع:

   python verify_pin_test.py

أو:

   python verify_pin_test.py 606060

متغير بيئة اختياري: ``EPIC_IMPERSONATE=chrome110`` (أو chrome131) إذا ظهر حظر Cloudflare.
"""

from __future__ import annotations

import os
import sys

from epic_auth import (
    device_auth_oauth_access_token_bearer_sync,
    verify_epic_content_control_pin_sync,
)

# عدّل هنا قبل التشغيل
DEVICE_AUTH: dict[str, str] = {
    "account_id": "f7aa8e6406e2450b933f5f45fdaa9758",
    "device_id": "e2bbca389df5481084063bcb43ccc864",
    "secret": "MDXJEJ6AVK46YIHF2XKGW6TLZ4TT5XIQ",
}


def main() -> None:
    pin = (sys.argv[1] if len(sys.argv) > 1 else input("PIN (6 digits): ")).strip()
    if not pin.isdigit() or len(pin) != 6:
        print("الـ PIN يجب أن يكون 6 أرقام بالضبط.")
        sys.exit(2)

    if "YOUR_" in str(DEVICE_AUTH.values()):
        print("عدّل DEVICE_AUTH في الملف أولاً (من user.json → saved_accounts → device_auth).")
        sys.exit(2)

    tok = device_auth_oauth_access_token_bearer_sync(DEVICE_AUTH)
    if not tok:
        print("فشل الحصول على access token من device_auth.")
        sys.exit(1)

    imp = (os.environ.get("EPIC_IMPERSONATE") or "chrome120").strip()
    ok, err = verify_epic_content_control_pin_sync(tok, pin, impersonate=imp)
    print("verify-pin →", "OK" if ok else "FAIL", f"(impersonate={imp})")
    if not ok:
        print(err)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
