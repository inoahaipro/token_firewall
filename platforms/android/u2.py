"""
platforms/android/u2.py -- uiautomator2 wrapper.

uiautomator2 is a Python library that runs a server on the device
and provides a much better API than raw uiautomator dump.

Install:
    pip install uiautomator2 --break-system-packages
    python -m uiautomator2 init  # installs server APK on device

Falls back to raw ADB uiautomator if u2 not available.
"""
from typing import Optional

_u2_device = None
_u2_available = None


def _get_device():
    """Get or create uiautomator2 device connection."""
    global _u2_device, _u2_available
    if _u2_available is False:
        return None
    if _u2_device is not None:
        return _u2_device
    try:
        import uiautomator2 as u2
        d = u2.connect()
        d.info  # test connection
        _u2_device = d
        _u2_available = True
        print("[U2] uiautomator2 connected")
        return d
    except Exception as e:
        print(f"[U2] not available ({e}) -- using raw ADB")
        _u2_available = False
        return None


def find_element(text: str = "", res_id: str = "", desc: str = "",
                 class_name: str = "", fuzzy: bool = True) -> Optional[tuple]:
    """
    Find UI element. Returns (x, y) center or None.
    Uses uiautomator2 if available, otherwise returns None (caller uses raw ADB).
    """
    d = _get_device()
    if d is None:
        return None
    try:
        kwargs = {}
        if text:
            kwargs["text"] = text if not fuzzy else None
            if fuzzy:
                kwargs["textContains"] = text
        if res_id:
            kwargs["resourceId"] = res_id
        if desc:
            kwargs["description"] = desc if not fuzzy else None
            if fuzzy:
                kwargs["descriptionContains"] = desc
        if class_name:
            kwargs["className"] = class_name

        # Remove None values
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        if not kwargs:
            return None

        el = d(**kwargs)
        if el.exists(timeout=2):
            bounds = el.info.get("bounds", {})
            x = (bounds.get("left", 0) + bounds.get("right", 0)) // 2
            y = (bounds.get("top", 0) + bounds.get("bottom", 0)) // 2
            return x, y
    except Exception as e:
        print(f"[U2] find error: {e}")
    return None


def tap(x: int, y: int) -> bool:
    """Tap at coordinates via uiautomator2."""
    d = _get_device()
    if d is None:
        return False
    try:
        d.click(x, y)
        return True
    except Exception:
        return False


def type_text(text: str) -> bool:
    """Type text into focused element via uiautomator2."""
    d = _get_device()
    if d is None:
        return False
    try:
        d.send_keys(text, clear=False)
        return True
    except Exception:
        return False


def get_screen_xml() -> Optional[str]:
    """Get UI hierarchy XML via uiautomator2."""
    d = _get_device()
    if d is None:
        return None
    try:
        return d.dump_hierarchy()
    except Exception:
        return None


def swipe(x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> bool:
    d = _get_device()
    if d is None:
        return False
    try:
        d.swipe(x1, y1, x2, y2, duration=duration)
        return True
    except Exception:
        return False


def is_available() -> bool:
    return _get_device() is not None
