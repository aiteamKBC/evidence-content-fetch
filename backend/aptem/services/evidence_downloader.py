import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePath
from urllib.parse import unquote, urlparse

import requests
from django.conf import settings
from django.core.files.storage import default_storage

from .aptem_auth import AptemLoginError, refresh_aptem_cookie


logger = logging.getLogger(__name__)

ALLOWED_HOSTNAME = "kentbusinesscollege.aptem.co.uk"
ALLOWED_PATH_PREFIX = "/MWS.LearningProgrammes/LearningItemEvidence/Document/"
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
PERMANENT_STATUS_CODES = {400, 404}
BLOCKED_HTML_TYPES = {"text/html", "application/xhtml+xml"}
MIME_EXTENSIONS = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "image/png": "png",
    "image/jpeg": "jpg",
    "text/plain": "txt",
}


class AptemDownloadError(Exception):
    def __init__(self, message, *, status_code=None, authentication_failed=False):
        super().__init__(message)
        self.status_code = status_code
        self.authentication_failed = authentication_failed


@dataclass
class DownloadResult:
    document_url: str
    status_code: int
    content_type: str
    content_disposition: str
    storage_name: str
    filename: str
    file_size_bytes: int


def build_document_url(evidence_id, document_url=None):
    if document_url:
        candidate = document_url
    else:
        base_url = settings.APTEM_BASE_URL.rstrip("/")
        candidate = f"{base_url}{ALLOWED_PATH_PREFIX}{evidence_id}"
    validate_document_url(candidate)
    return candidate


def validate_document_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Only HTTPS Aptem evidence URLs are allowed.")
    if parsed.hostname != ALLOWED_HOSTNAME:
        raise ValueError("Aptem evidence URL hostname is not allowed.")
    if not parsed.path.startswith(ALLOWED_PATH_PREFIX):
        raise ValueError("Aptem evidence URL path is not allowed.")
    return True


def extract_filename(content_disposition):
    if not content_disposition:
        return ""
    utf8_match = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", content_disposition, re.I)
    if utf8_match:
        return unquote(utf8_match.group(1).strip().strip('"'))
    filename_match = re.search(r'filename\s*=\s*"([^"]+)"', content_disposition, re.I)
    if filename_match:
        return filename_match.group(1).strip()
    bare_match = re.search(r"filename\s*=\s*([^;]+)", content_disposition, re.I)
    if bare_match:
        return bare_match.group(1).strip().strip('"')
    return ""


def sanitize_filename(filename):
    name = PurePath(filename).name
    name = re.sub(r"[^\w.\- ()]+", "_", name, flags=re.UNICODE).strip(" .")
    return name or "evidence.bin"


def filename_for_response(evidence_id, content_type, content_disposition):
    filename = extract_filename(content_disposition)
    if not filename:
        mime = content_type.split(";", 1)[0].strip().lower()
        extension = MIME_EXTENSIONS.get(mime, "bin")
        filename = f"evidence_{evidence_id}.{extension}"
    return sanitize_filename(filename)


def aptem_storage_directory(evidence_id):
    configured = str(settings.APTEM_DOWNLOAD_DIRECTORY).replace("\\", "/").strip("/")
    if configured == "media":
        configured = ""
    elif configured.startswith("media/"):
        configured = configured[len("media/") :]
    return "/".join(part for part in [configured, str(evidence_id)] if part)


def looks_like_login_url(url):
    return "/Users/Account/LogOn" in url


def looks_like_login_html(response):
    try:
        text = response.text[:10000]
    except UnicodeDecodeError:
        return False
    lowered = text.lower()
    if "usernameoremail" in lowered:
        return True
    if "__requestverificationtoken" in lowered and "password" in lowered:
        return True
    return "aptem" in lowered and "<form" in lowered and "password" in lowered


def response_is_authentication_failure(response):
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if response.status_code in {401, 403}:
        return True
    if looks_like_login_url(response.url):
        return True
    if content_type in BLOCKED_HTML_TYPES and looks_like_login_html(response):
        return True
    return False


