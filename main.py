import os
import hmac
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
import uvicorn
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==========================================================
# NEZZX GAME BOT — ONE FILE
# Telegram UI styled after the supplied dark game-bot examples.
# Virtual points only. No real-money deposits/withdrawals.
# ==========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
if OWNER_ID:
    ADMIN_IDS.add(OWNER_ID)

PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")

DEFAULT_BALANCE = int(os.getenv("DEFAULT_BALANCE", "10000"))
MIN_BET = int(os.getenv("MIN_BET", "10"))
MAX_BET = int(os.getenv("MAX_BET", "1000000"))
DAILY_BONUS = int(os.getenv("DAILY_BONUS", "1000"))
GAME_TIMEOUT_MINUTES = int(os.getenv("GAME_TIMEOUT_MINUTES", "30"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

# ----------------------------------------------------------
# Simple runtime storage
# ----------------------------------------------------------
# This version keeps game state in memory to keep the entire
# project inside main.py. A later PostgreSQL version can use
# the same handlers/game functions without changing the UI.

USERS: dict[int, dict] = {}
ACTIVE_GAMES: dict[int, dict] = {}
TRANSACTIONS: list[dict] = []
DAILY_BONUS_STATE: dict[int, datetime] = {}
REFERRALS: dict[int, int] = {}
LOCK = None

BOARD_SIZE = 25
TOWER_FLOORS = 10


# ----------------------------------------------------------
# Generic helpers
# ----------------------------------------------------------

def now() -> datetime:
    return datetime.now(timezone.utc)


def fmt(value: int | Decimal) -> str:
    return f"{int(value):,}".replace(",", " ")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_user(user_id: int, tg_user=None) -> dict:
    if user_id not in USERS:
        USERS[user_id] = {
            "id": user_id,
            "username": getattr(tg_user, "username", None),
            "first_name": getattr(tg_user, "first_name", "") or "",
            "balance": DEFAULT_BALANCE,
            "games": 0,
            "wins": 0,
            "losses": 0,
            "xp": 0,
            "level": 1,
            "streak": 0,
            "banned": False,
            "created": now(),
            "last_activity": now(),
        }
        TRANSACTIONS.append(
            {
                "time": now(),
                "user_id": user_id,
                "type": "start_balance",
                "amount": DEFAULT_BALANCE,
                "before": 0,
                "after": DEFAULT_BALANCE,
                "description": "Стартовый баланс",
            }
        )
    elif tg_user is not None:
        USERS[user_id]["username"] = tg_user.username
        USERS[user_id]["first_name"] = tg_user.first_name or ""
        USERS[user_id]["last_activity"] = now()
    return USERS[user_id]


def change_balance(
    user_id: int,
    amount: int,
    tx_type: str,
    description: str,
    admin_id: int | None = None,
) -> int:
    u = get_user(user_id)
    amount = int(amount)
    if amount < 0 and u["balance"] + amount < 0:
        raise ValueError("Недостаточно баллов.")
    before = u["balance"]
    u["balance"] += amount
    TRANSACTIONS.append(
        {
            "time": now(),
            "user_id": user_id,
            "type": tx_type,
            "amount": amount,
            "before": before,
            "after": u["balance"],
            "description": description,
            "admin_id": admin_id,
        }
    )
    return u["balance"]


def add_xp(u: dict, amount: int) -> None:
    u["xp"] += max(0, int(amount))
    while u["xp"] >= u["level"] * 100:
        u["xp"] -= u["level"] * 100
        u["level"] += 1


def hash_seed(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def separator() -> str:
    return "• • • • • • • • • • • • • • • • • • •"


def safe_edit(q, text: str, keyboard=None):
    return q.edit_message_text(text, reply_markup=keyboard)


# ----------------------------------------------------------
# Mines
# ----------------------------------------------------------

def deterministic_positions(server_seed: str, client_seed: str, nonce: int, count: int):
    positions = []
    counter = 0

    while len(positions) < count:
        digest = hmac.new(
            server_seed.encode(),
            f"{client_seed}:{nonce}:{counter}".encode(),
            hashlib.sha256,
        ).digest()

        for i in range(0, len(digest), 4):
            pos = int.from_bytes(digest[i:i + 4], "big") % BOARD_SIZE
            if pos not in positions:
                positions.append(pos)
            if len(positions) >= count:
                break

        counter += 1

    return sorted(positions[:count])


def mines_multiplier(mine_count: int, safe_opened: int) -> Decimal:
    if safe_opened <= 0:
        return Decimal("1.00")

    safe_total = BOARD_SIZE - mine_count
    result = Decimal("1")

    for k in range(safe_opened):
        result *= Decimal(BOARD_SIZE - k) / Decimal(safe_total - k)
        result *= Decimal("0.96")

    return result.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def new_mines_game(user_id: int, bet: int, mine_count: int) -> dict:
    seed = secrets.token_hex(32)
    client_seed = secrets.token_hex(16)

    game = {
        "id": secrets.token_hex(5),
        "type": "mines",
        "user_id": user_id,
        "bet": bet,
        "mines": mine_count,
        "server_seed": seed,
        "server_seed_hash": hash_seed(seed),
        "client_seed": client_seed,
        "nonce": 0,
        "mine_positions": deterministic_positions(
            seed, client_seed, 0, mine_count
        ),
        "opened": set(),
        "multiplier": Decimal("1.00"),
        "created": now(),
    }
    return game


def mines_keyboard(game: dict) -> InlineKeyboardMarkup:
    rows = []
    opened = game["opened"]

    for r in range(5):
        row = []
        for c in range(5):
            cell = r * 5 + c
            symbol = "✅" if cell in opened else "❓"
            row.append(
                InlineKeyboardButton(
                    symbol,
                    callback_data=f"mine_open:{game['id']}:{cell}",
                )
            )
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                "💰 Забрать",
                callback_data=f"mine_cash:{game['id']}",
            ),
            InlineKeyboardButton(
                "🔑 Честность",
                callback_data=f"mine_verify:{game['id']}",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "❌ Отменить",
                callback_data=f"mine_cancel:{game['id']}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


# ----------------------------------------------------------
# Tower
# ----------------------------------------------------------

def tower_multiplier(mine_count: int, floor: int) -> Decimal:
    # Virtual-game multiplier curve.
    # More mines => higher risk and higher multiplier.
    safe = max(1, 4 - mine_count)
    result = Decimal("1")

    for _ in range(max(0, floor)):
        result *= Decimal("4") / Decimal(safe)
        result *= Decimal("0.88")

    return result.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def tower_layout(server_seed: str, game_id: str, floor: int, mine_count: int):
    digest = hmac.new(
        server_seed.encode(),
        f"{game_id}:{floor}".encode(),
        hashlib.sha256,
    ).digest()

    cells = list(range(4))
    cells.sort(key=lambda x: digest[x])
    return set(cells[:mine_count])


def new_tower_game(user_id: int, bet: int, mine_count: int) -> dict:
    seed = secrets.token_hex(32)
    game_id = secrets.token_hex(5)

    game = {
        "id": game_id,
        "type": "tower",
        "user_id": user_id,
        "bet": bet,
        "mines": mine_count,
        "server_seed": seed,
        "server_seed_hash": hash_seed(seed),
        "floor": 1,
        "current_open": set(),
        "layout": tower_layout(seed, game_id, 1, mine_count),
        "created": now(),
    }
    return game


def tower_keyboard(game: dict) -> InlineKeyboardMarkup:
    row = []
    for cell in range(4):
        symbol = "✅" if cell in game["current_open"] else "❓"
        row.append(
            InlineKeyboardButton(
                symbol,
                callback_data=f"tower_open:{game['id']}:{cell}",
            )
        )

    return InlineKeyboardMarkup(
        [
            row,
            [
                InlineKeyboardButton(
                    "💰 Забрать",
                    callback_data=f"tower_cash:{game['id']}",
                ),
                InlineKeyboardButton(
                    "🔑 Честность",
                    callback_data=f"tower_verify:{game['id']}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Отменить",
                    callback_data=f"tower_cancel:{game['id']}",
                )
            ],
        ]
    )


# ----------------------------------------------------------
# Keyboards
# ----------------------------------------------------------

def back_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ Назад", callback_data="home")]]
    )


def home_keyboard(uid: int):
    rows = [
        [InlineKeyboardButton("🎮 Играть", callback_data="games")],
        [
            InlineKeyboardButton("💰 Баланс", callback_data="balance"),
            InlineKeyboardButton("🎁 Бонус", callback_data="bonus"),
        ],
        [
            InlineKeyboardButton("👤 Профиль", callback_data="profile"),
            InlineKeyboardButton("🏆 Рейтинг", callback_data="rating"),
        ],
        [
            InlineKeyboardButton("🎟 Рефералы", callback_data="refs"),
            InlineKeyboardButton("💬 Chat", callback_data="chat"),
        ],
        [InlineKeyboardButton("📖 Помощь", callback_data="help")],
    ]

    if is_admin(uid):
        rows.insert(0, [InlineKeyboardButton("🟢 АДМИН", callback_data="admin")])

    return InlineKeyboardMarkup(rows)


def catalog_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💣 Мины", callback_data="game:mines"),
                InlineKeyboardButton("🏰 Башня", callback_data="game:tower"),
            ],
            [
                InlineKeyboardButton("🎯 Дартс", callback_data="soon:darts"),
                InlineKeyboardButton("⚽ Футбол", callback_data="soon:football"),
            ],
            [
                InlineKeyboardButton("🎳 Боулинг", callback_data="soon:bowling"),
                InlineKeyboardButton("🎲 Кубик", callback_data="soon:dice"),
            ],
            [
                InlineKeyboardButton("🎰 Слоты", callback_data="soon:slots"),
                InlineKeyboardButton("🌐 WEB", callback_data="soon:web"),
            ],
            [
                InlineKeyboardButton("▶️ Играть", callback_data="games")
            ],
            [
                InlineKeyboardButton("◀️ Назад", callback_data="home")
            ],
        ]
    )


