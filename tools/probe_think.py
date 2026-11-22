"""How does each local model react to think=false and /no_think?  python tools/probe_think.py URL model..."""
import json
import sys
import time
import urllib.request


def chat(url, model, text, think):
    body = {"model": model, "stream": False, "messages": [{"role": "user", "content": text}],
            "options": {"num_predict": 300, "temperature": 0.6}}
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(url + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        res = json.loads(r.read())
    msg = res["message"]
    return time.time() - t0, res.get("eval_count"), msg.get("content", "").strip(), len(msg.get("thinking") or "")


url = sys.argv[1]
q = "Привет! Как тебя зовут? Ответь одним коротким предложением."
for m in sys.argv[2:]:
    print(f"=== {m}")
    for label, text, think in [("think=false", q, False), ("think=false + /no_think", q + " /no_think", False)]:
        try:
            s, n, content, th = chat(url, m, text, think)
            print(f"  {label:24s} {s:5.1f}s tokens={n} thinking_chars={th} -> {content[:120]!r}")
        except Exception as e:
            print(f"  {label:24s} ERROR {e}")
