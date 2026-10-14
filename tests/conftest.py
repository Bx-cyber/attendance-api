"""Test fixtures.

If MongoDB answers on MONGO_URI (default mongodb://localhost:27017) the tests run against it in a
throw-away database. Otherwise they fall back to mongomock, which covers the CRUD paths but not the
aggregation features ($setWindowFields, $facet ...); tests marked `real_mongo` are skipped then.
"""
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

os.environ.setdefault("MONGO_DB", "attendance_test_" + uuid.uuid4().hex[:8])

from app import main  # noqa: E402


def _real_mongo_available() -> bool:
    try:
        c = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=1500)
        c.admin.command("ping")
        return True
    except Exception:
        return False


REAL = _real_mongo_available()
if not REAL:
    import mongomock

    main.client = mongomock.MongoClient(tz_aware=True)
    main.db = main.client["attendance_test"]


def pytest_collection_modifyitems(config, items):
    if REAL:
        return
    skip = pytest.mark.skip(reason="needs a real MongoDB (aggregation features missing in mongomock)")
    for item in items:
        if "real_mongo" in item.keywords:
            item.add_marker(skip)


def _empty_collections():
    # delete_many instead of drop_database: managed services such as Atlas often give the
    # test user read/write but not the "dropData" privilege. Indexes are kept (and re-ensured).
    for name in ("employees", "attendance_logs"):
        main.db[name].delete_many({})


@pytest.fixture()
def api():
    """A TestClient on an empty database with the indexes in place."""
    _empty_collections()
    main.ensure_indexes()
    with TestClient(main.app) as c:
        yield c
    _empty_collections()
