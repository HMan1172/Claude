"""HTTP with retries, so one network hiccup or rate limit doesn't sink an unattended day."""
import csv
import time
import xml.etree.ElementTree as ET
import zlib

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}
# What an affiliate network or its feeds can throw: HTTP and network errors, and bad, corrupt or
# unexpected data (ValueError covers UnicodeDecodeError and JSON errors; OSError covers gzip's).
NETWORK_ERRORS = (ValueError, OSError, EOFError, zlib.error, csv.Error, ET.ParseError,
                  requests.RequestException)
WAITS = (2, 8, 30)  # seconds before each retry


def request(method: str, url: str, idempotent: bool = True, **kw) -> requests.Response:
    """requests.request, retried on rate limits, and on server or network errors when repeating the
    call is harmless. Creates (idempotent=False) retry only on 429, which means "not processed";
    a retried create after a server error could make a duplicate."""
    for wait in (*WAITS, None):
        try:
            resp = requests.request(method, url, **kw)
        except (requests.ConnectionError, requests.Timeout):
            if wait is None or not idempotent:
                raise
        else:
            retry = resp.status_code == 429 or (idempotent and resp.status_code in RETRY_STATUS)
            if not retry or wait is None:
                resp.raise_for_status()
                return resp
            wait = max(wait, _retry_after(resp))
        time.sleep(wait)


def _retry_after(resp) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", 0)), 300)
    except ValueError:
        return 0
