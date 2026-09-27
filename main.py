import uvicorn

from app.main import app  # noqa: F401  (保持根模块可用)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000)
