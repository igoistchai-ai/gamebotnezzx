import os, json, hmac, hashlib, secrets, asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN

from fastapi import FastAPI
from contextlib import asynccontextmanager
import uvicorn
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ============================================================
# NEZZX GAME — SINGLE FILE VERSION
# Virtual points only. No real-money deposits/withdrawals.
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
PORT = int(os.getenv("PORT", "10000"))
DEFAULT_BALANCE = int(os.getenv("DEFAULT_BALANCE", "10000"))
MIN_BET = int(os.getenv("MIN_BET", "10"))
MAX_BET = int(os.getenv("MAX_BET", "1000000"))
DAILY_BONUS = int(os.getenv("DAILY_BONUS", "1000"))

if OWNER_ID:
    ADMIN_IDS.add(OWNER_ID)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

# ---------------- In-memory storage ----------------
# For production on Render, attach PostgreSQL and replace these stores
# with persistent storage. This single-file edition is intentionally simple.

USERS = {}
ACTIVE_GAMES = {}
TRANSACTIONS = []
DAILY = {}
REFERRALS = {}
LOCK = asyncio.Lock()

# ---------------- Helpers ----------------

def now():
    return datetime.now(timezone.utc)

def fmt(n):
    return f"{int(n):,}".replace(",", " ")

def is_admin(uid):
    return uid in ADMIN_IDS

def user(uid, tg=None):
    if uid not in USERS:
        USERS[uid] = {
            "id": uid,
            "username": getattr(tg, "username", None),
            "first_name": getattr(tg, "first_name", ""),
            "balance": DEFAULT_BALANCE,
            "games": 0,
            "wins": 0,
            "losses": 0,
            "xp": 0,
            "level": 1,
            "streak": 0,
            "banned": False,
            "created": now(),
        }
    elif tg:
        USERS[uid]["username"] = tg.username
        USERS[uid]["first_name"] = tg.first_name
    return USERS[uid]

def tx(uid, amount, kind, description):
    u = USERS[uid]
    before = u["balance"]
    u["balance"] += int(amount)
    TRANSACTIONS.append({
        "time": now(),
        "user_id": uid,
        "type": kind,
        "amount": int(amount),
        "before": before,
        "after": u["balance"],
        "description": description,
    })
    return u["balance"]

def add_xp(u, amount):
    u["xp"] += amount
    while u["xp"] >= u["level"] * 100:
        u["xp"] -= u["level"] * 100
        u["level"] += 1

def hash_seed(seed):
    return hashlib.sha256(seed.encode()).hexdigest()

# ---------------- Mines ----------------

BOARD = 25
MINES_ALLOWED = range(1, 24)

def deterministic_positions(server_seed, client_seed, nonce, count):
    result = []
    counter = 0
    while len(result) < count:
        digest = hmac.new(
            server_seed.encode(),
            f"{client_seed}:{nonce}:{counter}".encode(),
            hashlib.sha256
        ).digest()
        for i in range(0, len(digest), 4):
            pos = int.from_bytes(digest[i:i+4], "big") % BOARD
            if pos not in result:
                result.append(pos)
            if len(result) == count:
                break
        counter += 1
    return sorted(result)

def mines_multiplier(mines, safe):
    if safe <= 0:
        return Decimal("1.00")
    result = Decimal("1")
    safe_total = BOARD - mines
    for k in range(safe):
        result *= Decimal(BOARD-k) / Decimal(safe_total-k)
        result *= Decimal("0.96")
    return result.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

# ---------------- Tower ----------------

# Tower is 10 floors. Every floor has 4 cells.
# User chooses 1-4 mines for the tower.
# A safe pick advances to the next floor.
TOWER_FLOORS = 10
TOWER_CELLS = 4
TOWER_MINES = range(1, 5)

def tower_multiplier(mines, floor):
    # Virtual-game multiplier. Higher mine count = higher risk.
    value = Decimal("1")
    safe = TOWER_CELLS - mines
    for level in range(floor):
        value *= Decimal(TOWER_CELLS) / Decimal(max(1, safe))
        value *= Decimal("0.88")
    return value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

def tower_layout(seed, game_id, floor, mines):
    digest = hmac.new(
        seed.encode(),
        f"{game_id}:{floor}".encode(),
        hashlib.sha256
    ).digest()
    cells = list(range(4))
    score = {i: digest[i] for i in cells}
    cells.sort(key=lambda x: score[x])
    return set(cells[:mines])

