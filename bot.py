import asyncio
import io
import os
import sqlite3
from collections import defaultdict
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from openpyxl import Workbook

# ============ НАСТРОЙКИ ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "8965560502:AAFsP2v-4zbzUUG7croI4WZTzZuxlOZR7uU")
ADMIN_IDS = {697012628}   # ← замените на свой Telegram ID (узнать у @userinfobot)
DB_PATH = "/data/bot.db"

TYPE_LABELS = {
    "meeting": "Встреча", "ko": "КО", "pd": "ПД",
    "od": "ОД", "dvou": "ДВОУ", "dou": "ДОУ",
    "publication": "Публикация",
}
CAT_LABELS = {"buyer": "покупатель", "seller": "продавец", "both": "покупатель и продавец"}

# ============ БАЗА ДАННЫХ ============
def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            fio TEXT NOT NULL,
            role TEXT DEFAULT 'employee',
            is_active INTEGER DEFAULT 1,
            registered_at TEXT
        );
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            record_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            client_fio TEXT,
            client_crm_id TEXT,
            client_category TEXT,
            deal_amount REAL,
            revenue REAL,
            business_name TEXT,
            pd_id INTEGER
        );
        """)

def get_user(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def create_user(user_id, fio):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users(user_id, fio, role, is_active, registered_at) VALUES(?,?,?,?,?)",
            (user_id, fio, "admin" if user_id in ADMIN_IDS else "employee", 1,
             datetime.now().isoformat())
        )

def add_record(user_id, rtype, **kwargs):
    cols = ["user_id", "record_type", "created_at"] + list(kwargs.keys())
    vals = [user_id, rtype, datetime.now().isoformat()] + list(kwargs.values())
    q = f"INSERT INTO records({','.join(cols)}) VALUES({','.join(['?']*len(cols))})"
    with db() as conn:
        conn.execute(q, vals)

def is_admin(user_id):
    return user_id in ADMIN_IDS

async def ensure_access(message: Message) -> bool:
    u = get_user(message.from_user.id)
    if not u or not u["is_active"]:
        await message.answer("У вас нет доступа. Введите /start для регистрации.")
        return False
    return True

# ============ КЛАВИАТУРЫ ============
def main_menu(user_id):
    kb = ReplyKeyboardBuilder()
    kb.button(text="📅 Встреча")
    kb.button(text="🏢 КО в офисе")
    kb.button(text="📄 Открыл ПД")
    kb.button(text="✅ Закрыл ОД")
    kb.button(text="💰 Платный ДВОУ")
    kb.button(text="💳 Платный ДОУ")
    kb.button(text="📢 Публикации")
    kb.button(text="👤 Личный кабинет")
    kb.button(text="🗑 Удалить открытый ПД")
    if is_admin(user_id):
        kb.button(text="📊 Общая статистика")
        kb.button(text="⚙️ Админ-панель")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def category_kb(prefix: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="Мой покупатель", callback_data=f"cat:{prefix}:buyer")
    kb.button(text="Мой продавец", callback_data=f"cat:{prefix}:seller")
    kb.button(text="Мой покупатель и продавец", callback_data=f"cat:{prefix}:both")
    kb.adjust(1)
    return kb.as_markup()

# ============ FSM ============
class Reg(StatesGroup):
    fio = State()

class Meeting(StatesGroup):
    client_fio = State()
    crm_id = State()

class KO(StatesGroup):
    client_fio = State()
    crm_id = State()

class PD(StatesGroup):
    amount = State()
    revenue = State()
    business = State()

class OD(StatesGroup):
    amount = State()
    revenue = State()
    business = State()

class Payment(StatesGroup):
    amount = State()
    client = State()

class Publication(StatesGroup):
    amount = State()
    
# ============ ОБЩИЕ ХЕНДЛЕРЫ ============
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    u = get_user(message.from_user.id)
    if u:
        if not u["is_active"]:
            await message.answer("Ваш доступ удалён администратором.")
            return
        await message.answer("Главное меню:", reply_markup=main_menu(message.from_user.id))
        return
    await message.answer("Здравствуйте! Введите ваше ФИО для регистрации:")
    await state.set_state(Reg.fio)

@dp.message(Reg.fio)
async def reg_fio(message: Message, state: FSMContext):
    fio = (message.text or "").strip()
    if len(fio) < 3:
        await message.answer("Слишком короткое ФИО, попробуйте ещё раз.")
        return
    create_user(message.from_user.id, fio)
    await state.clear()
    await message.answer(f"✅ Регистрация завершена, {fio}!",
                         reply_markup=main_menu(message.from_user.id))

# ============ ОТМЕНА ДЕЙСТВИЯ ============
@dp.message(Command("cancel"))
@dp.message(F.text.casefold() == "отмена")
async def cancel_handler(message: Message, state: FSMContext):
    current = await state.get_state()
    await state.clear()
    if current is None:
        await message.answer("Нечего отменять. Главное меню:",
                             reply_markup=main_menu(message.from_user.id))
        return
    await message.answer("❌ Действие отменено. Главное меню:",
                         reply_markup=main_menu(message.from_user.id))

# ============ УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК КАТЕГОРИИ ============
@dp.callback_query(F.data.startswith("cat:"))
async def cat_handler(cb: CallbackQuery, state: FSMContext):
    _, prefix, cat = cb.data.split(":")
    await state.update_data(category=cat)
    label = CAT_LABELS[cat]
    if prefix == "meet":
        await state.set_state(Meeting.client_fio)
        await cb.message.edit_text(f"Категория: {label}\n\nВведите ФИО клиента:")
    elif prefix == "ko":
        await state.set_state(KO.client_fio)
        await cb.message.edit_text(f"Категория: {label}\n\nВведите ФИО клиента:")
    elif prefix == "pd":
        await state.set_state(PD.amount)
        await cb.message.edit_text(f"Категория: {label}\n\nВведите сумму сделки:")
    elif prefix == "od":
        await state.set_state(OD.amount)
        await cb.message.edit_text(f"Категория: {label}\n\nВведите сумму сделки:")
    await cb.answer()

# ============ ВСТРЕЧА ============
@dp.message(F.text == "📅 Встреча")
async def meeting_start(message: Message, state: FSMContext):
    if not await ensure_access(message): return
    await state.clear()
    await message.answer("Выберите категорию клиента:", reply_markup=category_kb("meet"))

@dp.message(Meeting.client_fio)
async def meeting_fio(message: Message, state: FSMContext):
    await state.update_data(client_fio=(message.text or "").strip())
    await state.set_state(Meeting.crm_id)
    await message.answer("Введите ID клиента из CRM (или '-', если нет):")

@dp.message(Meeting.crm_id)
async def meeting_crm(message: Message, state: FSMContext):
    data = await state.get_data()
    crm = (message.text or "").strip()
    if crm == "-": crm = None
    add_record(message.from_user.id, "meeting",
               client_fio=data.get("client_fio"),
               client_crm_id=crm,
               client_category=data.get("category"))
    await state.clear()
    await message.answer("✅ Встреча зафиксирована.",
                         reply_markup=main_menu(message.from_user.id))

# ============ КО ============
@dp.message(F.text == "🏢 КО в офисе")
async def ko_start(message: Message, state: FSMContext):
    if not await ensure_access(message): return
    await state.clear()
    await message.answer("Выберите категорию клиента:", reply_markup=category_kb("ko"))

@dp.message(KO.client_fio)
async def ko_fio(message: Message, state: FSMContext):
    await state.update_data(client_fio=(message.text or "").strip())
    await state.set_state(KO.crm_id)
    await message.answer("Введите ID клиента из CRM (или '-', если нет):")

@dp.message(KO.crm_id)
async def ko_crm(message: Message, state: FSMContext):
    data = await state.get_data()
    crm = (message.text or "").strip()
    if crm == "-": crm = None
    add_record(message.from_user.id, "ko",
               client_fio=data.get("client_fio"),
               client_crm_id=crm,
               client_category=data.get("category"))
    await state.clear()
    await message.answer("✅ КО зафиксировано.",
                         reply_markup=main_menu(message.from_user.id))

# ============ ОТКРЫЛ ПД ============
@dp.message(F.text == "📄 Открыл ПД")
async def pd_start(message: Message, state: FSMContext):
    if not await ensure_access(message): return
    await state.clear()
    await message.answer("Выберите категорию клиента:", reply_markup=category_kb("pd"))

@dp.message(PD.amount)
async def pd_amount(message: Message, state: FSMContext):
    try:
        amount = float((message.text or "").replace(",", ".").replace(" ", ""))
    except ValueError:
        await message.answer("Введите число.")
        return
    await state.update_data(deal_amount=amount)
    await state.set_state(PD.revenue)
    await message.answer("Введите сумму выручки со сделки:")

@dp.message(PD.revenue)
async def pd_revenue(message: Message, state: FSMContext):
    try:
        rev = float((message.text or "").replace(",", ".").replace(" ", ""))
    except ValueError:
        await message.answer("Введите число.")
        return
    await state.update_data(revenue=rev)
    await state.set_state(PD.business)
    await message.answer("Введите наименование бизнеса:")

@dp.message(PD.business)
async def pd_business(message: Message, state: FSMContext):
    data = await state.get_data()
    add_record(message.from_user.id, "pd",
               client_category=data.get("category"),
               deal_amount=data.get("deal_amount"),
               revenue=data.get("revenue"),
               business_name=(message.text or "").strip())
    await state.clear()
    await message.answer("✅ ПД заведён.",
                         reply_markup=main_menu(message.from_user.id))

# ============ ЗАКРЫЛ ОД ============
@dp.message(F.text == "✅ Закрыл ОД")
async def od_start(message: Message, state: FSMContext):
    if not await ensure_access(message): return
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Из открытого ПД", callback_data="od:from_pd")
    kb.button(text="🆕 Без ПД", callback_data="od:no_pd")
    kb.adjust(1)
    await message.answer("Выберите источник ОД:", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "od:from_pd")
async def od_from_pd(cb: CallbackQuery, state: FSMContext):
    with db() as conn:
        pds = conn.execute("""
            SELECT id, business_name, deal_amount
            FROM records
            WHERE user_id=? AND record_type='pd'
              AND id NOT IN (SELECT pd_id FROM records WHERE record_type='od' AND pd_id IS NOT NULL)
            ORDER BY id DESC
        """, (cb.from_user.id,)).fetchall()
    if not pds:
        await cb.message.edit_text("У вас нет открытых ПД.")
        await cb.answer()
        return
    kb = InlineKeyboardBuilder()
    for pd in pds:
        kb.button(text=f"#{pd['id']} {pd['business_name']} — {pd['deal_amount']}",
                  callback_data=f"od:pick:{pd['id']}")
    kb.adjust(1)
    await cb.message.edit_text("Выберите ПД для закрытия в ОД:", reply_markup=kb.as_markup())
    await cb.answer()

@dp.callback_query(F.data.startswith("od:pick:"))
async def od_pick(cb: CallbackQuery, state: FSMContext):
    pd_id = int(cb.data.split(":")[2])
    with db() as conn:
        pd = conn.execute("SELECT * FROM records WHERE id=?", (pd_id,)).fetchone()
    add_record(cb.from_user.id, "od",
               pd_id=pd_id,
               client_category=pd["client_category"],
               deal_amount=pd["deal_amount"],
               revenue=pd["revenue"],
               business_name=pd["business_name"])
    await cb.message.edit_text(f"✅ ОД закрыт из ПД #{pd_id}.")
    await cb.message.answer("Главное меню:", reply_markup=main_menu(cb.from_user.id))
    await cb.answer()

@dp.callback_query(F.data == "od:no_pd")
async def od_no_pd(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Выберите категорию клиента:", reply_markup=category_kb("od"))
    await cb.answer()

@dp.message(OD.amount)
async def od_amount(message: Message, state: FSMContext):
    try:
        amount = float((message.text or "").replace(",", ".").replace(" ", ""))
    except ValueError:
        await message.answer("Введите число.")
        return
    await state.update_data(deal_amount=amount)
    await state.set_state(OD.revenue)
    await message.answer("Введите сумму выручки со сделки:")

@dp.message(OD.revenue)
async def od_revenue(message: Message, state: FSMContext):
    try:
        rev = float((message.text or "").replace(",", ".").replace(" ", ""))
    except ValueError:
        await message.answer("Введите число.")
        return
    await state.update_data(revenue=rev)
    await state.set_state(OD.business)
    await message.answer("Введите наименование бизнеса:")

@dp.message(OD.business)
async def od_business(message: Message, state: FSMContext):
    data = await state.get_data()
    add_record(message.from_user.id, "od",
               client_category=data.get("category"),
               deal_amount=data.get("deal_amount"),
               revenue=data.get("revenue"),
               business_name=(message.text or "").strip())
    await state.clear()
    await message.answer("✅ ОД зафиксирован.",
                         reply_markup=main_menu(message.from_user.id))

# ============ ПЛАТНЫЙ ДВОУ / ДОУ ============
@dp.message(F.text == "💰 Платный ДВОУ")
async def dvou_start(message: Message, state: FSMContext):
    if not await ensure_access(message): return
    await state.clear()
    await state.update_data(rtype="dvou")
    await state.set_state(Payment.amount)
    await message.answer("Введите сумму ДВОУ:")

@dp.message(F.text == "💳 Платный ДОУ")
async def dou_start(message: Message, state: FSMContext):
    if not await ensure_access(message): return
    await state.clear()
    await state.update_data(rtype="dou")
    await state.set_state(Payment.amount)
    await message.answer("Введите сумму ДОУ:")

@dp.message(Payment.amount)
async def pay_amount(message: Message, state: FSMContext):
    try:
        amount = float((message.text or "").replace(",", ".").replace(" ", ""))
    except ValueError:
        await message.answer("Введите число.")
        return
    await state.update_data(deal_amount=amount)
    await state.set_state(Payment.client)
    await message.answer("Введите наименование или ID клиента из CRM:")

@dp.message(Payment.client)
async def pay_client(message: Message, state: FSMContext):
    data = await state.get_data()
    add_record(message.from_user.id, data["rtype"],
               deal_amount=data["deal_amount"],
               revenue=data["deal_amount"],
               client_fio=(message.text or "").strip())
    rtype = data["rtype"]
    await state.clear()
    await message.answer(f"✅ {TYPE_LABELS[rtype]} зафиксирован.",
                         reply_markup=main_menu(message.from_user.id))
    
# ============ ПУБЛИКАЦИИ ============
@dp.message(F.text == "📢 Публикации")
async def publication_start(message: Message, state: FSMContext):
    if not await ensure_access(message): return
    await state.clear()
    await state.set_state(Publication.amount)
    await message.answer("Введите сумму продажи объекта:")

@dp.message(Publication.amount)
async def publication_amount(message: Message, state: FSMContext):
    try:
        amount = float((message.text or "").replace(",", ".").replace(" ", ""))
    except ValueError:
        await message.answer("Введите число.")
        return
    add_record(message.from_user.id, "publication",
               deal_amount=amount)
    await state.clear()
    await message.answer("✅ Публикация зафиксирована.",
                         reply_markup=main_menu(message.from_user.id))
    
# ============ УДАЛЕНИЕ СВОЕГО ПД ============
@dp.message(F.text == "🗑 Удалить открытый ПД")
async def my_delpd(message: Message, state: FSMContext):
    if not await ensure_access(message): return
    with db() as conn:
        pds = conn.execute("""
            SELECT * FROM records
            WHERE user_id=? AND record_type='pd'
              AND id NOT IN (SELECT pd_id FROM records WHERE record_type='od' AND pd_id IS NOT NULL)
            ORDER BY id DESC
        """, (message.from_user.id,)).fetchall()
    if not pds:
        await message.answer("У вас нет открытых ПД.",
                             reply_markup=main_menu(message.from_user.id))
        return
    kb = InlineKeyboardBuilder()
    for pd in pds:
        kb.button(text=f"#{pd['id']} {pd['business_name']} ({pd['deal_amount']})",
                  callback_data=f"mydel:{pd['id']}")
    kb.adjust(1)
    await message.answer("Выберите ПД для удаления:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("mydel:"))
async def my_delpd_confirm(cb: CallbackQuery, state: FSMContext):
    pd_id = int(cb.data.split(":")[2])
    with db() as conn:
        row = conn.execute("SELECT * FROM records WHERE id=?", (pd_id,)).fetchone()
        if not row or row["user_id"] != cb.from_user.id:
            await cb.answer("Нет доступа", show_alert=True); return
        ref = conn.execute("SELECT 1 FROM records WHERE pd_id=? AND record_type='od'",
                           (pd_id,)).fetchone()
        if ref:
            await cb.answer("По этому ПД уже закрыт ОД — удаление невозможно",
                            show_alert=True); return
        conn.execute("DELETE FROM records WHERE id=?", (pd_id,))
    await cb.message.edit_text(f"✅ ПД #{pd_id} удалён.")
    await cb.answer()

# ============ ЛИЧНЫЙ КАБИНЕТ ============
async def show_personal(target, user_id, year, month, edit: bool):
    with db() as conn:
        rows = conn.execute("""
            SELECT record_type, COUNT(*) as c FROM records
            WHERE user_id=? AND strftime('%Y', created_at)=? AND strftime('%m', created_at)=?
            GROUP BY record_type
        """, (user_id, f"{year:04d}", f"{month:02d}")).fetchall()
    stats = {r["record_type"]: r["c"] for r in rows}
    text = (
        f"📊 <b>Личный кабинет за {month:02d}.{year}</b>\n\n"
        f"📅 Встречи: {stats.get('meeting', 0)}\n"
        f"🏢 КО: {stats.get('ko', 0)}\n"
        f"📄 ПД: {stats.get('pd', 0)}\n"
        f"✅ ОД: {stats.get('od', 0)}\n"
        f"💰 ДВОУ: {stats.get('dvou', 0)}\n"
        f"💳 ДОУ: {stats.get('dou', 0)}\n"
        f"📢 Публикации: {stats.get('publication', 0)}"
    )
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Пред. месяц", callback_data=f"pstats:{user_id}:{prev_y}:{prev_m}")
    kb.button(text="След. месяц ▶️", callback_data=f"pstats:{user_id}:{next_y}:{next_m}")
    kb.adjust(2)
    if edit:
        await target.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.message(F.text == "👤 Личный кабинет")
async def cabinet(message: Message, state: FSMContext):
    if not await ensure_access(message): return
    now = datetime.now()
    await show_personal(message, message.from_user.id, now.year, now.month, edit=False)

@dp.callback_query(F.data.startswith("pstats:"))
async def pstats_nav(cb: CallbackQuery):
    _, uid, y, m = cb.data.split(":")
    uid = int(uid)
    if uid != cb.from_user.id and not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return
    await show_personal(cb, uid, int(y), int(m), edit=True)
    await cb.answer()

# ============ ОБЩАЯ СТАТИСТИКА (АДМИН) ============
async def show_admin_stats(target, year, month, edit: bool):
    with db() as conn:
        rows = conn.execute("""
            SELECT u.fio, r.record_type, COUNT(*) as c
            FROM records r JOIN users u ON u.user_id=r.user_id
            WHERE strftime('%Y', r.created_at)=? AND strftime('%m', r.created_at)=?
            GROUP BY u.fio, r.record_type
        """, (f"{year:04d}", f"{month:02d}")).fetchall()
    data = defaultdict(lambda: defaultdict(int))
    for r in rows:
        data[r["fio"]][r["record_type"]] = r["c"]
    text = f"📊 <b>Общая статистика за {month:02d}.{year}</b>\n\n"
    if not data:
        text += "Нет данных."
    for fio, s in data.items():
        text += (f"<b>{fio}</b>\n"
                 f"  📅 {s.get('meeting',0)} | 🏢 {s.get('ko',0)} | "
                 f"📄 {s.get('pd',0)} | ✅ {s.get('od',0)} | "
                 f"💰 {s.get('dvou',0)} | 💳 {s.get('dou',0)} | "
                 f"📢 {s.get('publication',0)}\n\n")
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️", callback_data=f"astats:{prev_y}:{prev_m}")
    kb.button(text="▶️", callback_data=f"astats:{next_y}:{next_m}")
    kb.button(text="⬅️ В админку", callback_data="adm:back")
    kb.adjust(2, 1)
    if edit:
        await target.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.message(F.text == "📊 Общая статистика")
async def adm_stats_msg(message: Message):
    if not is_admin(message.from_user.id): return
    now = datetime.now()
    await show_admin_stats(message, now.year, now.month, edit=False)

@dp.callback_query(F.data.startswith("astats:"))
async def astats_nav(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return
    _, y, m = cb.data.split(":")
    await show_admin_stats(cb, int(y), int(m), edit=True)
    await cb.answer()

# ============ АДМИН-ПАНЕЛЬ ============
def admin_panel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Список пользователей", callback_data="adm:users")
    kb.button(text="📊 Статистика сотрудников", callback_data="adm:stats")
    kb.button(text="📥 Выгрузить в Excel", callback_data="adm:export")
    kb.button(text="🗑 Удалить открытый ПД", callback_data="adm:delpd")
    kb.adjust(1)
    return kb.as_markup()

@dp.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.clear()
    await message.answer("Админ-панель:", reply_markup=admin_panel_kb())

@dp.callback_query(F.data == "adm:back")
async def adm_back(cb: CallbackQuery):
    await cb.message.edit_text("Админ-панель:", reply_markup=admin_panel_kb())
    await cb.answer()

@dp.callback_query(F.data == "adm:users")
async def adm_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    with db() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY registered_at").fetchall()
    text = "👥 Пользователи:\n\n"
    kb = InlineKeyboardBuilder()
    for u in users:
        status = "✅" if u["is_active"] else "🚫"
        text += f"{status} <b>{u['fio']}</b> (ID: <code>{u['user_id']}</code>)\n"
        if u["user_id"] in ADMIN_IDS:
            continue
        if u["is_active"]:
            kb.button(text=f"🚫 Забрать доступ: {u['fio']}",
                      callback_data=f"adm:ban:{u['user_id']}")
        else:
            kb.button(text=f"✅ Вернуть доступ: {u['fio']}",
                      callback_data=f"adm:unban:{u['user_id']}")
    kb.button(text="⬅️ Назад", callback_data="adm:back")
    kb.adjust(1)
    await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await cb.answer()

@dp.callback_query(F.data.startswith("adm:ban:"))
async def adm_ban(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    uid = int(cb.data.split(":")[2])
    with db() as conn:
        conn.execute("UPDATE users SET is_active=0 WHERE user_id=?", (uid,))
    await cb.answer("Доступ удалён")
    await adm_users(cb)

@dp.callback_query(F.data.startswith("adm:unban:"))
async def adm_unban(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    uid = int(cb.data.split(":")[2])
    with db() as conn:
        conn.execute("UPDATE users SET is_active=1 WHERE user_id=?", (uid,))
    await cb.answer("Доступ восстановлен")
    await adm_users(cb)

@dp.callback_query(F.data == "adm:stats")
async def adm_stats_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    now = datetime.now()
    await show_admin_stats(cb, now.year, now.month, edit=True)
    await cb.answer()

@dp.callback_query(F.data == "adm:export")
async def adm_export(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    wb = Workbook()
    ws = wb.active
    ws.title = "Records"
    ws.append(["ID", "Сотрудник", "Тип", "Дата и время", "Категория клиента",
               "ФИО клиента", "CRM ID", "Сумма сделки", "Выручка",
               "Наименование бизнеса", "PD_ID"])
    with db() as conn:
        rows = conn.execute("""
            SELECT r.*, u.fio FROM records r
            LEFT JOIN users u ON u.user_id=r.user_id
            ORDER BY r.created_at
        """).fetchall()
    for r in rows:
        ws.append([
            r["id"], r["fio"] or "",
            TYPE_LABELS.get(r["record_type"], r["record_type"]),
            r["created_at"],
            CAT_LABELS.get(r["client_category"], r["client_category"] or ""),
            r["client_fio"] or "",
            r["client_crm_id"] or "",
            r["deal_amount"] or 0,
            r["revenue"] or 0,
            r["business_name"] or "",
            r["pd_id"] or "",
        ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    await cb.message.answer_document(
        BufferedInputFile(buf.read(),
                          filename=f"export_{datetime.now():%Y%m%d_%H%M}.xlsx")
    )
    await cb.answer("Файл отправлен")

@dp.callback_query(F.data == "adm:delpd")
async def adm_delpd(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    with db() as conn:
        pds = conn.execute("""
            SELECT r.*, u.fio FROM records r
            LEFT JOIN users u ON u.user_id=r.user_id
            WHERE r.record_type='pd'
              AND r.id NOT IN (SELECT pd_id FROM records WHERE record_type='od' AND pd_id IS NOT NULL)
            ORDER BY r.id DESC
        """).fetchall()
    if not pds:
        await cb.message.edit_text("Открытых ПД нет.", reply_markup=admin_panel_kb())
        await cb.answer(); return
    kb = InlineKeyboardBuilder()
    for pd in pds:
        kb.button(text=f"#{pd['id']} {pd['fio']} — {pd['business_name']} ({pd['deal_amount']})",
                  callback_data=f"adm:delpd_ok:{pd['id']}")
    kb.button(text="⬅️ Назад", callback_data="adm:back")
    kb.adjust(1)
    await cb.message.edit_text("Выберите ПД для удаления:", reply_markup=kb.as_markup())
    await cb.answer()

@dp.callback_query(F.data.startswith("adm:delpd_ok:"))
async def adm_delpd_ok(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    pd_id = int(cb.data.split(":")[2])
    with db() as conn:
        conn.execute("DELETE FROM records WHERE id=? AND record_type='pd'", (pd_id,))
    await cb.answer("ПД удалён")
    await adm_delpd(cb)

# ============ ЗАПУСК ============
async def main():
    db_init()
    bot = Bot(token=BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    print("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
