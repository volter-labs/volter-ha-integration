"""N-6: token tożsamości urządzenia dla prywatnych kanałów Realtime.

HA uwierzytelnia się w chmurze kluczem API, ale Supabase Realtime sprawdza
RLS na `realtime.messages` przez `auth.uid()` — a to wymaga JWT podpisanego
sekretem projektu. Edge Function `device-token` wydaje krótkożyjący token
z `sub = user_id`; ten moduł go trzyma i odświeża przed wygaśnięciem.

Degradacja: gdy chmura tokenu nie daje (brak funkcji przed cutoverem, brak
łącza), `async_get()` zwraca `None` i wołający dołącza do kanału PUBLICZNEGO —
czyli zachowanie sprzed N-6. Po wdrożeniu migracji 060 taki join się nie uda
i pętla reconnect spróbuje ponownie już z tokenem.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)

#: Ile sekund PRZED `expires_at` token uznajemy za wymagający odświeżenia.
#: 10 minut: pętla heartbeatu chodzi co 30 s, a chmura bywa chwilowo niedostępna.
REFRESH_MARGIN_S = 600

DEVICE_TOKEN_PATH = "/functions/v1/device-token"


@dataclass(frozen=True)
class DeviceToken:
    access_token: str
    expires_at: datetime

    def needs_refresh(self, now: datetime) -> bool:
        return (self.expires_at - now).total_seconds() < REFRESH_MARGIN_S

    def expired(self, now: datetime) -> bool:
        return self.expires_at <= now

    @classmethod
    def from_response(cls, raw: Any) -> DeviceToken | None:
        if not isinstance(raw, dict):
            return None
        token = raw.get("access_token")
        exp = raw.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(exp, str):
            return None
        try:
            parsed = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return cls(token, parsed)


class DeviceTokenProvider:
    """Cache tokenu z odświeżaniem. `fetch` to I/O podane z zewnątrz (testowalne)."""

    def __init__(
        self,
        fetch: Callable[[], Awaitable[Any]],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._fetch = fetch
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._token: DeviceToken | None = None
        self._ostrzezono = False

    async def async_get(self) -> str | None:
        now = self._now()
        if self._token is not None and not self._token.needs_refresh(now):
            return self._token.access_token
        try:
            token = DeviceToken.from_response(await self._fetch())
        except Exception as err:  # noqa: BLE001 - błąd I/O to „brak tokenu", nie awaria HA
            token = None
            if not self._ostrzezono:
                _LOGGER.warning(
                    "Nie udało się pobrać tokenu urządzenia (%s) - kanał Realtime "
                    "publiczny do czasu odświeżenia [N-6]", err,
                )
                self._ostrzezono = True
        if token is not None:
            self._token = token
            self._ostrzezono = False
            return token.access_token
        # Odświeżenie padło: stary token jest lepszy niż żaden, dopóki formalnie żyje.
        if self._token is not None and not self._token.expired(now):
            return self._token.access_token
        return None

    def changed_since(self, sent: str | None) -> bool:
        """Czy trzymany token różni się od ostatnio wysłanego na kanał."""
        return self._token is not None and self._token.access_token != sent

    @property
    def current(self) -> str | None:
        return self._token.access_token if self._token else None


def make_fetch(session_factory: Callable[[], Any], supabase_url: str, api_key: str):
    """Fabryka `fetch` dla produkcji: POST device-token z kluczem API."""
    import aiohttp

    url = f"{supabase_url}{DEVICE_TOKEN_PATH}"

    async def fetch() -> Any:
        session = session_factory()
        async with session.post(
            url,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise ConnectionError(f"device-token HTTP {resp.status}")
            return await resp.json()

    return fetch
