"""
platforms/android/resolver.py -- Auto-discover installed app package names.

Runs once at startup. Maps friendly names to actual installed packages.
Handles OEM variants (Samsung vs Pixel vs OnePlus vs Xiaomi etc.)
"""
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

# Same SSH+rish channel platforms/rish_phone/hands.py uses for every other
# phone action on this deployment. Found live: this resolver was the only
# thing on the phone-control path still hardcoded to a raw `adb shell`
# call, which this deployment runs with TF_DISABLE_ADB=true and no wired/
# paired adb device -- so it always failed, even though the exact same
# package list is trivially reachable over the SSH channel everything else
# already uses successfully.
_SSH_PHONE_CMD = [
    "ssh", "-tt", "-p", "8022", "-i", str(Path.home() / ".ssh/id_ed25519_phone"),
    "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", "u0_a337@100.75.171.40",
]


def _list_packages_via_rish() -> Optional[str]:
    remote = 'export RISH_APPLICATION_ID=com.termux; ~/rish/rish -c "pm list packages"'
    try:
        r = subprocess.run(_SSH_PHONE_CMD + [remote], capture_output=True, text=True, timeout=15)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None

# Priority-ordered candidates. First installed one wins.
_CANDIDATES = {
    "chrome":      ["com.android.chrome", "org.chromium.chrome", "com.chrome.beta"],
    "browser":     ["com.android.chrome", "org.mozilla.firefox", "com.opera.browser",
                    "com.microsoft.emmx", "com.brave.browser"],
    "firefox":     ["org.mozilla.firefox", "org.mozilla.firefox_beta"],
    "camera":      ["com.samsung.android.app.camera", "com.google.android.GoogleCamera",
                    "com.oneplus.camera", "com.huawei.camera", "com.android.camera2",
                    "com.android.camera"],
    "settings":    ["com.android.settings", "com.samsung.android.settings"],
    "youtube":     ["com.google.android.youtube"],
    "maps":        ["com.google.android.apps.maps"],
    "gmail":       ["com.google.android.gm"],
    "telegram":    ["org.telegram.messenger", "org.telegram.messenger.web"],
    "whatsapp":    ["com.whatsapp", "com.whatsapp.w4b"],
    "spotify":     ["com.spotify.music"],
    "netflix":     ["com.netflix.mediaclient"],
    "instagram":   ["com.instagram.android"],
    "twitter":     ["com.twitter.android", "com.twitter.android.lite"],
    "tiktok":      ["com.zhiliaoapp.musically", "com.ss.android.ugc.trill"],
    "messages":    ["com.google.android.apps.messaging", "com.samsung.android.messaging"],
    "phone":       ["com.google.android.dialer", "com.samsung.android.dialer"],
    "contacts":    ["com.google.android.contacts", "com.samsung.android.contacts"],
    "gallery":     ["com.samsung.android.gallery3d", "com.google.android.apps.photos"],
    "photos":      ["com.google.android.apps.photos", "com.samsung.android.gallery3d"],
    "clock":       ["com.google.android.deskclock", "com.samsung.android.app.clockpackage"],
    "calculator":  ["com.google.android.calculator", "com.samsung.android.calculator"],
    "files":       ["com.google.android.documentsui", "com.samsung.android.myfiles"],
    "play":        ["com.android.vending"],
    "play store":  ["com.android.vending"],
    "claude":      ["com.anthropic.claude"],
    "chatgpt":     ["com.openai.chatgpt"],
    "reddit":      ["com.reddit.frontpage"],
    "discord":     ["com.discord"],
    "snapchat":    ["com.snapchat.android"],
    "facebook":    ["com.facebook.katana", "com.facebook.lite"],
    "uber":        ["com.ubercab"],
    "amazon":      ["com.amazon.mShop.android.shopping"],
    "twitch":      ["tv.twitch.android.app"],
    "linkedin":    ["com.linkedin.android"],
    "outlook":     ["com.microsoft.office.outlook"],
    "teams":       ["com.microsoft.teams"],
    "zoom":        ["us.zoom.videomeetings"],
    "waze":        ["com.waze"],
    "shazam":      ["com.shazam.android"],
    "calendar":    ["com.google.android.calendar", "com.samsung.android.calendar"],
    "keep":        ["com.google.android.keep"],
    "drive":       ["com.google.android.apps.docs"],
    "translate":   ["com.google.android.apps.translate"],
    "youtube music":["com.google.android.apps.youtube.music"],
    "maps":        ["com.google.android.apps.maps"],
    "meet":        ["com.google.android.apps.meetings"],
}


