"""Tests for RIO favorite mutation commands."""

from __future__ import annotations

import pytest

from aiorussound.exceptions import UnsupportedFeatureError
from aiorussound.rio.favorites import (
    delete_system_favorite,
    delete_zone_favorite,
    rename_system_favorite,
    restore_player_data,
    restore_system_favorite,
    restore_zone_favorite,
    save_system_favorite,
    save_zone_favorite,
)


class FakeFavoriteCommandClient:
    """A command-capturing client used for favorite event assertions."""

    def __init__(self, rio_version: str) -> None:
        self.rio_version = rio_version
        self.commands: list[str] = []

    async def request(self, cmd: str) -> str:
        self.commands.append(cmd)
        return "S"


@pytest.mark.asyncio
async def test_sends_favorite_mutation_commands_with_documented_targets() -> None:
    """Zone-targeted events and system SETs use the documented RIO syntax."""
    client = FakeFavoriteCommandClient("1.15.00")
    zone = "C[1].Z[2]"

    await save_system_favorite(client, zone, 1, 'Road "Trip"')
    await save_zone_favorite(client, zone, 2, "Kitchen")
    await restore_system_favorite(client, zone, 3)
    await restore_zone_favorite(client, zone, 4)
    await delete_system_favorite(client, zone, 5)
    await delete_zone_favorite(client, zone, 1)
    await rename_system_favorite(client, 2, "Renamed")
    await restore_player_data(client, zone, '{"id":"playlist-1"}')

    assert client.commands == [
        'EVENT C[1].Z[2]!SaveSystemFavorite "Road \\"Trip\\"" 1',
        'EVENT C[1].Z[2]!SaveZoneFavorite "Kitchen" 2',
        "EVENT C[1].Z[2]!RestoreSystemFavorite 3",
        "EVENT C[1].Z[2]!RestoreZoneFavorite 4",
        "EVENT C[1].Z[2]!DeleteSystemFavorite 5",
        "EVENT C[1].Z[2]!DeleteZoneFavorite 1",
        'SET System.favorite[2].name="Renamed"',
        'EVENT C[1].Z[2]!RestorePlayerData "{\\"id\\":\\"playlist-1\\"}"',
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "favorite_id", "match"),
    [
        (save_system_favorite, 33, "system favorite_id"),
        (restore_system_favorite, 0, "system favorite_id"),
        (save_zone_favorite, 5, "zone favorite_id"),
        (delete_zone_favorite, 0, "zone favorite_id"),
    ],
)
async def test_rejects_out_of_range_favorite_ids(
    operation, favorite_id: int, match: str
) -> None:
    """Favorite mutations are constrained to RIO's documented slot ranges."""
    client = FakeFavoriteCommandClient("1.15.00")

    with pytest.raises(ValueError, match=match):
        if operation in (save_system_favorite, save_zone_favorite):
            await operation(client, "C[1].Z[1]", favorite_id, "Name")
        else:
            await operation(client, "C[1].Z[1]", favorite_id)

    assert client.commands == []


@pytest.mark.asyncio
async def test_rejects_unsafe_or_invalid_favorite_names() -> None:
    """Names cannot overflow the protocol field or introduce new commands."""
    client = FakeFavoriteCommandClient("1.15.00")

    with pytest.raises(ValueError, match="between 1 and 50"):
        await save_system_favorite(client, "C[1].Z[1]", 1, "")
    with pytest.raises(ValueError, match="between 1 and 50"):
        await save_zone_favorite(client, "C[1].Z[1]", 1, "x" * 51)
    with pytest.raises(ValueError, match="line breaks"):
        await rename_system_favorite(client, 1, "bad\nname")

    assert client.commands == []


@pytest.mark.asyncio
async def test_rejects_unsupported_or_invalid_player_data_restore() -> None:
    """RestorePlayerData requires RIO 1.15 and a JSON payload."""
    old_client = FakeFavoriteCommandClient("1.14.02")
    client = FakeFavoriteCommandClient("1.15.00")

    with pytest.raises(UnsupportedFeatureError, match="player data"):
        await restore_player_data(old_client, "C[1].Z[1]", "{}")
    with pytest.raises(ValueError, match="valid JSON"):
        await restore_player_data(client, "C[1].Z[1]", "not json")
    with pytest.raises(TypeError, match="JSON string"):
        await restore_player_data(client, "C[1].Z[1]", b"{}")  # type: ignore[arg-type]

    assert old_client.commands == []
    assert client.commands == []


@pytest.mark.asyncio
async def test_rejects_rename_before_its_rio_version() -> None:
    """System favorite rename was introduced after basic favorite support."""
    client = FakeFavoriteCommandClient("1.07.00")

    with pytest.raises(UnsupportedFeatureError, match="rename"):
        await rename_system_favorite(client, 1, "New Name")

    assert client.commands == []
