import app


def test_package_is_importable() -> None:
    assert app.__version__
