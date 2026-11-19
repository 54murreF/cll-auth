"""
CLL Auth — Discord bot + Flask API
Render: Web Service, Start Command: python main.py
Env: TOKEN, optional DEV_ROLE_ID, LOG_CHANNEL_ID
"""

import os
import re
import json
import time
import random
import string
from datetime import datetime, timedelta
from threading import Thread, Lock
from collections import defaultdict

import discord
from discord import app_commands, ui
from discord.ext import commands
from flask import Flask, request, jsonify

# ================== CONFIG ==================
TOKEN = os.environ["TOKEN"]
DEV_ROLE_ID = int(os.environ.get("DEV_ROLE_ID", "1566118227489071344"))
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "1566120795934040314"))

DB_FILE = os.environ.get("DB_FILE", "database.json")
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 30
MAX_BODY = 4096

# ================== DISCORD ==================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ================== FLASK ==================
app = Flask(__name__)
db_lock = Lock()
rate_lock = Lock()
rate_buckets = defaultdict(list)

# ================== DB ==================
def _default_db():
    return {
        "keys": {},
        "whitelist": [],
        "banned_hwids": [],
        "banned_ips": [],
        "activations": [],
    }

def load_db():
    with db_lock:
        if not os.path.exists(DB_FILE):
            data = _default_db()
            with open(DB_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return data
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in _default_db().items():
            data.setdefault(k, v)
        return data

def save_db(data):
    with db_lock:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

def generate_key():
    parts = [
        "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        for _ in range(3)
    ]
    return "-".join(parts)

def is_dev(interaction: discord.Interaction) -> bool:
    return discord.utils.get(interaction.user.roles, id=DEV_ROLE_ID) is not None

async def send_log(text: str):
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        try:
            await channel.send(text[:1900])
        except Exception:
            pass

def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.remote_addr or "unknown").strip()

def rate_limited(ip: str) -> bool:
    now = time.time()
    with rate_lock:
        bucket = rate_buckets[ip]
        rate_buckets[ip] = [t for t in bucket if now - t < RATE_LIMIT_WINDOW]
        if len(rate_buckets[ip]) >= RATE_LIMIT_MAX:
            return True
        rate_buckets[ip].append(now)
        return False

def append_activation(db, entry: dict):
    db.setdefault("activations", [])
    db["activations"].append(entry)
    db["activations"] = db["activations"][-200:]

KEY_TYPES = {
    "tester": {"role": "tester", "days": None, "label": "Tester — inf"},
    "dev": {"role": "dev", "days": None, "label": "Dev — inf"},
    "user_7d": {"role": "user", "days": 7, "label": "User — 7d"},
    "user_14d": {"role": "user", "days": 14, "label": "User — 14d"},
    "user_30d": {"role": "user", "days": 30, "label": "User — 30d"},
}

# ================== KEY UI ==================
class KeyTypeView(ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return False
        return True

    async def create_key(self, interaction: discord.Interaction, key_type: str):
        info = KEY_TYPES[key_type]
        key = generate_key()
        now = datetime.utcnow()
        expires_at = None
        if info["days"]:
            expires_at = (now + timedelta(days=info["days"])).isoformat()

        db = load_db()
        db["keys"][key] = {
            "role": info["role"],
            "type": key_type,
            "created_at": now.isoformat(),
            "created_by": str(interaction.user),
            "expires_at": expires_at,
            "userid": None,
            "hwid": None,
            "ips": [],
            "banned": False,
            "activated_at": None,
        }
        save_db(db)

        exp = "inf" if not expires_at else expires_at[:10]
        await interaction.response.edit_message(
            content=(
                f"**Key created**\n"
                f"Type: `{info['label']}`\n"
                f"Key: `{key}`\n"
                f"Expires: `{exp}`\n"
                f"_One-time bind: first HWID + UserId locks the key._"
            ),
            view=None,
        )
        await send_log(f"[CREATE] `{key}` | {info['label']} | by {interaction.user.mention}")

    @ui.button(label="Tester inf", style=discord.ButtonStyle.primary)
    async def tester(self, interaction: discord.Interaction, button: ui.Button):
        await self.create_key(interaction, "tester")

    @ui.button(label="Dev inf", style=discord.ButtonStyle.primary)
    async def dev(self, interaction: discord.Interaction, button: ui.Button):
        await self.create_key(interaction, "dev")

    @ui.button(label="User 7d", style=discord.ButtonStyle.secondary)
    async def user7(self, interaction: discord.Interaction, button: ui.Button):
        await self.create_key(interaction, "user_7d")

    @ui.button(label="User 14d", style=discord.ButtonStyle.secondary)
    async def user14(self, interaction: discord.Interaction, button: ui.Button):
        await self.create_key(interaction, "user_14d")

    @ui.button(label="User 30d", style=discord.ButtonStyle.secondary)
    async def user30(self, interaction: discord.Interaction, button: ui.Button):
        await self.create_key(interaction, "user_30d")

# ================== API ==================
@app.after_request
def force_json_headers(resp):
    if request.path in ("/check", "/health"):
        resp.headers["Content-Type"] = "application/json"
        resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/")
def home():
    return "CLL Auth Online", 200, {"Content-Type": "text/plain"}

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.utcnow().isoformat()})

