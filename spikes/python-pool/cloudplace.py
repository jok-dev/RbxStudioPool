"""Downloads a cloud place as .rbxl through the Open Cloud Asset Delivery API, cached by version.

    GET https://apis.roblox.com/asset-delivery-api/v1/assetId/{placeId}     (x-api-key, scope legacy-asset:manage)
    -> {"location": "<temporary CDN URL>", ...}; the CDN body may be gzip.

The API has no "latest version" field, so the cache is keyed by the CDN path in `location` (the query string, which
carries the signature, is dropped): a new place version gets a new path, so it downloads again; an unchanged place
costs one small JSON call per acquire.

The API key must be user-owned (community reports say group-owned keys are refused here) by an account that can
open the place. It is read from ROBLOX_API_KEY, else from the OS keychain (Windows Credential Manager), where
`rsp.py cloud-key set` stores it. It is never logged or written to disk by the pool.
"""

import gzip
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PREFIX = "cloud:"
API = "https://apis.roblox.com/asset-delivery-api/v1/assetId/{}"
ENV_KEY = "ROBLOX_API_KEY"
KEYRING_SERVICE = "rbx-studio-pool"
KEYRING_USER = "roblox-api-key"
TIMEOUT = 30
DOWNLOAD_TIMEOUT = 600

_locks = {}
_locks_guard = threading.Lock()


class CloudPlaceError(Exception):
    pass


def parse(spec):
    """'cloud:123' -> '123'; anything else -> None."""
    if not isinstance(spec, str) or not spec.startswith(PREFIX):
        return None
    place = spec[len(PREFIX):].strip()
    if not re.fullmatch(r"\d+", place):
        raise CloudPlaceError(f"{spec!r}: expected cloud:<placeId>")
    return place


def api_key():
    key = os.environ.get(ENV_KEY, "").strip()
    if key:
        return key, f"env {ENV_KEY}"
    try:
        import keyring
        key = (keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or "").strip()
    except Exception as e:  # no backend, locked keychain, ...
        raise CloudPlaceError(f"could not read the OS keychain: {type(e).__name__}: {e}") from None
    if key:
        return key, "OS keychain"
    raise CloudPlaceError(
        f"no Roblox API key: run `python rsp.py cloud-key set` (stores it in the OS keychain) or set {ENV_KEY}. "
        "It needs a user-owned Open Cloud key with the legacy-asset:manage scope, from an account that can open "
        "the place (create.roblox.com/dashboard/credentials).")


def _lock(place):
    with _locks_guard:
        return _locks.setdefault(place, threading.Lock())


def _http(req, timeout):
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        raise CloudPlaceError(f"HTTP {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise CloudPlaceError(f"network error: {e.reason}") from None


def locate(place, key):
    """Asks Asset Delivery where place `place`'s current version lives. Returns the CDN URL."""
    req = urllib.request.Request(API.format(place), headers={"x-api-key": key, "Accept": "application/json"})
    try:
        with _http(req, TIMEOUT) as r:
            data = json.loads(r.read())
    except CloudPlaceError as e:
        hint = ""
        if re.match(r"HTTP (401|403)", str(e)):
            hint = (" (the key needs the legacy-asset:manage scope and must be user-owned by an account that can "
                    "open the place; group-owned keys are reported not to work here)")
        raise CloudPlaceError(f"asset delivery for place {place}: {e}{hint}") from None
    location = data.get("location") if isinstance(data, dict) else None
    if not location:
        raise CloudPlaceError(f"asset delivery for place {place}: no location in the response ({str(data)[:300]})")
    return location


def version_key(location):
    """The cache key for a CDN URL: its host + path, without the (signed, expiring) query string."""
    u = urllib.parse.urlsplit(location)
    return hashlib.sha1(f"{u.netloc}{u.path}".encode()).hexdigest()[:16]


def _download(location, out):
    req = urllib.request.Request(location, headers={"Accept-Encoding": "gzip"})
    tmp = out.with_name(out.name + ".part")
    try:
        with _http(req, DOWNLOAD_TIMEOUT) as r, open(tmp, "wb") as f:
            gz = "gzip" in (r.headers.get("Content-Encoding") or "").lower()
            src = gzip.GzipFile(fileobj=r) if gz else r
            while chunk := src.read(1 << 20):
                f.write(chunk)
        with open(tmp, "rb") as f:
            head = f.read(8)
        if head[:2] == b"\x1f\x8b":  # gzip without the header saying so
            with gzip.open(tmp, "rb") as g, open(out.with_name(out.name + ".gunzip"), "wb") as f:
                while chunk := g.read(1 << 20):
                    f.write(chunk)
            os.replace(out.with_name(out.name + ".gunzip"), tmp)
            with open(tmp, "rb") as f:
                head = f.read(8)
        if not head.startswith(b"<roblox"):
            raise CloudPlaceError(f"the download is not a place file (starts with {head!r})")
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


def fetch(place, cache_dir, in_use=()):
    """The current version of cloud place `place` as a local .rbxl, downloading it only when its version changed.
    Older cached versions are deleted unless their path is in `in_use`. Returns (path, info)."""
    key, source = api_key()
    t0 = time.time()
    folder = Path(cache_dir) / place
    with _lock(place):
        location = locate(place, key)
        out = folder / f"{version_key(location)}.rbxl"
        hit = out.is_file()
        if not hit:
            folder.mkdir(parents=True, exist_ok=True)
            _download(location, out)
        keep = {Path(p).resolve() for p in in_use} | {out.resolve()}
        for old in folder.glob("*.rbxl"):
            if old.resolve() not in keep:
                try:
                    old.unlink()
                except OSError:
                    pass  # still open somewhere; next fetch retries
    return out, {"place": place, "cache": "hit" if hit else "downloaded", "file": str(out),
                 "mb": round(out.stat().st_size / 1e6, 1), "took": round(time.time() - t0, 1), "key": source}


def set_key(key):
    import keyring
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)


def clear_key():
    import keyring
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        return True
    except keyring.errors.PasswordDeleteError:
        return False
