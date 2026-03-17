from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import ui, v1
from app.core.database import close_db_pool, init_db_pool
from app.services.scheduler import cancel_all_temporary_unblock_jobs

app = FastAPI(title="Gatekeeper", version="1.0.0")
app.mount("/assets", StaticFiles(directory="assets"), name="assets")

app.include_router(ui.router)
app.include_router(v1.router)


@app.on_event("startup")
async def on_startup():
    await init_db_pool()


@app.on_event("shutdown")
async def on_shutdown():
    await cancel_all_temporary_unblock_jobs()
    await close_db_pool()
