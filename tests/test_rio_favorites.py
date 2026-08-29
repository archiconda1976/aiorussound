"""Tests for RIO favorite discovery."""

from __future__ import annotations

import pytest

from aiorussound.exceptions import CommandError, UnsupportedFeatureError
from aiorussound.rio.client import ZoneControlSurface
from aiorussound.rio.favorites import (
    discover_system_favorites,
    discover_zone_favorites,
)
from aiorussound.rio.models import FavoriteScope, RussoundFavorite


class FakeFavoriteClient:
    """A small RIO client substitute with deterministic GET responses."""

    def __init__(self, rio_version: str | None, values: dict[tuple[str, str], object]):
        self.rio_version = rio_version
        self.values = values
        self.calls: list[tuple[str, str]] = []

    async def get_variable(self, device_str: str, key: str) -> str:
        self.calls.append((device_str, key))
        value = self.values.get((device_str, key))
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise CommandError(f"Unknown variable {device_str}.{key}")
        return str(value)


class FakeZoneClient:
    """A narrow client substitute used to test the zone convenience API."""

    def __init__(self, favorites: tuple[RussoundFavorite, ...]) -> None:
        self.favorites = favorites
        self.calls: list[tuple[str, bool]] = []

    async def get_zone_favorites(
        self, zone_device_str: str, *, include_player_data: bool = False
    ) -> tuple[RussoundFavorite, ...]:
        self.calls.append((zone_device_str, include_player_data))
        return self.favorites


@pytest.mark.asyncio
async def test_discovers_system_favorites_with_extended_metadata() -> None:
    """RIO 1.15 favorites include source, provider, art, and player metadata."""
    client = FakeFavoriteClient(
        "1.15.00",
        {
            ("System", "favorite[1].valid"): "TRUE",
            ("System", "favorite[1].name"): "Road Trip",
            ("System", "favorite[1].source"): "2",
            ("System", "favorite[1].sourceType"): "Russound Media Streamer",
            ("System", "favorite[1].providerMode"): "Tidal",
            ("System", "favorite[1].albumCoverURL"): "https://example.invalid/art.jpg",
            ("System", "favorite[1].playerData"): '{"id":"playlist-1"}',
            ("System", "favorite[2].valid"): "FALSE",
            ("System", "favorite[3].valid"): CommandError("No more favorites"),
        },
    )

    favorites = await discover_system_favorites(client, include_player_data=True)

    assert len(favorites) == 1
    favorite = favorites[0]
    assert favorite.favorite_id == 1
    assert favorite.scope is FavoriteScope.SYSTEM
    assert favorite.name == "Road Trip"
    assert favorite.zone_device_str is None
    assert favorite.source_id == 2
    assert favorite.source_type == "Russound Media Streamer"
    assert favorite.provider_mode == "Tidal"
    assert favorite.album_cover_url == "https://example.invalid/art.jpg"
    assert favorite.player_data == '{"id":"playlist-1"}'
    assert ("System", "favorite[2].name") not in client.calls


@pytest.mark.asyncio
async def test_discovers_zone_favorites_on_pre_extended_firmware() -> None:
    """Old RIO versions return the name but do not request 1.15 metadata."""
    zone = "C[1].Z[2]"
    client = FakeFavoriteClient(
        "1.14.02",
        {
            (zone, "favorite[1].valid"): "TRUE",
            (zone, "favorite[1].name"): "Kitchen",
            (zone, "favorite[2].valid"): "FALSE",
            (zone, "favorite[3].valid"): CommandError("Only two slots"),
        },
    )

    favorites = await discover_zone_favorites(client, zone)

    assert len(favorites) == 1
    favorite = favorites[0]
    assert favorite.favorite_id == 1
    assert favorite.scope is FavoriteScope.ZONE
    assert favorite.zone_device_str == zone
    assert favorite.name == "Kitchen"
    assert favorite.source_id is None
    assert favorite.provider_mode is None
    assert not any("source" in key for _, key in client.calls)


@pytest.mark.asyncio
async def test_does_not_read_player_data_unless_requested() -> None:
    """Large player-data payloads are opt-in until restoration is implemented."""
    client = FakeFavoriteClient(
        "1.15.00",
        {
            ("System", "favorite[1].valid"): "TRUE",
            ("System", "favorite[1].name"): "No Player Data",
            ("System", "favorite[1].source"): "1",
            ("System", "favorite[1].sourceType"): "Russound Media Streamer",
            ("System", "favorite[1].providerMode"): "Spotify",
            ("System", "favorite[1].albumCoverURL"): "",
            ("System", "favorite[2].valid"): CommandError("No more favorites"),
        },
    )

    favorites = await discover_system_favorites(client)

    assert favorites[0].player_data is None
    assert ("System", "favorite[1].playerData") not in client.calls


@pytest.mark.asyncio
async def test_rejects_unsupported_favorite_firmware() -> None:
    """Favorite GETs are not attempted before RIO 1.07 support."""
    client = FakeFavoriteClient("1.06.00", {})

    with pytest.raises(UnsupportedFeatureError, match="not supported"):
        await discover_system_favorites(client)

    assert client.calls == []


@pytest.mark.asyncio
async def test_rejects_non_controller_zone_for_favorites() -> None:
    """The zone API has the same controller-zone targeting as RIO MM."""
    client = FakeFavoriteClient("1.15.00", {})

    with pytest.raises(ValueError, match="controller zone"):
        await discover_zone_favorites(client, "S[1]")


@pytest.mark.asyncio
async def test_zone_control_surface_exposes_its_favorites() -> None:
    """Zone callers do not need to reconstruct their controller-zone path."""
    favorite = RussoundFavorite(1, FavoriteScope.ZONE, name="Kitchen")
    client = FakeZoneClient((favorite,))
    zone = ZoneControlSurface()
    zone.client = client  # type: ignore[assignment]
    zone.device_str = "C[1].Z[2]"

    assert await zone.get_favorites(include_player_data=True) == (favorite,)
    assert client.calls == [("C[1].Z[2]", True)]
