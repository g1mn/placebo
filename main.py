from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="SoundCheck")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "soundcheck"}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>SoundCheck</title></head><body>"
        "<h1>SoundCheck</h1>"
        "<p>Accessibility fixes that can't fool the checker. Hear the difference.</p>"
        "</body></html>"
    )
