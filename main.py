import os, time
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, List
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from fastapi import Request, Header, HTTPException, Depends



# Utilisation :
from fastapi import Depends


load_dotenv()

PRONOTE_URL = os.getenv("PRONOTE_URL", "https://0061884r.index-education.net/pronote/eleve.html")
ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "https://ton-front.example")
MOCK = os.getenv("MOCK", "0").strip().lower() in {"1","true","yes"} 
INCLUDE_CONTENT = os.getenv("INCLUDE_CONTENT", "1").strip().lower() in {"1","true","yes"}
API_KEY = os.getenv("API_KEY", "").strip()

def require_api_key(request: Request, x_api_key: str | None = Header(None)):
    # Ne pas valider pour preflight OPTIONS
    if request.method == "OPTIONS":
        return
    if API_KEY:
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(401, "invalid_api_key")
# ---- Utils ----
def safe_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", ".")
        if s == "" or s.lower() in {"abs", "ab", "nn", "n.n", "na", "n/a", "null", "-"}:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None

def fmt_dt(d) -> Optional[str]:
    try:
        if d is None:
            return None
        if isinstance(d, datetime):
            return d.date().isoformat()
        if isinstance(d, date):
            return d.isoformat()
        return str(d)
    except Exception:
        return None

# ---- Models ----
class FetchPayload(BaseModel):
    username: str
    password: str
    days: int = 7
    start: Optional[str] = None
    end:   Optional[str] = None

# ---- App ----
app = FastAPI(title="Pronote JSON API (optimisée)")

from fastapi.middleware.cors import CORSMiddleware

ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*")

# si ALLOW_ORIGINS est "*" ou vide, laissons tout ouvert pour debug
if ALLOW_ORIGINS.strip() in {"", "*"}:
    origins = ["*"]
else:
    origins = [o.strip() for o in ALLOW_ORIGINS.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "PUT", "DELETE", "PATCH"],
    allow_headers=["*"],               # autorise tous les headers, y compris x-api-key
    expose_headers=["*"],
    max_age=600

)

# ---- MOCK ----
MOCK_NOTES = {"periods":[{"name":"T1","grades":[
    {"date":"2025-09-10","subjectId":"MATH","subjectLabel":"Maths","value":15,"outOf":20},
    {"date":"2025-09-12","subjectId":"HIST","subjectLabel":"Histoire","value":13,"outOf":20}
]}]}
MOCK_LESSONS_PAST = {"lessons":[{"date":"2025-09-15","start":"09:00","end":"10:00","subjectId":"MATH","subjectLabel":"Maths","room":"B12","canceled":False}]}
MOCK_LESSONS_NEXT7 = {"lessons":[{"date":"2025-09-18","start":"14:00","end":"15:00","subjectId":"PHY","subjectLabel":"Physique","room":"Labo","canceled":False}]}
MOCK_HOMEWORK_NEXT7 = {"homework":[{"id":"hw1","given":"2025-09-15","due":"2025-09-18","subjectId":"MATH","subjectLabel":"Maths","title":"Exos 12-15","description":"Équations","done":False}]}

@app.get("/ping")
def ping():
    return {"ok": True, "mode": "MOCK" if MOCK else "REAL", "include_content": INCLUDE_CONTENT}

# ---- Core helpers (sync) ----
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

        content = None
        if INCLUDE_CONTENT:
            content = {
                "title": getattr(getattr(c, "content", None), "title", None),
                "description": getattr(getattr(c, "content", None), "description", None)
            }
        arr.append({
            "date": c.start.strftime("%Y-%m-%d"),
            "start": c.start.strftime("%H:%M"),
            "end": c.end.strftime("%H:%M"),
            "subjectId": subj_code or subj_name,
            "subjectLabel": subj_name,
            "room": c.classroom or None,
            "canceled": bool(c.canceled),
            **({"content": content} if INCLUDE_CONTENT else {})
        })
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

@app.post("/pronote/fetch")
def pronote_fetch(payload: FetchPayload):
    t0 = time.perf_counter()
    # Plages
    if payload.start and payload.end:
        start_d = datetime.fromisoformat(payload.start).date()
        end_d   = datetime.fromisoformat(payload.end).date()
    else:
        end_d = date.today()
        start_d = end_d - timedelta(days=max(1, payload.days))
    f_start = date.today()
    f_end   = f_start + timedelta(days=7)

    if MOCK:
        return {
            "notes": MOCK_NOTES,
            "lessons": MOCK_LESSONS_PAST,
            "lessons_next7": MOCK_LESSONS_NEXT7,
            "homework_next7": MOCK_HOMEWORK_NEXT7,
            "meta": {
                "school_url": "MOCK",
                "range_past": {"start": start_d.isoformat(), "end": end_d.isoformat()},
                "range_next7": {"start": f_start.isoformat(), "end": f_end.isoformat()},
                "status": {"notes":"ok","lessons":"ok","lessons_next7":"ok","homework_next7":"ok"},
                "timing": {"total_s": round(time.perf_counter()-t0, 3)}
            }
        }

    # --- REAL ---
    try:
        import pronotepy
        ver = getattr(pronotepy, "__version__", "unknown")
        if ver != "2.14.4":
            raise HTTPException(500, f"pronotepy {ver} détecté — attendu 2.14.4")
        from pronotepy.ent import atrium_sud

        client = pronotepy.Client(PRONOTE_URL, username=payload.username, password=payload.password, ent=atrium_sud)
        if not client.logged_in:
            raise HTTPException(401, "invalid_credentials")

        status: Dict[str,str] = {}
        timing: Dict[str,float] = {}
        errors: Dict[str,str] = {}


        with ThreadPoolExecutor(max_workers=4) as ex:
            tasks = {
                "notes":       lambda: build_notes(client),
                "lessons":     lambda: build_lessons(client, start_d, end_d),
                "lessons_next7": lambda: build_lessons(client, f_start, f_end),
                "homework_next7": lambda: build_homework(client, f_start, f_end),
            }
            time_budget = {"notes":6.0, "lessons":6.0, "lessons_next7":4.0, "homework_next7":4.0}
            results: Dict[str, Any] = {}

            for name, fn in tasks.items():
                t1 = time.perf_counter()
                try:
                    results[name] = with_timeout(ex, fn, time_budget[name])
                    status[name] = "ok"
                except FuturesTimeout:
                    status[name] = "timeout"
                    errors[name] = f"timeout>{time_budget[name]}s"
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

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"connexion_pronote_failed: {type(e).__name__}")

if __name__ == "__main__":
    import os, uvicorn
    port = int(os.getenv("PORT", "8081"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="debug")  # <-- PAS "main:app"


