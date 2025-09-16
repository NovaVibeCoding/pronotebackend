import os, sys
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()  # charge .env si présent

PRONOTE_URL = os.getenv("PRONOTE_URL", "https://0061884r.index-education.net/pronote/eleve.html")
ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*")
MOCK = os.getenv("MOCK", "1") == "1"  # 1 = test sans Pronote

class FetchPayload(BaseModel):
    username: str
    password: str
    days: int = 7
    start: Optional[str] = None
    end:   Optional[str] = None

app = FastAPI(title="Pronote JSON API (Thonny)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOW_ORIGINS.split(",")] if ALLOW_ORIGINS else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MOCK_NOTES = {
    "periods": [
        {"name": "Trimestre 1", "grades": [
            {"date":"2025-09-10","subjectId":"MATH","subjectLabel":"Maths","value":15,"outOf":20,"coefficient":2,"comment":"Bon travail"},
            {"date":"2025-09-12","subjectId":"HIST","subjectLabel":"Histoire","value":13,"outOf":20}
        ]}
    ]
}
MOCK_LESSONS = {
    "lessons":[
        {"date":"2025-09-15","start":"09:00","end":"10:00","subjectId":"MATH","subjectLabel":"Maths","room":"B12","canceled":False,
         "content":{"title":"Équations","description":"Méthodes de résolution"}},
        {"date":"2025-09-15","start":"10:15","end":"11:15","subjectId":"FR","subjectLabel":"Français","room":"C03","canceled":False,
         "content":{"title":"Argumentation","description":None}}
    ]
}

@app.get("/")
def root():
    return {"ok": True, "service": "pronote-json-api", "mode": "MOCK" if MOCK else "REAL"}

@app.post("/pronote/fetch")
def pronote_fetch(payload: FetchPayload):
    # plage de dates
    if payload.start and payload.end:
        start_d = datetime.fromisoformat(payload.start).date()
        end_d   = datetime.fromisoformat(payload.end).date()
    else:
        end_d = date.today()
        start_d = end_d - timedelta(days=max(1, payload.days))

    if MOCK:
        return {
            "notes": MOCK_NOTES,
            "lessons": MOCK_LESSONS,
            "meta": {"school_url": "MOCK", "range": {"start": start_d.isoformat(), "end": end_d.isoformat()}}
        }

    # --- Mode réel : Pronote + atrium_sud, pronotepy==2.14.4 ---
    import pronotepy
    EXPECTED = "2.14.4"
    if getattr(pronotepy, "__version__", None) != EXPECTED:
        raise HTTPException(500, f"pronotepy {pronotepy.__version__} détecté — attendu {EXPECTED}")

    from pronotepy.ent import atrium_sud
    try:
        client = pronotepy.Client(PRONOTE_URL, username=payload.username, password=payload.password, ent=atrium_sud)
        if not client.logged_in:
            raise HTTPException(401, "invalid_credentials")
    except Exception as e:
        raise HTTPException(502, f"connexion_pronote_failed: {type(e).__name__}")

    def json_from_notes(client) -> Dict[str, Any]:
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
                    "value": float(g.grade) if g.grade is not None else None,
                    "outOf": float(g.out_of) if g.out_of is not None else None,
                    "coefficient": getattr(g, "coefficient", None),
                    "comment": getattr(g, "comment", None),
                })
            out["periods"].append({"name": period.name, "grades": grades})
        return out

    def json_from_lessons(client, start_d: date, end_d: date) -> Dict[str, Any]:
        lessons = client.lessons(start_d, end_d)
        lessons.sort(key=lambda c: (c.start, c.end))
        arr: List[Dict[str, Any]] = []
        for c in lessons:
            subj_name = getattr(c.subject, "name", "?")
            subj_code = getattr(c.subject, "code", None)
            arr.append({
                "date": c.start.strftime("%Y-%m-%d"),
                "start": c.start.strftime("%H:%M"),
                "end": c.end.strftime("%H:%M"),
                "subjectId": subj_code or subj_name,
                "subjectLabel": subj_name,
                "room": c.classroom or None,
                "canceled": bool(c.canceled),
                "content": {
                    "title": c.content.title if c.content and c.content.title else None,
                    "description": c.content.description if c.content and c.content.description else None
                }
            })
        return {"lessons": arr}

    notes = json_from_notes(client)
    lessons = json_from_lessons(client, start_d, end_d)
    return {"notes": notes, "lessons": lessons,
            "meta": {"school_url": PRONOTE_URL, "range": {"start": start_d.isoformat(), "end": end_d.isoformat()}}}

# --- Démarrage pratique via F5 dans Thonny ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8080, reload=True)
