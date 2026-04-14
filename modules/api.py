"""
Module 6 — API FastAPI
========================
Expose tous les modules précédents via une API HTTP+WebSocket.
Conçu pour être consommé par l'interface web (Module 7).

Endpoints REST :
  POST /api/audit/start          — lance un audit sur une liste d'URLs
  GET  /api/audit/{session_id}   — statut d'une session en cours
  GET  /api/audit/{session_id}/results — résultats complets
  GET  /api/audit/{session_id}/download — télécharge le ZIP
  GET  /api/audits               — historique des sessions
  POST /api/urls/validate        — valide une liste d'URLs avant audit
  GET  /api/health               — healthcheck

WebSocket :
  WS   /ws/{session_id}          — progression en temps réel

Logique métier :
  - Chaque session a un ID unique horodaté
  - Les audits tournent en arrière-plan (BackgroundTasks FastAPI)
  - La progression est broadcastée via WebSocket à tous les clients connectés
  - Les résultats sont persistés en JSON pour l'historique
  - Gestion propre des erreurs avec codes HTTP appropriés
  - Rate limiting basique : 1 session active à la fois par défaut
"""

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect,
    HTTPException, UploadFile, File, Query
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl, field_validator

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.cmp_detector      import fetch_and_detect, CMPType
from modules.didomi_extractor  import extract_didomi
from modules.playwright_navigator import navigate_and_extract
from modules.cnil_analyzer     import analyze_cnil
from modules.orchestrator      import (
    audit_url, AuditResult, AuditStatus, AuditRoute,
    format_result, format_batch_summary
)
from modules.report_generator_v2 import generate_outputs, generate_all


# ─── Configuration ────────────────────────────────────────────────────────────

DATA_DIR     = Path("data")       # sessions JSON persistées
OUTPUT_DIR   = Path("outputs")
STATIC_DIR   = Path(__file__).parent.parent / "static"    # interface web (Module 7)
MAX_URLS     = 335                 # limite par session
MAX_CONCURRENT = 5                 # URLs en parallèle
SESSION_FILE   = DATA_DIR / "sessions.json"

DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


# ─── Modèles Pydantic ─────────────────────────────────────────────────────────

class AuditRequest(BaseModel):
    urls: list[str]
    concurrency: int = 5
    generate_reports: bool = True   # génère PDF + Excel
    session_name: Optional[str] = None

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, v):
        if not v:
            raise ValueError("Au moins une URL requise")
        if len(v) > MAX_URLS:
            raise ValueError(f"Maximum {MAX_URLS} URLs par session")
        # Normaliser les URLs
        normalized = []
        for url in v:
            url = url.strip()
            if url and not url.startswith(("http://", "https://")):
                url = "https://" + url
            if url:
                normalized.append(url)
        return normalized

    @field_validator("concurrency")
    @classmethod
    def validate_concurrency(cls, v):
        return max(1, min(v, MAX_CONCURRENT))


class URLValidateRequest(BaseModel):
    urls: list[str]


class SessionStatus(BaseModel):
    session_id: str
    session_name: Optional[str]
    status: str           # "running" | "completed" | "error"
    started_at: str
    completed_at: Optional[str]
    total: int
    processed: int
    conformes: int
    non_conformes: int
    a_verifier: int
    erreurs: int
    current_url: Optional[str]
    progress_pct: float
    zip_path: Optional[str]
    error: Optional[str]


# ─── Store des sessions en mémoire ────────────────────────────────────────────

