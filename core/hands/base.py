"""
core/hands/base.py -- Platform-agnostic Hands protocol.

Every platform implements this. The router never imports platform code directly.
"""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class ActionResult:
    success: bool
    output:  str = ""
    error:   str = ""


@runtime_checkable
class Hands(Protocol):
    """Every platform hands implementation must satisfy this protocol."""

    @property
    def platform_id(self) -> str:
        """e.g. 'android', 'macos', 'windows'"""
        ...

    def can_execute(self, action: dict) -> bool:
        """Return True if this handler knows how to run this action type."""
        ...

    def execute(self, action: dict) -> ActionResult:
        """Execute the action. Never raises -- errors returned in ActionResult."""
        ...

    def capabilities(self) -> list[dict]:
        """Return list of {name, description, params} dicts for /v1/capabilities."""
        ...
