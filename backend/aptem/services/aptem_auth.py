import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from django.conf import settings


LOGIN_PATH = "/Users/Account/LogOn"


class AptemLoginError(Exception):
    pass


@dataclass
class AptemLoginResult:
    login_success: bool
    cookie_header: str
    auth_token_present: bool
    expires_at: datetime | None


def refresh_aptem_cookie(*, session=None, env_path=None):
    if not settings.APTEM_LOGIN_EMAIL or not settings.APTEM_LOGIN_PASSWORD:
        raise AptemLoginError("APTEM_LOGIN_EMAIL and APTEM_LOGIN_PASSWORD must be configured.")

    login_result = login_to_aptem(
        settings.APTEM_LOGIN_EMAIL,
        settings.APTEM_LOGIN_PASSWORD,
        session=session,
    )
    if not login_result.login_success:
        raise AptemLoginError("Aptem login failed; no AUTHTOKEN cookie was returned.")

    write_env_value(
        env_path or settings.BASE_DIR / ".env",
        "APTEM_COOKIE_HEADER",
        login_result.cookie_header,
    )
    return login_result


def login_to_aptem(email, password, *, session=None):
    session = session or requests.Session()
    base_url = settings.APTEM_BASE_URL.rstrip("/")
    login_url = urljoin(base_url, LOGIN_PATH)

    step1 = session.get(
        login_url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
        timeout=settings.APTEM_REQUEST_TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    csrf_token = extract_csrf_token(step1.text)
    if not csrf_token:
        raise AptemLoginError("Could not find Aptem login CSRF token.")

    cookies1 = cookie_header_from_session(session)
    step2 = session.post(
        login_url,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,*/*",
            "User-Agent": "Mozilla/5.0",
            "Referer": login_url,
            "Origin": base_url,
            "Cookie": cookies1,
        },
        data={
            "userNameOrEmail": email,
            "password": password,
            "__RequestVerificationToken": csrf_token,
        },
        timeout=settings.APTEM_REQUEST_TIMEOUT_SECONDS,
        allow_redirects=False,
    )

    redirect_to = step2.headers.get("Location") or step2.headers.get("location")
    if redirect_to:
        session.get(
            urljoin(base_url, redirect_to),
            headers={
                "Cookie": cookie_header_from_session(session),
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html",
            },
            timeout=settings.APTEM_REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )

    cookie_header = cookie_header_from_session(session)
    cookies = session.cookies.get_dict()
    auth_token = cookies.get("AUTHTOKEN")
    expires_at = decode_jwt_expiry(auth_token) if auth_token else None
    login_success = step2.status_code in {301, 302, 303, 307, 308} and bool(auth_token)

    return AptemLoginResult(
        login_success=login_success,
        cookie_header=cookie_header,
        auth_token_present=bool(auth_token),
        expires_at=expires_at,
    )


def extract_csrf_token(html):
    patterns = [
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
        r'value="([^"]+)"[^>]*name="__RequestVerificationToken"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html or "", flags=re.I)
        if match:
            return match.group(1)
    return ""


def cookie_header_from_session(session):
    return "; ".join(f"{name}={value}" for name, value in session.cookies.get_dict().items())


def decode_jwt_expiry(token):
    if not token or "." not in token:
        return None
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
        if "exp" not in data:
            return None
        return datetime.fromtimestamp(int(data["exp"]), tz=timezone.utc)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def write_env_value(env_path, key, value):
    env_path = Path(env_path)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    replacement = f"{key}={value}"
    updated = False
    next_lines = []

    for line in lines:
        if line.startswith(f"{key}="):
            next_lines.append(replacement)
            updated = True
        else:
            next_lines.append(line)

    if not updated:
        next_lines.append(replacement)

    env_path.write_text("\n".join(next_lines) + "\n", encoding="utf-8")