@app.route("/check", methods=["POST", "OPTIONS"])
def check_key():
    try:
        if request.method == "OPTIONS":
            return jsonify({"status": "ok"}), 200

        ip = client_ip()

        if rate_limited(ip):
            return jsonify({"status": "rate_limited", "message": "too many requests"}), 200

        db = load_db()

        if ip in db.get("banned_ips", []):
            append_activation(db, {
                "ts": datetime.utcnow().isoformat(),
                "event": "blocked_ip",
                "ip": ip,
            })
            save_db(db)
            return jsonify({"status": "banned", "reason": "ip"}), 200

        if not request.data or len(request.data) > MAX_BODY:
            return jsonify({"status": "invalid", "reason": "bad_body"}), 200

        data = request.get_json(silent=True) or {}
        key = str(data.get("key", "")).strip().upper()
        userid = data.get("userid")
        hwid = str(data.get("hwid", "")).strip()
        username = str(data.get("username", ""))[:32]

        key = re.sub(r"[^A-Z0-9\-]", "", key)

        if not key or userid is None or not hwid:
            return jsonify({"status": "invalid", "reason": "missing_data"}), 200

        try:
            userid = int(userid)
        except (TypeError, ValueError):
            return jsonify({"status": "invalid", "reason": "bad_userid"}), 200

        if len(hwid) < 4 or len(hwid) > 128:
            return jsonify({"status": "invalid", "reason": "bad_hwid"}), 200

        if hwid in db.get("banned_hwids", []):
            append_activation(db, {
                "ts": datetime.utcnow().isoformat(),
                "event": "blocked_hwid",
                "ip": ip,
                "userid": userid,
                "hwid": hwid[:16],
            })
            save_db(db)
            return jsonify({"status": "banned", "reason": "hwid"}), 200

        key_data = db["keys"].get(key)
        if not key_data:
            append_activation(db, {
                "ts": datetime.utcnow().isoformat(),
                "event": "not_found",
                "ip": ip,
                "userid": userid,
                "key": key[:12],
            })
            save_db(db)
            return jsonify({"status": "invalid", "reason": "not_found"}), 200

        if key_data.get("banned"):
            return jsonify({"status": "banned", "reason": "key"}), 200

        expires_at = key_data.get("expires_at")
        if expires_at:
            try:
                if datetime.utcnow() > datetime.fromisoformat(expires_at):
                    return jsonify({"status": "expired", "expires_at": expires_at}), 200
            except Exception:
                pass

        if key_data.get("hwid") is None:
            key_data["hwid"] = hwid
            key_data["userid"] = userid
            key_data["activated_at"] = datetime.utcnow().isoformat()
            key_data.setdefault("ips", [])
            if ip not in key_data["ips"]:
                key_data["ips"].append(ip)
            append_activation(db, {
                "ts": datetime.utcnow().isoformat(),
                "event": "activate",
                "key": key,
                "ip": ip,
                "userid": userid,
                "username": username,
                "hwid": hwid[:16],
                "role": key_data.get("role"),
            })
            save_db(db)
            print(f"[ACTIVATE] {key} user={userid} ip={ip} hwid={hwid[:8]}...")
            return jsonify({
                "status": "valid",
                "role": key_data.get("role", "user"),
                "type": key_data.get("type"),
                "expires_at": key_data.get("expires_at"),
                "whitelist": userid in db.get("whitelist", []),
            }), 200

        if key_data.get("hwid") != hwid:
            append_activation(db, {
                "ts": datetime.utcnow().isoformat(),
                "event": "hwid_mismatch",
                "key": key,
                "ip": ip,
                "userid": userid,
                "hwid": hwid[:16],
                "bound_hwid": str(key_data.get("hwid"))[:16],
            })
            save_db(db)
            return jsonify({"status": "hwid_mismatch"}), 200

        if key_data.get("userid") is not None and int(key_data["userid"]) != userid:
            append_activation(db, {
                "ts": datetime.utcnow().isoformat(),
                "event": "userid_mismatch",
                "key": key,
                "ip": ip,
                "userid": userid,
            })
            save_db(db)
            return jsonify({"status": "userid_mismatch"}), 200

        key_data.setdefault("ips", [])
        if ip not in key_data["ips"]:
            key_data["ips"].append(ip)
            key_data["ips"] = key_data["ips"][-10:]
        append_activation(db, {
            "ts": datetime.utcnow().isoformat(),
            "event": "check_ok",
            "key": key,
            "ip": ip,
            "userid": userid,
            "username": username,
        })
        save_db(db)

        return jsonify({
            "status": "valid",
            "role": key_data.get("role", "user"),
            "type": key_data.get("type"),
            "expires_at": key_data.get("expires_at"),
            "whitelist": userid in db.get("whitelist", []),
        }), 200

    except Exception as e:
        print("[CHECK ERROR]", repr(e))
        return jsonify({"status": "error", "message": "server_error"}), 200

