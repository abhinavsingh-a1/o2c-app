from fastapi import FastAPI

app = FastAPI(title="hello")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