def amount_keyboard(prefix: str, include_all: bool = False):
    values = [10, 100, 500, 1000, 5000, 10000]
    rows = []
    current = []

    for value in values:
        current.append(
            InlineKeyboardButton(
                f"💵 {value}",
                callback_data=f"{prefix}:{value}",
            )
        )
        if len(current) == 3:
            rows.append(current)
            current = []

    if current:
        rows.append(current)

    if include_all:
        rows.append(
            [InlineKeyboardButton("💎 ВСЕ", callback_data=f"{prefix}:all")]
        )

    rows.append(
        [InlineKeyboardButton("✍️ Своя сумма", callback_data="soon:custom")]
    )
    rows.append(
        [InlineKeyboardButton("◀️ Назад", callback_data="games")]
    )

    return InlineKeyboardMarkup(rows)


def mines_count_keyboard(bet: int):
    rows = []
    current = []

    for count in range(1, 24):
        current.append(
            InlineKeyboardButton(
                f"💣 {count}",
                callback_data=f"mines_start:{bet}:{count}",
            )
        )
        if len(current) == 6:
            rows.append(current)
            current = []

    if current:
        rows.append(current)

    rows.append(
        [InlineKeyboardButton("◀️ Назад", callback_data="game:mines")]
    )
    return InlineKeyboardMarkup(rows)


def tower_mines_keyboard(bet: int):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💣 1", callback_data=f"tower_start:{bet}:1"),
                InlineKeyboardButton("💣 2", callback_data=f"tower_start:{bet}:2"),
            ],
            [
                InlineKeyboardButton("💣 3", callback_data=f"tower_start:{bet}:3"),
                InlineKeyboardButton("💣 4", callback_data=f"tower_start:{bet}:4"),
            ],
            [InlineKeyboardButton("◀️ Назад", callback_data="game:tower")],
        ]
    )


def admin_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👤 Пользователи", callback_data="adm_users"),
                InlineKeyboardButton("💰 Баланс", callback_data="adm_balance"),
            ],
            [
                InlineKeyboardButton("📊 Статистика", callback_data="adm_stats"),
                InlineKeyboardButton("📋 Логи", callback_data="adm_logs"),
            ],
            [InlineKeyboardButton("◀️ Назад", callback_data="home")],
        ]
    )


# ----------------------------------------------------------
# User commands
# ----------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid, update.effective_user)

    if context.args and context.args[0].startswith("ref_"):
        try:
            ref_id = int(context.args[0][4:])
            if ref_id != uid and uid not in REFERRALS:
                REFERRALS[uid] = ref_id
        except ValueError:
            pass

    await update.message.reply_text(
        f"💣 Мины Бот\n"
        f"неzкс фамилионо...\n\n"
        f"🎮 NEZZX GAME\n"
        f"{separator()}\n"
        f"💰 Баланс: {fmt(u['balance'])} mCoin\n"
        f"🏆 Уровень: {u['level']}\n"
        f"⭐ XP: {u['xp']}\n\n"
        f"🎯 Добро пожаловать в игровое меню.",
        reply_markup=home_keyboard(uid),
    )


# ----------------------------------------------------------
# Admin commands
# ----------------------------------------------------------

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    await update.message.reply_text(
        f"🟢 АДМИН\n"
        f"{separator()}\n\n"
        f"👤 Пользователь — посмотреть игрока\n"
        f"💰 Баланс @username — посмотреть баланс\n"
        f"➕ Выдать @username 1000 — выдать mCoin\n"
        f"➖ Снять @username 1000 — снять mCoin\n"
        f"🚫 Бан @username — заблокировать\n"
        f"✅ Разбан @username — разблокировать\n"
        f"📊 Статистика — статистика",
        reply_markup=admin_keyboard(),
    )


def find_user_by_ref(ref: str):
    ref = ref.strip().lstrip("@").lower()
    if ref.isdigit():
        uid = int(ref)
        return USERS.get(uid)
    for user in USERS.values():
        if str(user.get("username") or "").lower() == ref:
            return user
    return None


def user_label(user: dict) -> str:
    username = user.get("username")
    return f"@{username}" if username else str(user["id"])

