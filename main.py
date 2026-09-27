import uvicorn
from fastapi import FastAPI

from app.deps import build_engine
from app.routes import register

app = FastAPI(title="multi-tenant-api-gateway")

_engine = build_engine()
register(app, _engine)


@app.get("/")
def read_root():
    return {"Hello": "World"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
