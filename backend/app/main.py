from collections import defaultdict, deque
from time import monotonic
from urllib.parse import urlparse
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.exc import IntegrityError
from .config import get_settings, ROOT
from .auth_routes import router as auth
from .task_routes import router as tasks
from .quote_routes import router as quotes
from .outreach_routes import router as outreach
from .file_routes import router as files
from .workspace_routes import router as workspace
from .ai_routes import router as ai

app = FastAPI(title="NIRA API", version="0.2.0", description="Закупки и продажи: ИИ-анализ документов, сравнение КП, подготовка объявлений и писем, локальный OCR и поиск.", docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json")
settings = get_settings()
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts.split(","))
attempts = defaultdict(deque)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.netloc != request.url.netloc or parsed.scheme != request.url.scheme:
                return JSONResponse({"detail": "Запрос с другого сайта запрещён"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Межсайтовый запрос запрещён"}, status_code=403)
    if request.url.path in {"/api/v1/auth/login", "/api/v1/auth/register", "/api/v1/auth/accept-invitation"} and request.method == "POST":
        key = request.client.host if request.client else "unknown"
        current = monotonic()
        queue = attempts[key]
        while queue and queue[0] < current - 60:
            queue.popleft()
        if len(queue) >= 20:
            return JSONResponse({"detail": "Слишком много попыток. Повторите через минуту"}, status_code=429)
        queue.append(current)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/v1"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(IntegrityError)
async def integrity_error(request, error):
    return JSONResponse({"detail": "Запись уже существует или данные изменились. Обновите страницу"}, status_code=409)


@app.get("/api/health", tags=["Состояние"])
def health():
    return {"status": "ok", "version": "0.2.0", "external_actions_enabled": False}


for router in (auth, tasks, quotes, outreach, files, workspace, ai):
    app.include_router(router)


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/app/")


if (ROOT / "frontend").exists():
    app.mount("/app", StaticFiles(directory=ROOT / "frontend", html=True), name="app")
if (ROOT / "design").exists():
    app.mount("/design", StaticFiles(directory=ROOT / "design", html=True), name="design")
