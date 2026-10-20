"""
FastAPI application entrypoint for the Flood Prediction and Early Warning System.

Run:
    python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
or simply double-click run_project.bat.

The dashboard is served at  http://localhost:8000/
The ESP32 must post to         http://<COMPUTER_WIFI_IP>:8000/api/sensors/data
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import config, database
from backend.api import (
    alert_routes,
    dashboard_routes,
    email_routes,
    esp_routes,
    prediction_routes,
    sensor_routes,
)
from ml.predict import get_predictor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("flood")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    predictor = get_predictor()
    from backend.services import alert_state_service, email_service
    es = email_service.email_service
    ok, reason = es.validate_config()
    if ok:
        logger.info("Email alerts: CONFIGURED (Gmail SMTP %s:%s)", es.host, es.port)
    else:
        logger.warning("Email alerts: NOT CONFIGURED (%s) — alerts are logged locally only.", reason)
    st = alert_state_service.alert_state
    if st.is_initialized():
        _s = st.get_state()
        logger.info(
            "Alert state restored from database (last risk: %s) — no startup email will be sent.",
            _s.get("last_valid_risk"),
        )
    else:
        logger.info(
            "Alert state is fresh — the first valid reading will establish the initial "
            "risk state silently (no startup email)."
        )
    logger.info("Database ready at %s", config.DB_PATH)
    if predictor.is_available:
        logger.info("ML model loaded (%s)", predictor.metadata.get("model_version"))
    else:
        logger.warning(
            "ML model NOT available (%s). Forecasts will be reported as unavailable. "
            "Run train_model.bat to train it.",
            predictor.load_error,
        )
    if not alert_service_configured():
        logger.info("Remote notifications: NOT configured (alerts are logged locally).")

    from backend.services import esp_client
    esp_cfg = esp_client.get_config()
    if esp_cfg["host"]:
        logger.info(
            "ESP32 pull mode: polling http://%s:%s/api/readings",
            esp_cfg["host"], esp_cfg["port"],
        )
    else:
        logger.info("ESP32 pull mode: no address set (ESP32 may still PUSH to this backend).")
    esp_client.start()
    yield
    esp_client.stop()
    logger.info("Shutting down.")


def alert_service_configured() -> bool:
    from backend.services import alert_service

    return alert_service.sms_configured()


app = FastAPI(
    title="Flood Prediction and Early Warning System",
    version="1.0.0",
    description="ESP32 -> FastAPI -> SQLite -> ML -> dashboard flood early-warning system.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routers (registered before the static mount so they take precedence)
app.include_router(sensor_routes.router)
app.include_router(prediction_routes.router)
app.include_router(alert_routes.router)
app.include_router(email_routes.router)
app.include_router(esp_routes.router)
app.include_router(dashboard_routes.router)


@app.get("/health", tags=["system"])
async def health():
    predictor = get_predictor()
    try:
        stats = database.db_stats()
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        stats = {"error": str(exc)}
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "database": {"ok": db_ok, "path": str(config.DB_PATH), "stats": stats},
        "model": {"available": predictor.is_available, "error": predictor.load_error,
                  "version": predictor.metadata.get("model_version")},
        "server": {"host": config.HOST, "port": config.PORT},
        "thresholds": config.thresholds(),
    }


@app.get("/api", tags=["system"])
async def api_index():
    return {
        "name": "Flood Prediction and Early Warning System",
        "endpoints": {
            "health": "/health",
            "ingest": "POST /api/sensors/data",
            "latest_sensor": "GET /api/sensors/latest",
            "recent_sensors": "GET /api/sensors/recent",
            "status": "GET /api/sensors/status",
            "latest_prediction": "GET /api/predictions/latest",
            "prediction_history": "GET /api/predictions/history",
            "model_info": "GET /api/predictions/model",
            "alert_history": "GET /api/alerts/history",
            "thresholds": "GET /api/alerts/thresholds",
            "dashboard_summary": "GET /api/dashboard/summary",
            "test_scenario": "POST /api/test/scenario",
            "test_reset": "POST /api/test/reset",
        },
    }


# Serve the dashboard (must be mounted last so API routes win).
if config.FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
else:  # pragma: no cover
    @app.get("/")
    async def _no_frontend():
        return JSONResponse({"error": "frontend directory not found"}, status_code=404)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("backend.main:app", host=config.HOST, port=config.PORT, reload=False)
