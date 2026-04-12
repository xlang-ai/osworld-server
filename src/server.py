import os

import uvicorn
from flask import Flask

from .http_routes import register_http_routes
from .streaming import create_asgi_app

app = Flask(__name__)
register_http_routes(app)
asgi_app = create_asgi_app(app)


def main() -> None:
    host = os.environ.get("OSWORLD_SERVER_HOST", "0.0.0.0")
    port = int(os.environ.get("OSWORLD_SERVER_PORT", "5000"))
    uvicorn.run(asgi_app, host=host, port=port, log_level="info")
