"""Asynchronous Python client for Russound RIO."""

from __future__ import annotations

import asyncio
from asyncio import AbstractEventLoop, Future, Queue, StreamReader, StreamWriter
import logging
from typing import Any, Coroutine

from aiorussound.const import (
    DEFAULT_PORT,
    FLAGS_BY_VERSION,
    MAX_SOURCE,
    MINIMUM_API_SUPPORT,
    RECONNECT_DELAY,
    RESPONSE_REGEX,
    SOURCE_PROPERTIES,
    ZONE_PROPERTIES,
    FeatureFlag,
)
from aiorussound.exceptions import (
    CommandError,
    UncachedVariableError,
    UnsupportedFeatureError,
)
from aiorussound.util import (
    controller_device_str,
    get_max_zones,
    is_feature_supported,
    is_fw_version_higher,
    source_device_str,
    zone_device_str,
)

# Maintain compat with various 3.x async changes
if hasattr(asyncio, "ensure_future"):
    ensure_future = asyncio.ensure_future
else:
    ensure_future = getattr(asyncio, "async")

_LOGGER = logging.getLogger(__package__)


class Russound:
    """Manages the RIO connection to a Russound device."""

    def __init__(
            self, loop: AbstractEventLoop, host: str, port: int = DEFAULT_PORT
    ) -> None:
        """Initialize the Russound object using the event loop, host and port
        provided.
        """
        self._loop = loop
        self.host = host
        self.port = port
        self._ioloop_future = None
        self._cmd_queue: Queue = Queue()
        self._state: dict[str, dict[str, str]] = {}
        self._callbacks: dict[str, list[Any]] = {}
        self._connection_callbacks: list[Any] = []
        self._connection_started: bool = False
        self._watched_devices: dict[str, bool] = {}
        self._controllers: dict[int, Controller] = {}
        self.sources: dict[int, Zone] = {}
        self.rio_version: str | None = None
        self.connected: bool = False

    def _retrieve_cached_variable(self, device_str: str, key: str) -> str:
        """Retrieve the cache state of the named variable for a particular
        device. If the variable has not been cached then the UncachedVariable
        exception is raised.
        """
        try:
            s = self._state[device_str][key.lower()]
            _LOGGER.debug("Zone Cache retrieve %s.%s = %s", device_str, key, s)
            return s
        except KeyError:
            raise UncachedVariableError

    def _store_cached_variable(self, device_str: str, key: str, value: str) -> None:
        """Store the current known value of a device variable into the cache.
        Calls any device callbacks.
        """
        zone_state = self._state.setdefault(device_str, {})
        key = key.lower()
        zone_state[key] = value
        _LOGGER.debug("Cache store %s.%s = %s", device_str, key, value)
        # Handle callbacks
        for callback in self._callbacks.get(device_str, []):
            callback(device_str, key, value)
        # Handle source callback
        if device_str[0] == "S":
            for controller in self._controllers.values():
                for zone in controller.zones.values():
                    source = zone.fetch_current_source()
                    if source and source.device_str() == device_str:
                        for callback in self._callbacks.get(zone.device_str(), []):
                            callback(device_str, key, value)

    def _process_response(self, res: bytes) -> [str, str]:
        s = str(res, "utf-8").strip()
        if not s:
            return None, None
        ty, payload = s[0], s[2:]
        if ty == "E":
            _LOGGER.debug("Device responded with error: %s", payload)
            raise CommandError(payload)

        m = RESPONSE_REGEX.match(payload)
        if not m:
            return ty, None

        p = m.groupdict()
        if p["source"]:
            source_id = int(p["source"])
            self._store_cached_variable(
                source_device_str(source_id), p["variable"], p["value"]
            )
        elif p["zone"]:
            controller_id = int(p["controller"])
            zone_id = int(p["zone"])
            self._store_cached_variable(
                zone_device_str(controller_id, zone_id), p["variable"], p["value"]
            )

        return ty, p["value"] or p["value_only"]

    async def _keep_alive(self) -> None:
        while True:
            await asyncio.sleep(900)  # 15 minutes
            _LOGGER.debug("Sending keep alive to device")
            await self.send_cmd("VERSION")

    async def _ioloop(
            self, reader: StreamReader, writer: StreamWriter, reconnect: bool
    ) -> None:
        queue_future = ensure_future(self._cmd_queue.get())
        net_future = ensure_future(reader.readline())
        keep_alive_task = asyncio.create_task(self._keep_alive())

        try:
            _LOGGER.debug("Starting IO loop")
            while True:
                done, _ = await asyncio.wait(
                    [queue_future, net_future], return_when=asyncio.FIRST_COMPLETED
                )

                if net_future in done:
                    response = net_future.result()
                    try:
                        self._process_response(response)
                    except CommandError:
                        pass
                    net_future = ensure_future(reader.readline())

                if queue_future in done:
                    cmd, future = queue_future.result()
                    cmd += "\r"
                    writer.write(bytearray(cmd, "utf-8"))
                    await writer.drain()

                    queue_future = ensure_future(self._cmd_queue.get())

                    while True:
                        response = await net_future
                        net_future = ensure_future(reader.readline())
                        try:
                            ty, value = self._process_response(response)
                            if ty == "S":
                                future.set_result(value)
                                break
                        except CommandError as e:
                            future.set_exception(e)
                            break
        except asyncio.CancelledError:
            _LOGGER.debug("IO loop cancelled")
            self._set_connected(False)
            raise
        except asyncio.TimeoutError:
            _LOGGER.warning("Connection to Russound client timed out")
        except ConnectionResetError:
            _LOGGER.warning("Connection to Russound client reset")
        except Exception:
            _LOGGER.exception("Unhandled exception in IO loop")
            self._set_connected(False)
            raise
        finally:
            _LOGGER.debug("Cancelling all tasks...")
            writer.close()
            queue_future.cancel()
            net_future.cancel()
            keep_alive_task.cancel()
            self._set_connected(False)
            if reconnect and self._connection_started:
                _LOGGER.info("Retrying connection to Russound client in 5s")
                await asyncio.sleep(RECONNECT_DELAY)
                await self.connect(reconnect)

    async def send_cmd(self, cmd: str) -> str:
        """Send a command to the Russound client."""
        _LOGGER.debug("Sending command '%s' to Russound client", cmd)
        future: Future = Future()
        await self._cmd_queue.put((cmd, future))
        return await future

    def add_callback(self, device_str: str, callback) -> None:
        """Register a callback to be called whenever a device variable changes.
        The callback will be passed three arguments: the device_str, the variable
        name and the variable value.
        """
        callbacks = self._callbacks.setdefault(device_str, [])
        callbacks.append(callback)

    def remove_callback(self, callback) -> None:
        """Remove a previously registered callback."""
        for callbacks in self._callbacks.values():
            callbacks.remove(callback)

    def add_connection_callback(self, callback) -> None:
        """Register a callback to be called whenever the instance is connected/disconnected.
        The callback will be passed one argument: connected: bool.
        """
        self._connection_callbacks.append(callback)

    def remove_connection_callback(self, callback) -> None:
        """Removes a previously registered callback."""
        self._connection_callbacks.remove(callback)

    def _set_connected(self, connected: bool):
        self.connected = connected
        for callback in self._connection_callbacks:
            callback(connected)

    async def connect(self, reconnect=True) -> None:
        """Connect to the controller and start processing responses."""
        self._connection_started = True
        _LOGGER.info("Connecting to %s:%s", self.host, self.port)
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self._ioloop_future = ensure_future(self._ioloop(reader, writer, reconnect))
        self.rio_version = await self.send_cmd("VERSION")
        if not is_fw_version_higher(self.rio_version, MINIMUM_API_SUPPORT):
            raise UnsupportedFeatureError(
                f"Russound RIO API v{self.rio_version} is not supported. The minimum "
                f"supported version is v{MINIMUM_API_SUPPORT}"
            )
        _LOGGER.info("Connected (Russound RIO v%s})", self.rio_version)
        await self._watch_cached_devices()
        self._set_connected(True)

    async def close(self) -> None:
        """Disconnect from the controller."""
        self._connection_started = False
        _LOGGER.info("Closing connection to %s:%s", self.host, self.port)
        self._ioloop_future.cancel()
        try:
            await self._ioloop_future
        except asyncio.CancelledError:
            pass
        self._set_connected(False)

    async def set_variable(
            self, device_str: str, key: str, value: str
    ) -> Coroutine[Any, Any, str]:
        """Set a zone variable to a new value."""
        return self.send_cmd(f'SET {device_str}.{key}="{value}"')

    async def get_variable(self, device_str: str, key: str) -> str:
        """Retrieve the current value of a zone variable.  If the variable is
        not found in the local cache then the value is requested from the
        controller.
        """
        try:
            return self._retrieve_cached_variable(device_str, key)
        except UncachedVariableError:
            return await self.send_cmd(f"GET {device_str}.{key}")

    def get_cached_variable(self, device_str: str, key: str, default=None) -> str:
        """Retrieve the current value of a zone variable from the cache or
        return the default value if the variable is not present.
        """
        try:
            return self._retrieve_cached_variable(device_str, key)
        except UncachedVariableError:
            return default

    async def enumerate_controllers(self) -> dict[int, Controller]:
        """Return a list of (controller_id,
        controller_macAddress, controller_type) tuples.
        """
        controllers: dict[int, Controller] = {}
        # Search for first controller, then iterate if RNET is supported
        for controller_id in range(1, 9):
            device_str = controller_device_str(controller_id)
            try:
                controller_type = await self.get_variable(device_str, "type")
                if not controller_type:
                    continue
                mac_address = None
                try:
                    mac_address = await self.get_variable(device_str, "macAddress")
                except CommandError:
                    pass
                firmware_version = None
                if is_feature_supported(
                        self.rio_version, FeatureFlag.PROPERTY_FIRMWARE_VERSION
                ):
                    firmware_version = await self.get_variable(
                        device_str, "firmwareVersion"
                    )
                controller = Controller(
                    self,
                    controllers.get(1),
                    controller_id,
                    mac_address,
                    controller_type,
                    firmware_version,
                )
                await controller.fetch_configuration()
                controllers[controller_id] = controller
            except CommandError:
                continue
        self._controllers = controllers
        return controllers

    @property
    def supported_features(self) -> list[FeatureFlag]:
        """Gets a list of features supported by the controller."""
        flags: list[FeatureFlag] = []
        for key, value in FLAGS_BY_VERSION.items():
            if is_fw_version_higher(self.rio_version, key):
                for flag in value:
                    flags.append(flag)
        return flags

    async def watch(self, device_str: str) -> str:
        """Watch a device."""
        self._watched_devices[device_str] = True
        return await self.send_cmd(f"WATCH {device_str} ON")

    async def unwatch(self, device_str: str) -> str:
        """Unwatch a device."""
        del self._watched_devices[device_str]
        return await self.send_cmd(f"WATCH {device_str} OFF")

    async def _watch_cached_devices(self) -> None:
        _LOGGER.debug("Watching cached devices")
        for device in self._watched_devices.keys():
            await self.watch(device)

    async def init_sources(self) -> None:
        """Return a list of (zone_id, zone) tuples."""
        self.sources = {}
        for source_id in range(1, MAX_SOURCE):
            try:
                device_str = source_device_str(source_id)
                name = await self.get_variable(device_str, "name")
                if name:
                    source = Source(self, source_id, name)
                    await source.fetch_configuration()
                    self.sources[source_id] = source
            except CommandError:
                break


