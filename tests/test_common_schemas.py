"""Regression tests for the Python 3.12 generic response schema."""

import pytest
from pydantic import ValidationError

from app.schemas.common import Page


def test_page_keeps_item_validation_and_serialisation() -> None:
    page = Page[int].model_validate({"items": ["1", "2"], "total": 3, "limit": 2, "offset": 0})

    assert page.items == [1, 2]
    assert page.model_dump() == {"items": [1, 2], "total": 3, "limit": 2, "offset": 0}
    assert page.has_next is True


def test_page_rejects_items_of_the_wrong_type() -> None:
    with pytest.raises(ValidationError):
        Page[int].model_validate({"items": ["invalid"], "total": 1, "limit": 1, "offset": 0})
