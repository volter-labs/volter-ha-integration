"""Command Handler — subskrypcja Supabase Realtime i wykonywanie komend HA."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    COMMAND_ENTITY_MAP,
    DEVICE_TELEMETRY_PATH,
    OPT_ENTITY_EXPORT_LIMIT_SWITCH,
    REALTIME_HEARTBEAT_INTERVAL,
    REALTIME_RECONNECT_BASE,
    REALTIME_RECONNECT_MAX,
)

_LOGGER = logging.getLogger(__name__)


class VolterCommandHandler:
    """Subskrybuje kanał Realtime device:{device_id} i wykonuje komendy HA.

    Protokół Phoenix Channels (Supabase Realtime):
    1. Connect: wss://{project}.supabase.co/realtime/v1/websocket
    2. Join: topic=realtime:device:{device_id}
    3. Heartbeat: co 30s na topic=phoenix
    4. Odbieraj: event=broadcast, payload.event=command
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        supabase_url: str,
        anon_key: str,
        api_key: str,
    ) -> None:
        """Inicjalizacja."""
        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._anon_key = anon_key
        self._api_key = api_key
        self._telemetry_url = f"{supabase_url}{DEVICE_TELEMETRY_PATH}"

        # Zamień https:// na wss:// dla WebSocket
        ws_base = supabase_url.replace("https://", "wss://").replace("http://", "ws://")
        self._ws_url = (
            f"{ws_base}/realtime/v1/websocket"
            f"?apikey={anon_key}&vsn=1.0.0"
        )
        self._channel_topic = f"realtime:device:{device_id}"

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._listen_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False
        self._ref_counter = 0
        self._reconnect_delay = REALTIME_RECONNECT_BASE

    async def async_start(self) -> None:
        """Uruchom nasłuchiwanie komend."""
        if not self._has_control_entities():
            _LOGGER.info("No control entities mapped — command handler disabled")
            return

        self._running = True
        self._listen_task = self.hass.async_create_task(self._connection_loop())
        _LOGGER.debug("Command handler started")

    async def async_stop(self) -> None:
        """Zatrzymaj nasłuchiwanie."""
        self._running = False

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()

        await self._close_ws()
        _LOGGER.debug("Command handler stopped")

    def _has_control_entities(self) -> bool:
        """Sprawdź czy jakiekolwiek encje sterujące są zmapowane."""
        options = self._entry.options
        for _param, (opt_key, *_rest) in COMMAND_ENTITY_MAP.items():
            if options.get(opt_key):
                return True
        if options.get(OPT_ENTITY_EXPORT_LIMIT_SWITCH):
            return True
        return False

    # ── WebSocket lifecycle ──────────────────────────────────────────────────

    async def _connection_loop(self) -> None:
        """Pętla reconnect z exponential backoff."""
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.warning("Realtime connection error: %s", err)

            if not self._running:
                break

            # Exponential backoff
            _LOGGER.info(
                "Reconnecting in %ds...", self._reconnect_delay
            )
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                self._reconnect_delay * 2, REALTIME_RECONNECT_MAX
            )

    async def _connect_and_listen(self) -> None:
        """Połącz się z Realtime WebSocket i nasłuchuj."""
        self._session = aiohttp.ClientSession()

        try:
            self._ws = await self._session.ws_connect(
                self._ws_url,
                heartbeat=REALTIME_HEARTBEAT_INTERVAL,
                timeout=15,
            )

            _LOGGER.info("Connected to Supabase Realtime")
            self._reconnect_delay = REALTIME_RECONNECT_BASE

            # Join channel
            await self._send_json({
                "topic": self._channel_topic,
                "event": "phx_join",
                "payload": {
                    "config": {
                        "broadcast": {"self": False},
                    },
                },
                "ref": self._next_ref(),
            })

            # Heartbeat task
            self._heartbeat_task = self.hass.async_create_task(self._heartbeat_loop())

            # Nasłuchuj wiadomości
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(json.loads(msg.data))
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    _LOGGER.warning("WebSocket closed/error: %s", msg.type)
                    break

        finally:
            await self._close_ws()

    async def _close_ws(self) -> None:
        """Zamknij WebSocket i sesję."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _heartbeat_loop(self) -> None:
        """Wysyłaj heartbeat co REALTIME_HEARTBEAT_INTERVAL sekund."""
        try:
            while self._running and self._ws and not self._ws.closed:
                await asyncio.sleep(REALTIME_HEARTBEAT_INTERVAL)
                await self._send_json({
                    "topic": "phoenix",
                    "event": "heartbeat",
                    "payload": {},
                    "ref": self._next_ref(),
                })
        except (asyncio.CancelledError, ConnectionError):
            pass

    async def _send_json(self, data: dict) -> None:
        """Wyślij JSON przez WebSocket."""
        if self._ws and not self._ws.closed:
            await self._ws.send_json(data)

    def _next_ref(self) -> str:
        """Generuj kolejny ref dla Phoenix protocol."""
        self._ref_counter += 1
        return str(self._ref_counter)

    # ── Message handling ─────────────────────────────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        """Obsłuż wiadomość z Realtime."""
        topic = msg.get("topic", "")
        event = msg.get("event", "")

        if event == "phx_reply":
            status = msg.get("payload", {}).get("status")
            if topic == self._channel_topic:
                _LOGGER.info("Channel join status: %s", status)
            return

        if event == "phx_error":
            _LOGGER.error("Channel error: %s", msg.get("payload"))
            return

        # Broadcast event — komendy z chmury
        if event == "broadcast" and topic == self._channel_topic:
            payload = msg.get("payload", {})
            broadcast_event = payload.get("event", "")
            broadcast_payload = payload.get("payload", {})

            if broadcast_event == "command":
                await self._execute_command(broadcast_payload)

    async def _execute_command(self, payload: dict) -> None:
        """Wykonaj komendę z chmury — routuj do odpowiednich service calls HA."""
        command = payload.get("command", "")
        params = payload.get("params", {})
        request_id = payload.get("request_id", "unknown")

        _LOGGER.info(
            "Executing command: %s (request_id=%s, params=%s)",
            command, request_id, params,
        )

        if command != "SET_WORK_MODE":
            _LOGGER.warning("Unknown command: %s", command)
            await self._report_result(request_id, "error", errors=[
                {"entity": "command", "error": f"Unknown command: {command}"}
            ])
            return

        options = self._entry.options
        executed: list[str] = []
        errors: list[dict[str, str]] = []

        # Iteruj po parametrach komendy i wywołuj odpowiednie service calls
        for param_key, value in params.items():
            mapping = COMMAND_ENTITY_MAP.get(param_key)
            if not mapping:
                continue

            opt_key, ha_domain, ha_service, data_key = mapping
            entity_id = options.get(opt_key, "")

            if not entity_id:
                _LOGGER.debug("Param %s: entity not mapped, skip", param_key)
                continue

            try:
                await self.hass.services.async_call(
                    ha_domain,
                    ha_service,
                    {"entity_id": entity_id, data_key: value},
                    blocking=True,
                )
                executed.append(param_key)
                _LOGGER.info(
                    "Executed: %s.%s on %s = %s",
                    ha_domain, ha_service, entity_id, value,
                )
            except Exception as err:
                errors.append({"entity": param_key, "error": str(err)})
                _LOGGER.error(
                    "Failed: %s.%s on %s: %s",
                    ha_domain, ha_service, entity_id, err,
                )

        # export_limit_switch — obsługa osobna (on/off)
        if "export_limit_enabled" in params:
            switch_entity = options.get(OPT_ENTITY_EXPORT_LIMIT_SWITCH, "")
            if switch_entity:
                try:
                    service = "turn_on" if params["export_limit_enabled"] else "turn_off"
                    await self.hass.services.async_call(
                        "switch",
                        service,
                        {"entity_id": switch_entity},
                        blocking=True,
                    )
                    executed.append("export_limit_switch")
                except Exception as err:
                    errors.append({"entity": "export_limit_switch", "error": str(err)})

        # Określ status
        if errors and not executed:
            status = "error"
        elif errors:
            status = "partial"
        else:
            status = "success"

        _LOGGER.info(
            "Command %s result: %s (executed=%s, errors=%d)",
            request_id, status, executed, len(errors),
        )

        await self._report_result(request_id, status, executed, errors)

    async def _report_result(
        self,
        request_id: str,
        status: str,
        executed: list[str] | None = None,
        errors: list[dict[str, str]] | None = None,
    ) -> None:
        """Wyślij wynik komendy z powrotem do chmury jako telemetria z extra."""
        result = {
            "request_id": request_id,
            "status": status,
            "executed": executed or [],
            "errors": errors or [],
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._telemetry_url,
                    json={
                        "readings": [{
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "extra": {"command_result": result},
                        }],
                    },
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key": self._api_key,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.warning("Failed to report command result: %s", resp.status)
        except Exception as err:
            _LOGGER.warning("Error reporting command result: %s", err)
