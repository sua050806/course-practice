import os
import redis
from flask import Flask, jsonify

app = Flask(__name__)

redis_host = os.environ.get("REDIS_HOST", "localhost")
redis_client = redis.Redis(host=redis_host, port=6379, decode_responses=True)


@app.route("/api/health")
def health():
    redis_ok = False
    try:
        redis_client.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    result = {}
    result["status"] = "ok"
    result["redis"] = redis_ok
    result["worker_pid"] = os.getpid()
    return jsonify(result)