class Controller:
    """Uniquely identifies a controller."""

    def __init__(
            self,
            instance: Russound,
            parent_controller: Controller,
            controller_id: int,
            mac_address: str,
            controller_type: str,
            firmware_version: str,
    ) -> None:
        """Initialize the controller."""
        self.instance = instance
        self.parent_controller = parent_controller
        self.controller_id = controller_id
        self.mac_address = mac_address
        self.controller_type = controller_type
        self.firmware_version = firmware_version
        self.zones: dict[int, Zone] = {}
        self.max_zones = get_max_zones(controller_type)

    async def fetch_configuration(self) -> None:
        """Fetches source and zone configuration from controller."""
        await self._init_zones()

    def __str__(self) -> str:
        """Returns a string representation of the controller."""
        return f"{self.controller_id}"

    def __eq__(self, other: object) -> bool:
        """Equality check."""
        return (
                hasattr(other, "controller_id")
                and other.controller_id == self.controller_id
        )

    def __hash__(self) -> int:
        """Hashes the controller id."""
        return hash(str(self))

    async def _init_zones(self) -> None:
        """Return a list of (zone_id, zone) tuples."""
        self.zones = {}
        for zone_id in range(1, self.max_zones + 1):
            try:
                device_str = zone_device_str(self.controller_id, zone_id)
                name = await self.instance.get_variable(device_str, "name")
                if name:
                    zone = Zone(self.instance, self, zone_id, name)
                    await zone.fetch_configuration()
                    self.zones[zone_id] = zone

            except CommandError:
                break

    def add_callback(self, callback) -> None:
        """Add a callback function to be called when a zone is changed."""
        self.instance.add_callback(controller_device_str(self.controller_id), callback)

    def remove_callback(self, callback) -> None:
        """Remove a callback function to be called when a zone is changed."""
        self.instance.remove_callback(callback)