# ================== DISCORD COMMANDS ==================
@bot.event
async def on_ready():
    print(f"[CLL] Bot ready: {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"[CLL] Synced {len(synced)} commands")
    except Exception as e:
        print("[CLL] Sync error:", e)

@bot.tree.command(name="genkey", description="Create a key (dev only)")
async def genkey(interaction: discord.Interaction):
    if not is_dev(interaction):
        return await interaction.response.send_message("No access.", ephemeral=True)
    view = KeyTypeView(interaction.user.id)
    await interaction.response.send_message("Select key type:", view=view, ephemeral=True)

@bot.tree.command(name="ban", description="Ban a key")
@app_commands.describe(key="Key string")
async def ban_key(interaction: discord.Interaction, key: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("No access.", ephemeral=True)
    key = re.sub(r"[^A-Z0-9\-]", "", key.strip().upper())
    db = load_db()
    if key not in db["keys"]:
        return await interaction.response.send_message("Key not found.", ephemeral=True)
    db["keys"][key]["banned"] = True
    save_db(db)
    await interaction.response.send_message(f"Banned key `{key}`", ephemeral=True)
    await send_log(f"[BAN KEY] `{key}` | {interaction.user.mention}")

@bot.tree.command(name="unban", description="Unban a key")
@app_commands.describe(key="Key string")
async def unban_key(interaction: discord.Interaction, key: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("No access.", ephemeral=True)
    key = re.sub(r"[^A-Z0-9\-]", "", key.strip().upper())
    db = load_db()
    if key not in db["keys"]:
        return await interaction.response.send_message("Key not found.", ephemeral=True)
    db["keys"][key]["banned"] = False
    save_db(db)
    await interaction.response.send_message(f"Unbanned `{key}`", ephemeral=True)
    await send_log(f"[UNBAN KEY] `{key}` | {interaction.user.mention}")

@bot.tree.command(name="banhwid", description="Ban HWID globally")
@app_commands.describe(hwid="Hardware id string")
async def ban_hwid(interaction: discord.Interaction, hwid: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("No access.", ephemeral=True)
    hwid = hwid.strip()
    db = load_db()
    if hwid not in db["banned_hwids"]:
        db["banned_hwids"].append(hwid)
        save_db(db)
    await interaction.response.send_message(f"Banned HWID `{hwid[:24]}...`", ephemeral=True)
    await send_log(f"[BAN HWID] `{hwid[:32]}` | {interaction.user.mention}")

@bot.tree.command(name="banip", description="Ban IP globally")
@app_commands.describe(ip="IPv4/IPv6")
async def ban_ip(interaction: discord.Interaction, ip: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("No access.", ephemeral=True)
    ip = ip.strip()
    db = load_db()
    if ip not in db["banned_ips"]:
        db["banned_ips"].append(ip)
        save_db(db)
    await interaction.response.send_message(f"Banned IP `{ip}`", ephemeral=True)
    await send_log(f"[BAN IP] `{ip}` | {interaction.user.mention}")

@bot.tree.command(name="unbanip", description="Unban IP")
@app_commands.describe(ip="IP")
async def unban_ip(interaction: discord.Interaction, ip: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("No access.", ephemeral=True)
    db = load_db()
    if ip in db["banned_ips"]:
        db["banned_ips"].remove(ip)
        save_db(db)
    await interaction.response.send_message(f"Unbanned IP `{ip}`", ephemeral=True)

@bot.tree.command(name="resetbind", description="Reset HWID/UserId bind")
@app_commands.describe(key="Key")
async def reset_bind(interaction: discord.Interaction, key: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("No access.", ephemeral=True)
    key = re.sub(r"[^A-Z0-9\-]", "", key.strip().upper())
    db = load_db()
    if key not in db["keys"]:
        return await interaction.response.send_message("Key not found.", ephemeral=True)
    db["keys"][key]["hwid"] = None
    db["keys"][key]["userid"] = None
    db["keys"][key]["activated_at"] = None
    db["keys"][key]["ips"] = []
    save_db(db)
    await interaction.response.send_message(f"Bind reset for `{key}`", ephemeral=True)
    await send_log(f"[RESET BIND] `{key}` | {interaction.user.mention}")

@bot.tree.command(name="keyinfo", description="Key info + IPs")
@app_commands.describe(key="Key")
async def keyinfo(interaction: discord.Interaction, key: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("No access.", ephemeral=True)
    key = re.sub(r"[^A-Z0-9\-]", "", key.strip().upper())
    db = load_db()
    data = db["keys"].get(key)
    if not data:
        return await interaction.response.send_message("Not found.", ephemeral=True)
    ips = ", ".join(data.get("ips") or []) or "-"
    text = (
        f"**Key:** `{key}`\n"
        f"**Type/Role:** `{data.get('type')}` / `{data.get('role')}`\n"
        f"**UserId:** `{data.get('userid')}`\n"
        f"**HWID:** `{str(data.get('hwid') or '-')[:40]}`\n"
        f"**IPs:** `{ips}`\n"
        f"**Banned:** `{data.get('banned')}`\n"
        f"**Expires:** `{data.get('expires_at') or 'inf'}`\n"
        f"**Activated:** `{data.get('activated_at')}`\n"
        f"**Created by:** `{data.get('created_by')}`"
    )
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(name="logs", description="Last activation events")
@app_commands.describe(limit="How many (max 20)")
async def logs_cmd(interaction: discord.Interaction, limit: int = 10):
    if not is_dev(interaction):
        return await interaction.response.send_message("No access.", ephemeral=True)
    limit = max(1, min(20, limit))
    db = load_db()
    rows = (db.get("activations") or [])[-limit:]
    if not rows:
        return await interaction.response.send_message("No logs.", ephemeral=True)
    lines = []
    for r in reversed(rows):
        lines.append(
            f"`{r.get('ts','')[:19]}` **{r.get('event')}** "
            f"key=`{r.get('key','-')}` ip=`{r.get('ip','-')}` "
            f"uid=`{r.get('userid','-')}`"
        )
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

@bot.tree.command(name="addwl", description="Whitelist Roblox UserId")
@app_commands.describe(userid="Roblox UserId")
async def addwl(interaction: discord.Interaction, userid: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("No access.", ephemeral=True)
    try:
        uid = int(userid)
    except ValueError:
        return await interaction.response.send_message("UserId must be number.", ephemeral=True)
    db = load_db()
    if uid not in db["whitelist"]:
        db["whitelist"].append(uid)
        save_db(db)
    await interaction.response.send_message(f"WL + `{uid}`", ephemeral=True)
    await send_log(f"[WL+] `{uid}` | {interaction.user.mention}")

@bot.tree.command(name="removewl", description="Remove from whitelist")
@app_commands.describe(userid="Roblox UserId")
async def removewl(interaction: discord.Interaction, userid: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("No access.", ephemeral=True)
    try:
        uid = int(userid)
    except ValueError:
        return await interaction.response.send_message("UserId must be number.", ephemeral=True)
    db = load_db()
    if uid in db["whitelist"]:
        db["whitelist"].remove(uid)
        save_db(db)
    await interaction.response.send_message(f"WL - `{uid}`", ephemeral=True)

@bot.tree.command(name="listwl", description="Show whitelist")
async def listwl(interaction: discord.Interaction):
    if not is_dev(interaction):
        return await interaction.response.send_message("No access.", ephemeral=True)
    db = load_db()
    text = "\n".join(str(x) for x in db["whitelist"]) or "empty"
    await interaction.response.send_message(f"Whitelist:\n```{text}```", ephemeral=True)

# ================== START ==================
def run_bot():
    bot.run(TOKEN)

if __name__ == "__main__":
    Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"[CLL] Flask listening on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