# ---------------- UI ----------------

def home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Играть", callback_data="menu:games")],
        [InlineKeyboardButton("Баланс", callback_data="menu:balance"),
         InlineKeyboardButton("Бонус", callback_data="menu:bonus")],
        [InlineKeyboardButton("Профиль", callback_data="menu:profile"),
         InlineKeyboardButton("Рейтинг", callback_data="menu:rating")],
        [InlineKeyboardButton("Рефералы", callback_data="menu:refs"),
         InlineKeyboardButton("Chat", callback_data="menu:chat")],
        [InlineKeyboardButton("Помощь", callback_data="menu:help")],
    ])

def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Назад", callback_data="home")]
    ])

def games_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Mines", callback_data="game:mines")],
        [InlineKeyboardButton("Tower", callback_data="game:tower")],
        [InlineKeyboardButton("Назад", callback_data="home")]
    ])

def mines_bet_keyboard():
    vals = [10, 100, 500, 1000, 5000, 10000]
    rows = []
    row = []
    for v in vals:
        row.append(InlineKeyboardButton(str(v), callback_data=f"mb:{v}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Назад", callback_data="menu:games")])
    return InlineKeyboardMarkup(rows)

def mines_count_keyboard(bet):
    rows = []
    row = []
    for n in range(1, 24):
        row.append(InlineKeyboardButton(str(n), callback_data=f"ms:{bet}:{n}"))
        if len(row) == 6:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Назад", callback_data="game:mines")])
    return InlineKeyboardMarkup(rows)

def tower_bet_keyboard():
    vals = [10, 100, 500, 1000, 5000, 10000]
    rows = []
    row = []
    for v in vals:
        row.append(InlineKeyboardButton(str(v), callback_data=f"tb:{v}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Назад", callback_data="menu:games")])
    return InlineKeyboardMarkup(rows)

def tower_mines_keyboard(bet):
    rows = [
        [InlineKeyboardButton("1 мина", callback_data=f"ts:{bet}:1"),
         InlineKeyboardButton("2 мины", callback_data=f"ts:{bet}:2")],
        [InlineKeyboardButton("3 мины", callback_data=f"ts:{bet}:3"),
         InlineKeyboardButton("4 мины", callback_data=f"ts:{bet}:4")],
        [InlineKeyboardButton("Назад", callback_data="game:tower")]
    ]
    return InlineKeyboardMarkup(rows)