class Zone:
    """Uniquely identifies a zone

    Russound controllers can be linked together to expand the total zone count.
    Zones are identified by their zone index (1-N) within the controller they
    belong to and the controller index (1-N) within the entire system.
    """

    def __init__(
            self, instance: Russound, controller: Controller, zone_id: int, name: str
    ) -> None:
        """Initialize a zone object."""
        self.instance = instance
        self.controller = controller
        self.zone_id = int(zone_id)
        self.name = name

    async def fetch_configuration(self) -> None:
        """Fetches zone configuration from controller."""
        for prop in ZONE_PROPERTIES:
            try:
                await self.instance.get_variable(self.device_str(), prop)
            except CommandError:
                continue

    def __str__(self) -> str:
        """Return a string representation of the zone."""
        return f"{self.controller.mac_address} > Z{self.zone_id}"

    def __eq__(self, other: object) -> bool:
        """Equality check."""
        return (
                hasattr(other, "zone_id")
                and hasattr(other, "controller")
                and other.zone_id == self.zone_id
                and other.controller == self.controller
        )

    def __hash__(self) -> int:
        """Hashes the zone id."""
        return hash(str(self))

    def device_str(self) -> str:
        """Generate a string that can be used to reference this zone in a RIO
        command
        """
        return zone_device_str(self.controller.controller_id, self.zone_id)

    async def watch(self) -> str:
        """Add a zone to the watchlist.
        Zones on the watchlist will push all
        state changes (and those of the source they are currently connected to)
        back to the client.
        """
        return await self.instance.watch(self.device_str())

    async def unwatch(self) -> str:
        """Remove a zone from the watchlist."""
        return await self.instance.unwatch(self.device_str())

    def add_callback(self, callback) -> None:
        """Adds a callback function to be called when a zone is changed."""
        self.instance.add_callback(self.device_str(), callback)

    def remove_callback(self, callback) -> None:
        """Remove a zone from the watchlist."""
        self.instance.remove_callback(callback)

    async def send_event(self, event_name, *args) -> str:
        """Send an event to a zone."""
        cmd = f"EVENT {self.device_str()}!{event_name} {" ".join(str(x) for x in args)}"
        return await self.instance.send_cmd(cmd)

    def _get(self, variable, default=None) -> str:
        return self.instance.get_cached_variable(self.device_str(), variable, default)

    @property
    def current_source(self) -> str:
        """Return the current source."""
        # Default to one if not available at the present time
        return self._get("currentSource", "1")

    def fetch_current_source(self) -> Zone:
        """Return the current source as a source object."""
        current_source = int(self.current_source)
        return self.instance.sources[current_source]

    @property
    def volume(self) -> str:
        """Return the current volume."""
        return self._get("volume", "0")

    @property
    def bass(self) -> str:
        """Return the current bass."""
        return self._get("bass")

    @property
    def treble(self) -> str:
        """Return the current treble."""
        return self._get("treble")

    @property
    def balance(self) -> str:
        """Return the current balance."""
        return self._get("balance")

    @property
    def loudness(self) -> str:
        """Return the current loudness."""
        return self._get("loudness")

    @property
    def turn_on_volume(self) -> str:
        """Return the current turn on the volume."""
        return self._get("turnOnVolume")

    @property
    def do_not_disturb(self) -> str:
        """Return the current do-not-disturb."""
        return self._get("doNotDisturb")

    @property
    def party_mode(self) -> str:
        """Return the current party mode."""
        return self._get("partyMode")

    @property
    def status(self) -> str:
        """Return the current status of the zone."""
        return self._get("status", "OFF")

    @property
    def is_mute(self) -> str:
        """Return whether the zone is muted or not."""
        return self._get("mute")

    @property
    def shared_source(self) -> str:
        """Return the current shared source."""
        return self._get("sharedSource")

    @property
    def last_error(self) -> str:
        """Return the last error."""
        return self._get("lastError")

    @property
    def page(self) -> str:
        """Return the current page."""
        return self._get("page")

    @property
    def sleep_time_default(self) -> str:
        """Return the current sleep time in seconds."""
        return self._get("sleepTimeDefault")

    @property
    def sleep_time_remaining(self) -> str:
        """Return the current sleep time remaining in seconds."""
        return self._get("sleepTimeRemaining")

    @property
    def enabled(self) -> str:
        """Return whether the zone is enabled."""
        return self._get("enabled")

    async def mute(self) -> str:
        """Mute the zone."""
        return await self.send_event("ZoneMuteOn")

    async def unmute(self) -> str:
        """Unmute the zone."""
        return await self.send_event("ZoneMuteOff")

    async def set_volume(self, volume: str) -> str:
        """Set the volume."""
        return await self.send_event("KeyPress", "Volume", volume)

    async def volume_up(self) -> str:
        """Volume up the zone."""
        return await self.send_event("KeyPress", "VolumeUp")

    async def volume_down(self) -> str:
        """Volume down the zone."""
        return await self.send_event("KeyPress", "VolumeDown")

    async def previous(self) -> str:
        """Go to the previous song."""
        return await self.send_event("KeyPress", "Previous")

    async def next(self) -> str:
        """Go to the next song."""
        return await self.send_event("KeyPress", "Next")

    async def stop(self) -> str:
        """Stop the current song."""
        return await self.send_event("KeyPress", "Stop")

    async def pause(self) -> str:
        """Pause the current song."""
        return await self.send_event("KeyPress", "Pause")

    async def play(self) -> str:
        """Play the queued song."""
        return await self.send_event("KeyPress", "Play")

    async def zone_on(self) -> str:
        """Turn on the zone."""
        return await self.send_event("ZoneOn")

    async def zone_off(self) -> str:
        """Turn off the zone."""
        return await self.send_event("ZoneOff")

    async def select_source(self, source: int) -> str:
        """Select a source."""
        return await self.send_event("SelectSource", source)


