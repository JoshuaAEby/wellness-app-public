"""Household recipes read without assuming that app's schema (2026-10-29).

The household app dropped recipes.tags (its 0031, 2026-10-28); the hard-coded SELECT then
failed on every call and Log Food's Recipes button disappeared.
"""
import json
import sqlite3

from app.config import settings
from app.services import recipes as rs

ING = json.dumps([{"name": "rice", "quantity": "200", "unit": "g"}])


def _make(path, with_tags):
    con = sqlite3.connect(path)
    cols = "id INTEGER PRIMARY KEY, title TEXT, servings INTEGER, ingredients JSON, steps JSON, " \
           + ("tags JSON, " if with_tags else "") + "category TEXT, updated_at TEXT"
    con.execute(f"create table recipes ({cols})")
    row = [1, "Fried rice", 4, ING, "[]"] + (['["asian"]'] if with_tags else []) + ["main", "2026-10-28"]
    con.execute(f"insert into recipes values ({', '.join('?' * len(row))})", row)
    con.commit()
    con.close()


def test_reads_with_and_without_tags(tmp_path, monkeypatch):
    for with_tags in (True, False):
        p = tmp_path / f"h{with_tags}.db"
        _make(p, with_tags)
        monkeypatch.setattr(settings, "household_db_path", str(p))
        [r] = rs.list_recipes()
        assert r["recipe_id"] == 1 and r["title"] == "Fried rice" and r["servings"] == 4
        assert r["tags"] == (["asian"] if with_tags else [])
        assert r["n_ingredients"] == 1
        assert r["default_meal_type"] == "dinner"           # "main" = the old "meal"
        # The hash covers ingredients + servings only, so dropping tags marks nothing stale.
        assert r["source_hash"] == rs.recipe_hash(ING, 4)