class AptemEvidenceDownloader:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.cookie_header = settings.APTEM_COOKIE_HEADER

    def download(self, evidence_id, document_url=None):
        url = build_document_url(evidence_id, document_url)
        last_error = None
        cookie_refreshed = False

        for attempt in range(settings.APTEM_MAX_RETRIES + 1):
            try:
                response = self.session.get(
                    url,
                    headers=self._headers(),
                    timeout=settings.APTEM_REQUEST_TIMEOUT_SECONDS,
                    stream=True,
                    allow_redirects=True,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt < settings.APTEM_MAX_RETRIES:
                    self._backoff(attempt)
                    continue
                raise AptemDownloadError("Aptem request timed out or failed.") from exc

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < settings.APTEM_MAX_RETRIES:
                self._backoff(attempt)
                continue

            try:
                return self._handle_response(evidence_id, url, response)
            except AptemDownloadError as exc:
                if (
                    exc.authentication_failed
                    and settings.APTEM_AUTO_REFRESH_COOKIE
                    and settings.APTEM_LOGIN_EMAIL
                    and settings.APTEM_LOGIN_PASSWORD
                    and not cookie_refreshed
                ):
                    try:
                        login_result = refresh_aptem_cookie()
                    except AptemLoginError:
                        raise exc
                    self.cookie_header = login_result.cookie_header
                    cookie_refreshed = True
                    continue
                raise

        raise AptemDownloadError("Aptem request failed after retries.") from last_error

    def _headers(self):
        return {
            "Cookie": self.cookie_header,
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://kentbusinesscollege.aptem.co.uk/",
            "Accept": (
                "application/pdf,application/octet-stream,"
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
                "application/msword,image/*,text/plain,*/*"
            ),
        }

    def _backoff(self, attempt):
        delay = min(2**attempt, 30)
        time.sleep(delay)

    def _handle_response(self, evidence_id, original_url, response):
        content_type = response.headers.get("Content-Type", "")
        simple_content_type = content_type.split(";", 1)[0].strip().lower()
        content_disposition = response.headers.get("Content-Disposition", "")

        if response_is_authentication_failure(response):
            raise AptemDownloadError(
                "The Aptem session is invalid or expired.",
                status_code=response.status_code,
                authentication_failed=True,
            )
        if response.status_code in PERMANENT_STATUS_CODES:
            raise AptemDownloadError(
                f"Aptem returned permanent failure {response.status_code}.",
                status_code=response.status_code,
            )
        if not 200 <= response.status_code <= 299:
            raise AptemDownloadError(
                f"Aptem returned HTTP {response.status_code}.",
                status_code=response.status_code,
            )
        if looks_like_login_url(response.url):
            raise AptemDownloadError(
                "The Aptem session is invalid or expired.",
                status_code=response.status_code,
                authentication_failed=True,
            )
        if simple_content_type in BLOCKED_HTML_TYPES:
            raise AptemDownloadError(
                "The Aptem session is invalid or expired.",
                status_code=response.status_code,
                authentication_failed=True,
            )

        filename = filename_for_response(evidence_id, content_type, content_disposition)
        storage_directory = aptem_storage_directory(evidence_id)
        storage_name = default_storage.get_available_name(f"{storage_directory}/{filename}")
        file_size = 0

        try:
            Path(default_storage.path(storage_directory)).mkdir(parents=True, exist_ok=True)
        except NotImplementedError:
            pass

        with default_storage.open(storage_name, "wb") as destination:
            for chunk in response.iter_content(chunk_size=1024 * 64):
                if not chunk:
                    continue
                file_size += len(chunk)
                destination.write(chunk)

        if file_size == 0:
            default_storage.delete(storage_name)
            raise AptemDownloadError(
                "Aptem returned an empty evidence file.",
                status_code=response.status_code,
            )

        return DownloadResult(
            document_url=original_url,
            status_code=response.status_code,
            content_type=content_type,
            content_disposition=content_disposition,
            storage_name=storage_name,
            filename=PurePath(storage_name).name,
            file_size_bytes=file_size,
        )


def wait_between_evidence_files():
    time.sleep(settings.APTEM_REQUEST_DELAY_SECONDS)
