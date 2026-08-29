"""Read-only helpers for RIO system and zone favorites."""

from __future__ import annotations

import logging
import re
from typing import Protocol, TypedDict

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
