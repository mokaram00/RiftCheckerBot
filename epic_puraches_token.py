"""
POST https://accounts.epicgames.com/account/v2/payment/purchaseToken
نفس رؤوس الطلب والكوكيز من التقاط المتصفح.
ضع نفس JSON الـ Request payload من Network > Payload في PAYLOAD (الطلب الأصلي ~192 بايت).
"""
from __future__ import annotations

from curl_cffi import requests as curl_requests

from epic_response_log import epic_log_from_response

URL = "https://accounts.epicgames.com/account/v2/payment/purchaseToken?locale=en-US"

# نفس الـ body من تبويب Payload (عدّل حسب عملية الشراء الفعلية)
PAYLOAD: dict = {}

HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/143.0.0.0 Safari/537.36"
    ),
    "x-xsrf-token": "dfedf83ae409472b8a145bab1d98f9c0",
}

# لصق سطر الـ cookie كاملاً من نفس طلب المُتصفح
COOKIE_STR = (
    "EPIC_BEARER_TOKEN=a531afda7c3e4996b5f2628da905f4ed; "
   )


def parse_cookie_header(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v
    return out


def main() -> None:
    cookies = parse_cookie_header(COOKIE_STR)
    response = curl_requests.post(
        URL,
        headers=HEADERS,
        cookies=cookies,
        json=PAYLOAD,
        impersonate="chrome",  # طبّق بصمة متصفح قريبة (عدّل إن لزم: chrome120، chrome124، إلخ)
    )
    epic_log_from_response(
        "payment_purchaseToken_script", "POST", getattr(response, "url", None) or URL, response
    )
    print("status", response.status_code)
    print(response.text)


if __name__ == "__main__":
    main()
