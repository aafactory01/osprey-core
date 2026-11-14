from flask import Flask, render_template, jsonify, request
import os
import json

app = Flask(__name__)

# Mock data for demonstration
state = {
    "health": {"sla": 99.99, "slo": 99.95, "status": "Healthy"},
    "rollouts": [
        {"id": "R-101", "type": "canary", "status": "active", "traffic": 15},
        {"id": "R-100", "type": "production", "status": "completed", "traffic": 100}
    ],
    "workflow": [
        {"stage": "S1_SPEC", "status": "completed"},
        {"stage": "S2_CODE", "status": "completed"},
        {"stage": "S3_TEST", "status": "completed"},
        {"stage": "S4_REVIEW", "status": "active"},
        {"stage": "S5_RELEASE_GATE", "status": "pending"},
        {"stage": "S6_DEPLOY_CLOSE", "status": "pending"}
    ],
    "dreams": {
        "id": "DP-001",
        "description": "Skill upgrade: ops-release-guardrails v1.0.0 -> v1.1.0",
        "scorecard": "60% -> 100% generic skill shadow replay",
        "status": "Ready"
    },
    "receipts": [
        {"id": "REC-001", "signature": "hmac-sha256-a1b2c3d4", "timestamp": "2026-11-14T10:00:00Z"}
    ]
}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/health')
def get_health():
    return jsonify(state["health"])

@app.route('/api/rollouts')
def get_rollouts():
    return jsonify(state["rollouts"])

@app.route('/api/workflow')
def get_workflow():
    return jsonify(state["workflow"])

@app.route('/api/dreams')
def get_dreams():
    return jsonify(state["dreams"])

@app.route('/api/receipts')
def get_receipts():
    return jsonify(state["receipts"])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
