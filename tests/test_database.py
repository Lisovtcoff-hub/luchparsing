import sqlite3

import pytest

from database.db import Database


def test_route_crud(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    try:
        route_id = db.create_route("Moscow", "Kazan", 820)
        assert db.list_routes() == [
            {
                "id": route_id,
                "from_city": "Moscow",
                "to_city": "Kazan",
                "distance_km": 820,
            }
        ]

        db.update_route(route_id, "Moscow", "Samara", 1050)
        assert db.list_routes()[0]["to_city"] == "Samara"

        db.delete_route(route_id)
        assert db.list_routes() == []
    finally:
        db.close()


def test_duplicate_route_is_rejected(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    try:
        db.create_route("Moscow", "Kazan", 820)

        with pytest.raises(sqlite3.IntegrityError):
            db.create_route("Moscow", "Kazan", 820)
    finally:
        db.close()


def test_preset_crud(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    try:
        preset_id = db.create_preset(2, 25.0, 0.15, '{"length": 50}')
        preset = db.list_presets()[0]
        assert preset["id"] == preset_id
        assert preset["places"] == 2
        assert preset["weight_kg"] == 25.0

        db.update_preset(preset_id, 3, 40.0, 0.25, None)
        updated = db.list_presets()[0]
        assert updated["places"] == 3
        assert updated["volume_m3"] == 0.25

        db.delete_preset(preset_id)
        assert db.list_presets() == []
    finally:
        db.close()
