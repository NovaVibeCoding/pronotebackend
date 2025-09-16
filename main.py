# main.py
import os, time
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, List
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# =========================
# ENV / Réglages
# =========================
PRONOTE_URL = os.getenv("PRONOTE_URL", "https://0061884r.index-education.net/pronote/eleve.html")
ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*")  # ex: "http://localhost:5173,https://ton-front.example"
MOCK = os.getenv("MOCK", "0").strip().lower() in {"1", "true", "yes"}     # 1 = ne touche pas Pronote
INCLUDE_CONTENT = os.getenv("INCLUDE_CONTENT", "0").strip().lower() in {"1", "true", "yes"}  # c.content (lent)

# Budgets (secondes) — ajuste si besoin
LOGIN_TIMEOUT   = float(os.getenv("LOGIN_TIMEOUT_SECONDS", "10"))
NOTES_TIMEOUT   = float(os.getenv("NOTES_TIMEOUT_SECONDS", "6"))
LESSONS_TIMEOUT = float(os.getenv("LESSONS_TIMEOUT_SECONDS", "6"))
NEXT7_TIMEOUT   = float(os.getenv("NEXT7_TIMEOUT_SECONDS", "4"))
HW_TIMEOUT      = float(os.getenv("HOMEWORK_TIMEOUT_SECONDS", "4"))
HTTP_TIMEOUT    = float(os.getenv("HTTP_TIMEOUT_SECONDS", "6"))  # timeout par requête réseau (requests)