# Module-level cache shared across AppResolver instances. Found live under
# concurrent load: router.py builds a fresh AppResolver() and calls
# resolve() on EVERY "open <app>" request, and with adb disabled that means
# a live SSH round-trip listing 600+ packages every single time (~10-17s
# observed) -- easily the single biggest contributor to requests timing out
# the concurrency gate under any real burst. Installed apps change rarely
# (a real install/uninstall, not something that happens minute to minute),
# so cache a successful resolve for a good while -- an hour, not five
# minutes; there's no push signal for "an app just got installed" so this
# is a plain TTL, but there's no reason to pay the round-trip cost anywhere
# near that often for something that changes this infrequently. Failures
# are never cached -- a transient SSH/adb hiccup should retry fresh next
# call, not lock in a false negative for the TTL.
#
# The lock is held across the ENTIRE fetch on a cache miss, not just the
# read/write -- otherwise N concurrent requests that all see a stale cache
# each kick off their own redundant ~10-17s round-trip (a thundering herd,
# found while reviewing this after a burst test). Holding it means the
# first caller does the one real fetch; everyone else blocks on the lock
# and finds a warm cache the instant they acquire it, at the cost of only
# ever running one resolve at a time -- an easy trade since the whole point
# is that this should rarely run at all.
_cache_lock = threading.Lock()
_cache = {"resolved": None, "installed": None, "ts": 0.0}
_CACHE_TTL_S = 3600


class AppResolver:

    def __init__(self):
        self._resolved:  dict[str, str] = {}
        self._installed: set[str] = set()
        # False whenever the device query itself failed (adb unreachable, no
        # device attached, timeout) -- distinct from "queried fine, app just
        # isn't in the list". Found live: a dropped wireless-adb connection
        # made every "open <app>" request come back "Couldn't find X
        # installed" for an app that WAS installed, with zero indication the
        # check never actually ran. Callers must check this before treating
        # an empty resolve() as a real negative.
        self.query_ok = False

    def resolve(self) -> dict[str, str]:
        """Query device, build friendly name → package map."""
        with _cache_lock:
            if _cache["resolved"] is not None and time.time() - _cache["ts"] < _CACHE_TTL_S:
                self._resolved  = _cache["resolved"]
                self._installed = _cache["installed"]
                self.query_ok = True
                return self._resolved

            output = None
            try:
                r = subprocess.run(
                    ["adb", "shell", "pm", "list", "packages"],
                    capture_output=True, text=True, timeout=15,
                )
                if r.returncode == 0:
                    output = r.stdout
            except Exception as e:
                print(f"[RESOLVER] adb path failed: {e}")

            if output is None:
                output = _list_packages_via_rish()

            if output is None:
                return {}
            self.query_ok = True
            for line in output.splitlines():
                m = re.match(r"package:(.+)", line.strip())
                if m:
                    self._installed.add(m.group(1).strip())
            for name, candidates in _CANDIDATES.items():
                for pkg in candidates:
                    if pkg in self._installed:
                        self._resolved[name] = pkg
                        break
            print(f"[RESOLVER] {len(self._installed)} packages, resolved {len(self._resolved)} names")

            _cache["resolved"] = self._resolved
            _cache["installed"] = self._installed
            _cache["ts"] = time.time()

            return self._resolved

    def get(self, name: str) -> Optional[str]:
        return self._resolved.get(name.lower())

    def find_installed(self, keyword: str) -> Optional[str]:
        """Fuzzy search installed packages by keyword."""
        kw = keyword.lower().replace(" ", "")
        matches = [p for p in self._installed if kw in p.lower()]
        return sorted(matches, key=len)[0] if matches else None

    def resolve_unknown(self, name: str) -> Optional[str]:
        return self.get(name) or self.find_installed(name)

    def patch_pack(self, pack: list) -> list:
        """Update open_app entries in a knowledge pack with real package names."""
        patched = 0
        for entry in pack:
            action = entry.get("action", {})
            if action.get("type") == "open_app":
                intent = entry.get("intent", "").lower()
                for app_name in _CANDIDATES:
                    if app_name in intent:
                        resolved = self.get(app_name)
                        if resolved:
                            action.setdefault("params", {})["package"] = resolved
                            patched += 1
                            break
        if patched:
            print(f"[RESOLVER] Patched {patched} pack entries")
        return pack
