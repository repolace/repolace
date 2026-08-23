from fastapi import FastAPI

app = FastAPI(title="repolace")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
