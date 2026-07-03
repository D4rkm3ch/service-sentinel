import logging

import markdown
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.config import settings
from app.scheduler import start_scheduler, trigger_check_now
from app.webhook import router as webhook_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("release_radar")

app = FastAPI(title="release-radar")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
app.include_router(webhook_router)


@app.on_event("startup")
def on_startup():
    for problem in settings.validate():
        logger.warning(problem)
    db.init_db()
    start_scheduler()
    logger.info("release-radar started")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def dashboard(request: Request):
    updates = db.list_recent_updates(limit=100)
    containers = db.all_container_states()
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "updates": updates, "containers": containers}
    )


@app.post("/check-now")
def check_now():
    trigger_check_now()
    return RedirectResponse(url="/", status_code=303)


@app.get("/updates/{update_id}")
def update_detail(request: Request, update_id: int):
    update = db.get_update(update_id)
    if update is None:
        raise HTTPException(status_code=404, detail="Update not found")
    summary_html = markdown.markdown(update["summary_markdown"]) if update["summary_markdown"] else None
    return templates.TemplateResponse(
        "detail.html", {"request": request, "update": update, "summary_html": summary_html}
    )


@app.post("/updates/{update_id}/read")
def mark_read(update_id: int):
    db.mark_update_status(update_id, "read")
    return RedirectResponse(url=f"/updates/{update_id}", status_code=303)