# =========================
# Utils
# =========================
def safe_float(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", ".")
        if s == "" or s.lower() in {"abs","ab","nn","n.n","na","n/a","null","-"}: return None
        try: return float(s)
        except ValueError: return None
    return None

def fmt_dt(d) -> Optional[str]:
    try:
        if d is None: return None
        if isinstance(d, datetime): return d.date().isoformat()
        if isinstance(d, date): return d.isoformat()
        return str(d)
    except Exception:
        return None

# =========================
# Modèles
# =========================
class FetchPayload(BaseModel):
    username: str
    password: str
    days: int = 7
    start: Optional[str] = None
    end:   Optional[str] = None

# =========================
# App & CORS
# =========================
app = FastAPI(title="Pronote JSON API (time-box)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOW_ORIGINS.split(",")] if ALLOW_ORIGINS else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Endpoints de base
# =========================
@app.get("/ping")
def ping():
    return {"ok": True, "mode": "MOCK" if MOCK else "REAL", "include_content": INCLUDE_CONTENT}

# =========================
# Builders (sync)
# =========================
def build_notes(client) -> Dict[str, Any]:
    out = {"periods": []}
    for period in client.periods:
        grades = []
        for g in sorted(period.grades, key=lambda x: x.date or date.min):
            subj_name = getattr(g.subject, "name", g.subject)
            subj_code = getattr(g.subject, "code", None)
            grades.append({
                "date": g.date.strftime("%Y-%m-%d") if g.date else None,
                "subjectId": subj_code or subj_name,
                "subjectLabel": subj_name,
                "value": safe_float(getattr(g, "grade", None)),
                "outOf": safe_float(getattr(g, "out_of", None)),
                "coefficient": safe_float(getattr(g, "coefficient", None)),
                "comment": getattr(g, "comment", None),
            })
        out["periods"].append({"name": period.name, "grades": grades})
    return out

def build_lessons(client, start_d: date, end_d: date) -> Dict[str, Any]:
    lessons = client.lessons(start_d, end_d)
    lessons.sort(key=lambda c: (c.start, c.end))
    arr: List[Dict[str, Any]] = []
    for c in lessons:
        subj_name = getattr(c.subject, "name", "?")
        subj_code = getattr(c.subject, "code", None)
        item = {
            "date": c.start.strftime("%Y-%m-%d"),
            "start": c.start.strftime("%H:%M"),
            "end": c.end.strftime("%H:%M"),
            "subjectId": subj_code or subj_name,
            "subjectLabel": subj_name,
            "room": c.classroom or None,
            "canceled": bool(c.canceled),
        }
        if INCLUDE_CONTENT:
            item["content"] = {
                "title": getattr(getattr(c, "content", None), "title", None),
                "description": getattr(getattr(c, "content", None), "description", None)
            }
        arr.append(item)
    return {"lessons": arr}

def build_homework(client, start_d: date, end_d: date) -> Dict[str, Any]:
    try:
        hws = client.homework(start_d, end_d)
    except Exception:
        hws = getattr(client, "homeworks", lambda a,b: [])(start_d, end_d)
    arr: List[Dict[str, Any]] = []
    for h in sorted(hws, key=lambda x: getattr(x, "due_date", None) or getattr(x, "date", None) or date.max):
        subj = getattr(h, "subject", None)
        subj_name = getattr(subj, "name", str(subj) if subj else "?")
        subj_code = getattr(subj, "code", None)
        given = getattr(h, "date", None) or getattr(h, "assigned_date", None) or getattr(h, "given_date", None)
        due   = getattr(h, "due_date", None) or getattr(h, "for_date", None)
        arr.append({
            "id": getattr(h, "id", None) or f"hw_{fmt_dt(given)}_{subj_code or subj_name}",
            "given": fmt_dt(given),
            "due": fmt_dt(due),
            "subjectId": subj_code or subj_name,
            "subjectLabel": subj_name,
            "title": getattr(h, "title", None) or getattr(h, "description", None),
            "description": getattr(h, "description", None),
            "done": bool(getattr(h, "done", False)),
        })
    return {"homework": arr}

def with_timeout(executor: ThreadPoolExecutor, fn, timeout_s: float):
    fut = executor.submit(fn)
    return fut.result(timeout=timeout_s)

# =========================
# Probe login (diagnostic)
# =========================
@app.get("/probe/login")
def probe_login(username: str, password: str):
    """Vérifie uniquement le login Pronote, avec timeouts réseau + budget court."""
    if MOCK:
        return {"ok": True, "mode": "MOCK"}
    # Timeout réseau global pour requests (utilisé par pronotepy)
    import requests as _requests
    _orig = _requests.Session.request
    def _timeout_request(self, method, url, **kwargs):
        if "timeout" not in kwargs or kwargs["timeout"] is None:
            kwargs["timeout"] = HTTP_TIMEOUT
        return _orig(self, method, url, **kwargs)
    _requests.Session.request = _timeout_request

    import pronotepy
    if getattr(pronotepy, "__version__", "unknown") != "2.14.4":
        raise HTTPException(500, "bad_pronotepy_version")

    from pronotepy.ent import atrium_sud
    with ThreadPoolExecutor(max_workers=1) as ex:
        try:
            def _login():
                return pronotepy.Client(PRONOTE_URL, username=username, password=password, ent=atrium_sud)
            client = with_timeout(ex, _login, LOGIN_TIMEOUT)
        except FuturesTimeout:
            raise HTTPException(504, f"login_timeout>{LOGIN_TIMEOUT}s")

    if not client.logged_in:
        raise HTTPException(401, "invalid_credentials")
    return {"ok": True, "logged_in": True}

# =========================
# Endpoint principal
# =========================
@app.post("/pronote/fetch")
def pronote_fetch(payload: FetchPayload):
    t0 = time.perf_counter()

    # plages PAST/FUTURE
    if payload.start and payload.end:
        start_d = datetime.fromisoformat(payload.start).date()
        end_d   = datetime.fromisoformat(payload.end).date()
    else:
        end_d = date.today()
        start_d = end_d - timedelta(days=max(1, payload.days))
    f_start = date.today()
    f_end   = f_start + timedelta(days=7)

    # MOCK direct (ne touche pas Pronote)
    if MOCK:
        return {
            "notes": {"periods":[]},
            "lessons": {"lessons":[]},
            "lessons_next7": {"lessons":[]},
            "homework_next7": {"homework":[]},
            "meta": {
                "school_url": "MOCK",
                "range_past": {"start": start_d.isoformat(), "end": end_d.isoformat()},
                "range_next7": {"start": f_start.isoformat(), "end": f_end.isoformat()},
                "status": {"notes":"ok","lessons":"ok","lessons_next7":"ok","homework_next7":"ok"},
                "timing": {"total_s": round(time.perf_counter()-t0, 3)},
                "include_content": INCLUDE_CONTENT
            }
        }

    # Timeout réseau pour requests (utilisé par pronotepy)
    import requests as _requests
    _orig = _requests.Session.request
    def _timeout_request(self, method, url, **kwargs):
        if "timeout" not in kwargs or kwargs["timeout"] is None:
            kwargs["timeout"] = HTTP_TIMEOUT
        return _orig(self, method, url, **kwargs)
    _requests.Session.request = _timeout_request

    # Login time-boxé
    import pronotepy
    if getattr(pronotepy, "__version__", "unknown") != "2.14.4":
        raise HTTPException(500, "bad_pronotepy_version")
    from pronotepy.ent import atrium_sud

    with ThreadPoolExecutor(max_workers=4) as ex:
        try:
            def _login():
                return pronotepy.Client(PRONOTE_URL, username=payload.username, password=payload.password, ent=atrium_sud)
            client = with_timeout(ex, _login, LOGIN_TIMEOUT)
        except FuturesTimeout:
            # Réponse partielle claire si login trop long
            return {
                "notes": {"periods":[]},
                "lessons": {"lessons":[]},
                "lessons_next7": {"lessons":[]},
                "homework_next7": {"homework":[]},
                "meta": {
                    "school_url": PRONOTE_URL,
                    "range_past": {"start": start_d.isoformat(), "end": end_d.isoformat()},
                    "range_next7": {"start": f_start.isoformat(), "end": f_end.isoformat()},
                    "status": {"login": f"timeout>{LOGIN_TIMEOUT}s"},
                    "errors": {"login": f"timeout>{LOGIN_TIMEOUT}s"},
                    "timing": {"total_s": round(time.perf_counter()-t0, 3)},
                    "include_content": INCLUDE_CONTENT
                }
            }

        if not client.logged_in:
            raise HTTPException(401, "invalid_credentials")

        # Exécution parallèle time-boxée
        tasks = {
            "notes":           (lambda: build_notes(client),                       NOTES_TIMEOUT),
            "lessons":         (lambda: build_lessons(client, start_d, end_d),     LESSONS_TIMEOUT),
            "lessons_next7":   (lambda: build_lessons(client, f_start, f_end),     NEXT7_TIMEOUT),
            "homework_next7":  (lambda: build_homework(client, f_start, f_end),    HW_TIMEOUT),
        }

        results: Dict[str, Any] = {}
        status: Dict[str, str] = {}
        errors: Dict[str, str] = {}
        timing: Dict[str, float] = {}

        for name, (fn, budget) in tasks.items():
            t1 = time.perf_counter()
            try:
                results[name] = with_timeout(ex, fn, budget)
                status[name] = "ok"
            except FuturesTimeout:
                status[name] = "timeout"
                errors[name] = f"timeout>{budget}s"
                results[name] = {"periods": []} if name=="notes" else ({"lessons": []} if "lessons" in name else {"homework": []})
            except Exception as e:
                status[name] = "error"
                errors[name] = f"{type(e).__name__}: {e}"
                results[name] = {"periods": []} if name=="notes" else ({"lessons": []} if "lessons" in name else {"homework": []})
            finally:
                timing[name] = round(time.perf_counter()-t1, 3)

    timing["total_s"] = round(time.perf_counter()-t0, 3)
    return {
        "notes": results["notes"],
        "lessons": results["lessons"],
        "lessons_next7": results["lessons_next7"],
        "homework_next7": results["homework_next7"],
        "meta": {
            "school_url": PRONOTE_URL,
            "range_past": {"start": start_d.isoformat(), "end": end_d.isoformat()},
            "range_next7": {"start": f_start.isoformat(), "end": f_end.isoformat()},
            "status": status,
            "errors": errors,
            "timing": timing,
            "include_content": INCLUDE_CONTENT
        }
    }

# =========================
# Run (local & Render)
# =========================
if __name__ == "__main__":
    import uvicorn
    # Local : PORT non défini → 8080 par défaut
    # Render : Render fournit $PORT (obligatoire d'écouter dessus)
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