def mines_board(game):
    opened = game["opened"]
    rows = []
    for r in range(5):
        row = []
        for c in range(5):
            i = r * 5 + c
            text = "■" if i not in opened else "·"
            row.append(InlineKeyboardButton(text, callback_data=f"mo:{game['id']}:{i}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("Забрать", callback_data=f"mc:{game['id']}")])
    rows.append([InlineKeyboardButton("Seed Hash", callback_data=f"mv:{game['id']}")])
    return InlineKeyboardMarkup(rows)

def tower_board(game):
    rows = []
    for i in range(4):
        rows.append([
            InlineKeyboardButton(
                "■" if i not in game["current_open"] else "·",
                callback_data=f"to:{game['id']}:{i}"
            )
        ])
    rows.append([InlineKeyboardButton("Забрать", callback_data=f"tc:{game['id']}")])
    rows.append([InlineKeyboardButton("Seed Hash", callback_data=f"tv:{game['id']}")])
    return InlineKeyboardMarkup(rows)

# ---------------- Commands ----------------

async def start(update, context):
    uid = update.effective_user.id
    u = user(uid, update.effective_user)

    if context.args and context.args[0].startswith("ref_"):
        try:
            ref = int(context.args[0][4:])
            if ref != uid and uid not in REFERRALS:
                REFERRALS[uid] = ref
        except ValueError:
            pass

    await update.message.reply_text(
        f"NEZZX GAME\n\n"
        f"Баланс: {fmt(u['balance'])}\n"
        f"Уровень: {u['level']}\n"
        f"XP: {u['xp']}",
        reply_markup=home_keyboard()
    )

async def admin(update, context):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    await update.message.reply_text(
        "ADMIN PANEL\n\n"
        "/addbalance USER_ID AMOUNT REASON\n"
        "/removebalance USER_ID AMOUNT REASON\n"
        "/ban USER_ID REASON\n"
        "/unban USER_ID\n"
        "/stats"
    )

async def addbalance(update, context):
    if not is_admin(update.effective_user.id) or len(context.args) < 2:
        return await update.message.reply_text("Формат: /addbalance ID СУММА ПРИЧИНА")
    try:
        target = int(context.args[0])
        amount = int(context.args[1])
        reason = " ".join(context.args[2:]) or "Admin adjustment"
        u = user(target)
        new = tx(target, amount, "admin_add", reason)
        await update.message.reply_text(
            f"Готово.\nID: {target}\nИзменение: {amount:+}\nБаланс: {fmt(new)}"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def removebalance(update, context):
    if not is_admin(update.effective_user.id) or len(context.args) < 2:
        return await update.message.reply_text("Формат: /removebalance ID СУММА ПРИЧИНА")
    try:
        target = int(context.args[0])
        amount = int(context.args[1])
        if amount < 0:
            amount = abs(amount)
        u = user(target)
        if u["balance"] < amount:
            return await update.message.reply_text("У пользователя недостаточно баллов.")
        new = tx(target, -amount, "admin_remove", " ".join(context.args[2:]) or "Admin removal")
        await update.message.reply_text(f"Баланс: {fmt(new)}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def ban(update, context):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    target = int(context.args[0])
    user(target)["banned"] = True
    await update.message.reply_text("Пользователь заблокирован.")

async def unban(update, context):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    target = int(context.args[0])
    user(target)["banned"] = False
    await update.message.reply_text("Пользователь разблокирован.")

async def stats(update, context):
    if not is_admin(update.effective_user.id):
        return
    total_games = sum(x["games"] for x in USERS.values())
    await update.message.reply_text(
        f"Статистика\n\n"
        f"Пользователей: {len(USERS)}\n"
        f"Игр: {total_games}\n"
        f"Транзакций: {len(TRANSACTIONS)}"
    )

# ---------------- Callback router ----------------

async def callback(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    u = user(uid, q.from_user)

    if u["banned"]:
        return await q.edit_message_text("Доступ ограничен.", reply_markup=back_keyboard())

    data = q.data

    if data == "home":
        return await q.edit_message_text(
            f"NEZZX GAME\n\nБаланс: {fmt(u['balance'])}\nУровень: {u['level']}",
            reply_markup=home_keyboard()
        )

    if data == "menu:games":
        return await q.edit_message_text("Игры\n\nВыберите режим:", reply_markup=games_keyboard())

    if data == "game:mines":
        return await q.edit_message_text(
            f"Mines\n\nБаланс: {fmt(u['balance'])}\n\nВыберите сумму:",
            reply_markup=mines_bet_keyboard()
        )

    if data == "game:tower":
        return await q.edit_message_text(
            f"Tower\n\nБаланс: {fmt(u['balance'])}\n\nВыберите сумму:",
            reply_markup=tower_bet_keyboard()
        )

    if data.startswith("mb:"):
        bet = int(data.split(":")[1])
        if not MIN_BET <= bet <= MAX_BET or u["balance"] < bet:
            return await q.edit_message_text("Недопустимая ставка.", reply_markup=back_keyboard())
        return await q.edit_message_text(
            f"Mines\n\nСтавка: {fmt(bet)}\nВыберите количество мин: 1–23",
            reply_markup=mines_count_keyboard(bet)
        )

    if data.startswith("ms:"):
        _, bet, mine_count = data.split(":")
        bet, mine_count = int(bet), int(mine_count)
        if mine_count < 1 or mine_count > 23 or u["balance"] < bet:
            return await q.edit_message_text("Ошибка ставки.", reply_markup=back_keyboard())
        if uid in ACTIVE_GAMES:
            return await q.edit_message_text("У вас уже есть активная игра.", reply_markup=back_keyboard())

        seed = secrets.token_hex(32)
        game = {
            "id": secrets.token_hex(4),
            "type": "mines",
            "user_id": uid,
            "bet": bet,
            "mines": mine_count,
            "seed": seed,
            "seed_hash": hash_seed(seed),
            "client_seed": secrets.token_hex(8),
            "nonce": 0,
            "mine_positions": deterministic_positions(seed, secrets.token_hex(8), 0, mine_count),
            "opened": set(),
            "multiplier": Decimal("1.00"),
            "created": now(),
        }
        # Recompute using the actual client seed.
        game["mine_positions"] = deterministic_positions(
            seed, game["client_seed"], 0, mine_count
        )
        ACTIVE_GAMES[uid] = game
        tx(uid, -bet, "mines_bet", "Mines ставка")
        u["games"] += 1

        return await q.edit_message_text(
            f"Mines\n\n"
            f"Ставка: {fmt(bet)}\n"
            f"Мин: {mine_count}\n"
            f"Множитель: x1.00\n\n"
            f"Открывайте клетки.",
            reply_markup=mines_board(game)
        )

    if data.startswith("mo:"):
        _, gid, cell = data.split(":")
        game = ACTIVE_GAMES.get(uid)
        if not game or game["id"] != gid:
            return await q.edit_message_text("Игра не найдена.", reply_markup=back_keyboard())

        cell = int(cell)
        if cell in game["opened"]:
            return await q.answer("Уже открыто.")
        if cell in game["mine_positions"]:
            game["opened"].add(cell)
            u["losses"] += 1
            del ACTIVE_GAMES[uid]
            return await q.edit_message_text(
                f"МИНА\n\nСтавка: {fmt(game['bet'])}\nПотеряно: {fmt(game['bet'])}",
                reply_markup=back_keyboard()
            )

        game["opened"].add(cell)
        game["multiplier"] = mines_multiplier(game["mines"], len(game["opened"]))
        add_xp(u, 5)

        safe_total = BOARD - game["mines"]
        if len(game["opened"]) >= safe_total:
            reward = int(Decimal(game["bet"]) * game["multiplier"])
            tx(uid, reward, "mines_win", "Mines complete")
            u["wins"] += 1
            del ACTIVE_GAMES[uid]
            return await q.edit_message_text(
                f"Победа.\n\nПолучено: {fmt(reward)}\nМножитель: x{game['multiplier']}",
                reply_markup=back_keyboard()
            )

        current = int(Decimal(game["bet"]) * game["multiplier"])
        return await q.edit_message_text(
            f"Mines\n\n"
            f"Ставка: {fmt(game['bet'])}\n"
            f"Мин: {game['mines']}\n"
            f"Открыто: {len(game['opened'])}\n"
            f"Множитель: x{game['multiplier']}\n"
            f"Забрать: {fmt(current)}",
            reply_markup=mines_board(game)
        )

    if data.startswith("mc:"):
        _, gid = data.split(":")
        game = ACTIVE_GAMES.get(uid)
        if not game or game["id"] != gid:
            return await q.edit_message_text("Игра не найдена.", reply_markup=back_keyboard())
        if not game["opened"]:
            return await q.answer("Сначала откройте клетку.", show_alert=True)

        reward = int(Decimal(game["bet"]) * game["multiplier"])
        tx(uid, reward, "mines_cashout", "Mines cashout")
        u["wins"] += 1
        del ACTIVE_GAMES[uid]
        return await q.edit_message_text(
            f"Вы забрали выигрыш.\n\nПолучено: {fmt(reward)}\nМножитель: x{game['multiplier']}",
            reply_markup=back_keyboard()
        )

    if data.startswith("mv:"):
        _, gid = data.split(":")
        game = ACTIVE_GAMES.get(uid)
        if not game or game["id"] != gid:
            return await q.edit_message_text("Игра не найдена.", reply_markup=back_keyboard())
        return await q.edit_message_text(
            f"Provably Fair\n\nServer Seed Hash:\n{game['seed_hash']}\n\n"
            f"Server Seed раскроется после завершения игры.",
            reply_markup=back_keyboard()
        )

    if data.startswith("tb:"):
        bet = int(data.split(":")[1])
        if not MIN_BET <= bet <= MAX_BET or u["balance"] < bet:
            return await q.edit_message_text("Недопустимая ставка.", reply_markup=back_keyboard())
        return await q.edit_message_text(
            f"Tower\n\nСтавка: {fmt(bet)}\nВыберите мин: 1–4",
            reply_markup=tower_mines_keyboard(bet)
        )

    if data.startswith("ts:"):
        _, bet, mines = data.split(":")
        bet, mines = int(bet), int(mines)
        if not 1 <= mines <= 4 or u["balance"] < bet:
            return await q.edit_message_text("Ошибка ставки.", reply_markup=back_keyboard())
        if uid in ACTIVE_GAMES:
            return await q.edit_message_text("У вас уже есть активная игра.", reply_markup=back_keyboard())

        seed = secrets.token_hex(32)
        game = {
            "id": secrets.token_hex(4),
            "type": "tower",
            "user_id": uid,
            "bet": bet,
            "mines": mines,
            "seed": seed,
            "seed_hash": hash_seed(seed),
            "floor": 1,
            "current_open": set(),
            "created": now(),
        }
        game["layout"] = tower_layout(seed, game["id"], 1, mines)
        ACTIVE_GAMES[uid] = game
        tx(uid, -bet, "tower_bet", "Tower ставка")
        u["games"] += 1

        return await q.edit_message_text(
            f"Tower\n\n"
            f"Ставка: {fmt(bet)}\n"
            f"Мин: {mines}\n"
            f"Этаж: 1/{TOWER_FLOORS}\n"
            f"Множитель: x{tower_multiplier(mines, 1)}\n\n"
            f"Выберите клетку.",
            reply_markup=tower_board(game)
        )

    if data.startswith("to:"):
        _, gid, cell = data.split(":")
        game = ACTIVE_GAMES.get(uid)
        if not game or game["id"] != gid or game["type"] != "tower":
            return await q.edit_message_text("Игра не найдена.", reply_markup=back_keyboard())

        cell = int(cell)
        if cell in game["layout"]:
            u["losses"] += 1
            del ACTIVE_GAMES[uid]
            return await q.edit_message_text(
                f"Мина на этаже {game['floor']}.\n\nПотеряно: {fmt(game['bet'])}",
                reply_markup=back_keyboard()
            )

        game["current_open"].add(cell)
        floor = game["floor"]
        mult = tower_multiplier(game["mines"], floor)
        add_xp(u, 7)

        if floor >= TOWER_FLOORS:
            reward = int(Decimal(game["bet"]) * mult)
            tx(uid, reward, "tower_win", "Tower complete")
            u["wins"] += 1
            del ACTIVE_GAMES[uid]
            return await q.edit_message_text(
                f"Башня пройдена.\n\nПолучено: {fmt(reward)}\nМножитель: x{mult}",
                reply_markup=back_keyboard()
            )

        game["floor"] += 1
        game["current_open"] = set()
        game["layout"] = tower_layout(game["seed"], game["id"], game["floor"], game["mines"])

        return await q.edit_message_text(
            f"Tower\n\n"
            f"Ставка: {fmt(game['bet'])}\n"
            f"Мин: {game['mines']}\n"
            f"Этаж: {game['floor']}/{TOWER_FLOORS}\n"
            f"Множитель: x{tower_multiplier(game['mines'], game['floor'])}\n"
            f"Забрать: {fmt(int(Decimal(game['bet']) * tower_multiplier(game['mines'], game['floor'])))}",
            reply_markup=tower_board(game)
        )

    if data.startswith("tc:"):
        _, gid = data.split(":")
        game = ACTIVE_GAMES.get(uid)
        if not game or game["id"] != gid or game["type"] != "tower":
            return await q.edit_message_text("Игра не найдена.", reply_markup=back_keyboard())
        floor = max(1, game["floor"] - 1)
        mult = tower_multiplier(game["mines"], floor)
        reward = int(Decimal(game["bet"]) * mult)
        tx(uid, reward, "tower_cashout", "Tower cashout")
        u["wins"] += 1
        del ACTIVE_GAMES[uid]
        return await q.edit_message_text(
            f"Вы забрали награду.\n\nПолучено: {fmt(reward)}\nМножитель: x{mult}",
            reply_markup=back_keyboard()
        )

    if data.startswith("tv:"):
        _, gid = data.split(":")
        game = ACTIVE_GAMES.get(uid)
        if not game:
            return await q.edit_message_text("Игра не найдена.", reply_markup=back_keyboard())
        return await q.edit_message_text(
            f"Provably Fair\n\nServer Seed Hash:\n{game['seed_hash']}",
            reply_markup=back_keyboard()
        )

    if data == "menu:balance":
        return await q.edit_message_text(
            f"Баланс\n\n{fmt(u['balance'])} баллов",
            reply_markup=back_keyboard()
        )

    if data == "menu:bonus":
        last = DAILY.get(uid)
        if last and now() - last < timedelta(hours=24):
            return await q.edit_message_text("Ежедневный бонус уже получен.", reply_markup=back_keyboard())
        DAILY[uid] = now()
        reward = DAILY_BONUS
        tx(uid, reward, "daily_bonus", "Daily bonus")
        u["streak"] += 1
        add_xp(u, 20)
        return await q.edit_message_text(
            f"Бонус получен.\n\n+{fmt(reward)} баллов\nStreak: {u['streak']}",
            reply_markup=back_keyboard()
        )

    if data == "menu:profile":
        wr = (u["wins"] / u["games"] * 100) if u["games"] else 0
        return await q.edit_message_text(
            f"Профиль\n\n"
            f"ID: {uid}\n"
            f"Баланс: {fmt(u['balance'])}\n"
            f"Уровень: {u['level']}\n"
            f"XP: {u['xp']}\n"
            f"Игр: {u['games']}\n"
            f"Побед: {u['wins']}\n"
            f"Поражений: {u['losses']}\n"
            f"Winrate: {wr:.1f}%",
            reply_markup=back_keyboard()
        )

    if data == "menu:refs":
        bot = await context.bot.get_me()
        link = f"https://t.me/{bot.username}?start=ref_{uid}"
        count = sum(1 for x in REFERRALS.values() if x == uid)
        return await q.edit_message_text(
            f"Рефералы\n\n"
            f"Приглашено: {count}\n\n"
            f"{link}",
            reply_markup=back_keyboard()
        )

    if data == "menu:rating":
        top = sorted(USERS.values(), key=lambda x: x["balance"], reverse=True)[:10]
        text = "Рейтинг\n\n"
        for i, x in enumerate(top, 1):
            text += f"{i}. {x['first_name'] or x['id']} — {fmt(x['balance'])}\n"
        return await q.edit_message_text(text, reply_markup=back_keyboard())

    if data == "menu:chat":
        context.user_data["chat"] = True
        return await q.edit_message_text(
            "Chat включён.\n\nНапишите сообщение следующим сообщением.",
            reply_markup=back_keyboard()
        )

    if data == "menu:help":
        return await q.edit_message_text(
            "Помощь\n\n"
            "Mines: количество мин 1–23.\n"
            "Tower: количество мин 1–4.\n"
            "Вы можете забрать виртуальный выигрыш до проигрыша.\n\n"
            "Используются только виртуальные баллы.",
            reply_markup=back_keyboard()
        )

async def text_handler(update, context):
    if not context.user_data.get("chat"):
        return
    context.user_data["chat"] = False
    await update.message.reply_text(
        "Chat пока работает в базовом режиме. Для полноценной AI-модели "
        "добавьте совместимый API в следующей версии."
    )

# ---------------- FastAPI / Render ----------------

telegram_app = None

@asynccontextmanager
async def lifespan(app):
    global telegram_app
    telegram_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("admin", admin))
    telegram_app.add_handler(CommandHandler("addbalance", addbalance))
    telegram_app.add_handler(CommandHandler("removebalance", removebalance))
    telegram_app.add_handler(CommandHandler("ban", ban))
    telegram_app.add_handler(CommandHandler("unban", unban))
    telegram_app.add_handler(CommandHandler("stats", stats))
    telegram_app.add_handler(CallbackQueryHandler(callback))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    await telegram_app.initialize()
    await telegram_app.start()

    public_url = os.getenv("WEBHOOK_URL", "").rstrip("/")
    secret = os.getenv("WEBHOOK_SECRET", "nezzx-webhook-secret")

    if public_url:
        await telegram_app.bot.set_webhook(
            url=f"{public_url}/telegram/webhook",
            secret_token=secret
        )
    else:
        await telegram_app.updater.start_polling()

    yield

    if public_url:
        await telegram_app.bot.delete_webhook()
    else:
        await telegram_app.updater.stop()

    await telegram_app.stop()
    await telegram_app.shutdown()

api = FastAPI(title="NEZZX GAME", lifespan=lifespan)

@api.get("/")
async def root():
    return {"name": "NEZZX GAME", "status": "online"}

@api.get("/health")
async def health():
    return {"status": "ok"}

@api.post("/telegram/webhook")
async def webhook(update: dict):
    await telegram_app.update_queue.put(
        Update.de_json(update, telegram_app.bot)
    )
    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run("main:api", host="0.0.0.0", port=PORT)
