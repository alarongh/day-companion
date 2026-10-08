"""Stage on Workers Free, migrate private SQLite, then replace polling with webhook.

All credentials stay in memory or ignored data/. No billing mutations are issued.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import dotenv_values

PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT
CLOUD = PROJECT / "cloudflare"
STATE = PROJECT / "data" / "cloudflare-runtime.json"
PLAN = PROJECT / "data" / "cloudflare-plan-verification.json"
TABLES = ("users", "schedules", "personal", "drafts", "sessions", "overrides", "deliveries", "events")


class Deployment:
    def __init__(self, cli):
        self.cli = str(cli)
        self.local = dotenv_values(PROJECT / ".env")
        self.value = json.loads(STATE.read_text()) if STATE.exists() else {}
        self.http = httpx.Client(timeout=60, headers={"User-Agent": "DayCompanion-Deployment/0.2"})

    def save(self):
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(self.value, indent=2), encoding="utf-8")

    def command(self, *args, payload=None):
        env = {**os.environ, "WRANGLER_SEND_METRICS": "false"}
        if self.value.get("account"):
            env["CLOUDFLARE_ACCOUNT_ID"] = self.value["account"]
        result = subprocess.run(
            ["node", self.cli, *args],
            cwd=CLOUD,
            env=env,
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
        )
        if result.returncode:
            text = result.stdout + result.stderr
            for secret in (
                self.local.get("BOT_TOKEN"),
                self.value.get("webhook_secret"),
                self.value.get("deploy_secret"),
            ):
                if secret:
                    text = text.replace(secret, "<REDACTED>")
            (PROJECT / "data" / "cloudflare-last-error.log").write_text(text, encoding="utf-8")
            raise RuntimeError(
                "Cloudflare command failed: " + " ".join(args[:2]) + " (sanitized local log saved)"
            )
        return result.stdout

    def response(self, r):
        if r.status_code >= 400:
            raise RuntimeError("Remote request failed: HTTP " + str(r.status_code))
        return r.json()

    def free_account(self):
        identity = json.loads(self.command("whoami", "--json"))
        accounts = identity.get("accounts", [])
        selected = self.value.get("account") or os.getenv("CLOUDFLARE_ACCOUNT_ID")
        if not selected and len(accounts) == 1:
            selected = accounts[0]["id"]
        if selected not in {x["id"] for x in accounts}:
            raise RuntimeError("Select an authorized Cloudflare account")
        auth = json.loads(self.command("auth", "token", "--json"))
        r = self.http.get(
            "https://api.cloudflare.com/client/v4/accounts/" + selected + "/subscriptions",
            headers={"Authorization": "Bearer " + auth["token"]},
        )
        if r.status_code == 403:
            proof = json.loads(PLAN.read_text()) if PLAN.exists() else {}
            age = time.time() - float(proof.get("verified_at", 0))
            if not (
                proof.get("account") == selected
                and proof.get("plan") == "free"
                and proof.get("source") == "https://dash.cloudflare.com/" + selected + "/workers/plans"
                and 0 <= age <= 3600
            ):
                raise RuntimeError(
                    "Current Workers Free plan must be verified in the dashboard; billing access is not available"
                )
        else:
            data = self.response(r)
            if not data.get("success") or not isinstance(data.get("result"), list):
                raise RuntimeError("Unable to verify a free plan")
            for item in data["result"]:
                description = json.dumps(item.get("rate_plan", {})).lower()
                if "worker" in description and (
                    "free" not in description or float(item.get("price") or 0) > 0
                ):
                    raise RuntimeError("Paid Workers plan detected; deployment stopped")
        self.value["account"] = selected
        self.save()

    def api(self, operation, body=None):
        method = "POST" if body is not None else "GET"
        r = self.http.request(
            method,
            self.value["url"] + "/_admin/" + operation,
            json=body,
            headers={"X-Deployment-Secret": self.value["deploy_secret"]},
        )
        return self.response(r)

    def telegram(self, method, body=None):
        r = self.http.post(
            "https://api.telegram.org/bot" + self.local["BOT_TOKEN"] + "/" + method, json=body or {}
        )
        data = self.response(r)
        if not data.get("ok"):
            raise RuntimeError("Telegram rejected " + method)
        return data["result"]

    def stage(self):
        self.free_account()
        subprocess.run([sys.executable, str(CLOUD / "prepare.py")], check=True, capture_output=True)
        self.value.setdefault("webhook_secret", secrets.token_urlsafe(36))
        self.value.setdefault("deploy_secret", secrets.token_urlsafe(36))
        self.save()
        output = self.command("deploy")
        urls = re.findall(r"https://day-companion-bot\.[a-z0-9-]+\.workers\.dev", output)
        if not urls:
            raise RuntimeError("Deployment did not return this Worker's URL")
        self.value["url"] = urls[-1]
        self.save()
        values = {
            "BOT_TOKEN": self.local["BOT_TOKEN"],
            "ALLOWED_USER_IDS": self.local.get("ALLOWED_USER_IDS") or "",
            "WEBHOOK_SECRET": self.value["webhook_secret"],
            "DEPLOY_SECRET": self.value["deploy_secret"],
        }
        self.command("secret", "bulk", payload=json.dumps(values))
        status = self.api("status")
        if not status.get("configured"):
            raise RuntimeError("Cloud runtime not configured")
        self.probe()
        print("Workers Free deployment staged and probed; local polling unchanged")
        print(self.value["url"])

    def probe(self):
        day = datetime.now(ZoneInfo("Europe/Moscow")).date()
        profile = {
            "travel": 120,
            "buffer": 0,
            "campus_walk": 0,
            "preparation": 60,
            "sleep": {"enabled": True, "hours": 8, "latency_minutes": 15},
            "home_place": "a",
            "destination_place": "b",
            "places": [
                {"id": "a", "label": "Test A", "latitude": 55.75, "longitude": 37.61},
                {"id": "b", "label": "Test B", "latitude": 56.01, "longitude": 37.48},
            ],
        }
        night = {
            "profile": profile,
            "day": str(day),
            "lessons": [{"title": "Synthetic evening", "date": str(day), "start": "22:00", "end": "23:30"}],
            "weather": True,
        }
        result = self.api("probe", night)
        if result.get("home") != f"{day + timedelta(days=1)}T01:30:00+03:00" or not result.get(
            "weather_complete"
        ):
            raise RuntimeError("Cloud route/weather probe failed")
        morning = {
            "profile": profile,
            "day": str(day),
            "lessons": [{"title": "Synthetic morning", "date": str(day), "start": "09:00", "end": "10:00"}],
        }
        result = self.api("probe", morning)
        if (
            result.get("wake") != f"{day}T06:00:00+03:00"
            or result.get("bed") != f"{day - timedelta(days=1)}T21:45:00+03:00"
        ):
            raise RuntimeError("Cloud sleep date probe failed")

    def local_process(self, action):
        script = PROJECT / "scripts" / "cloudflare_local.ps1"
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-File",
                str(script),
                "-Action",
                action,
                "-Workspace",
                str(WORKSPACE),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
        if result.returncode:
            raise RuntimeError("Local process " + action + " failed")

    def snapshot(self):
        db_path = PROJECT / "data" / "bot.sqlite3"
        backup = PROJECT / "data" / "backups" / ("bot-before-cloud-" + str(int(time.time())) + ".sqlite3")
        backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as db, sqlite3.connect(backup) as copy:
            db.backup(copy)
            data = {table: [list(row) for row in copy.execute(f"SELECT * FROM {table}")] for table in TABLES}
        canonical = {
            table: sorted(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in data[table])
            for table in TABLES
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        return data, digest

    def cutover(self):
        self.free_account()
        self.probe()
        existing = self.telegram("getWebhookInfo")
        desired = self.value["url"] + "/telegram"
        if existing.get("url") and existing["url"] != desired:
            raise RuntimeError("Another webhook is active; migration stopped")
        if self.api("status")["active"]:
            if existing.get("url") == desired:
                return self.status()
            raise RuntimeError("Cloud is already active with an unexpected webhook state")
        self.local_process("stop")
        try:
            data, digest = self.snapshot()
            status = self.api("status")
            if any(status["counts"].values()):
                if self.api("digest")["sha256"] != digest:
                    raise RuntimeError("Cloud contains a different snapshot; refusing to overwrite it")
            else:
                migrated = self.api("import", data)
                if migrated["counts"] != {k: len(v) for k, v in data.items()}:
                    raise RuntimeError("Migration row counts differ")
            if self.api("digest")["sha256"] != digest:
                raise RuntimeError("Private migration digest differs")
            self.telegram(
                "setWebhook",
                {
                    "url": desired,
                    "secret_token": self.value["webhook_secret"],
                    "allowed_updates": ["message", "callback_query"],
                    "max_connections": 1,
                    "drop_pending_updates": False,
                },
            )
            self.api("activate", {})
            self.value["cutover_at"] = time.time()
            self.value["migration_digest"] = digest
            self.save()
        except Exception:
            # Preserve local availability without two concurrent senders.
            self.api("pause", {})
            self.telegram("deleteWebhook", {"drop_pending_updates": False})
            self.local_process("start")
            raise
        print("Private database migrated and digest verified; local polling stopped; cloud activated")
        self.status()

    def status(self):
        status = self.api("status")
        webhook = self.telegram("getWebhookInfo")
        desired = self.value["url"] + "/telegram"
        health = self.response(self.http.get(self.value["url"] + "/health"))
        print(
            json.dumps(
                {
                    "health": health,
                    "webhook_matches": webhook.get("url") == desired,
                    "pending_updates": webhook.get("pending_update_count"),
                    "webhook_error": bool(webhook.get("last_error_message")),
                    "status": status,
                },
                ensure_ascii=True,
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["stage", "cutover", "status", "probe"])
    parser.add_argument(
        "--cli", type=Path, default=CLOUD / "node_modules" / "wrangler" / "bin" / "wrangler.js"
    )
    args = parser.parse_args()
    try:
        getattr(Deployment(args.cli), args.action)()
    except Exception as exc:
        # Network exceptions may embed a bot URL; only our own RuntimeErrors are shown.
        print(str(exc) if isinstance(exc, RuntimeError) else "Deployment failed: " + type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