async def addbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or len(context.args) < 2:
        return await update.message.reply_text(
            "Формат:\n/addbalance ID СУММА ПРИЧИНА"
        )

    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
        reason = " ".join(context.args[2:]) or "Admin adjustment"

        get_user(target_id)
        old_balance = USERS[target_id]["balance"]
        new_balance = change_balance(
            target_id,
            amount,
            "admin_add",
            reason,
            admin_id=uid,
        )

        await update.message.reply_text(
            f"✅ Баланс изменён\n\n"
            f"👤 ID: {target_id}\n"
            f"💰 Было: {fmt(old_balance)}\n"
            f"➕ Изменение: {fmt(amount)}\n"
            f"💰 Стало: {fmt(new_balance)}\n"
            f"📝 Причина: {reason}"
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Ошибка: {exc}")


async def removebalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or len(context.args) < 2:
        return await update.message.reply_text(
            "Формат:\n/removebalance ID СУММА ПРИЧИНА"
        )

    try:
        target_id = int(context.args[0])
        amount = abs(int(context.args[1]))
        reason = " ".join(context.args[2:]) or "Admin removal"

        get_user(target_id)
        old_balance = USERS[target_id]["balance"]
        new_balance = change_balance(
            target_id,
            -amount,
            "admin_remove",
            reason,
            admin_id=uid,
        )

        await update.message.reply_text(
            f"✅ Баланс изменён\n\n"
            f"👤 ID: {target_id}\n"
            f"💰 Было: {fmt(old_balance)}\n"
            f"➖ Изменение: {fmt(amount)}\n"
            f"💰 Стало: {fmt(new_balance)}\n"
            f"📝 Причина: {reason}"
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Ошибка: {exc}")


async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or not context.args:
        return

    target_id = int(context.args[0])
    reason = " ".join(context.args[1:]) or "Административная блокировка"
    get_user(target_id)["banned"] = True

    await update.message.reply_text(
        f"🚫 Пользователь {target_id} заблокирован.\n"
        f"Причина: {reason}"
    )


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or not context.args:
        return

    target_id = int(context.args[0])
    get_user(target_id)["banned"] = False

    await update.message.reply_text(
        f"✅ Пользователь {target_id} разблокирован."
    )


async def user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or not context.args:
        return

    target_id = int(context.args[0])
    u = get_user(target_id)

    await update.message.reply_text(
        f"👤 ПОЛЬЗОВАТЕЛЬ\n"
        f"{separator()}\n\n"
        f"🆔 ID: {target_id}\n"
        f"📛 Username: @{u['username'] or 'нет'}\n"
        f"💰 Баланс: {fmt(u['balance'])} mCoin\n"
        f"🏆 Уровень: {u['level']}\n"
        f"⭐ XP: {u['xp']}\n"
        f"🎮 Игр: {u['games']}\n"
        f"✅ Побед: {u['wins']}\n"
        f"💥 Поражений: {u['losses']}\n"
        f"🚫 Бан: {'да' if u['banned'] else 'нет'}"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    total_games = sum(x["games"] for x in USERS.values())
    total_balance = sum(x["balance"] for x in USERS.values())

    await update.message.reply_text(
        f"📊 СТАТИСТИКА\n"
        f"{separator()}\n\n"
        f"👥 Пользователей: {len(USERS)}\n"
        f"🎮 Игр: {total_games}\n"
        f"💰 Баланс всех пользователей: {fmt(total_balance)} mCoin\n"
        f"📋 Транзакций: {len(TRANSACTIONS)}"
    )



# ----------------------------------------------------------
# Text command info
# ----------------------------------------------------------

def info_text(user_id: int) -> str:
    base = (
        "📖 ИНФО\n"
        f"{separator()}\n\n"
        "🎮 ИГРЫ\n"
        "• мины — открыть Mines\n"
        "• башня — открыть Tower\n"
        "• игры — каталог игр\n\n"
        "💰 АККАУНТ\n"
        "• баланс — показать баланс\n"
        "• профиль — открыть профиль\n"
        "• бонус — получить ежедневный бонус\n"
        "• рейтинг — рейтинг игроков\n"
        "• рефералы — реферальная ссылка\n\n"
        "📚 ПРОЧЕЕ\n"
        "• инфо — показать этот список\n"
        "• помощь — правила и помощь\n"
    )

    if is_admin(user_id):
        base += (
            "\n🟢 АДМИН\n"
            "• выдать @username сумма — выдать mCoin\n"
            "• снять @username сумма — снять mCoin\n"
            "• инфо @username — данные игрока\n"
            "• бан @username — заблокировать игрока\n"
            "• разбан @username — снять блокировку\n"
            "• статистика — статистика бота\n"
        )

    return base


def normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().split())


async def handle_text_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Natural-language commands without slash prefixes."""
    uid = update.effective_user.id
    u = get_user(uid, update.effective_user)

    if u["banned"]:
        await update.message.reply_text("🚫 Доступ ограничен администрацией.")
        return

    raw = update.message.text or ""
    text = normalize_text(raw)

    # Info/help
    if text in {"инфо", "команды", "команды бота", "список команд"}:
        await update.message.reply_text(
            info_text(uid),
            reply_markup=home_keyboard(uid),
        )
        return

    if text in {"помощь", "help"}:
        await update.message.reply_text(
            "📖 ПОМОЩЬ\n"
            f"{separator()}\n\n"
            "💣 Мины — поле 5×5, мин: 1–23.\n"
            "🏰 Башня — 10 этажей, мин: 1–4.\n"
            "💰 Cashout забирает текущую виртуальную награду.\n"
            "🔑 Честность показывает hash игрового seed.\n\n"
            "Все mCoin в боте являются виртуальными баллами.",
            reply_markup=home_keyboard(uid),
        )
        return

    # User actions
    if text in {"баланс", "б"}:
        await update.message.reply_text(
            f"💰 БАЛАНС\n{separator()}\n\n"
            f"💵 {fmt(u['balance'])} mCoin",
            reply_markup=home_keyboard(uid),
        )
        return

    if text in {"профиль", "проф"}:
        games = u["games"]
        winrate = (u["wins"] / games * 100) if games else 0
        await update.message.reply_text(
            f"👤 ПРОФИЛЬ\n{separator()}\n\n"
            f"🆔 ID: {uid}\n"
            f"📛 Username: @{u['username'] or 'нет'}\n"
            f"💰 Баланс: {fmt(u['balance'])} mCoin\n"
            f"🏆 Уровень: {u['level']}\n"
            f"⭐ XP: {u['xp']}\n"
            f"🎮 Игр: {games}\n"
            f"✅ Побед: {u['wins']}\n"
            f"💥 Поражений: {u['losses']}\n"
            f"📈 Winrate: {winrate:.1f}%\n"
            f"🔥 Streak: {u['streak']}",
            reply_markup=home_keyboard(uid),
        )
        return

    if text in {"игры", "игра", "каталог"}:
        await update.message.reply_text(
            "🕹 КАТАЛОГ ИГР\n"
            f"{separator()}\n\n"
            "💣 Мины — 1–23 мин\n"
            "🏰 Башня — 1–4 мин",
            reply_markup=catalog_keyboard(),
        )
        return

    if text in {"мины", "мин", "mines"}:
        await update.message.reply_text(
            f"🍀 МИНЫ\n{separator()}\n\n"
            f"💰 Баланс: {fmt(u['balance'])} mCoin\n"
            "💣 Выберите ставку:",
            reply_markup=amount_keyboard("mines_bet"),
        )
        return

    if text in {"башня", "tower"}:
        await update.message.reply_text(
            f"🏰 БАШНЯ\n{separator()}\n\n"
            f"💰 Баланс: {fmt(u['balance'])} mCoin\n"
            "💵 Выберите ставку:",
            reply_markup=amount_keyboard("tower_bet", include_all=True),
        )
        return

    if text in {"бонус", "бонусы", "bonus"}:
        last = DAILY_BONUS_STATE.get(uid)
        if last and now() - last < timedelta(hours=24):
            await update.message.reply_text(
                "🎁 БОНУС\n"
                f"{separator()}\n\n"
                "⏳ Ежедневный бонус уже получен.\n"
                "Возвращайтесь после 24 часов.",
                reply_markup=home_keyboard(uid),
            )
            return

        DAILY_BONUS_STATE[uid] = now()
        u["streak"] += 1
        reward = DAILY_BONUS + max(0, u["streak"] - 1) * 250
        change_balance(uid, reward, "daily_bonus", "Ежедневный бонус")
        add_xp(u, 20)

        await update.message.reply_text(
            f"🎁 БОНУС\n{separator()}\n\n"
            f"🎉 +{fmt(reward)} mCoin\n"
            f"🔥 Streak: {u['streak']}",
            reply_markup=home_keyboard(uid),
        )
        return

    if text in {"рейтинг", "топ", "топ игроков"}:
        top = sorted(USERS.values(), key=lambda x: x["balance"], reverse=True)[:10]
        lines = ["🏆 РЕЙТИНГ", separator(), ""]
        for index, item in enumerate(top, 1):
            name = item["first_name"] or str(item["id"])
            lines.append(f"{index}. {name} — {fmt(item['balance'])} mCoin")
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=home_keyboard(uid),
        )
        return

    if text in {"рефералы", "реф", "реферал"}:
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{uid}"
        count = sum(1 for ref in REFERRALS.values() if ref == uid)
        await update.message.reply_text(
            f"🎟 РЕФЕРАЛЫ\n{separator()}\n\n"
            f"👥 Приглашено: {count}\n\n"
            f"🔗 {link}",
            reply_markup=home_keyboard(uid),
        )
        return

    # Admin natural-language actions
    if is_admin(uid):
        parts = raw.strip().split()
        command = parts[0].lower().lstrip("/") if parts else ""

        if command in {"статистика", "стата"}:
            total_games = sum(x["games"] for x in USERS.values())
            total_balance = sum(x["balance"] for x in USERS.values())
            await update.message.reply_text(
                f"📊 СТАТИСТИКА\n{separator()}\n\n"
                f"👥 Пользователей: {len(USERS)}\n"
                f"🎮 Игр: {total_games}\n"
                f"💰 Баланс всех: {fmt(total_balance)} mCoin\n"
                f"📋 Транзакций: {len(TRANSACTIONS)}"
            )
            return

        if command in {"выдать", "выдатьбаланс", "add", "addbalance"} and len(parts) >= 3:
            target = parts[1]
            try:
                amount = int(parts[2])
            except ValueError:
                await update.message.reply_text("❌ Сумма должна быть числом.")
                return

            target_user = None
            if target.startswith("@"):
                username = target[1:].lower()
                for item in USERS.values():
                    if (item.get("username") or "").lower() == username:
                        target_user = item
                        break
            else:
                try:
                    target_user = get_user(int(target))
                except ValueError:
                    pass

            if not target_user:
                await update.message.reply_text("❌ Пользователь не найден.")
                return

            old = target_user["balance"]
            new = change_balance(
                target_user["id"],
                abs(amount),
                "admin_add",
                "Выдано администратором",
                admin_id=uid,
            )
            await update.message.reply_text(
                f"✅ Баланс выдан\n\n"
                f"👤 ID: {target_user['id']}\n"
                f"💰 Было: {fmt(old)}\n"
                f"➕ Выдано: {fmt(abs(amount))}\n"
                f"💰 Стало: {fmt(new)}"
            )
            return

        if command in {"снять", "снятьбаланс", "remove", "removebalance"} and len(parts) >= 3:
            target = parts[1]
            try:
                amount = abs(int(parts[2]))
            except ValueError:
                await update.message.reply_text("❌ Сумма должна быть числом.")
                return

            target_user = None
            if target.startswith("@"):
                username = target[1:].lower()
                for item in USERS.values():
                    if (item.get("username") or "").lower() == username:
                        target_user = item
                        break
            else:
                try:
                    target_user = get_user(int(target))
                except ValueError:
                    pass

            if not target_user:
                await update.message.reply_text("❌ Пользователь не найден.")
                return

            try:
                old = target_user["balance"]
                new = change_balance(
                    target_user["id"],
                    -amount,
                    "admin_remove",
                    "Снято администратором",
                    admin_id=uid,
                )
            except ValueError:
                await update.message.reply_text("❌ У пользователя недостаточно mCoin.")
                return

            await update.message.reply_text(
                f"✅ Баланс снят\n\n"
                f"👤 ID: {target_user['id']}\n"
                f"💰 Было: {fmt(old)}\n"
                f"➖ Снято: {fmt(amount)}\n"
                f"💰 Стало: {fmt(new)}"
            )
            return

        if command == "бан" and len(parts) >= 2:
            target = parts[1]
            target_user = None
            if target.startswith("@"):
                username = target[1:].lower()
                for item in USERS.values():
                    if (item.get("username") or "").lower() == username:
                        target_user = item
                        break
            else:
                try:
                    target_user = get_user(int(target))
                except ValueError:
                    pass

            if not target_user:
                await update.message.reply_text("❌ Пользователь не найден.")
                return

            target_user["banned"] = True
            await update.message.reply_text(
                f"🚫 Пользователь {target_user['id']} заблокирован."
            )
            return

        if command == "разбан" and len(parts) >= 2:
            target = parts[1]
            target_user = None
            if target.startswith("@"):
                username = target[1:].lower()
                for item in USERS.values():
                    if (item.get("username") or "").lower() == username:
                        target_user = item
                        break
            else:
                try:
                    target_user = get_user(int(target))
                except ValueError:
                    pass

            if not target_user:
                await update.message.reply_text("❌ Пользователь не найден.")
                return

            target_user["banned"] = False
            await update.message.reply_text(
                f"✅ Пользователь {target_user['id']} разблокирован."
            )
            return

        if command in {"инфо", "юзер"} and len(parts) >= 2:
            target = parts[1]
            target_user = None
            if target.startswith("@"):
                username = target[1:].lower()
                for item in USERS.values():
                    if (item.get("username") or "").lower() == username:
                        target_user = item
                        break
            else:
                try:
                    target_user = get_user(int(target))
                except ValueError:
                    pass

            if not target_user:
                await update.message.reply_text("❌ Пользователь не найден.")
                return

            await update.message.reply_text(
                f"👤 ПОЛЬЗОВАТЕЛЬ\n{separator()}\n\n"
                f"🆔 ID: {target_user['id']}\n"
                f"📛 Username: @{target_user['username'] or 'нет'}\n"
                f"💰 Баланс: {fmt(target_user['balance'])} mCoin\n"
                f"🏆 Уровень: {target_user['level']}\n"
                f"⭐ XP: {target_user['xp']}\n"
                f"🎮 Игр: {target_user['games']}\n"
                f"✅ Побед: {target_user['wins']}\n"
                f"💥 Поражений: {target_user['losses']}\n"
                f"🚫 Бан: {'да' if target_user['banned'] else 'нет'}"
            )
            return

    # Chat mode has priority only if no known command matched.
    if context.user_data.get("chat_mode"):
        context.user_data["chat_mode"] = False
        await update.message.reply_text(
            "💬 CHAT\n"
            f"{separator()}\n\n"
            "Сообщение получено.\n"
            "AI API можно подключить отдельным ключом.",
            reply_markup=home_keyboard(uid),
        )
        return

    # Do not answer to ordinary chat messages. Only recognized command words
    # and explicit admin actions above should be handled.
    return


# ----------------------------------------------------------
# Callback router
# ----------------------------------------------------------

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id
    u = get_user(uid, query.from_user)

    if u["banned"]:
        return await safe_edit(
            query,
            "🚫 Доступ ограничен администрацией.",
            back_keyboard(),
        )

    data = query.data or ""

    # ---------- Home ----------
    if data == "home":
        return await safe_edit(
            query,
            f"💣 Мины Бот\n"
            f"неzкс фамилионо...\n\n"
            f"🎮 ГЛАВНОЕ МЕНЮ\n"
            f"{separator()}\n"
            f"💰 Баланс: {fmt(u['balance'])} mCoin\n"
            f"🏆 Уровень: {u['level']}\n"
            f"⭐ XP: {u['xp']}",
            home_keyboard(uid),
        )

    # ---------- Games ----------
    if data == "games":
        return await safe_edit(
            query,
            f"🕹 КАТАЛОГ ИГР\n"
            f"{separator()}\n\n"
            f"ℹ️ В этом разделе вы можете познакомиться\n"
            f"со всеми доступными играми и запустить их.\n\n"
            f"💣 Мины — поле 5×5, мин: 1–23\n"
            f"🏰 Башня — 10 этажей, мин: 1–4",
            catalog_keyboard(),
        )

    # ---------- Mines setup ----------
    if data == "game:mines":
        return await safe_edit(
            query,
            f"🍀 МИНЫ · НАЧНИ ИГРУ!\n"
            f"{separator()}\n\n"
            f"💰 Баланс: {fmt(u['balance'])} mCoin\n"
            f"💣 Мин: 1–23\n"
            f"🎯 Поле: 5×5\n\n"
            f"💵 Выберите сумму ставки:",
            amount_keyboard("mines_bet"),
        )

    if data.startswith("mines_bet:"):
        bet = (
            u["balance"]
            if data.split(":")[1] == "all"
            else int(data.split(":")[1])
        )

        if not MIN_BET <= bet <= MAX_BET:
            return await safe_edit(
                query,
                "❌ Некорректная сумма ставки.",
                back_keyboard(),
            )

        if u["balance"] < bet:
            return await safe_edit(
                query,
                "❌ Недостаточно mCoin.",
                back_keyboard(),
            )

        return await safe_edit(
            query,
            f"🍀 МИНЫ · НАСТРОЙКА\n"
            f"{separator()}\n\n"
            f"💵 Ставка: {fmt(bet)} mCoin\n"
            f"💣 Количество мин: 1–23\n\n"
            f"🎯 Выберите количество мин:",
            mines_count_keyboard(bet),
        )

    if data.startswith("mines_start:"):
        _, bet_raw, mines_raw = data.split(":")
        bet = int(bet_raw)
        mines = int(mines_raw)

        if not 1 <= mines <= 23:
            return await safe_edit(
                query,
                "❌ Количество мин должно быть от 1 до 23.",
                back_keyboard(),
            )

        if u["balance"] < bet:
            return await safe_edit(
                query,
                "❌ Недостаточно mCoin.",
                back_keyboard(),
            )

        if uid in ACTIVE_GAMES:
            return await safe_edit(
                query,
                "⚠️ У вас уже есть активная игра.",
                back_keyboard(),
            )

        game = new_mines_game(uid, bet, mines)
        ACTIVE_GAMES[uid] = game
        change_balance(uid, -bet, "mines_bet", "Ставка Mines")

        u["games"] += 1
        add_xp(u, 5)

        return await safe_edit(
            query,
            f"🍀 МИНЫ · НАЧНИ ИГРУ!\n"
            f"{separator()}\n\n"
            f"💣 Мин: {mines}\n"
            f"💵 Ставка: {fmt(bet)} mCoin\n\n"
            f"🧮 Следующий множитель:\n"
            f"x{mines_multiplier(mines, 1)}\n\n"
            f"❓ Открывай клетки:",
            mines_keyboard(game),
        )

    # ---------- Mines play ----------
    if data.startswith("mine_open:"):
        _, game_id, cell_raw = data.split(":")
        game = ACTIVE_GAMES.get(uid)

        if not game or game["id"] != game_id or game["type"] != "mines":
            return await safe_edit(
                query,
                "❌ Игра не найдена или уже завершена.",
                back_keyboard(),
            )

        if now() - game["created"] > timedelta(minutes=GAME_TIMEOUT_MINUTES):
            del ACTIVE_GAMES[uid]
            return await safe_edit(
                query,
                "⏰ Игра истекла по таймауту.",
                back_keyboard(),
            )

        cell = int(cell_raw)

        if cell in game["opened"]:
            return await query.answer("Эта клетка уже открыта.")

        if cell in game["mine_positions"]:
            game["opened"].add(cell)
            u["losses"] += 1
            del ACTIVE_GAMES[uid]

            return await safe_edit(
                query,
                f"💥 МИНЫ · ПРОИГРЫШ!\n"
                f"{separator()}\n\n"
                f"💣 Мин: {game['mines']}\n"
                f"💵 Ставка: {fmt(game['bet'])} mCoin\n"
                f"📦 Открыто клеток: {len(game['opened']) - 1}\n\n"
                f"💣 Вы попали на мину.",
                back_keyboard(),
            )

        game["opened"].add(cell)
        opened = len(game["opened"])
        game["multiplier"] = mines_multiplier(game["mines"], opened)
        add_xp(u, 5)

        safe_cells = BOARD_SIZE - game["mines"]
        if opened >= safe_cells:
            reward = int(Decimal(game["bet"]) * game["multiplier"])
            change_balance(uid, reward, "mines_win", "Победа Mines")
            u["wins"] += 1
            del ACTIVE_GAMES[uid]

            return await safe_edit(
                query,
                f"🎉 МИНЫ · ПОБЕДА!\n"
                f"{separator()}\n\n"
                f"💣 Мин: {game['mines']}\n"
                f"🧳 Пройдено: {opened}/{safe_cells}\n"
                f"📈 Множитель: x{game['multiplier']}\n"
                f"💰 Получено: {fmt(reward)} mCoin",
                back_keyboard(),
            )

        current_value = int(Decimal(game["bet"]) * game["multiplier"])

        return await safe_edit(
            query,
            f"🍀 МИНЫ · ИГРА\n"
            f"{separator()}\n\n"
            f"💣 Мин: {game['mines']}\n"
            f"💵 Ставка: {fmt(game['bet'])} mCoin\n"
            f"🧳 Пройдено: {opened}/{safe_cells}\n"
            f"📈 Множитель: x{game['multiplier']}\n"
            f"💰 Забрать: {fmt(current_value)} mCoin\n\n"
            f"👇 Следующий ход:",
            mines_keyboard(game),
        )

    if data.startswith("mine_cash:"):
        _, game_id = data.split(":")
        game = ACTIVE_GAMES.get(uid)

        if not game or game["id"] != game_id or game["type"] != "mines":
            return await safe_edit(
                query,
                "❌ Игра уже завершена.",
                back_keyboard(),
            )

        if not game["opened"]:
            return await query.answer(
                "Сначала откройте хотя бы одну клетку.",
                show_alert=True,
            )

        reward = int(Decimal(game["bet"]) * game["multiplier"])
        change_balance(uid, reward, "mines_cashout", "Забор Mines")
        u["wins"] += 1
        del ACTIVE_GAMES[uid]

        return await safe_edit(
            query,
            f"💰 МИНЫ · ВЫ ЗАБРАЛИ!\n"
            f"{separator()}\n\n"
            f"💣 Мин: {game['mines']}\n"
            f"📈 Множитель: x{game['multiplier']}\n"
            f"💰 Получено: {fmt(reward)} mCoin\n\n"
            f"🔑 Игра завершена.",
            back_keyboard(),
        )

    if data.startswith("mine_cancel:"):
        _, game_id = data.split(":")
        game = ACTIVE_GAMES.get(uid)

        if game and game["id"] == game_id:
            del ACTIVE_GAMES[uid]

        return await safe_edit(
            query,
            "❌ Игра отменена.\n\nСтавка не возвращается.",
            back_keyboard(),
        )

    if data.startswith("mine_verify:"):
        _, game_id = data.split(":")
        game = ACTIVE_GAMES.get(uid)

        if not game or game["id"] != game_id:
            return await safe_edit(
                query,
                "❌ Игра не найдена.",
                back_keyboard(),
            )

        return await safe_edit(
            query,
            f"🔑 БАШНЯ / МИНЫ · ЧЕСТНОСТЬ\n"
            f"{separator()}\n\n"
            f"🔐 SHA-256 Hash:\n"
            f"{game['server_seed_hash']}\n\n"
            f"🎲 Client Seed:\n"
            f"{game['client_seed']}\n\n"
            f"После завершения игры серверный seed можно использовать для проверки.",
            back_keyboard(),
        )

    # ---------- Tower setup ----------
    if data == "game:tower":
        return await safe_edit(
            query,
            f"🏰 БАШНЯ · НАЧНИ ИГРУ!\n"
            f"{separator()}\n\n"
            f"💰 Баланс: {fmt(u['balance'])} mCoin\n"
            f"🏢 Этажей: {TOWER_FLOORS}\n"
            f"💣 Мин на этаже: 1–4\n\n"
            f"💵 Выберите сумму:",
            amount_keyboard("tower_bet", include_all=True),
        )

    if data.startswith("tower_bet:"):
        raw = data.split(":")[1]
        bet = u["balance"] if raw == "all" else int(raw)

        if not MIN_BET <= bet <= MAX_BET:
            return await safe_edit(
                query,
                "❌ Некорректная ставка.",
                back_keyboard(),
            )

        if u["balance"] < bet:
            return await safe_edit(
                query,
                "❌ Недостаточно mCoin.",
                back_keyboard(),
            )

        return await safe_edit(
            query,
            f"🏰 БАШНЯ · НАСТРОЙКА\n"
            f"{separator()}\n\n"
            f"💵 Сумма: {fmt(bet)} mCoin\n"
            f"💣 Мин: 1–4\n\n"
            f"🎯 Выберите количество мин:",
            tower_mines_keyboard(bet),
        )

    if data.startswith("tower_start:"):
        _, bet_raw, mines_raw = data.split(":")
        bet = int(bet_raw)
        mines = int(mines_raw)

        if not 1 <= mines <= 4:
            return await safe_edit(
                query,
                "❌ На этаже должно быть от 1 до 4 мин.",
                back_keyboard(),
            )

        if u["balance"] < bet:
            return await safe_edit(
                query,
                "❌ Недостаточно mCoin.",
                back_keyboard(),
            )

        if uid in ACTIVE_GAMES:
            return await safe_edit(
                query,
                "⚠️ У вас уже есть активная игра.",
                back_keyboard(),
            )

        game = new_tower_game(uid, bet, mines)
        ACTIVE_GAMES[uid] = game
        change_balance(uid, -bet, "tower_bet", "Ставка Tower")

        u["games"] += 1
        add_xp(u, 5)

        return await safe_edit(
            query,
            f"🍀 БАШНЯ · НАЧНИ ИГРУ!\n"
            f"{separator()}\n\n"
            f"💣 Мин: {mines}\n"
            f"💵 Ставка: {fmt(bet)} mCoin\n"
            f"🏢 Этаж: 1/{TOWER_FLOORS}\n"
            f"📈 Следующий множитель: x{tower_multiplier(mines, 1)}\n\n"
            f"❓ Выберите одну из 4 клеток:",
            tower_keyboard(game),
        )

    # ---------- Tower play ----------
    if data.startswith("tower_open:"):
        _, game_id, cell_raw = data.split(":")
        game = ACTIVE_GAMES.get(uid)

        if (
            not game
            or game["id"] != game_id
            or game["type"] != "tower"
        ):
            return await safe_edit(
                query,
                "❌ Игра не найдена.",
                back_keyboard(),
            )

        if now() - game["created"] > timedelta(minutes=GAME_TIMEOUT_MINUTES):
            del ACTIVE_GAMES[uid]
            return await safe_edit(
                query,
                "⏰ Игра истекла по таймауту.",
                back_keyboard(),
            )

        cell = int(cell_raw)

        if cell in game["current_open"]:
            return await query.answer("Клетка уже выбрана.")

        if cell in game["layout"]:
            u["losses"] += 1
            floor_lost = game["floor"]
            del ACTIVE_GAMES[uid]

            return await safe_edit(
                query,
                f"💥 БАШНЯ · ПРОИГРЫШ!\n"
                f"{separator()}\n\n"
                f"💣 Мин: {game['mines']}\n"
                f"🏢 Этаж: {floor_lost}/{TOWER_FLOORS}\n"
                f"💵 Ставка: {fmt(game['bet'])} mCoin\n"
                f"📦 Пройдено: {max(0, floor_lost - 1)} из {TOWER_FLOORS}",
                back_keyboard(),
            )

        game["current_open"].add(cell)
        current_floor = game["floor"]
        add_xp(u, 7)

        if current_floor >= TOWER_FLOORS:
            multiplier = tower_multiplier(game["mines"], TOWER_FLOORS)
            reward = int(Decimal(game["bet"]) * multiplier)
            change_balance(uid, reward, "tower_win", "Победа Tower")
            u["wins"] += 1
            del ACTIVE_GAMES[uid]

            return await safe_edit(
                query,
                f"🎉 БАШНЯ · ПРОЙДЕНА!\n"
                f"{separator()}\n\n"
                f"💣 Мин: {game['mines']}\n"
                f"🏢 Пройдено: {TOWER_FLOORS}/{TOWER_FLOORS}\n"
                f"📈 Множитель: x{multiplier}\n"
                f"💰 Получено: {fmt(reward)} mCoin",
                back_keyboard(),
            )

        game["floor"] += 1
        game["current_open"] = set()
        game["layout"] = tower_layout(
            game["server_seed"],
            game["id"],
            game["floor"],
            game["mines"],
        )

        multiplier = tower_multiplier(game["mines"], game["floor"])
        current_value = int(Decimal(game["bet"]) * multiplier)

        return await safe_edit(
            query,
            f"🏰 БАШНЯ · ИГРА\n"
            f"{separator()}\n\n"
            f"💣 Мин: {game['mines']}\n"
            f"💵 Ставка: {fmt(game['bet'])} mCoin\n"
            f"🏢 Этаж: {game['floor']}/{TOWER_FLOORS}\n"
            f"📈 Множитель: x{multiplier}\n"
            f"💰 Забрать: {fmt(current_value)} mCoin\n\n"
            f"❓ Выберите клетку:",
            tower_keyboard(game),
        )

    if data.startswith("tower_cash:"):
        _, game_id = data.split(":")
        game = ACTIVE_GAMES.get(uid)

        if not game or game["id"] != game_id:
            return await safe_edit(
                query,
                "❌ Игра не найдена.",
                back_keyboard(),
            )

        multiplier = tower_multiplier(
            game["mines"],
            max(1, game["floor"] - 1),
        )
        reward = int(Decimal(game["bet"]) * multiplier)
        change_balance(uid, reward, "tower_cashout", "Забор Tower")
        u["wins"] += 1
        del ACTIVE_GAMES[uid]

        return await safe_edit(
            query,
            f"💰 БАШНЯ · ВЫ ЗАБРАЛИ!\n"
            f"{separator()}\n\n"
            f"💣 Мин: {game['mines']}\n"
            f"🏢 Пройдено: {max(0, game['floor'] - 1)} этажей\n"
            f"📈 Множитель: x{multiplier}\n"
            f"💰 Получено: {fmt(reward)} mCoin",
            back_keyboard(),
        )

    if data.startswith("tower_cancel:"):
        _, game_id = data.split(":")
        game = ACTIVE_GAMES.get(uid)

        if game and game["id"] == game_id:
            del ACTIVE_GAMES[uid]

        return await safe_edit(
            query,
            "❌ Башня отменена.\n\nСтавка не возвращается.",
            back_keyboard(),
        )

    if data.startswith("tower_verify:"):
        _, game_id = data.split(":")
        game = ACTIVE_GAMES.get(uid)

        if not game or game["id"] != game_id:
            return await safe_edit(
                query,
                "❌ Игра не найдена.",
                back_keyboard(),
            )

        return await safe_edit(
            query,
            f"🔑 БАШНЯ · ЧЕСТНОСТЬ\n"
            f"{separator()}\n\n"
            f"🔐 SHA-256 Hash:\n"
            f"{game['server_seed_hash']}\n\n"
            f"🏢 Текущий этаж: {game['floor']}/{TOWER_FLOORS}",
            back_keyboard(),
        )

    # ---------- Main sections ----------
    if data == "balance":
        return await safe_edit(
            query,
            f"💰 БАЛАНС\n"
            f"{separator()}\n\n"
            f"💵 {fmt(u['balance'])} mCoin",
            back_keyboard(),
        )

    if data == "bonus":
        last = DAILY_BONUS_STATE.get(uid)

        if last and now() - last < timedelta(hours=24):
            return await safe_edit(
                query,
                "🎁 БОНУС\n"
                f"{separator()}\n\n"
                "⏳ Ежедневный бонус уже получен.\n"
                "Возвращайтесь после 24 часов.",
                back_keyboard(),
            )

        DAILY_BONUS_STATE[uid] = now()
        u["streak"] += 1

        reward = DAILY_BONUS + max(0, u["streak"] - 1) * 250
        change_balance(uid, reward, "daily_bonus", "Ежедневный бонус")
        add_xp(u, 20)

        return await safe_edit(
            query,
            f"🎁 БОНУС\n"
            f"{separator()}\n\n"
            f"🎉 Тебе выдан бонус:\n"
            f"💰 +{fmt(reward)} mCoin\n\n"
            f"🔥 Streak: {u['streak']}\n\n"
            f"👇 Следующий бонус будет доступен позже.",
            back_keyboard(),
        )

    if data == "profile":
        games = u["games"]
        winrate = (u["wins"] / games * 100) if games else 0

        return await safe_edit(
            query,
            f"👤 ПРОФИЛЬ\n"
            f"{separator()}\n\n"
            f"🆔 ID: {uid}\n"
            f"📛 Username: @{u['username'] or 'нет'}\n"
            f"💰 Баланс: {fmt(u['balance'])} mCoin\n"
            f"🏆 Уровень: {u['level']}\n"
            f"⭐ XP: {u['xp']}\n"
            f"💣 Сыграно игр: {games}\n"
            f"✅ Выиграно: {u['wins']}\n"
            f"💥 Проиграно: {u['losses']}\n"
            f"📈 Winrate: {winrate:.1f}%\n"
            f"🔥 Streak: {u['streak']}",
            back_keyboard(),
        )

    if data == "refs":
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{uid}"
        count = sum(1 for ref in REFERRALS.values() if ref == uid)

        return await safe_edit(
            query,
            f"🎟 РЕФЕРАЛЫ\n"
            f"{separator()}\n\n"
            f"👥 Приглашено: {count}\n\n"
            f"🔗 Твоя ссылка:\n{link}",
            back_keyboard(),
        )

    if data == "rating":
        top = sorted(
            USERS.values(),
            key=lambda x: x["balance"],
            reverse=True,
        )[:10]

        lines = [
            "🏆 РЕЙТИНГ",
            separator(),
            "",
        ]

        for index, item in enumerate(top, 1):
            name = item["first_name"] or str(item["id"])
            lines.append(
                f"{index}. {name} — {fmt(item['balance'])} mCoin"
            )

        return await safe_edit(
            query,
            "\n".join(lines),
            back_keyboard(),
        )

    if data == "chat":
        context.user_data["chat_mode"] = True

        return await safe_edit(
            query,
            f"💬 CHAT\n"
            f"{separator()}\n\n"
            f"✍️ Отправьте сообщение следующим сообщением.\n\n"
            f"Для выхода используйте /start.",
            back_keyboard(),
        )

    if data == "help":
        return await safe_edit(
            query,
            f"📖 ПОМОЩЬ\n"
            f"{separator()}\n\n"
            f"💣 Мины — поле 5×5, мин можно выбрать от 1 до 23.\n"
            f"🏰 Башня — 10 этажей, на каждом 1–4 мины.\n"
            f"💰 Cashout позволяет забрать текущую виртуальную награду.\n"
            f"🔑 Честность показывает hash игрового seed.\n"
            f"🎁 Ежедневный бонус увеличивает streak.\n\n"
            f"Все mCoin внутри этого проекта являются виртуальными баллами.",
            back_keyboard(),
        )

    # ---------- Admin UI ----------
    if data == "admin":
        if not is_admin(uid):
            return await safe_edit(query, "⛔ Доступ запрещён.", back_keyboard())

        return await safe_edit(
            query,
            f"🟢 АДМИН · ПАНЕЛЬ\n"
            f"{separator()}\n\n"
            f"👤 Пользователи\n"
            f"💰 Выдача / снятие mCoin\n"
            f"📊 Статистика\n"
            f"📋 Логи операций",
            admin_keyboard(),
        )

    if data == "adm_stats":
        if not is_admin(uid):
            return await safe_edit(query, "⛔ Доступ запрещён.", back_keyboard())

        total_games = sum(x["games"] for x in USERS.values())
        return await safe_edit(
            query,
            f"📊 АДМИН · СТАТИСТИКА\n"
            f"{separator()}\n\n"
            f"👥 Пользователей: {len(USERS)}\n"
            f"🎮 Игр: {total_games}\n"
            f"📋 Транзакций: {len(TRANSACTIONS)}",
            admin_keyboard(),
        )

    if data == "adm_logs":
        if not is_admin(uid):
            return await safe_edit(query, "⛔ Доступ запрещён.", back_keyboard())

        lines = ["📋 АДМИН · ЛОГИ", separator(), ""]
        for entry in TRANSACTIONS[-8:][::-1]:
            lines.append(
                f"👤 {entry['user_id']} · "
                f"{entry['type']} · "
                f"{entry['amount']:+} · "
                f"{entry['description']}"
            )

        return await safe_edit(
            query,
            "\n".join(lines),
            admin_keyboard(),
        )

    if data in {"adm_users", "adm_balance"}:
        if not is_admin(uid):
            return await safe_edit(query, "⛔ Доступ запрещён.", back_keyboard())

        return await safe_edit(
            query,
            f"🛠️ АДМИН · КОМАНДЫ\n"
            f"{separator()}\n\n"
            f"👤 /user ID\n"
            f"💰 /addbalance ID SUM REASON\n"
            f"💸 /removebalance ID SUM REASON\n"
            f"🚫 /ban ID REASON\n"
            f"✅ /unban ID",
            admin_keyboard(),
        )

    # ---------- Future catalog entries ----------
    if data.startswith("soon:"):
        return await safe_edit(
            query,
            f"🎮 ИГРА ПОКА В РАЗРАБОТКЕ\n"
            f"{separator()}\n\n"
            f"⚙️ Этот раздел уже добавлен в каталог.\n"
            f"Следующим обновлением его можно подключить.",
            back_keyboard(),
        )


# ----------------------------------------------------------
# Chat
# ----------------------------------------------------------

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    uid = update.effective_user.id
    u = get_user(uid, update.effective_user)

    if u["banned"] and not is_admin(uid):
        return await update.message.reply_text("🚫 Доступ ограничен администрацией.")

    # Chat mode has priority for arbitrary messages.
    if context.user_data.get("chat_mode"):
        context.user_data["chat_mode"] = False
        return await update.message.reply_text(
            f"💬 CHAT\n{separator()}\n\n"
            f"Сообщение получено.\n"
            f"AI API можно подключить через отдельный ключ и модель.",
            reply_markup=back_keyboard(),
        )

    parts = text.split()
    cmd = parts[0].lower().lstrip("/")

    # Common player words instead of slash commands.
    aliases = {
        "старт": "start", "начать": "start", "меню": "start",
        "баланс": "balance", "б": "balance",
        "профиль": "profile", "проф": "profile",
        "игры": "games", "игра": "games", "каталог": "games",
        "мины": "mines", "мину": "mines",
        "башня": "tower",
        "админ": "admin", "админка": "admin",
        "статистика": "stats", "стата": "stats",
        "помощь": "help", "команды": "help",
    }
    cmd = aliases.get(cmd, cmd)

    if cmd == "start":
        return await update.message.reply_text(
            f"💣 Мины Бот\nнеzкс фамилионо...\n\n"
            f"🎮 ГЛАВНОЕ МЕНЮ\n{separator()}\n"
            f"💰 Баланс: {fmt(u['balance'])} mCoin\n"
            f"🏆 Уровень: {u['level']}\n⭐ XP: {u['xp']}",
            reply_markup=home_keyboard(uid),
        )

    if cmd == "balance":
        # Admin can check another player's balance: "баланс @username" or "баланс ID".
        target = u
        if is_admin(uid) and len(parts) >= 2:
            target = find_user_by_ref(parts[1])
            if not target:
                return await update.message.reply_text("❌ Пользователь не найден.")
        return await update.message.reply_text(
            f"💰 БАЛАНС\n{separator()}\n\n"
            f"👤 {user_label(target)}\n"
            f"💰 {fmt(target['balance'])} mCoin",
            reply_markup=back_keyboard(),
        )

    if cmd == "profile":
        return await update.message.reply_text(
            f"👤 ПРОФИЛЬ\n{separator()}\n\n"
            f"🆔 ID: {uid}\n"
            f"📛 Username: @{u['username'] or 'нет'}\n"
            f"💰 Баланс: {fmt(u['balance'])} mCoin\n"
            f"🏆 Уровень: {u['level']}\n⭐ XP: {u['xp']}\n"
            f"🎮 Игр: {u['games']}\n✅ Побед: {u['wins']}\n💥 Поражений: {u['losses']}",
            reply_markup=back_keyboard(),
        )

    if cmd == "games":
        return await update.message.reply_text(
            f"🕹 КАТАЛОГ ИГР\n{separator()}\n\n"
            f"💣 Мины — 1–23 мин\n"
            f"🏰 Башня — 1–4 мин",
            reply_markup=catalog_keyboard(),
        )

    if cmd == "mines":
        return await update.message.reply_text(
            f"🍀 МИНЫ · НАЧНИ ИГРУ!\n{separator()}\n\n"
            f"💰 Баланс: {fmt(u['balance'])} mCoin\n"
            f"💣 Мин: 1–23\n🎯 Поле: 5×5\n\n"
            f"💵 Выберите сумму ставки:",
            reply_markup=amount_keyboard("mines_bet"),
        )

    if cmd == "tower":
        return await update.message.reply_text(
            f"🏰 БАШНЯ · НАЧНИ ИГРУ!\n{separator()}\n\n"
            f"💰 Баланс: {fmt(u['balance'])} mCoin\n"
            f"💣 Мин: 1–4\n\n"
            f"💵 Выберите сумму ставки:",
            reply_markup=amount_keyboard("tower_bet"),
        )

    if cmd == "help":
        return await update.message.reply_text(
            f"📖 ПОМОЩЬ\n{separator()}\n\n"
            f"Просто напиши слово без /:\n"
            f"💰 баланс\n👤 профиль\n🎮 игры\n"
            f"💣 мины\n🏰 башня\n📊 статистика\n"
            f"🟢 админ — только для администраторов",
            reply_markup=back_keyboard(),
        )

    # Admin-only natural language commands.
    if cmd in {"выдать", "начислить", "добавить"}:
        if not is_admin(uid):
            return await update.message.reply_text("⛔ Доступ запрещён.")
        if len(parts) < 3:
            return await update.message.reply_text("Формат: Выдать @username СУММА")
        target = find_user_by_ref(parts[1])
        if not target:
            return await update.message.reply_text("❌ Пользователь не найден.")
        try:
            amount = abs(int(parts[2]))
        except ValueError:
            return await update.message.reply_text("❌ Сумма должна быть числом.")
        reason = " ".join(parts[3:]) or "Выдача администратором"
        old = target["balance"]
        new = change_balance(target["id"], amount, "admin_add", reason, admin_id=uid)
        return await update.message.reply_text(
            f"✅ Баланс выдан\n\n👤 {user_label(target)}\n"
            f"💰 Было: {fmt(old)} mCoin\n➕ Выдано: {fmt(amount)} mCoin\n"
            f"💰 Стало: {fmt(new)} mCoin"
        )

    if cmd in {"бан", "забанить"}:
        if not is_admin(uid):
            return await update.message.reply_text("⛔ Доступ запрещён.")
        if len(parts) < 2:
            return await update.message.reply_text("Формат: Бан @username")
        target = find_user_by_ref(parts[1])
        if not target:
            return await update.message.reply_text("❌ Пользователь не найден.")
        target["banned"] = True
        return await update.message.reply_text(f"🚫 {user_label(target)} заблокирован.")

    if cmd in {"разбан", "разблокировать"}:
        if not is_admin(uid):
            return await update.message.reply_text("⛔ Доступ запрещён.")
        if len(parts) < 2:
            return await update.message.reply_text("Формат: Разбан @username")
        target = find_user_by_ref(parts[1])
        if not target:
            return await update.message.reply_text("❌ Пользователь не найден.")
        target["banned"] = False
        return await update.message.reply_text(f"✅ {user_label(target)} разблокирован.")

    if cmd in {"снять", "убрать", "вычесть"}:
        if not is_admin(uid):
            return await update.message.reply_text("⛔ Доступ запрещён.")
        if len(parts) < 3:
            return await update.message.reply_text("Формат: Снять @username СУММА")
        target = find_user_by_ref(parts[1])
        if not target:
            return await update.message.reply_text("❌ Пользователь не найден.")
        try:
            amount = abs(int(parts[2]))
        except ValueError:
            return await update.message.reply_text("❌ Сумма должна быть числом.")
        reason = " ".join(parts[3:]) or "Списание администратором"
        old = target["balance"]
        new = change_balance(target["id"], -amount, "admin_remove", reason, admin_id=uid)
        return await update.message.reply_text(
            f"✅ Баланс изменён\n\n👤 {user_label(target)}\n"
            f"💰 Было: {fmt(old)} mCoin\n➖ Снято: {fmt(amount)} mCoin\n"
            f"💰 Стало: {fmt(new)} mCoin"
        )

    if cmd == "админ" and is_admin(uid):
        return await admin_command(update, context)

    if cmd == "stats" and is_admin(uid):
        total_games = sum(x["games"] for x in USERS.values())
        total_balance = sum(x["balance"] for x in USERS.values())
        return await update.message.reply_text(
            f"📊 СТАТИСТИКА\n{separator()}\n\n"
            f"👥 Пользователей: {len(USERS)}\n"
            f"🎮 Игр: {total_games}\n"
            f"💰 Баланс: {fmt(total_balance)} mCoin\n"
            f"📋 Транзакций: {len(TRANSACTIONS)}"
        )

    # Admin: balance/profile lookup by username or ID.
    if cmd in {"юзер", "пользователь", "инфо"}:
        if not is_admin(uid):
            return await update.message.reply_text("⛔ Доступ запрещён.")
        if len(parts) < 2:
            return await update.message.reply_text("Формат: Юзер @username или ID")
        target = find_user_by_ref(parts[1])
        if not target:
            return await update.message.reply_text("❌ Пользователь не найден.")
        return await update.message.reply_text(
            f"👤 ПОЛЬЗОВАТЕЛЬ\n{separator()}\n\n"
            f"🆔 ID: {target['id']}\n"
            f"📛 Username: @{target['username'] or 'нет'}\n"
            f"💰 Баланс: {fmt(target['balance'])} mCoin\n"
            f"🏆 Уровень: {target['level']}\n⭐ XP: {target['xp']}\n"
            f"🎮 Игр: {target['games']}\n🚫 Бан: {'да' if target['banned'] else 'нет'}"
        )

    # If a plain username is sent by an admin, show its balance.
    if is_admin(uid) and text.startswith("@") and len(parts) == 1:
        target = find_user_by_ref(text)
        if target:
            return await update.message.reply_text(
                f"💰 Баланс {user_label(target)}: {fmt(target['balance'])} mCoin"
            )

    # Keep unknown text quiet instead of treating it as a slash command.
    return await update.message.reply_text(
        "❓ Не понял сообщение. Напиши «помощь», чтобы увидеть доступные слова."
    )


# ----------------------------------------------------------
# FastAPI / Render
# ----------------------------------------------------------

telegram_app: Application | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app

    telegram_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CallbackQueryHandler(callback))
    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text_commands,
        )
    )

    await telegram_app.initialize()
    await telegram_app.start()

    if WEBHOOK_URL:
        await telegram_app.bot.set_webhook(
            url=f"{WEBHOOK_URL}/telegram/webhook",
            secret_token=WEBHOOK_SECRET,
        )
    else:
        await telegram_app.updater.start_polling()

    yield

    if WEBHOOK_URL:
        await telegram_app.bot.delete_webhook()
    else:
        await telegram_app.updater.stop()

    await telegram_app.stop()
    await telegram_app.shutdown()


api = FastAPI(
    title="NEZZX GAME",
    lifespan=lifespan,
)


@api.get("/")
async def root():
    return {
        "name": "NEZZX GAME",
        "status": "online",
    }


@api.get("/health")
async def health():
    return {
        "status": "ok",
        "users": len(USERS),
        "active_games": len(ACTIVE_GAMES),
    }


@api.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    body = await request.json()

    if telegram_app is None:
        return {"ok": False, "error": "telegram app not ready"}

    await telegram_app.update_queue.put(
        Update.de_json(body, telegram_app.bot)
    )

    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(
        "main:api",
        host="0.0.0.0",
        port=PORT,
    )
