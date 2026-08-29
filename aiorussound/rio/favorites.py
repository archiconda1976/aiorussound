"""Read-only helpers for RIO system and zone favorites."""

from __future__ import annotations

import logging
import re
from typing import Protocol, TypedDict

import orjson

from aiorussound.const import FeatureFlag
from aiorussound.exceptions import CommandError, RussoundError, UnsupportedFeatureError
from aiorussound.rio.models import FavoriteScope, RussoundFavorite
from aiorussound.util import is_feature_supported

SYSTEM_DEVICE_STR = "System"
SYSTEM_FAVORITE_LIMIT = 32
ZONE_FAVORITE_LIMIT = 4

_CONTROLLER_ZONE_RE = re.compile(r"^C\[\d+]\.Z\[\d+]$")
_LOGGER = logging.getLogger(__package__)


class FavoriteClient(Protocol):
    """The client surface needed to read favorites."""

    rio_version: str | None

    async def get_variable(self, device_str: str, key: str) -> str:
        """Read a RIO variable."""


class FavoriteMetadata(TypedDict, total=False):
    """Optional metadata fields added to the RIO favorite API in 1.15."""

    source_id: int | None
    source_type: str | None
    provider_mode: str | None
    album_cover_url: str | None
    player_data: str | None


class FavoriteCommandClient(FavoriteClient, Protocol):
    """The client surface needed to change favorites."""

    async def request(self, cmd: str) -> str:
        """Send a complete RIO command."""


async def discover_system_favorites(
    client: FavoriteClient, *, include_player_data: bool = False
) -> tuple[RussoundFavorite, ...]:
    """Return all valid system favorites exposed by the controller."""
    return await _discover_favorites(
        client,
        device_str=SYSTEM_DEVICE_STR,
        scope=FavoriteScope.SYSTEM,
        favorite_limit=SYSTEM_FAVORITE_LIMIT,
        include_player_data=include_player_data,
    )


async def discover_zone_favorites(
    client: FavoriteClient,
    zone_device_str: str,
    *,
    include_player_data: bool = False,
) -> tuple[RussoundFavorite, ...]:
    """Return all valid favorites for a controller-routed zone."""
    if not _CONTROLLER_ZONE_RE.match(zone_device_str):
        raise ValueError(
            "Zone favorites must target a controller zone, such as C[1].Z[1]"
        )
    return await _discover_favorites(
        client,
        device_str=zone_device_str,
        scope=FavoriteScope.ZONE,
        favorite_limit=ZONE_FAVORITE_LIMIT,
        include_player_data=include_player_data,
    )


async def save_system_favorite(
    client: FavoriteCommandClient,
    zone_device_str: str,
    favorite_id: int,
    name: str,
) -> str:
    """Save the current zone source as a system-wide favorite."""
    return await _request_favorite_event(
        client,
        zone_device_str,
        "SaveSystemFavorite",
        _favorite_name(name),
        _system_favorite_id(favorite_id),
    )


async def save_zone_favorite(
    client: FavoriteCommandClient,
    zone_device_str: str,
    favorite_id: int,
    name: str,
) -> str:
    """Save the current zone source as a zone-specific favorite."""
    return await _request_favorite_event(
        client,
        zone_device_str,
        "SaveZoneFavorite",
        _favorite_name(name),
        _zone_favorite_id(favorite_id),
    )


async def restore_system_favorite(
    client: FavoriteCommandClient, zone_device_str: str, favorite_id: int
) -> str:
    """Restore a system-wide favorite in the target zone."""
    return await _request_favorite_event(
        client,
        zone_device_str,
        "RestoreSystemFavorite",
        _system_favorite_id(favorite_id),
    )


async def restore_zone_favorite(
    client: FavoriteCommandClient, zone_device_str: str, favorite_id: int
) -> str:
    """Restore a zone-specific favorite in the target zone."""
    return await _request_favorite_event(
        client,
        zone_device_str,
        "RestoreZoneFavorite",
        _zone_favorite_id(favorite_id),
    )


