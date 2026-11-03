import discord
from discord.ext import commands
from discord import app_commands, ui
import os
import random
import string
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from threading import Thread
from pymongo import MongoClient

# ================== НАСТРОЙКИ ==================
TOKEN = os.environ["TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]  # Сюда вставишь ссылку из MongoDB

DEV_ROLE_ID = 1566118227489071344  # Твой ID роли
LOG_CHANNEL_ID = 1566120795934040314  # Твой ID канала логов
# ===============================================

# ---------- Подключение к MongoDB ----------
client = MongoClient(MONGO_URI)
db = client["auth_database"]
keys_col = db["keys"]
wl_col = db["whitelist"]

def get_key(key):
    return keys_col.find_one({"_id": key})

def save_key(key, data):
    keys_col.update_one({"_id": key}, {"$set": data}, upsert=True)

def get_whitelist():
    return [doc["_id"] for doc in wl_col.find()]

# -------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

app = Flask("")

def generate_key():
    parts = []
    for _ in range(3):
        parts.append("".join(random.choices(string.ascii_uppercase + string.digits, k=4)))
    return "-".join(parts)

def is_dev(interaction: discord.Interaction) -> bool:
    role = discord.utils.get(interaction.user.roles, id=DEV_ROLE_ID)
    return role is not None

async def send_log(text: str):
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        try:
            await channel.send(text)
        except Exception:
            pass

# ---------- Типы ключей ----------
KEY_TYPES = {
    "tester": {"role": "tester", "days": None, "label": "Tester — ∞"},
    "dev": {"role": "dev", "days": None, "label": "Dev — ∞"},
    "user_7d": {"role": "user", "days": 7, "label": "User — 7 дней"},
    "user_14d": {"role": "user", "days": 14, "label": "User — 14 дней"},
    "user_30d": {"role": "user", "days": 30, "label": "User — 30 дней"},
}

# ---------- View с кнопками ----------
class KeyTypeView(ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Это не твоя панель.", ephemeral=True)
            return False
        return True

    async def create_key(self, interaction: discord.Interaction, key_type: str):
        info = KEY_TYPES[key_type]
        key = generate_key()
        now = datetime.utcnow()

        expires_at = None
        if info["days"]:
            expires_at = (now + timedelta(days=info["days"])).isoformat()

        key_data = {
            "role": info["role"],
            "type": key_type,
            "created_at": now.isoformat(),
            "created_by": str(interaction.user),
            "expires_at": expires_at,
            "userid": None,
            "hwid": None,
            "banned": False,
            "activated_at": None
        }
        save_key(key, key_data)

        expires_text = "∞" if not expires_at else expires_at[:10]

        await interaction.response.edit_message(
            content=(
                f"**Ключ создан**\n"
                f"Тип: `{info['label']}`\n"
                f"Ключ: `{key}`\n"
                f"Действует до: `{expires_text}`"
            ),
            view=None
        )

        await send_log(
            f"[СОЗДАН] `{key}` | {info['label']} | создал: {interaction.user.mention}"
        )

    @ui.button(label="Tester — ∞", style=discord.ButtonStyle.primary)
    async def tester(self, interaction: discord.Interaction, button: ui.Button):
        await self.create_key(interaction, "tester")

    @ui.button(label="Dev — ∞", style=discord.ButtonStyle.primary)
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

# ---------- Flask API ----------
@app.route("/")
def home():
    return "CLL Auth Online"

@app.route("/check", methods=["POST"])
def check_key():
    data = request.json or {}
    key = str(data.get("key", "")).strip().upper()
    userid = data.get("userid")
    hwid = str(data.get("hwid", "")).strip()

    if not key or not userid or not hwid:
        return jsonify({"status": "invalid", "reason": "missing_data"})

    key_data = get_key(key)

    if not key_data:
        return jsonify({"status": "invalid", "reason": "not_found"})

    if key_data.get("banned"):
        return jsonify({"status": "banned"})

    # Проверка срока
    expires_at = key_data.get("expires_at")
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if datetime.utcnow() > exp:
                return jsonify({"status": "expired"})
        except Exception:
            pass

    # Первая активация
    if key_data.get("hwid") is None:
        key_data["hwid"] = hwid
        key_data["userid"] = int(userid)
        key_data["activated_at"] = datetime.utcnow().isoformat()
        save_key(key, key_data)

        print(f"[ACTIVATE] {key} -> user {userid} | hwid {hwid[:8]}...")

        return jsonify({
            "status": "valid",
            "role": key_data.get("role", "user"),
            "type": key_data.get("type"),
            "expires_at": key_data.get("expires_at"),
            "whitelist": get_whitelist() # Берем актуальный вайтлист из базы
        })

    # Уже активирован — проверяем HWID
    if key_data.get("hwid") != hwid:
        return jsonify({"status": "hwid_mismatch"})

    if key_data.get("userid") and int(key_data["userid"]) != int(userid):
        return jsonify({"status": "userid_mismatch"})

    return jsonify({
        "status": "valid",
        "role": key_data.get("role", "user"),
        "type": key_data.get("type"),
        "expires_at": key_data.get("expires_at"),
        "whitelist": get_whitelist() # Берем актуальный вайтлист из базы
    })

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_flask).start()

# ---------- Discord команды ----------
@bot.event
async def on_ready():
    print(f"Бот запущен: {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Команд синхронизировано: {len(synced)}")
    except Exception as e:
        print("Sync error:", e)

@bot.tree.command(name="genkey", description="Создать ключ (только dev)")
async def genkey(interaction: discord.Interaction):
    if not is_dev(interaction):
        return await interaction.response.send_message("Нет доступа. Нужна роль dev.", ephemeral=True)

    view = KeyTypeView(interaction.user.id)
    await interaction.response.send_message("Выберите тип ключа:", view=view, ephemeral=True)

@bot.tree.command(name="ban", description="Забанить ключ")
@app_commands.describe(key="Ключ")
async def ban_key(interaction: discord.Interaction, key: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("Нет доступа.", ephemeral=True)

    key = key.strip().upper()
    if not get_key(key):
        return await interaction.response.send_message("Ключ не найден.", ephemeral=True)

    save_key(key, {"banned": True})
    await interaction.response.send_message(f"Ключ `{key}` забанен.", ephemeral=True)
    await send_log(f"[БАН] `{key}` | {interaction.user.mention}")

@bot.tree.command(name="unban", description="Разбанить ключ")
@app_commands.describe(key="Ключ")
async def unban_key(interaction: discord.Interaction, key: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("Нет доступа.", ephemeral=True)

    key = key.strip().upper()
    if not get_key(key):
        return await interaction.response.send_message("Ключ не найден.", ephemeral=True)

    save_key(key, {"banned": False})
    await interaction.response.send_message(f"Ключ `{key}` разбанен.", ephemeral=True)
    await send_log(f"[РАЗБАН] `{key}` | {interaction.user.mention}")

@bot.tree.command(name="resethwid", description="Сбросить HWID у ключа")
@app_commands.describe(key="Ключ")
async def resethwid(interaction: discord.Interaction, key: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("Нет доступа.", ephemeral=True)

    key = key.strip().upper()
    if not get_key(key):
        return await interaction.response.send_message("Ключ не найден.", ephemeral=True)

    save_key(key, {"hwid": None, "userid": None, "activated_at": None})
    await interaction.response.send_message(f"HWID ключа `{key}` сброшен.", ephemeral=True)
    await send_log(f"[RESET HWID] `{key}` | {interaction.user.mention}")

@bot.tree.command(name="addwl", description="Добавить UserId в вайтлист")
@app_commands.describe(userid="Roblox UserId")
async def addwl(interaction: discord.Interaction, userid: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("Нет доступа.", ephemeral=True)

    try:
        uid = int(userid)
    except ValueError:
        return await interaction.response.send_message("UserId должен быть числом.", ephemeral=True)

    # В MongoDB используем update_one с upsert=True, чтобы не было дублей
    wl_col.update_one({"_id": uid}, {"$set": {"_id": uid}}, upsert=True)

    await interaction.response.send_message(f"Добавлен в вайтлист: `{uid}`", ephemeral=True)
    await send_log(f"[WL +] `{uid}` | {interaction.user.mention}")

@bot.tree.command(name="removewl", description="Убрать из вайтлиста")
@app_commands.describe(userid="Roblox UserId")
async def removewl(interaction: discord.Interaction, userid: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("Нет доступа.", ephemeral=True)

    try:
        uid = int(userid)
    except ValueError:
        return await interaction.response.send_message("UserId должен быть числом.", ephemeral=True)

    wl_col.delete_one({"_id": uid})
    await interaction.response.send_message(f"Убран из вайтлиста: `{uid}`", ephemeral=True)
    await send_log(f"[WL -] `{uid}` | {interaction.user.mention}")

@bot.tree.command(name="listwl", description="Показать вайтлист")
async def listwl(interaction: discord.Interaction):
    if not is_dev(interaction):
        return await interaction.response.send_message("Нет доступа.", ephemeral=True)

    wl = get_whitelist()
    text = "\n".join(str(x) for x in wl) or "пусто"
    await interaction.response.send_message(f"Вайтлист:\n```{text}```", ephemeral=True)

@bot.tree.command(name="keyinfo", description="Информация о ключе")
@app_commands.describe(key="Ключ")
async def keyinfo(interaction: discord.Interaction, key: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("Нет доступа.", ephemeral=True)

    key = key.strip().upper()
    data = get_key(key)
    if not data:
        return await interaction.response.send_message("Ключ не найден.", ephemeral=True)

    text = (
        f"**Ключ:** `{key}`\n"
        f"**Тип:** `{data.get('type')}`\n"
        f"**Роль:** `{data.get('role')}`\n"
        f"**UserId:** `{data.get('userid')}`\n"
        f"**HWID:** `{str(data.get('hwid'))[:16] if data.get('hwid') else None}`\n"
        f"**Banned:** `{data.get('banned')}`\n"
        f"**Expires:** `{data.get('expires_at') or '∞'}`\n"
        f"**Activated:** `{data.get('activated_at')}`"
    )
    await interaction.response.send_message(text, ephemeral=True)

bot.run(TOKEN)
