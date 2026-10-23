import uvicorn

if __name__ == "__main__":
    uvicorn.run("hub.main:create_app", factory=True, host="127.0.0.1", port=8840,
                proxy_headers=False, access_log=False, workers=1)
