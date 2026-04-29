from fastapi import FastAPI

from feishu_agent.routers.feishu import router as feishu_router

app = FastAPI(
    title="FeishuOPC Agent",
    version="1.0.0",
)

app.include_router(feishu_router, prefix="/api/v1")


@app.get("/health")
@app.get("/api/v1/health")
async def health_check():
    return {"status": "healthy", "service": "feishu-agent"}


@app.get("/")
async def root():
    return {"message": "FeishuOPC Agent", "version": "1.0.0"}
