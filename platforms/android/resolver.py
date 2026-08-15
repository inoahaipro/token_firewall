"""
platforms/android/resolver.py -- Auto-discover installed app package names.

Runs once at startup. Maps friendly names to actual installed packages.
Handles OEM variants (Samsung vs Pixel vs OnePlus vs Xiaomi etc.)
"""
import re
import subprocess
from typing import Optional

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


class AppResolver:

    def __init__(self):
        self._resolved:  dict[str, str] = {}
        self._installed: set[str] = set()

    def resolve(self) -> dict[str, str]:
        """Query device, build friendly name → package map."""
        try:
            r = subprocess.run(
                ["adb", "shell", "pm", "list", "packages"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                return {}
            for line in r.stdout.splitlines():
                m = re.match(r"package:(.+)", line.strip())
                if m:
                    self._installed.add(m.group(1).strip())
            for name, candidates in _CANDIDATES.items():
                for pkg in candidates:
                    if pkg in self._installed:
                        self._resolved[name] = pkg
                        break
            print(f"[RESOLVER] {len(self._installed)} packages, resolved {len(self._resolved)} names")
            return self._resolved
        except Exception as e:
            print(f"[RESOLVER] Failed: {e}")
            return {}

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
