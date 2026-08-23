# build-pilot

Session 1 is a thin FastAPI stub for the build-pilot API. It exposes health and GitHub webhook endpoints; queues, databases, agents, and the dashboard come later.

## Run locally

Install dependencies and start the API:

```sh
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Check health:

```sh
curl http://localhost:8000/health
```

Send a webhook payload:

```sh
curl -X POST http://localhost:8000/webhook \
  -H 'Content-Type: application/json' \
  -d '{"zen":"GitHub ping"}'
```

Open the interactive API docs at http://localhost:8000/docs.

Optional Docker run:

```sh
docker build -t build-pilot .
docker run -p 8000:8000 -e PORT=8000 build-pilot
```
