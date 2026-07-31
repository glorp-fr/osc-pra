import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.db import init_db
from app.jobs import reconcile_orphaned_jobs
from app.routers import admin, auth, suivi
from app.secret import load_or_create_secret

logger = logging.getLogger("osc-pra")

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Outscale Net Replicator")
app.add_middleware(SessionMiddleware, secret_key=load_or_create_secret())
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

app.include_router(auth.router)
app.include_router(suivi.router)
app.include_router(admin.router)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    cleaned = reconcile_orphaned_jobs()
    if cleaned:
        logger.warning("%d job(s) orphelin(s) (statut 'running' au démarrage) marqué(s) en erreur.", cleaned)


@app.get("/")
def index():
    return RedirectResponse("/suivi")
