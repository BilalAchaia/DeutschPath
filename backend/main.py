from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import os
import platform
import signal
import subprocess
import threading

load_dotenv()

from database import init_db
from routers import books, words, grammar, scenarios, users, tts, settings, writing, annotations

# Directory holding the statically-exported Next.js frontend (populated in the
# Docker image). When absent (local dev with `next dev`) the app runs API-only.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="DeutschPath API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        os.getenv("FRONTEND_URL", "http://localhost:3000"),
        "http://localhost:3000",
        "http://localhost:9731",  # launcher progress page
        "http://127.0.0.1:3000",  # Windows: localhost may resolve to IPv6
        "http://127.0.0.1:9731",
        "null",                   # file:// origin (manual setup.html open)
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(books.router, prefix="/api")
app.include_router(words.router, prefix="/api")
app.include_router(grammar.router, prefix="/api")
app.include_router(scenarios.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(tts.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(writing.router, prefix="/api")
app.include_router(annotations.router, prefix="/api")


@app.on_event("startup")
def startup():
    init_db()
    key = os.getenv("GEMINI_API_KEY", "")
    if not key or key == "your-gemini-api-key-here":
        print("\nWARNING: GEMINI_API_KEY is not set.")
        print("   Set it as an environment variable (cloud) or in backend/.env (local).")
        print("   Get a free key at https://aistudio.google.com/apikey\n")


def _is_local_request(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


@app.post("/api/shutdown")
@app.post("/shutdown")
async def shutdown(request: Request):
    """Stop the local launcher's servers.

    This only works when the request originates from the same machine (the local
    app) or when ALLOW_SHUTDOWN=1 is explicitly set. In the cloud deployment it
    is a no-op so a visitor can never take the service down.
    """
    if not _is_local_request(request) and os.getenv("ALLOW_SHUTDOWN") != "1":
        return {"ok": False, "detail": "Remote shutdown is disabled"}

    def _do():
        import time
        time.sleep(0.4)
        if platform.system() == "Windows":
            r = subprocess.run(
                'netstat -ano | findstr ":3000 "',
                shell=True, capture_output=True, text=True,
            )
            for line in r.stdout.splitlines():
                if "LISTENING" in line:
                    parts = line.split()
                    if parts:
                        subprocess.run(["taskkill", "/F", "/PID", parts[-1]],
                                       capture_output=True)
        else:
            subprocess.run(
                "lsof -ti:3000 | xargs kill -9 2>/dev/null || true",
                shell=True, capture_output=True,
            )
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    """Cheap liveness probe (used by the platform health check)."""
    return {"status": "ok"}


@app.get("/health")
@app.get("/api/health")
async def health():
    key = os.getenv("GEMINI_API_KEY", "")
    gemini_ok = False
    gemini_error = None
    if key:
        try:
            from google import genai
            client = genai.Client(api_key=key)
            client.models.generate_content(model="gemini-2.5-flash", contents="ping")
            gemini_ok = True
        except Exception as e:
            gemini_error = str(e)[:200]
    return {"status": "ok", "gemini_ok": gemini_ok, "gemini_error": gemini_error}


# ── Static frontend (Next.js export) ────────────────────────────────────────
# Mounted last so it never shadows the API routes above. Present only in the
# container build (backend/static).
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")
