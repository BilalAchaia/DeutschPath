import os
import shutil
import tempfile
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/settings", tags=["settings"])

# .env is one level up from this file (backend/.env)
ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)


def _read_env() -> dict:
    result: dict = {}
    if not os.path.exists(ENV_PATH):
        return result
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    return result


def _write_env(updates: dict):
    lines: list[str] = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

    written: set = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.partition("=")[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}\n")
                written.add(k)
            else:
                new_lines.append(line if line.endswith("\n") else line + "\n")
        else:
            new_lines.append(line if line.endswith("\n") else line + "\n")

    for k, v in updates.items():
        if k not in written:
            new_lines.append(f"{k}={v}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


class SettingsUpdateRequest(BaseModel):
    gemini_api_key: Optional[str] = None


class TestConnectionRequest(BaseModel):
    api_key: Optional[str] = None


@router.get("")
def get_settings():
    env = _read_env()
    # Prefer an explicit env var (e.g. set in the cloud dashboard) over .env.
    raw_key = os.getenv("GEMINI_API_KEY") or env.get("GEMINI_API_KEY", "")
    key_set = bool(raw_key and raw_key not in ("", "your-gemini-api-key-here"))
    masked = ""
    if key_set and len(raw_key) > 8:
        masked = raw_key[:8] + "•" * max(0, len(raw_key) - 12) + raw_key[-4:]
    elif key_set:
        masked = raw_key[:4] + "•" * (len(raw_key) - 4)
    return {"gemini_key_set": key_set, "gemini_key_masked": masked}


@router.post("")
def update_settings(req: SettingsUpdateRequest):
    updates: dict = {}

    if req.gemini_api_key:
        new_key = req.gemini_api_key.strip()
        updates["GEMINI_API_KEY"] = new_key
        # Update the running process immediately — no restart needed
        os.environ["GEMINI_API_KEY"] = new_key
        try:
            from services.ai_service import reinit_client
            reinit_client(new_key)
        except Exception:
            pass

    if not updates:
        return {"ok": True, "message": "Nothing to update"}

    if not os.path.exists(ENV_PATH):
        updates.setdefault("FRONTEND_URL", "http://localhost:3000")

    _write_env(updates)
    return {"ok": True}


@router.post("/test-connection")
def test_connection(req: TestConnectionRequest):
    from services.ai_service import test_connection as ai_test
    key = req.api_key.strip() if req.api_key else None
    result = ai_test(key)
    if not result["ok"]:
        raise HTTPException(400, result.get("error", "Connection failed"))
    return {"ok": True}


@router.get("/usage")
def get_usage():
    from services.usage_tracker import get_stats
    return get_stats()


@router.delete("/usage")
def reset_usage():
    from services.usage_tracker import reset
    reset()
    return {"ok": True}


@router.delete("/gemini-key")
def delete_gemini_key():
    os.environ.pop("GEMINI_API_KEY", None)
    try:
        from services.ai_service import reinit_client
        reinit_client("")
    except Exception:
        pass
    _write_env({"GEMINI_API_KEY": ""})
    return {"ok": True}


@router.get("/backup")
def download_backup():
    from database import DB_PATH, IS_SQLITE
    if not IS_SQLITE:
        raise HTTPException(
            400,
            "Backup is only available for the local SQLite database. "
            "Use your database provider's backup tools for Postgres.",
        )
    if not os.path.isfile(DB_PATH):
        raise HTTPException(404, "Database not found")
    return FileResponse(
        DB_PATH,
        media_type="application/octet-stream",
        filename="deutschpath_backup.db",
    )


@router.post("/restore")
async def restore_backup(file: UploadFile = File(...)):
    from database import DB_PATH, IS_SQLITE, engine
    if not IS_SQLITE:
        raise HTTPException(
            400,
            "Restore is only available for the local SQLite database. "
            "Use your database provider's tools for Postgres.",
        )
    content = await file.read()
    if not content.startswith(b"SQLite format 3"):
        raise HTTPException(400, "Not a valid SQLite database file")
    # Write to a temp file first, then atomically replace
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(content)
        engine.dispose()
        shutil.move(tmp_path, DB_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise HTTPException(500, "Failed to restore database")
    return {"ok": True}
