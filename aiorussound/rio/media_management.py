"""Controller-routed Russound Media Management sessions."""

from __future__ import annotations

import asyncio
import logging
import re
from asyncio import Future, Task
from typing import Literal, Self

from aiorussound.connection import RussoundConnectionHandler
from aiorussound.const import TIMEOUT
from aiorussound.exceptions import CommandError, RussoundError
from aiorussound.rio.models import (
    MediaManagementMenuPage,
    MessageType,
    RussoundMessage,
)
from aiorussound.rio.protocol import process_response

DEFAULT_MEDIA_MANAGEMENT_PAGE_SIZE = 100
DEFAULT_MEDIA_MANAGEMENT_KEEP_ALIVE_INTERVAL = 45.0
MediaManagementSelectOption = Literal["default", "norestore"]

_CONTROLLER_ZONE_RE = re.compile(r"^C\[\d+]\.Z\[\d+]$")
_LOGGER = logging.getLogger(__package__)


class MediaManagementSession:
    """A dedicated controller-routed Russound Media Management session.

    Media Management state is scoped to an IP socket. This class owns a dedicated
    connection rather than sharing the RIO client's state and subscription socket.
    """

    def __init__(
        self,
        connection_handler: RussoundConnectionHandler,
        zone_device_str: str,
        *,
        page_size: int = DEFAULT_MEDIA_MANAGEMENT_PAGE_SIZE,
        keep_alive_interval: float = DEFAULT_MEDIA_MANAGEMENT_KEEP_ALIVE_INTERVAL,
    ) -> None:
        """Initialize the session."""
        if not _CONTROLLER_ZONE_RE.match(zone_device_str):
            raise ValueError(
                "Media Management sessions must target a controller zone, such as C[1].Z[1]"
            )
        if not 1 <= page_size <= 255:
            raise ValueError("Media Management page size must be between 1 and 255")
        if keep_alive_interval <= 0:
            raise ValueError("Media Management keep-alive interval must be positive")

        self._connection_handler = connection_handler
        self._zone_device_str = zone_device_str
        self._page_size = page_size
        self._keep_alive_interval = keep_alive_interval
        self._command_lock = asyncio.Lock()
        self._consumer_task: Task[None] | None = None
        self._keep_alive_task: Task[None] | None = None
        self._response_future: Future[RussoundMessage] | None = None
        self._page_future: Future[MediaManagementMenuPage] | None = None
        self._current_page: MediaManagementMenuPage | None = None

    async def __aenter__(self) -> Self:
        """Connect to the dedicated Media Management socket."""
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the dedicated Media Management socket."""
        await self.close()

    @property
    def is_connected(self) -> bool:
        """Return whether the session consumer is active."""
        return self._consumer_task is not None and not self._consumer_task.done()

    @property
    def current_page(self) -> MediaManagementMenuPage | None:
        """Return the most recently received Media Management menu page."""
        return self._current_page

    async def connect(self) -> None:
        """Connect and begin consuming RIO responses."""
        if self.is_connected:
            return
        await self._connection_handler.connect()
        if self._connection_handler.reader is None:
            raise RussoundError("Media Management connection did not provide a reader")
        self._consumer_task = asyncio.create_task(self._consume())

    async def initialize(self) -> MediaManagementMenuPage:
        """Configure a JSON session and return the top-level Media Management page."""
        await self.connect()
        async with self._command_lock:
            await self._send_event_locked("MMVerbosity", "2")
            await self._send_event_locked("MMIndex", "ABSOLUTE")
            await self._send_event_locked("MMMaxItems", str(self._page_size))
            await self._send_event_locked("MMFormat", "JSON")
            page = await self._send_event_locked("MMInit", expect_page=True)

        if page is None:
            raise RussoundError(
                "Media Management initialization did not return a menu page"
            )

        if self._keep_alive_task is None or self._keep_alive_task.done():
            self._keep_alive_task = asyncio.create_task(self._keep_alive())
        return page

    async def start_item(self, item_id: int) -> None:
        """Set the absolute menu position for later item pagination.

        This is available only because the session is configured with
        ``MMIndex ABSOLUTE``.
        """
        await self._send_event("MMStartItem", _menu_item_index(item_id))

    async def next_items(self) -> MediaManagementMenuPage:
        """Return the next page in the current menu."""
        return await self._request_menu_page("MMNextItems")

    async def previous_items(self) -> MediaManagementMenuPage:
        """Return the previous page in the current menu."""
        return await self._request_menu_page("MMPrevItems")

    async def previous_screen(
        self, *, expect_page: bool = True
    ) -> MediaManagementMenuPage | None:
        """Navigate to the previous MM screen.

        Set ``expect_page`` to ``False`` when the caller expects an info or
        now-playing screen rather than a menu.
        """
        return await self._send_event("MMPrevScreen", expect_page=expect_page)

    async def select_item(
        self,
        item_id: int,
        *,
        select_option: MediaManagementSelectOption | None = None,
        expect_page: bool = True,
    ) -> MediaManagementMenuPage | None:
        """Select a menu item from the current page.

        ``select_option`` is sent immediately before ``MMSelectItem`` while
        retaining the session command lock, as required by the RIO protocol.
        For a playable leaf item, set ``expect_page`` to ``False`` because the
        device transitions to its now-playing screen instead of returning a
        menu page.
        """
        item_index = _menu_item_index(item_id)
        if select_option not in (None, "default", "norestore"):
            raise ValueError("select_option must be 'default' or 'norestore'")

        async with self._command_lock:
            if select_option is not None:
                await self._send_event_locked("MMSelectOption", select_option)
            return await self._send_event_locked(
                "MMSelectItem", item_index, expect_page=expect_page
            )

    async def open_item_context_menu(self, item_id: int) -> MediaManagementMenuPage:
        """Return the context menu for an item marked as having one."""
        return await self._request_menu_page(
            "MMItemContextMenu", _menu_item_index(item_id)
        )

    async def open_context_menu(self) -> MediaManagementMenuPage:
        """Return the context menu for the current now-playing item."""
        return await self._request_menu_page("MMContextMenu")

    async def close(self) -> None:
        """Close the Media Management session and its dedicated connection."""
        if self.is_connected:
            try:
                await self._send_event("MMClose")
            except (CommandError, RussoundError, TimeoutError):
                _LOGGER.debug("Unable to close Media Management session cleanly")

        for task in (self._keep_alive_task, self._consumer_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._keep_alive_task, self._consumer_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._fail_pending(RussoundError("Media Management session closed"))
        await self._connection_handler.close()

    async def _send_event(
        self, event_name: str, *args: str, expect_page: bool = False
    ) -> MediaManagementMenuPage | None:
        """Serialize an MM EVENT and wait for its response."""
        async with self._command_lock:
            return await self._send_event_locked(
                event_name, *args, expect_page=expect_page
            )

    async def _request_menu_page(
        self, event_name: str, *args: str
    ) -> MediaManagementMenuPage:
        """Send a command which is documented to produce a menu page."""
        page = await self._send_event(event_name, *args, expect_page=True)
        if page is None:
            raise RussoundError(f"{event_name} did not return a menu page")
        return page

    async def _send_event_locked(
        self, event_name: str, *args: str, expect_page: bool = False
    ) -> MediaManagementMenuPage | None:
        """Send an MM EVENT while the command lock is held."""
        if not self.is_connected:
            raise RussoundError("Media Management session is not connected")

        loop = asyncio.get_running_loop()
        response_future: Future[RussoundMessage] = loop.create_future()
        page_future: Future[MediaManagementMenuPage] | None = None
        if expect_page:
            page_future = loop.create_future()
        self._response_future = response_future
        self._page_future = page_future

        command = _event_command(self._zone_device_str, event_name, *args)
        try:
            await self._connection_handler.write_str(command)
            await asyncio.wait_for(response_future, timeout=TIMEOUT)
            if page_future is not None:
                return await asyncio.wait_for(page_future, timeout=TIMEOUT)
            return None
        finally:
            if self._response_future is response_future:
                self._response_future = None
            if self._page_future is page_future:
                self._page_future = None

    async def _consume(self) -> None:
        """Consume responses from the dedicated Media Management connection."""
        reader = self._connection_handler.reader
        if reader is None:
            return
        try:
            async for raw_message in reader:
                message = process_response(raw_message)
                if message is None:
                    continue
                if message.type == MessageType.STATE:
                    self._set_response(message)
                elif message.type == MessageType.ERROR:
                    self._set_error(message)
                if message.media_management_page is not None:
                    self._set_page(message.media_management_page)
        except (asyncio.CancelledError, OSError):
            pass
        finally:
            self._fail_pending(RussoundError("Media Management connection closed"))

    def _set_response(self, message: RussoundMessage) -> None:
        """Resolve the command response currently in flight."""
        if self._response_future is not None and not self._response_future.done():
            self._response_future.set_result(message)

    def _set_error(self, message: RussoundMessage) -> None:
        """Fail the command response currently in flight."""
        error = CommandError(message.value or "Media Management command failed")
        if self._response_future is not None and not self._response_future.done():
            self._response_future.set_exception(error)
        elif self._page_future is not None and not self._page_future.done():
            self._page_future.set_exception(error)

    def _set_page(self, page: MediaManagementMenuPage) -> None:
        """Resolve the page expected by the current navigation operation."""
        self._current_page = page
        if self._page_future is not None and not self._page_future.done():
            self._page_future.set_result(page)

    def _fail_pending(self, error: RussoundError) -> None:
        """Fail outstanding waiters when the socket stops."""
        if self._response_future is not None and not self._response_future.done():
            self._response_future.set_exception(error)
        elif self._page_future is not None and not self._page_future.done():
            self._page_future.set_exception(error)

    async def _keep_alive(self) -> None:
        """Keep an initialized session alive before the device's one-minute timeout."""
        try:
            while True:
                await asyncio.sleep(self._keep_alive_interval)
                await self._send_event("MMKeepAlive")
        except asyncio.CancelledError:
            raise
        except (CommandError, RussoundError, TimeoutError):
            _LOGGER.warning("Media Management keep-alive failed")


def _event_command(zone_device_str: str, event_name: str, *args: str) -> str:
    """Build a controller-routed Media Management EVENT command."""
    arguments = f" {' '.join(args)}" if args else ""
    return f"EVENT {zone_device_str}!{event_name}{arguments}"


def _menu_item_index(item_id: int) -> str:
    """Validate and format a protocol menu item index."""
    if isinstance(item_id, bool) or not isinstance(item_id, int):
        raise TypeError("Media Management item_id must be an integer")
    if not 1 <= item_id <= 2**32:
        raise ValueError("Media Management item_id must be between 1 and 2^32")
    return str(item_id)
