# fluxdiff/rag/server.py
"""
Single-URL server for a hosted FluxLink Chat (used by the Dockerfile).

    /            → redirects to /chat
    /api/...     → the chat API (fluxdiff/rag/api.py), e.g. /api/chat, /api/repos
    /chat, ...   → the built React app (single-page app: unknown paths get index.html)

Serving the page and the API from one origin means one deployment, one URL
and no CORS setup.  Build the frontend with VITE_API_URL=/api (the
Dockerfile does) and point FLUXLINK_FRONTEND_DIST at the build.

Run locally:   uvicorn fluxdiff.rag.server:app --port 8000
"""

import os

from starlette.applications import Starlette
from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DIST = os.path.join(_REPO_ROOT, "fluxdiff-ui", "fluxlink", "dist")


def create_app(api_app=None, dist: str = None) -> Starlette:
    if api_app is None:
        from fluxdiff.rag.api import app as api_app
    dist = os.path.abspath(dist or os.getenv("FLUXLINK_FRONTEND_DIST") or DEFAULT_DIST)
    index_html = os.path.join(dist, "index.html")

    async def home(request):
        return RedirectResponse("/chat")

    async def frontend(request):
        path = request.path_params.get("path", "")
        candidate = os.path.abspath(os.path.join(dist, path))
        # Serve real files (JS/CSS/images) from inside dist only.
        if path and candidate.startswith(dist + os.sep) and os.path.isfile(candidate):
            return FileResponse(candidate)
        if os.path.isfile(index_html):
            return FileResponse(index_html)       # client-side routes such as /chat
        return PlainTextResponse(
            f"FluxLink frontend not built: {index_html} is missing.\n"
            f"Run `npm run build` in fluxdiff-ui/fluxlink with VITE_API_URL=/api, "
            f"or set FLUXLINK_FRONTEND_DIST.", status_code=404)

    return Starlette(routes=[
        Route("/", home),
        Mount("/api", app=api_app),
        Route("/{path:path}", frontend),
    ])


def __getattr__(name):
    # `uvicorn fluxdiff.rag.server:app` builds the app on first access, so
    # importing this module (e.g. in tests) does not import the chat API.
    if name == "app":
        app = create_app()
        globals()["app"] = app
        return app
    raise AttributeError(name)