async def delete_system_favorite(
    client: FavoriteCommandClient, zone_device_str: str, favorite_id: int
) -> str:
    """Delete a system-wide favorite from the target zone context."""
    return await _request_favorite_event(
        client,
        zone_device_str,
        "DeleteSystemFavorite",
        _system_favorite_id(favorite_id),
    )


async def delete_zone_favorite(
    client: FavoriteCommandClient, zone_device_str: str, favorite_id: int
) -> str:
    """Delete a zone-specific favorite from the target zone context."""
    return await _request_favorite_event(
        client,
        zone_device_str,
        "DeleteZoneFavorite",
        _zone_favorite_id(favorite_id),
    )


async def rename_system_favorite(
    client: FavoriteCommandClient, favorite_id: int, name: str
) -> str:
    """Rename an existing system favorite using the RIO SET command."""
    _require_favorites_support(client)
    if not _supports_feature(client, FeatureFlag.SUPPORT_SYSTEM_FAVORITE_RENAME):
        raise UnsupportedFeatureError(
            f"System favorite rename is not supported in RIO v{client.rio_version}"
        )
    return await client.request(
        f"SET {SYSTEM_DEVICE_STR}.favorite[{_system_favorite_id(favorite_id)}].name="
        f"{_favorite_name(name)}"
    )


async def restore_player_data(
    client: FavoriteCommandClient, zone_device_str: str, player_data: str
) -> str:
    """Restore playback into a zone from a favorite's JSON player-data string."""
    _require_favorites_support(client)
    if not _supports_feature(client, FeatureFlag.SUPPORT_SYSTEM_FAVORITE_SOURCE):
        raise UnsupportedFeatureError(
            f"Favorite player data is not supported in RIO v{client.rio_version}"
        )
    if not isinstance(player_data, str):
        raise TypeError("player_data must be a JSON string")
    try:
        orjson.loads(player_data)
    except (orjson.JSONDecodeError, TypeError) as err:
        raise ValueError("player_data must be a valid JSON string") from err
    return await _request_favorite_event(
        client,
        zone_device_str,
        "RestorePlayerData",
        _quoted_rio_string(player_data),
        require_support=False,
    )


async def _discover_favorites(
    client: FavoriteClient,
    *,
    device_str: str,
    scope: FavoriteScope,
    favorite_limit: int,
    include_player_data: bool,
) -> tuple[RussoundFavorite, ...]:
    """Read valid favorite records from a single RIO device scope."""
    _require_favorites_support(client)
    has_extended_metadata = _supports_feature(
        client, FeatureFlag.SUPPORT_SYSTEM_FAVORITE_SOURCE
    )
    favorites: list[RussoundFavorite] = []

    for favorite_id in range(1, favorite_limit + 1):
        prefix = f"favorite[{favorite_id}]"
        try:
            valid = await client.get_variable(device_str, f"{prefix}.valid")
        except CommandError:
            _LOGGER.debug(
                "Stopping favorite discovery at unavailable %s slot %s",
                scope,
                favorite_id,
            )
            break
        if valid.upper() != "TRUE":
            continue

        name = await _get_optional(client, device_str, f"{prefix}.name")
        metadata = await _get_favorite_metadata(
            client,
            device_str,
            prefix,
            enabled=has_extended_metadata,
            include_player_data=include_player_data,
        )
        favorites.append(
            RussoundFavorite(
                favorite_id=favorite_id,
                scope=scope,
                name=name,
                zone_device_str=device_str if scope is FavoriteScope.ZONE else None,
                **metadata,
            )
        )

    return tuple(favorites)


