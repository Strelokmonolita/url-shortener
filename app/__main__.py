"""Local development entry point.

Run the API using settings from the environment::

    python -m app

Set ``DEBUG=true`` to enable live reload.
"""

import uvicorn

from app.core.config import settings


def main() -> None:
    """Start the ASGI server using settings from the environment."""
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