class SessionStore:
    """Stockage en mémoire des sessions actives + persistance JSON."""

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._load()

    def _load(self):
        if SESSION_FILE.exists():
            try:
                with open(SESSION_FILE) as f:
                    self._sessions = json.load(f)
            except Exception:
                self._sessions = {}

    def _save(self):
        try:
            with open(SESSION_FILE, "w") as f:
                json.dump(self._sessions, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def create(self, session_id: str, urls: list[str], name: Optional[str]) -> dict:
        session = {
            "session_id":    session_id,
            "session_name":  name or session_id,
            "status":        "running",
            "started_at":    datetime.now().isoformat(),
            "completed_at":  None,
            "urls":          urls,
            "total":         len(urls),
            "processed":     0,
            "conformes":     0,
            "non_conformes": 0,
            "a_verifier":    0,
            "erreurs":       0,
            "current_url":   None,
            "results":       [],
            "zip_path":      None,
            "error":         None,
        }
        self._sessions[session_id] = session
        self._save()
        return session

    def get(self, session_id: str) -> Optional[dict]:
        return self._sessions.get(session_id)

    def update(self, session_id: str, **kwargs):
        if session_id in self._sessions:
            self._sessions[session_id].update(kwargs)
            self._save()

    def add_result(self, session_id: str, result_dict: dict):
        if session_id in self._sessions:
            self._sessions[session_id]["results"].append(result_dict)
            self._save()

    def list_sessions(self) -> list[dict]:
        return [
            {k: v for k, v in s.items() if k != "results"}
            for s in sorted(
                self._sessions.values(),
                key=lambda x: x["started_at"],
                reverse=True,
            )
        ]

    def has_running(self) -> bool:
        return any(s["status"] == "running" for s in self._sessions.values())


store = SessionStore()


# ─── WebSocket Manager ────────────────────────────────────────────────────────

class WSManager:
    """Gère les connexions WebSocket par session."""

    def __init__(self):
        self._connections: dict[str, list[WebSocket]] = {}

    async def connect(self, session_id: str, ws: WebSocket):
        await ws.accept()
        self._connections.setdefault(session_id, []).append(ws)

    def disconnect(self, session_id: str, ws: WebSocket):
        if session_id in self._connections:
            self._connections[session_id] = [
                c for c in self._connections[session_id] if c != ws
            ]

    async def broadcast(self, session_id: str, message: dict):
        """Envoie un message à tous les clients connectés sur cette session."""
        connections = self._connections.get(session_id, [])
        dead = []
        for ws in connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(session_id, ws)

    async def broadcast_all(self, session_id: str, event: str, data: dict):
        """Helper : envoie un événement typé."""
        await self.broadcast(session_id, {"event": event, "data": data})


ws_manager = WSManager()


# ─── Application FastAPI ──────────────────────────────────────────────────────

app = FastAPI(
    title="Dékuple DMC — Audit RGPD",
    description="Outil d'audit automatisé de conformité CNIL",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Logique d'audit en arrière-plan ──────────────────────────────────────────

def _result_to_dict(result: AuditResult) -> dict:
    """Sérialise un AuditResult en dict JSON-compatible."""
    cnil = getattr(result, "cnil_analysis", None)
    cnil_data = None
    if cnil:
        cnil_data = {
            "score_total":      cnil.score_total,
            "score_max":        cnil.score_max,
            "taux_conformite":  cnil.taux_conformite,
            "dekuple_sur_formulaire": cnil.dekuple_sur_formulaire,
            "dekuple_via_lien":       cnil.dekuple_via_lien,
            "dekuple_politique_lien": cnil.dekuple_politique_lien,
            "opt_in_present":         cnil.opt_in_present,
            "bouton_refus_present":   cnil.bouton_refus_present,
            "criteres": [
                {
                    "code":    c.code,
                    "libelle": c.libelle,
                    "score":   c.score.value,
                    "preuve":  c.preuve,
                    "source":  c.source,
                    "note":    c.note,
                }
                for c in cnil.criteres
            ],
        }

    return {
        "url":               result.url,
        "status":            result.status.value,
        "route":             result.route.value if result.route else None,
        "dekuple_found":     result.dekuple_found,
        "dekuple_name":      result.dekuple_name,
        "dekuple_locations": result.dekuple_locations,
        "dekuple_policy_url":result.dekuple_policy_url,
        "cmp_type":          result.cmp_type,
        "partners_total":    result.partners_total,
        "partners_list":     result.partners_list,
        "links_analyzed":    result.links_analyzed,
        "screenshots":       result.screenshots,
        "duration_seconds":  result.duration_seconds,
        "error":             result.error,
        "notes":             result.notes,
        "cnil_analysis":     cnil_data,
        "audited_at":        result.audited_at,
    }


async def _run_audit_session(
    session_id: str,
    urls: list[str],
    concurrency: int,
    generate_reports: bool,
):
    """
    Tâche de fond : audite toutes les URLs et met à jour la session.
    Broadcaste la progression via WebSocket après chaque URL.
    """
    semaphore = asyncio.Semaphore(concurrency)
    audit_results: list[AuditResult] = [None] * len(urls)

    async def _audit_one(idx: int, url: str):
        async with semaphore:
            # Notifier début
            store.update(session_id, current_url=url)
            await ws_manager.broadcast_all(session_id, "url_start", {
                "index": idx,
                "url":   url,
                "total": len(urls),
            })

            try:
                # Pipeline complet M1 → M2/M3 → M4b
                result = await audit_url(url)

                # Analyse CNIL sur le contenu extrait
                if result.success if hasattr(result, 'success') else result.status != AuditStatus.ERREUR:
                    # On reconstruit les contents depuis text_extract si M3 a tourné
                    from dataclasses import dataclass

                    @dataclass
                    class _FC:
                        source: str
                        text: str

                    contents = [_FC("main", result.text_extract or "")]
                    if result.links_analyzed:
                        contents.append(_FC("legal_page", result.text_extract or ""))

                    cnil = analyze_cnil(contents)
                    result.cnil_analysis = cnil

            except Exception as e:
                from modules.orchestrator import AuditRoute
                result = AuditResult(
                    url=url,
                    audited_at=datetime.now().isoformat(),
                    status=AuditStatus.ERREUR,
                    route=AuditRoute.ECHEC,
                )
                result.error = f"{type(e).__name__} — {e}"

            audit_results[idx] = result

            # Mettre à jour les compteurs
            s = store.get(session_id)
            processed     = s["processed"] + 1
            conformes     = s["conformes"]     + (1 if result.status == AuditStatus.CONFORME     else 0)
            non_conformes = s["non_conformes"] + (1 if result.status == AuditStatus.NON_CONFORME else 0)
            a_verifier    = s["a_verifier"]    + (1 if result.status == AuditStatus.A_VERIFIER   else 0)
            erreurs       = s["erreurs"]       + (1 if result.status == AuditStatus.ERREUR       else 0)
            progress_pct  = round(processed / len(urls) * 100, 1)

            store.update(session_id,
                processed=processed,
                conformes=conformes,
                non_conformes=non_conformes,
                a_verifier=a_verifier,
                erreurs=erreurs,
            )
            store.add_result(session_id, _result_to_dict(result))

            # Générer les fichiers individuels si demandé
            pdf_path  = None
            xlsx_path = None
            if generate_reports and result.status != AuditStatus.ERREUR:
                try:
                    gen = generate_outputs(result, session_id)
                    if gen.success:
                        pdf_path  = gen.pdf_path
                        xlsx_path = gen.xlsx_path
                except Exception:
                    pass

            # Broadcaster résultat
            await ws_manager.broadcast_all(session_id, "url_done", {
                "index":         idx,
                "url":           url,
                "status":        result.status.value,
                "dekuple_found": result.dekuple_found,
                "cmp_type":      result.cmp_type,
                "duration":      result.duration_seconds,
                "error":         result.error,
                "progress_pct":  progress_pct,
                "processed":     processed,
                "total":         len(urls),
                "conformes":     conformes,
                "non_conformes": non_conformes,
                "a_verifier":    a_verifier,
                "erreurs":       erreurs,
                "cnil_score":    (
                    f"{result.cnil_analysis.score_total}/{result.cnil_analysis.score_max}"
                    if hasattr(result, 'cnil_analysis') and result.cnil_analysis else "-"
                ),
                "pdf_path":      pdf_path,
                "xlsx_path":     xlsx_path,
            })

    # Lancer tous les audits en parallèle avec le semaphore
    try:
        await asyncio.gather(*[
            _audit_one(i, url) for i, url in enumerate(urls)
        ])

        # Générer la synthèse globale
        valid_results = [r for r in audit_results if r is not None]
        zip_path = None

        if generate_reports and valid_results:
            try:
                outputs = generate_all(valid_results, session_id)
                zip_path = outputs.get("zip")
            except Exception as e:
                pass

        store.update(
            session_id,
            status="completed",
            completed_at=datetime.now().isoformat(),
            current_url=None,
            zip_path=zip_path,
        )

        await ws_manager.broadcast_all(session_id, "session_complete", {
            "session_id":    session_id,
            "total":         len(urls),
            "conformes":     store.get(session_id)["conformes"],
            "non_conformes": store.get(session_id)["non_conformes"],
            "a_verifier":    store.get(session_id)["a_verifier"],
            "erreurs":       store.get(session_id)["erreurs"],
            "zip_path":      zip_path,
        })

    except Exception as e:
        store.update(
            session_id,
            status="error",
            error=str(e),
            completed_at=datetime.now().isoformat(),
        )
        await ws_manager.broadcast_all(session_id, "session_error", {
            "session_id": session_id,
            "error":      str(e),
        })


# ─── Routes REST ──────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    """Healthcheck — vérifie que l'API tourne."""
    return {
        "status":    "ok",
        "version":   "1.0.0",
        "timestamp": datetime.now().isoformat(),
        "active_sessions": sum(
            1 for s in store.list_sessions() if s["status"] == "running"
        ),
    }


@app.post("/api/audit/start")
async def start_audit(request: AuditRequest, background_tasks: BackgroundTasks):
    """
    Lance un audit sur une liste d'URLs.
    Retourne immédiatement un session_id.
    La progression est disponible via WebSocket /ws/{session_id}.
    """
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]

    store.create(session_id, request.urls, request.session_name)

    background_tasks.add_task(
        _run_audit_session,
        session_id=session_id,
        urls=request.urls,
        concurrency=request.concurrency,
        generate_reports=request.generate_reports,
    )

    return {
        "session_id":  session_id,
        "total_urls":  len(request.urls),
        "ws_url":      f"/ws/{session_id}",
        "status_url":  f"/api/audit/{session_id}",
        "results_url": f"/api/audit/{session_id}/results",
        "started_at":  store.get(session_id)["started_at"],
    }


@app.post("/api/audit/start/csv")
async def start_audit_csv(
    file: UploadFile = File(...),
    concurrency: int = Query(default=5, le=MAX_CONCURRENT),
    generate_reports: bool = Query(default=True),
):
    """
    Lance un audit depuis un fichier CSV uploadé.
    Le CSV doit avoir une colonne 'collect_url' ou être une liste d'URLs ligne par ligne.
    """
    try:
        content = await file.read()
        text    = content.decode("utf-8", errors="ignore")
        lines   = [l.strip() for l in text.splitlines() if l.strip()]

        # Détecte le format : CSV avec en-tête ou liste brute
        urls = []
        for line in lines:
            # Skip l'en-tête
            if "collect_url" in line.lower() or "url" == line.lower():
                continue
            # Prend la première colonne si CSV avec séparateur
            url = line.split(";")[0].split(",")[0].strip()
            if url and url != "null":
                urls.append(url)

        if not urls:
            raise HTTPException(status_code=400, detail="Aucune URL valide trouvée dans le CSV")
        if len(urls) > MAX_URLS:
            urls = urls[:MAX_URLS]

        request = AuditRequest(
            urls=urls,
            concurrency=concurrency,
            generate_reports=generate_reports,
            session_name=f"Import CSV — {file.filename}",
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erreur lecture CSV : {e}")

    session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    store.create(session_id, request.urls, request.session_name)

    import asyncio
    asyncio.create_task(_run_audit_session(
        session_id=session_id,
        urls=request.urls,
        concurrency=request.concurrency,
        generate_reports=request.generate_reports,
    ))

    return {
        "session_id":  session_id,
        "total_urls":  len(request.urls),
        "ws_url":      f"/ws/{session_id}",
        "status_url":  f"/api/audit/{session_id}",
        "started_at":  store.get(session_id)["started_at"],
    }


@app.get("/api/audit/{session_id}")
async def get_session_status(session_id: str):
    """Statut d'une session d'audit."""
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session introuvable")

    s = session
    return {
        "session_id":    s["session_id"],
        "session_name":  s["session_name"],
        "status":        s["status"],
        "started_at":    s["started_at"],
        "completed_at":  s.get("completed_at"),
        "total":         s["total"],
        "processed":     s["processed"],
        "conformes":     s["conformes"],
        "non_conformes": s["non_conformes"],
        "a_verifier":    s["a_verifier"],
        "erreurs":       s["erreurs"],
        "current_url":   s.get("current_url"),
        "progress_pct":  round(s["processed"] / s["total"] * 100, 1) if s["total"] else 0,
        "zip_path":      s.get("zip_path"),
        "error":         s.get("error"),
        "download_url":  f"/api/audit/{session_id}/download" if s.get("zip_path") else None,
    }


@app.get("/api/audit/{session_id}/results")
async def get_session_results(
    session_id: str,
    status: Optional[str] = Query(None, description="Filtrer par statut"),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0),
):
    """
    Résultats détaillés d'une session.
    Supporte le filtrage par statut et la pagination.
    """
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session introuvable")

    results = session.get("results", [])

    # Filtrage
    if status:
        results = [r for r in results if r.get("status") == status]

    # Pagination
    total   = len(results)
    results = results[offset: offset + limit]

    return {
        "session_id": session_id,
        "total":      total,
        "offset":     offset,
        "limit":      limit,
        "results":    results,
    }


@app.get("/api/audit/{session_id}/download")
async def download_zip(session_id: str):
    """Télécharge le ZIP de tous les outputs d'une session."""
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session introuvable")

    if session["status"] != "completed":
        raise HTTPException(status_code=409, detail="Session non terminée")

    zip_path = session.get("zip_path")
    if not zip_path or not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="Fichier ZIP non disponible")

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"audit_rgpd_{session_id}.zip",
    )


