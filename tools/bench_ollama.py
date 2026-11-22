"""Try local models through the running server's Ollama brain (via the app API).

uv run python tools/bench_ollama.py http://127.0.0.1:11435 model1 model2 ...
"""
import json
import sys
import time
import urllib.request

API = "http://127.0.0.1:8765"
QUESTIONS = ["Привет! Как тебя зовут и что ты умеешь? Ответь коротко.",
             "Сколько будет семнадцать умножить на двадцать три?"]


def post(path, body, timeout=300):
    req = urllib.request.Request(API + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main():
    url, models = sys.argv[1], sys.argv[2:]
    before = json.loads(urllib.request.urlopen(API + "/api/state").read())["config"]
    post("/api/config", {"brain": "ollama", "ollama": {"url": url}})
    try:
        for m in models:
            post("/api/config", {"ollama": {"model": m}})
            print(f"\n=== {m}")
            for i, q in enumerate([QUESTIONS[0]] + QUESTIONS):   # first run = cold load
                t0 = time.time()
                r = post("/api/brain/test", {"text": q})
                tag = "cold" if i == 0 else "warm"
                print(f"[{tag} {time.time() - t0:5.1f}s] {q}\n    -> {r['reply'][:300]}")
    finally:
        post("/api/config", {"brain": before["brain"],
                             "ollama": {"url": before["ollama"]["url"], "model": before["ollama"]["model"]}})
        print("\nrestored brain:", before["brain"])


if __name__ == "__main__":
    main()
