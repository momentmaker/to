from datetime import date

import aiosqlite
import pytest
import pytest_asyncio

from bot import db


@pytest_asyncio.fixture
async def conn():
    c = await aiosqlite.connect(":memory:")
    c.row_factory = aiosqlite.Row
    await db.init_schema(c)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
def dob() -> date:
    return date(1990, 1, 1)


@pytest.fixture
def tz_name() -> str:
    return "UTC"
