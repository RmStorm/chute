import uvicorn

from chute.app import create_app
from chute.config import get_settings
from chute.logging import configure_logging


app = create_app(get_settings())


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
    )


def run_dev() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        "chute.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )


if __name__ == "__main__":
    run()
