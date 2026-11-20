import os
import json
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# Claves de OpenRouter (puedes sobrescribirlas con la variable de entorno OPENROUTER_API_KEYS separada por comas)
DEFAULT_KEYS = [
    "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",
    "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",
    "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",
    "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",
    "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",
    "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",
    "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",
]

env_keys = os.environ.get("OPENROUTER_API_KEYS", "").strip()
API_KEYS = [k.strip() for k in env_keys.split(",") if k.strip()] if env_keys else DEFAULT_KEYS
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "cambiar-este-token-secreto")

STATE_FILE = Path("sms_state.json")

PLANS = {
    "basico": {"name": "Plan Básico", "price_usd": 100, "quota": 100},
    "crecimiento": {"name": "Plan Crecimiento", "price_usd": 250, "quota": 600},
    "pro": {"name": "Plan Pro", "price_usd": 500, "quota": 2000},
}


class KeyRotator:
    def __init__(self, keys):
        self.keys = keys
        self.index = 0
        self.cooldowns = {}

    def get_key(self):
        now = time.time()
        total = len(self.keys)
        for _ in range(total):
            key = self.keys[self.index]
            self.index = (self.index + 1) % total
            if self.cooldowns.get(key, 0) <= now:
                return key
        # Si todas están en enfriamiento, devuelve la próxima de todos modos
        key = self.keys[self.index]
        self.index = (self.index + 1) % total
        return key

    def penalize(self, key, seconds=60):
        self.cooldowns[key] = time.time() + seconds


rotator = KeyRotator(API_KEYS)


def current_cycle():
    return datetime.utcnow().strftime("%Y-%m")


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"clients": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def ensure_client(state, client_id, plan_key="basico"):
    cycle = current_cycle()
    plan = PLANS.get(plan_key, PLANS["basico"])
    if client_id not in state["clients"]:
        state["clients"][client_id] = {
            "plan": plan_key if plan_key in PLANS else "basico",
            "quota": plan["quota"],
            "used": 0,
            "cycle": cycle,
        }
    client = state["clients"][client_id]
    if client.get("cycle") != cycle:
        client["cycle"] = cycle
        client["used"] = 0
        client["quota"] = PLANS[client["plan"]]["quota"]
    return client


def is_authorized():
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {ADMIN_TOKEN}"


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "keys_loaded": len(API_KEYS),
        "plans": PLANS,
        "cycle": current_cycle(),
    })


@app.post("/admin/activate-plan")
def activate_plan():
    if not is_authorized():
        return jsonify({"error": "No autorizado"}), 401

    data = request.get_json(force=True, silent=True) or {}
    client_id = str(data.get("client_id", "")).strip()
    plan_key = str(data.get("plan", "basico")).strip().lower()

    if not client_id or plan_key not in PLANS:
        return jsonify({"error": "client_id o plan inválido"}), 400

    state = load_state()
    client = ensure_client(state, client_id, plan_key)
    client["plan"] = plan_key
    client["quota"] = PLANS[plan_key]["quota"]
    client["used"] = 0
    client["cycle"] = current_cycle()
    save_state(state)

    return jsonify({"ok": True, "client_id": client_id, "state": client})


@app.get("/client/<client_id>/status")
def client_status(client_id):
    state = load_state()
    client = ensure_client(state, client_id)
    save_state(state)
    remaining = max(0, client["quota"] - client["used"])
    return jsonify({
        "client_id": client_id,
        "plan": client["plan"],
        "quota": client["quota"],
        "used": client["used"],
        "remaining": remaining,
        "cycle": client["cycle"],
    })


@app.post("/sms/consume")
def consume_sms():
    if not is_authorized():
        return jsonify({"error": "No autorizado"}), 401

    data = request.get_json(force=True, silent=True) or {}
    client_id = str(data.get("client_id", "")).strip()
    count = int(data.get("count", 1))

    if not client_id or count < 1:
        return jsonify({"error": "client_id y count (>=1) requeridos"}), 400

    state = load_state()
    client = ensure_client(state, client_id)

    if client["used"] + count > client["quota"]:
        save_state(state)
        return jsonify({
            "allowed": False,
            "reason": "Límite mensual alcanzado. Sube de plan o espera el reinicio del próximo mes.",
            "state": client,
        }), 402

    client["used"] += count
    save_state(state)
    return jsonify({
        "allowed": True,
        "remaining": client["quota"] - client["used"],
        "state": client,
    })


@app.post("/ai/chat")
def ai_chat():
    data = request.get_json(force=True, silent=True) or {}
    prompt = str(data.get("prompt", "")).strip()
    model = str(data.get("model", "openrouter/free")).strip()

    if not prompt:
        return jsonify({"error": "Falta el campo prompt"}), 400

    last_error = None
    for _ in range(len(API_KEYS)):
        key = rotator.get_key()
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            if resp.status_code == 200:
                payload = resp.json()
                reply = payload["choices"][0]["message"]["content"]
                return jsonify({"ok": True, "reply": reply})
            if resp.status_code in (401, 402, 429, 500, 502, 503):
                rotator.penalize(key, seconds=60)
                last_error = f"HTTP {resp.status_code}: {resp.text[:180]}"
                continue
            return jsonify({"ok": False, "error": f"HTTP {resp.status_code}", "detail": resp.text[:300]}), resp.status_code
        except requests.RequestException as exc:
            rotator.penalize(key, seconds=30)
            last_error = str(exc)

    return jsonify({"ok": False, "error": "Todas las claves fallaron o están en espera", "detail": last_error}), 503


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