class Source:
    """Uniquely identifies a Source."""

    def __init__(
            self, instance: Russound, source_id: int, name: str
    ) -> None:
        """Initialize a Source."""
        self.instance = instance
        self.source_id = int(source_id)
        self.name = name

    async def fetch_configuration(self) -> None:
        """Fetch the current configuration of the source."""
        for prop in SOURCE_PROPERTIES:
            try:
                await self.instance.get_variable(self.device_str(), prop)
            except CommandError:
                continue

    def __str__(self) -> str:
        """Return the current configuration of the source."""
        return f"S{self.source_id}"

    def __eq__(self, other: object) -> bool:
        """Equality check."""
        return (
                hasattr(other, "source_id")
                and other.source_id == self.source_id
        )

    def __hash__(self) -> int:
        """Hash the current configuration of the source."""
        return hash(str(self))

    def device_str(self) -> str:
        """Generate a string that can be used to reference this zone in a RIO
        command.
        """
        return source_device_str(self.source_id)

    def add_callback(self, callback: Any) -> None:
        """Add a callback function to the zone."""
        self.instance.add_callback(self.device_str(), callback)

    def remove_callback(self, callback: Any) -> None:
        """Remove a callback from the source."""
        self.instance.remove_callback(callback)

    async def watch(self) -> str:
        """Add a source to the watchlist.
        Sources on the watchlist will push all
        state changes (and those of the source they are currently connected to)
        back to the client.
        """
        return await self.instance.watch(self.device_str())

    async def unwatch(self) -> str:
        """Remove a source from the watchlist."""
        return await self.instance.unwatch(self.device_str())

    async def send_event(self, event_name: str, *args: tuple[str, ...]) -> str:
        """Send an event to a source."""
        cmd = (
            f"EVENT {self.device_str()}!{event_name} %{" ".join(str(x) for x in args)}"
        )
        return await self.instance.send_cmd(cmd)

    def _get(self, variable: str) -> str:
        return self.instance.get_cached_variable(self.device_str(), variable)

    @property
    def type(self) -> str:
        """Get the type of the source."""
        return self._get("type")

    @property
    def channel(self) -> str:
        """Get the channel of the source."""
        return self._get("channel")

    @property
    def cover_art_url(self) -> str:
        """Get the cover art url of the source."""
        return self._get("coverArtURL")

    @property
    def channel_name(self) -> str:
        """Get the current channel name of the source."""
        return self._get("channelName")

    @property
    def genre(self) -> str:
        """Get the current genre of the source."""
        return self._get("genre")

    @property
    def artist_name(self) -> str:
        """Get the current artist of the source."""
        return self._get("artistName")

    @property
    def album_name(self) -> str:
        """Get the current album of the source."""
        return self._get("albumName")

    @property
    def playlist_name(self) -> str:
        """Get the current playlist of the source."""
        return self._get("playlistName")

    @property
    def song_name(self) -> str:
        """Get the current song of the source."""
        return self._get("songName")

    @property
    def program_service_name(self) -> str:
        """Get the current program service name."""
        return self._get("programServiceName")

    @property
    def radio_text(self) -> str:
        """Get the current radio text of the source."""
        return self._get("radioText")

    @property
    def shuffle_mode(self) -> str:
        """Get the current shuffle mode of the source."""
        return self._get("shuffleMode")

    @property
    def repeat_mode(self) -> str:
        """Get the current repeat mode of the source."""
        return self._get("repeatMode")

    @property
    def mode(self) -> str:
        """Get the current mode of the source."""
        return self._get("mode")

    @property
    def play_status(self) -> str:
        """Get the current play status of the source."""
        return self._get("playStatus")

    @property
    def sample_rate(self) -> str:
        """Get the current sample rate of the source."""
        return self._get("sampleRate")

    @property
    def bit_rate(self) -> str:
        """Get the current bit rate of the source."""
        return self._get("bitRate")

    @property
    def bit_depth(self) -> str:
        """Get the current bit depth of the source."""
        return self._get("bitDepth")

    @property
    def play_time(self) -> str:
        """Get the current play time of the source."""
        return self._get("playTime")

    @property
    def track_time(self) -> str:
        """Get the current track time of the source."""
        return self._get("trackTime")