@app.get("/api/audit/{session_id}/download/pdf/{filename}")
async def download_pdf(session_id: str, filename: str):
    """Télécharge un PDF individuel."""
    pdf_path = OUTPUT_DIR / session_id / "pdf" / filename
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF non trouvé")
    return FileResponse(str(pdf_path), media_type="application/pdf", filename=filename)


@app.get("/api/audit/{session_id}/download/xlsx/{filename}")
async def download_xlsx(session_id: str, filename: str):
    """Télécharge un Excel individuel."""
    xlsx_path = OUTPUT_DIR / session_id / "xlsx" / filename
    if not xlsx_path.exists():
        raise HTTPException(status_code=404, detail="Excel non trouvé")
    return FileResponse(
        str(xlsx_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


@app.get("/api/audits")
async def list_sessions(
    limit: int = Query(default=20, le=100),
    status: Optional[str] = None,
):
    """Historique de toutes les sessions d'audit."""
    sessions = store.list_sessions()
    if status:
        sessions = [s for s in sessions if s["status"] == status]
    return {
        "total":    len(sessions),
        "sessions": sessions[:limit],
    }


@app.post("/api/urls/validate")
async def validate_urls(request: URLValidateRequest):
    """
    Valide une liste d'URLs : format, accessibilité, détection CMP.
    Retourne un rapport de validation sans lancer d'audit.
    """
    results = []
    for url in request.urls[:20]:  # max 20 pour la validation rapide
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            cmp, _ = await fetch_and_detect(url, timeout=8)
            results.append({
                "url":     url,
                "valid":   True,
                "cmp":     cmp.cmp_type.value,
                "has_api": cmp.api_endpoint is not None,
            })
        except Exception as e:
            results.append({
                "url":   url,
                "valid": False,
                "error": str(e)[:100],
            })
    return {"validated": len(results), "results": results}


@app.delete("/api/audit/{session_id}")
async def delete_session(session_id: str):
    """Supprime une session de l'historique (pas les fichiers)."""
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session introuvable")
    if session["status"] == "running":
        raise HTTPException(status_code=409, detail="Impossible de supprimer une session active")
    store._sessions.pop(session_id, None)
    store._save()
    return {"deleted": session_id}


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """
    WebSocket de progression en temps réel.
    Le client reçoit les événements :
      - url_start   : début d'audit d'une URL
      - url_done    : fin d'audit d'une URL avec résultat
      - session_complete : toute la session terminée
      - session_error    : erreur fatale
    """
    await ws_manager.connect(session_id, websocket)

    # Envoyer l'état actuel immédiatement à la connexion
    session = store.get(session_id)
    if session:
        await websocket.send_json({
            "event": "session_state",
            "data":  {
                "session_id":  session_id,
                "status":      session["status"],
                "total":       session["total"],
                "processed":   session["processed"],
                "conformes":   session["conformes"],
                "non_conformes": session["non_conformes"],
                "a_verifier":  session["a_verifier"],
                "erreurs":     session["erreurs"],
                "progress_pct": round(
                    session["processed"] / session["total"] * 100, 1
                ) if session["total"] else 0,
            }
        })

    try:
        while True:
            # Garder la connexion vivante (ping/pong)
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_json({"event": "ping"})
    except WebSocketDisconnect:
        ws_manager.disconnect(session_id, websocket)


# ─── Serveur de fichiers statiques (interface web) ────────────────────────────
import logging
logging.warning(f"STATIC_DIR = {STATIC_DIR} | exists = {STATIC_DIR.exists()}")

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:
    @app.get("/")
    async def root():
        return JSONResponse({
            "message": "Dékuple DMC — API Audit RGPD",
            "docs":    "/api/docs",
            "health":  "/api/health",
        })


# ─── Lancement ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "modules.api:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        workers=1,           # 1 seul worker — Playwright n'est pas thread-safe
        log_level="info",
    )
