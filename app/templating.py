from __future__ import annotations

from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR

templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