async def _get_favorite_metadata(
    client: FavoriteClient,
    device_str: str,
    prefix: str,
    *,
    enabled: bool,
    include_player_data: bool,
) -> FavoriteMetadata:
    """Read optional metadata only available with RIO 1.15 and later."""
    if not enabled:
        return {}

    source_id = _as_optional_int(
        await _get_optional(client, device_str, f"{prefix}.source")
    )
    metadata: FavoriteMetadata = {
        "source_id": source_id,
        "source_type": await _get_optional(client, device_str, f"{prefix}.sourceType"),
        "provider_mode": await _get_optional(
            client, device_str, f"{prefix}.providerMode"
        ),
        "album_cover_url": await _get_optional(
            client, device_str, f"{prefix}.albumCoverURL"
        ),
    }
    if include_player_data:
        metadata["player_data"] = await _get_optional(
            client, device_str, f"{prefix}.playerData"
        )
    return metadata


async def _get_optional(
    client: FavoriteClient, device_str: str, key: str
) -> str | None:
    """Return an optional favorite field without failing the whole record."""
    try:
        return await client.get_variable(device_str, key)
    except CommandError:
        _LOGGER.debug("Favorite field %s.%s is unavailable", device_str, key)
        return None


def _require_favorites_support(client: FavoriteClient) -> None:
    """Raise a useful error before issuing unsupported favorite requests."""
    if client.rio_version is None:
        raise RussoundError(
            "Russound client must be connected before reading favorites"
        )
    if not _supports_feature(client, FeatureFlag.SUPPORT_FAVORITES):
        raise UnsupportedFeatureError(
            f"Russound favorites are not supported in RIO v{client.rio_version}"
        )


def _supports_feature(client: FavoriteClient, feature: FeatureFlag) -> bool:
    """Return whether a client version supports a given RIO feature."""
    return client.rio_version is not None and is_feature_supported(
        client.rio_version, feature
    )


def _as_optional_int(value: str | None) -> int | None:
    """Parse an optional source identifier without rejecting an entire favorite."""
    if value is None or not value:
        return None
    try:
        return int(value)
    except ValueError:
        _LOGGER.warning("Ignoring non-numeric favorite source identifier %r", value)
        return None


async def _request_favorite_event(
    client: FavoriteCommandClient,
    zone_device_str: str,
    event_name: str,
    *args: str,
    require_support: bool = True,
) -> str:
    """Send a validated favorite event through a controller-zone target."""
    if not _CONTROLLER_ZONE_RE.match(zone_device_str):
        raise ValueError(
            "Favorite events must target a controller zone, such as C[1].Z[1]"
        )
    if require_support:
        _require_favorites_support(client)
    arguments = f" {' '.join(args)}" if args else ""
    return await client.request(f"EVENT {zone_device_str}!{event_name}{arguments}")


def _system_favorite_id(favorite_id: int) -> str:
    """Validate and format a system favorite number."""
    return _favorite_id(favorite_id, SYSTEM_FAVORITE_LIMIT, "system")


def _zone_favorite_id(favorite_id: int) -> str:
    """Validate and format a zone favorite number."""
    return _favorite_id(favorite_id, ZONE_FAVORITE_LIMIT, "zone")


def _favorite_id(favorite_id: int, limit: int, scope: str) -> str:
    """Validate a positive favorite number within its RIO-defined limit."""
    if isinstance(favorite_id, bool) or not isinstance(favorite_id, int):
        raise TypeError(f"{scope} favorite_id must be an integer")
    if not 1 <= favorite_id <= limit:
        raise ValueError(f"{scope} favorite_id must be between 1 and {limit}")
    return str(favorite_id)


def _favorite_name(name: str) -> str:
    """Validate and quote a RIO favorite name."""
    if not isinstance(name, str):
        raise TypeError("Favorite name must be a string")
    if not 1 <= len(name) <= 50:
        raise ValueError("Favorite name must contain between 1 and 50 characters")
    return _quoted_rio_string(name)


def _quoted_rio_string(value: str) -> str:
    """Quote a string argument while preventing command-line injection."""
    if "\r" in value or "\n" in value:
        raise ValueError("RIO string arguments cannot contain line breaks")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
