"""
Telegram-бот, который рисует картинки по текстовому запросу.

Фишки: запросы на русском (автоперевод), 13 стилей, 5 форматов, «ещё вариант», ×4 варианта альбомом,
смена стиля/формата у готовой картинки, избранное, история, «Удиви меня», скачивание файлом,
дневной лимит, работа в группах через /img.

Картинки рисует цепочка провайдеров — если один не ответил, пробуется следующий:
  1. Pollinations с ключом   (POLLINATIONS_KEY)            — много моделей, выбор через /model
  2. Cloudflare Workers AI    (CF_ACCOUNT_ID + CF_API_TOKEN) — FLUX.1 schnell, есть бесплатный дневной лимит
  3. Hugging Face             (HF_TOKEN)                     — FLUX.1 schnell
  4. Pollinations без ключа   (всегда)                       — бесплатно, но медленно и нестабильно

Запуск:  pip install -r requirements.txt  &&  BOT_TOKEN=... python bot.py
Render:  Build Command: pip install -r requirements.txt  ·  Start Command: python bot.py
"""
import asyncio
import base64
import hashlib
import html
import logging
import os
import random
import re
import sys
import time
from collections import OrderedDict
from datetime import date, datetime, timezone
from io import BytesIO
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton as B,
                           InlineKeyboardMarkup, InputMediaPhoto, Message, ReplyKeyboardMarkup, User as TgUser)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from PIL import Image
from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, String, Text, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ======================================================================
# НАСТРОЙКИ
# ======================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///images.db")
PORT = int(os.getenv("PORT", "8080"))
BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]

POLLINATIONS_KEY = os.getenv("POLLINATIONS_KEY", "")
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "")
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "")
HF_TOKEN = os.getenv("HF_TOKEN", "")

DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "30"))  # картинок в день на человека, 0 = без лимита
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.isdigit()}
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "3"))  # одновременных генераций на весь бот
FREE_INTERVAL = float(os.getenv("FREE_INTERVAL", "15"))  # пауза между запросами к бесплатному Pollinations

# Адреса API (вынесены, чтобы было удобно тестировать)
POLLINATIONS_GEN_URL = "https://gen.pollinations.ai/image/"
POLLINATIONS_FREE_URL = "https://image.pollinations.ai/prompt/"
CF_URL = "https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/"
HF_URL = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
GOOGLE_TR_URL = "https://translate.googleapis.com/translate_a/single"

log = logging.getLogger("imagebot")

# ======================================================================
# СТИЛИ, ФОРМАТЫ, МОДЕЛИ
# ======================================================================

STYLES = {
    "none": ("🚫 Без стиля", ""),
    "photo": ("📷 Фото", "professional photography, 35mm lens, natural light, sharp focus, highly detailed, photorealistic"),
    "cinema": ("🎬 Кино", "cinematic film still, dramatic lighting, shallow depth of field, anamorphic lens, color graded"),
    "anime": ("🌸 Аниме", "anime style illustration, vibrant colors, clean lineart, studio ghibli inspired, detailed background"),
    "3d": ("🧸 3D-мульт", "3d render, pixar style, cute, soft studio lighting, octane render, highly detailed"),
    "watercolor": ("🖌 Акварель", "watercolor painting, soft washes, paper texture, delicate brush strokes"),
    "oil": ("🖼 Масло", "oil painting, impressionism, thick visible brush strokes, rich colors, museum quality"),
    "pixel": ("👾 Пиксель-арт", "pixel art, 16-bit retro video game style, limited palette, crisp pixels"),
    "cyberpunk": ("🌆 Киберпанк", "cyberpunk style, neon lights, rainy night, futuristic, blade runner atmosphere"),
    "fantasy": ("🐉 Фэнтези", "epic fantasy digital painting, dramatic lighting, magical atmosphere, trending on artstation"),
    "sketch": ("✏️ Скетч", "pencil sketch, graphite, hand-drawn, black and white, detailed linework"),
    "logo": ("💠 Логотип", "minimalist flat vector logo, simple geometric shapes, plain background, professional branding"),
    "sticker": ("🏷 Стикер", "die-cut sticker, cute cartoon, bold outline, white border, plain background"),
}
RATIOS = {  # код (без двоеточия — оно запрещено в callback_data) -> (подпись, ширина, высота)
    "1x1": ("⬛ 1:1 квадрат", 1024, 1024),
    "16x9": ("🖥 16:9 широкий", 1344, 768),
    "9x16": ("📱 9:16 сторис", 768, 1344),
    "4x3": ("🖼 4:3", 1152, 864),
    "3x4": ("📄 3:4 портрет", 864, 1152),
}
MODELS = {  # доступны только с POLLINATIONS_KEY
    "zimage": ("⚡ Z-Image Turbo — быстро и дёшево", "tongyi-mai/z-image-turbo"),
    "flux": ("🌀 FLUX.1 schnell", "black-forest-labs/flux.1-schnell"),
    "klein": ("🌀 FLUX.2 klein", "black-forest-labs/flux.2-klein-4b"),
    "gptmini": ("🤖 GPT Image mini", "openai/gpt-image-1-mini"),
    "banana": ("🍌 Gemini Flash Image", "google/gemini-2.5-flash-image"),
    "seedream": ("🌱 Seedream 4", "bytedance/seedream-4.0"),
}

SURPRISES = [
    "Кот-космонавт пьёт чай на Луне", "Уютная библиотека внутри огромного дерева",
    "Город будущего на спине гигантской черепахи", "Лиса в свитере читает книгу у камина",
    "Подводный замок из кораллов и жемчуга", "Робот поливает цветы на крыше небоскрёба",
    "Дракон, свернувшийся клубком на горе из пончиков", "Маяк посреди моря облаков на закате",
    "Енот-детектив в плаще под дождём", "Остров-кит с маленькой деревней на спине",
    "Панда-самурай в бамбуковом лесу", "Поезд, летящий сквозь северное сияние",
    "Кофейня на облаке, где официанты — совы", "Сад светящихся грибов в ночном лесу",
    "Капибара-капитан на пиратском корабле", "Стеклянный шар с целой вселенной внутри",
    "Бабушкина кухня в стиле космической станции", "Горный храм над морем тумана",
    "Мопс-рыцарь в сияющих доспехах", "Жираф в очках работает бариста",
]
DRAW_FRAMES = ["🎨", "🖌", "🖍", "🖼", "✨"]
CYR = re.compile(r"[а-яА-ЯёЁіїєґІЇЄҐ]")

# ======================================================================
# БАЗА ДАННЫХ
# ======================================================================


def _prepare_url(url: str) -> tuple[str, dict]:
    connect_args: dict = {}
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+asyncpg://" + url[len(prefix):]
    if url.startswith("postgresql+asyncpg://"):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        mode = query.pop("sslmode", None)
        query.pop("channel_binding", None)
        if mode in ("require", "verify-ca", "verify-full"):
            connect_args["ssl"] = "require"
        url = urlunsplit(parts._replace(query=urlencode(query)))
    return url, connect_args


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_url, _args = _prepare_url(DATABASE_URL)
engine = create_async_engine(_url, connect_args=_args, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "img_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    style: Mapped[str] = mapped_column(String(32), default="none")
    ratio: Mapped[str] = mapped_column(String(8), default="1x1")
    model: Mapped[str] = mapped_column(String(32), default="zimage")
    day: Mapped[date | None] = mapped_column(Date, nullable=True)
    used: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Gen(Base):
    __tablename__ = "img_generations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    prompt: Mapped[str] = mapped_column(Text)
    prompt_en: Mapped[str] = mapped_column(Text, default="")
    style: Mapped[str] = mapped_column(String(32), default="none")
    ratio: Mapped[str] = mapped_column(String(8), default="1x1")
    seed: Mapped[int] = mapped_column(BigInteger, default=0)
    provider: Mapped[str] = mapped_column(String(64), default="")
    file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    fav: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_user(s, tg: TgUser) -> User:
    u = await s.get(User, tg.id)
    if u is None:
        u = User(id=tg.id, name=tg.first_name or "", style="none", ratio="1x1", model="zimage", used=0, total=0)
        s.add(u)
        await s.flush()
    return u


def left_today(u: User) -> int:
    if DAILY_LIMIT <= 0 or u.id in ADMIN_IDS:
        return 10 ** 6
    if u.day != utcnow().date():
        return DAILY_LIMIT
    return max(0, DAILY_LIMIT - u.used)


def spend(u: User, n: int) -> None:
    today = utcnow().date()
    if u.day != today:
        u.day, u.used = today, 0
    u.used += n
    u.total += n


# ======================================================================
# ПЕРЕВОД И ГЕНЕРАЦИЯ
# ======================================================================

class GenError(Exception):
    pass


_http: aiohttp.ClientSession | None = None


def http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180),
                                      headers={"User-Agent": "telegram-image-bot/1.0"})
    return _http


async def _tr_mymemory(text: str) -> str:
    if len(text) > 480:
        raise GenError("слишком длинно для MyMemory")
    async with http().get(MYMEMORY_URL, params={"q": text, "langpair": "ru|en"},
                          timeout=aiohttp.ClientTimeout(total=15)) as r:
        data = await r.json(content_type=None)
    res = (data.get("responseData") or {}).get("translatedText") or ""
    if data.get("responseStatus") != 200 or "MYMEMORY WARNING" in res:
        raise GenError(f"mymemory: {data.get('responseDetails')}")
    return html.unescape(res)


async def _tr_google(text: str) -> str:
    params = {"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": text}
    async with http().get(GOOGLE_TR_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        data = await r.json(content_type=None)
    return "".join(part[0] for part in data[0] if part and part[0])


async def _tr_cloudflare(text: str) -> str:
    if not (CF_ACCOUNT_ID and CF_API_TOKEN):
        raise GenError("нет ключей Cloudflare")
    url = CF_URL.format(acc=CF_ACCOUNT_ID) + "@cf/meta/m2m100-1.2b"
    async with http().post(url, json={"text": text, "source_lang": "russian", "target_lang": "english"},
                           headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
                           timeout=aiohttp.ClientTimeout(total=30)) as r:
        data = await r.json(content_type=None)
    return data["result"]["translated_text"]


async def translate(text: str) -> str:
    """Модели лучше понимают английский — переводим, если в запросе есть кириллица."""
    if not CYR.search(text):
        return text
    for fn in (_tr_mymemory, _tr_google, _tr_cloudflare):
        try:
            res = (await fn(text)).strip()
            if res and not CYR.search(res):
                return res
        except Exception as e:
            log.info("translator %s failed: %s", fn.__name__, e)
    return text


def build_prompt(prompt_en: str, style: str) -> str:
    suffix = STYLES.get(style, STYLES["none"])[1]
    return f"{prompt_en}, {suffix}" if suffix else prompt_en


async def gen_pollinations_key(prompt: str, w: int, h: int, seed: int, model: str) -> bytes:
    params = {"model": MODELS.get(model, MODELS["zimage"])[1], "width": w, "height": h, "seed": seed}
    async with http().get(POLLINATIONS_GEN_URL + quote(prompt, safe=""), params=params,
                          headers={"Authorization": f"Bearer {POLLINATIONS_KEY}"}) as r:
        if r.status != 200:
            raise GenError(f"HTTP {r.status}: {(await r.text())[:200]}")
        return await r.read()


async def gen_cloudflare(prompt: str, w: int, h: int, seed: int, model: str) -> bytes:
    url = CF_URL.format(acc=CF_ACCOUNT_ID) + "@cf/black-forest-labs/flux-1-schnell"
    async with http().post(url, json={"prompt": prompt[:2048], "steps": 8},
                           headers={"Authorization": f"Bearer {CF_API_TOKEN}"}) as r:
        data = await r.json(content_type=None)
    if not data.get("success") or not (data.get("result") or {}).get("image"):
        raise GenError(f"{data.get('errors') or data}"[:200])
    return base64.b64decode(data["result"]["image"])


async def gen_huggingface(prompt: str, w: int, h: int, seed: int, model: str) -> bytes:
    payload = {"inputs": prompt, "parameters": {"width": w, "height": h, "seed": seed, "num_inference_steps": 4}}
    async with http().post(HF_URL, json=payload, headers={"Authorization": f"Bearer {HF_TOKEN}"}) as r:
        if r.status != 200 or not r.content_type.startswith("image/"):
            raise GenError(f"HTTP {r.status}: {(await r.text())[:200]}")
        return await r.read()


_free_lock = asyncio.Lock()
_free_last = 0.0


async def gen_pollinations_free(prompt: str, w: int, h: int, seed: int, model: str) -> bytes:
    """Бесплатный анонимный Pollinations: ограничен по частоте, поэтому запросы идут по одному с паузой."""
    global _free_last
    params = {"width": w, "height": h, "seed": seed, "nologo": "true"}
    err = ""
    async with _free_lock:
        for attempt in range(3):
            wait = FREE_INTERVAL * (attempt + 1) if attempt else FREE_INTERVAL - (time.monotonic() - _free_last)
            if wait > 0:
                await asyncio.sleep(wait)
            _free_last = time.monotonic()
            try:
                async with http().get(POLLINATIONS_FREE_URL + quote(prompt, safe=""), params=params) as r:
                    if r.status == 200 and r.content_type.startswith("image/"):
                        return await r.read()
                    err = f"HTTP {r.status}"
                    if r.status not in (402, 429, 500, 502, 503, 504):
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                err = repr(e)
    raise GenError(err)


def providers() -> list[tuple[str, object]]:
    chain = []
    if POLLINATIONS_KEY:
        chain.append(("Pollinations", gen_pollinations_key))
    if CF_ACCOUNT_ID and CF_API_TOKEN:
        chain.append(("Cloudflare FLUX", gen_cloudflare))
    if HF_TOKEN:
        chain.append(("HuggingFace FLUX", gen_huggingface))
    chain.append(("Pollinations free", gen_pollinations_free))
    return chain


def fit_image(raw: bytes, w: int, h: int) -> bytes:
    """Проверяем, что пришла картинка, обрезаем под нужный формат и сжимаем в JPEG для Telegram."""
    try:
        img = Image.open(BytesIO(raw))
        img.load()
    except Exception:
        raise GenError("сервис вернул не картинку")
    img = img.convert("RGB")
    target, cur = w / h, img.width / img.height
    if abs(cur - target) / target > 0.03:
        if cur > target:
            nw = int(img.height * target)
            x = (img.width - nw) // 2
            img = img.crop((x, 0, x + nw, img.height))
        else:
            nh = int(img.width / target)
            y = (img.height - nh) // 2
            img = img.crop((0, y, img.width, y + nh))
    buf = BytesIO()
    img.save(buf, "JPEG", quality=93, optimize=True)
    return buf.getvalue()


async def generate(prompt_en: str, style: str, ratio: str, seed: int, model: str) -> tuple[bytes, bytes, str]:
    """-> (jpeg для отправки фото, оригинал, имя провайдера)"""
    _, w, h = RATIOS.get(ratio, RATIOS["1x1"])
    prompt = build_prompt(prompt_en, style)
    errors = []
    for name, fn in providers():
        try:
            raw = await fn(prompt, w, h, seed, model)
            return fit_image(raw, w, h), raw, name
        except Exception as e:
            log.warning("provider %s failed: %s", name, e)
            errors.append(f"{name}: {e}")
    raise GenError("; ".join(errors))


# Оригиналы последних картинок — для кнопки «Файлом»
ORIGINALS: OrderedDict[int, bytes] = OrderedDict()


def remember_original(gen_id: int, data: bytes) -> None:
    ORIGINALS[gen_id] = data
    while len(ORIGINALS) > 40:
        ORIGINALS.popitem(last=False)


# ======================================================================
# КЛАВИАТУРЫ
# ======================================================================

class GenCb(CallbackData, prefix="g"):
    a: str
    id: int
    x: str = ""


class SetCb(CallbackData, prefix="s"):
    a: str
    v: str


BTN_SURPRISE, BTN_STYLE, BTN_RATIO = "🎲 Удиви меня", "🎨 Стиль", "📐 Формат"
BTN_FAVS, BTN_HISTORY, BTN_PROFILE, BTN_MODEL = "❤️ Избранное", "🕘 История", "👤 Профиль", "🤖 Модель"


def esc(s: str | None) -> str:
    return html.escape(s or "")


def ratio_name(code: str) -> str:
    return code.replace("x", ":")


def main_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    buttons = [BTN_SURPRISE, BTN_STYLE, BTN_RATIO, BTN_FAVS, BTN_HISTORY, BTN_PROFILE]
    if POLLINATIONS_KEY:
        buttons.append(BTN_MODEL)
    for t in buttons:
        kb.button(text=t)
    kb.adjust(3, 3, 1)
    return kb.as_markup(resize_keyboard=True, is_persistent=True,
                        input_field_placeholder="Опиши картинку, например: кот в скафандре")


def image_kb(g: Gen) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Ещё вариант", callback_data=GenCb(a="again", id=g.id))
    kb.button(text="🎲 ×4", callback_data=GenCb(a="x4", id=g.id))
    kb.button(text="🎨 Другой стиль", callback_data=GenCb(a="stylemenu", id=g.id))
    kb.button(text="📐 Формат", callback_data=GenCb(a="ratiomenu", id=g.id))
    kb.button(text="❤️ В избранном" if g.fav else "🤍 В избранное", callback_data=GenCb(a="fav", id=g.id))
    kb.button(text="📎 Файлом", callback_data=GenCb(a="file", id=g.id))
    kb.button(text="🔤 Промпт", callback_data=GenCb(a="prompt", id=g.id))
    kb.adjust(2, 2, 3)
    return kb.as_markup()


def restyle_kb(g: Gen) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, (label, _) in STYLES.items():
        kb.button(text=("• " if code == g.style else "") + label, callback_data=GenCb(a="restyle", id=g.id, x=code))
    kb.button(text="✖ Назад", callback_data=GenCb(a="back", id=g.id))
    kb.adjust(3)
    return kb.as_markup()


def reratio_kb(g: Gen) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, (label, _, _) in RATIOS.items():
        kb.button(text=("• " if code == g.ratio else "") + label, callback_data=GenCb(a="reratio", id=g.id, x=code))
    kb.button(text="✖ Назад", callback_data=GenCb(a="back", id=g.id))
    kb.adjust(2)
    return kb.as_markup()


def settings_kb(kind: str, current: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    items = {"style": {k: v[0] for k, v in STYLES.items()},
             "ratio": {k: v[0] for k, v in RATIOS.items()},
             "model": {k: v[0] for k, v in MODELS.items()}}[kind]
    for code, label in items.items():
        kb.button(text=("✅ " if code == current else "") + label, callback_data=SetCb(a=kind, v=code))
    kb.adjust({"style": 3, "ratio": 2, "model": 1}[kind])
    return kb.as_markup()


def batch_kb(gens: list[Gen]) -> InlineKeyboardMarkup:
    rows = [[B(text=f"🤍 {i}", callback_data=GenCb(a="favb", id=g.id).pack()) for i, g in enumerate(gens, 1)],
            [B(text=f"📎 {i}", callback_data=GenCb(a="file", id=g.id).pack()) for i, g in enumerate(gens, 1)],
            [B(text="🎲 Ещё 4 варианта", callback_data=GenCb(a="x4", id=gens[0].id).pack())]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ======================================================================
# ГЕНЕРАЦИЯ В ЧАТЕ
# ======================================================================

SEM = asyncio.Semaphore(MAX_PARALLEL)


def caption(g: Gen, left: int) -> str:
    text = (f"🎨 <b>{esc(g.prompt[:700])}</b>\n"
            f"<i>{STYLES.get(g.style, STYLES['none'])[0]} · {ratio_name(g.ratio)} · {esc(g.provider)}</i>")
    if left < 10 ** 5:
        text += f"\nОсталось сегодня: {left}"
    return text


async def animate(bot: Bot, status: Message, text: str, progress: dict | None = None) -> None:
    """Пока рисуем — крутим эмодзи и показываем «отправляет фото…»."""
    i = 0
    try:
        while True:
            await bot.send_chat_action(status.chat.id, ChatAction.UPLOAD_PHOTO)
            await asyncio.sleep(3)
            i += 1
            extra = f"\nГотово {progress['done']}/{progress['total']}" if progress else ""
            bar = "▰" * (i % 8) + "▱" * (7 - i % 8)
            try:
                await bot.edit_message_text(f"{DRAW_FRAMES[i % len(DRAW_FRAMES)]} {text}\n{bar}{extra}",
                                            chat_id=status.chat.id, message_id=status.message_id)
            except TelegramBadRequest:
                pass
    except asyncio.CancelledError:
        pass


async def edit_status(bot: Bot, status: Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    try:
        await bot.edit_message_text(text, chat_id=status.chat.id, message_id=status.message_id, reply_markup=kb)
    except TelegramBadRequest:
        await bot.send_message(status.chat.id, text, reply_markup=kb)


async def delete_status(bot: Bot, status: Message) -> None:
    try:
        await bot.delete_message(status.chat.id, status.message_id)
    except TelegramBadRequest:
        pass


def status_text(prompt: str, style: str, ratio: str, n: int = 1) -> str:
    what = "Рисую" if n == 1 else f"Рисую {n} варианта"
    return f"{what}…\n<i>{esc(prompt[:200])}</i>\n{STYLES[style][0]} · {ratio_name(ratio)}"


async def check_limit(bot: Bot, chat_id: int, tg: TgUser, need: int) -> tuple[User, int] | None:
    async with Session() as s:
        u = await get_user(s, tg)
        await s.commit()
    left = left_today(u)
    if left < need:
        msg = "🚫 Лимит на сегодня исчерпан. Приходи завтра!" if left == 0 else \
            f"🚫 Сегодня осталось генераций: {left} — на {need} не хватит."
        await bot.send_message(chat_id, msg)
        return None
    return u, left


async def run_generation(bot: Bot, chat_id: int, tg: TgUser, prompt: str,
                         style: str | None = None, ratio: str | None = None) -> None:
    prompt = prompt.strip()[:1000]
    res = await check_limit(bot, chat_id, tg, 1)
    if not res:
        return
    u, _ = res
    style, ratio = style or u.style, ratio or u.ratio
    status = await bot.send_message(chat_id, f"🎨 {status_text(prompt, style, ratio)}")
    anim = asyncio.create_task(animate(bot, status, status_text(prompt, style, ratio)))
    seed = random.randint(1, 2 ** 31 - 1)
    async with Session() as s:
        g = Gen(user_id=u.id, prompt=prompt, style=style, ratio=ratio, seed=seed, fav=False)
        s.add(g)
        await s.commit()
    try:
        async with SEM:
            g.prompt_en = await translate(prompt)
            photo, original, provider = await generate(g.prompt_en, style, ratio, seed, u.model)
    except Exception as e:
        anim.cancel()
        log.warning("generation %s failed: %s", g.id, e)
        kb = InlineKeyboardMarkup(inline_keyboard=[[B(text="🔁 Попробовать ещё раз",
                                                      callback_data=GenCb(a="again", id=g.id).pack())]])
        await edit_status(bot, status, "😔 Не получилось нарисовать — все сервисы сейчас заняты или недоступны.\n"
                                       "Попробуй ещё раз через минуту.", kb)
        return
    anim.cancel()
    async with Session() as s:
        u = await s.get(User, u.id)
        spend(u, 1)
        g.provider = provider
        sent = await bot.send_photo(chat_id, BufferedInputFile(photo, f"image-{g.id}.jpg"),
                                    caption=caption(g, left_today(u)), reply_markup=image_kb(g))
        g.file_id = sent.photo[-1].file_id
        await s.merge(g)
        await s.commit()
    remember_original(g.id, original)
    await delete_status(bot, status)


async def run_batch(bot: Bot, chat_id: int, tg: TgUser, prompt: str, style: str, ratio: str, n: int = 4) -> None:
    res = await check_limit(bot, chat_id, tg, n)
    if not res:
        return
    u, _ = res
    text = status_text(prompt, style, ratio, n)
    status = await bot.send_message(chat_id, f"🎨 {text}")
    progress = {"done": 0, "total": n}
    anim = asyncio.create_task(animate(bot, status, text, progress))
    prompt_en = await translate(prompt)

    async def one():
        seed = random.randint(1, 2 ** 31 - 1)
        async with SEM:
            out = await generate(prompt_en, style, ratio, seed, u.model)
        progress["done"] += 1
        return seed, out

    results = await asyncio.gather(*(one() for _ in range(n)), return_exceptions=True)
    anim.cancel()
    ok = [r for r in results if not isinstance(r, BaseException)]
    if not ok:
        await edit_status(bot, status, "😔 Не получилось нарисовать ни одного варианта. Попробуй позже.")
        return
    async with Session() as s:
        gens = [Gen(user_id=u.id, prompt=prompt, prompt_en=prompt_en, style=style, ratio=ratio,
                    seed=seed, provider=prov, fav=False) for seed, (_, _, prov) in ok]
        s.add_all(gens)
        await s.flush()
        album_caption = f"🎲 <b>{esc(prompt[:700])}</b>\n<i>{STYLES[style][0]} · {ratio_name(ratio)}</i>"
        media = [InputMediaPhoto(media=BufferedInputFile(photo, f"v{i}.jpg"), caption=album_caption if i == 1 else None)
                 for i, (_, (photo, _, _)) in enumerate(ok, 1)]
        sent = await bot.send_media_group(chat_id, media)
        for g, m, (_, (_, orig, _)) in zip(gens, sent, ok):
            g.file_id = m.photo[-1].file_id
            remember_original(g.id, orig)
        u = await s.get(User, u.id)
        spend(u, len(ok))
        await s.commit()
    await bot.send_message(chat_id, f"Какой нравится? ❤️ — в избранное, 📎 — скачать файлом\n"
                                    f"Осталось сегодня: {left_today(u) if left_today(u) < 10 ** 5 else '∞'}",
                           reply_markup=batch_kb(gens))
    await delete_status(bot, status)


def spawn(coro) -> None:
    """Генерация идёт в фоне, чтобы не держать webhook-запрос Telegram открытым."""
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


_BACKGROUND: set[asyncio.Task] = set()

# ======================================================================
# ОБРАБОТЧИКИ
# ======================================================================

router = Router(name="main")

HELP = """<b>Я рисую картинки по описанию 🎨</b>

Просто напиши, что нарисовать — можно по-русски:
• <code>кот-космонавт пьёт чай на Луне</code>
• <code>уютная кофейня в дождливом Париже</code>
• <code>логотип для пекарни «Булочка»</code>

<b>Под каждой картинкой:</b>
🔄 ещё вариант · 🎲 сразу 4 варианта · 🎨 перерисовать в другом стиле
📐 другой формат · ❤️ в избранное · 📎 скачать файлом · 🔤 показать промпт

<b>Команды:</b>
/img <i>текст</i> — нарисовать (работает и в группах)
/x4 <i>текст</i> — 4 варианта сразу
/style — стиль по умолчанию (аниме, фото, пиксель-арт…)
/ratio — формат: квадрат, 16:9, сторис 9:16…
/surprise — случайная идея 🎲
/favorites — избранное · /history — история
/me — профиль и лимиты"""


@router.message(CommandStart())
async def start(message: Message):
    async with Session() as s:
        await get_user(s, message.from_user)
        await s.commit()
    await message.answer(
        f"Привет, {esc(message.from_user.first_name)}! 👋\n\n"
        "Я нарисую любую картинку по твоему описанию. Просто напиши, что хочешь увидеть, например:\n"
        "<code>енот-детектив в плаще под дождём</code>\n\n"
        "Выбери стиль 🎨 и формат 📐 в меню, а если нет идей — жми 🎲 <b>Удиви меня</b>\n\nВсе возможности — /help",
        reply_markup=main_menu())


@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(HELP, reply_markup=main_menu() if message.chat.type == "private" else None)


@router.message(Command("style"))
@router.message(F.text == BTN_STYLE)
async def style_cmd(message: Message):
    async with Session() as s:
        u = await get_user(s, message.from_user)
        await s.commit()
    await message.answer("🎨 <b>Стиль по умолчанию</b> — будет добавляться ко всем запросам:",
                         reply_markup=settings_kb("style", u.style))


@router.message(Command("ratio"))
@router.message(F.text == BTN_RATIO)
async def ratio_cmd(message: Message):
    async with Session() as s:
        u = await get_user(s, message.from_user)
        await s.commit()
    await message.answer("📐 <b>Формат картинок:</b>", reply_markup=settings_kb("ratio", u.ratio))


@router.message(Command("model"))
@router.message(F.text == BTN_MODEL)
async def model_cmd(message: Message):
    if not POLLINATIONS_KEY:
        return await message.answer("Выбор модели доступен, когда у бота задан POLLINATIONS_KEY.")
    async with Session() as s:
        u = await get_user(s, message.from_user)
        await s.commit()
    await message.answer("🤖 <b>Модель для рисования:</b>", reply_markup=settings_kb("model", u.model))


@router.callback_query(SetCb.filter())
async def settings_cb(cb: CallbackQuery, callback_data: SetCb):
    kind, value = callback_data.a, callback_data.v
    valid = {"style": STYLES, "ratio": RATIOS, "model": MODELS}.get(kind, {})
    if value not in valid:
        return await cb.answer()
    async with Session() as s:
        u = await get_user(s, cb.from_user)
        setattr(u, kind, value)
        await s.commit()
    try:
        await cb.message.edit_reply_markup(reply_markup=settings_kb(kind, value))
    except TelegramBadRequest:
        pass
    await cb.answer(f"Готово: {valid[value][0]}")


@router.message(Command("me"))
@router.message(F.text == BTN_PROFILE)
async def me_cmd(message: Message):
    async with Session() as s:
        u = await get_user(s, message.from_user)
        favs = await s.scalar(select(func.count()).where(Gen.user_id == u.id, Gen.fav.is_(True))) or 0
        await s.commit()
    left = left_today(u)
    limit = "без ограничений ♾" if left >= 10 ** 5 else f"{left} из {DAILY_LIMIT}"
    text = (f"👤 <b>{esc(u.name)}</b>\n\n"
            f"🖼 Нарисовано всего: <b>{u.total}</b>\n"
            f"⏳ Осталось сегодня: <b>{limit}</b>\n"
            f"❤️ В избранном: <b>{favs}</b>\n\n"
            f"🎨 Стиль: {STYLES[u.style][0]}\n📐 Формат: {RATIOS[u.ratio][0]}")
    if POLLINATIONS_KEY:
        text += f"\n🤖 Модель: {MODELS.get(u.model, MODELS['zimage'])[0]}"
    await message.answer(text)


@router.message(Command("surprise"))
@router.message(F.text == BTN_SURPRISE)
async def surprise_cmd(message: Message, bot: Bot):
    idea = random.choice(SURPRISES)
    style = random.choice([k for k in STYLES if k not in ("none", "logo", "sticker")])
    await message.answer(f"🎲 Идея: <b>{idea}</b>\nСтиль: {STYLES[style][0]}")
    spawn(run_generation(bot, message.chat.id, message.from_user, idea, style=style))


@router.message(Command("img", "draw", "i"))
async def img_cmd(message: Message, command: CommandObject, bot: Bot):
    if not command.args:
        return await message.answer("Напиши, что нарисовать: <code>/img закат над морем</code>")
    spawn(run_generation(bot, message.chat.id, message.from_user, command.args))


@router.message(Command("x4"))
async def x4_cmd(message: Message, command: CommandObject, bot: Bot):
    if not command.args:
        return await message.answer("Напиши, что нарисовать: <code>/x4 домик в горах</code>")
    async with Session() as s:
        u = await get_user(s, message.from_user)
        await s.commit()
    spawn(run_batch(bot, message.chat.id, message.from_user, command.args.strip()[:1000], u.style, u.ratio))


@router.message(Command("favorites"))
@router.message(F.text == BTN_FAVS)
async def favorites_cmd(message: Message):
    async with Session() as s:
        gens = (await s.scalars(select(Gen).where(Gen.user_id == message.from_user.id, Gen.fav.is_(True),
                                                  Gen.file_id.is_not(None))
                                .order_by(Gen.id.desc()).limit(20))).all()
    if not gens:
        return await message.answer("❤️ Избранное пусто. Жми 🤍 под картинками, чтобы сохранить их сюда.")
    await message.answer(f"❤️ <b>Избранное</b> — последние {len(gens)}:")
    for i in range(0, len(gens), 10):
        chunk = gens[i:i + 10]
        if len(chunk) == 1:
            await message.answer_photo(chunk[0].file_id, caption=esc(chunk[0].prompt[:900]))
        else:
            await message.answer_media_group([InputMediaPhoto(media=g.file_id, caption=esc(g.prompt[:900]))
                                              for g in chunk])


@router.message(Command("history"))
@router.message(F.text == BTN_HISTORY)
async def history_cmd(message: Message):
    async with Session() as s:
        gens = (await s.scalars(select(Gen).where(Gen.user_id == message.from_user.id, Gen.file_id.is_not(None))
                                .order_by(Gen.id.desc()).limit(10))).all()
    if not gens:
        return await message.answer("🕘 История пуста — самое время что-нибудь нарисовать!")
    lines = ["🕘 <b>Последние запросы</b>\n"]
    lines += [f"{i}. {esc(g.prompt[:80])} <i>({STYLES.get(g.style, STYLES['none'])[0]})</i>"
              for i, g in enumerate(gens, 1)]
    lines.append("\nНажми номер, чтобы открыть картинку:")
    kb = InlineKeyboardBuilder()
    for i, g in enumerate(gens, 1):
        kb.button(text=str(i), callback_data=GenCb(a="show", id=g.id))
    kb.adjust(5)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.message(F.photo | F.sticker | F.document | F.voice | F.video)
async def not_text(message: Message):
    if message.chat.type == "private":
        await message.answer("Пока я рисую только по текстовому описанию ✍️ Напиши, что нарисовать!")


@router.message(F.text, F.chat.type == "private")
async def text_prompt(message: Message, bot: Bot):
    if message.text.startswith("/"):
        return await message.answer("Не знаю такой команды 🤔 Загляни в /help")
    spawn(run_generation(bot, message.chat.id, message.from_user, message.text))


# ---------- Кнопки под картинками ----------

async def load_gen(user_id: int, gen_id: int) -> Gen | None:
    async with Session() as s:
        g = await s.get(Gen, gen_id)
    return g if g and g.user_id == user_id else None


@router.callback_query(GenCb.filter())
async def gen_buttons(cb: CallbackQuery, callback_data: GenCb, bot: Bot):
    a = callback_data.a
    g = await load_gen(cb.from_user.id, callback_data.id)
    if not g:
        return await cb.answer("Это не твоя картинка или она удалена 🙈", show_alert=True)
    chat_id = cb.message.chat.id

    if a == "again":
        await cb.answer("Рисую новый вариант 🎨")
        if not g.file_id:  # это была неудачная попытка — убираем сообщение об ошибке
            try:
                await cb.message.delete()
            except TelegramBadRequest:
                pass
        spawn(run_generation(bot, chat_id, cb.from_user, g.prompt, g.style, g.ratio))
    elif a == "x4":
        await cb.answer("Рисую 4 варианта 🎲")
        spawn(run_batch(bot, chat_id, cb.from_user, g.prompt, g.style, g.ratio))
    elif a == "stylemenu":
        await cb.message.edit_reply_markup(reply_markup=restyle_kb(g))
        await cb.answer("В каком стиле перерисовать?")
    elif a == "ratiomenu":
        await cb.message.edit_reply_markup(reply_markup=reratio_kb(g))
        await cb.answer("Какой формат?")
    elif a == "restyle" and callback_data.x in STYLES:
        await cb.message.edit_reply_markup(reply_markup=image_kb(g))
        await cb.answer(f"Перерисовываю: {STYLES[callback_data.x][0]}")
        spawn(run_generation(bot, chat_id, cb.from_user, g.prompt, callback_data.x, g.ratio))
    elif a == "reratio" and callback_data.x in RATIOS:
        await cb.message.edit_reply_markup(reply_markup=image_kb(g))
        await cb.answer(f"Перерисовываю в {ratio_name(callback_data.x)}")
        spawn(run_generation(bot, chat_id, cb.from_user, g.prompt, g.style, callback_data.x))
    elif a == "back":
        await cb.message.edit_reply_markup(reply_markup=image_kb(g))
        await cb.answer()
    elif a in ("fav", "favb"):
        async with Session() as s:
            g = await s.get(Gen, g.id)
            g.fav = not g.fav
            await s.commit()
        if a == "fav":
            await cb.message.edit_reply_markup(reply_markup=image_kb(g))
        elif cb.message.reply_markup:  # кнопки под альбомом: перерисуем сердечко у нужного номера
            rows = [list(r) for r in cb.message.reply_markup.inline_keyboard]
            rows[0] = [btn.model_copy(update={"text": ("❤️ " if g.fav else "🤍 ") + btn.text.split(" ", 1)[-1]})
                       if btn.callback_data and GenCb.unpack(btn.callback_data).id == g.id else btn
                       for btn in rows[0]]
            try:
                await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
            except TelegramBadRequest:
                pass
        await cb.answer("Добавлено в избранное ❤️" if g.fav else "Убрано из избранного")
    elif a == "file":
        await cb.answer("Отправляю файл 📎")
        data = ORIGINALS.get(g.id)
        if data is None and g.file_id:
            buf = await bot.download(g.file_id)
            data = buf.read()
        if data:
            ext = "png" if data[:4] == b"\x89PNG" else "jpg"
            await bot.send_document(chat_id, BufferedInputFile(data, f"image-{g.id}.{ext}"),
                                    caption=esc(g.prompt[:900]))
    elif a == "prompt":
        await cb.answer()
        await bot.send_message(chat_id, f"🔤 <b>Промпт, который ушёл в нейросеть:</b>\n"
                                        f"<code>{esc(build_prompt(g.prompt_en or g.prompt, g.style))}</code>\n\n"
                                        f"seed: <code>{g.seed}</code>")
    elif a == "show" and g.file_id:
        await cb.answer()
        await bot.send_photo(chat_id, g.file_id, caption=caption(g, 10 ** 6), reply_markup=image_kb(g))
    else:
        await cb.answer()


# ======================================================================
# ЗАПУСК
# ======================================================================

COMMANDS = [
    ("img", "🎨 Нарисовать по описанию"), ("x4", "🎲 4 варианта сразу"), ("surprise", "✨ Удиви меня"),
    ("style", "🖌 Стиль по умолчанию"), ("ratio", "📐 Формат картинки"), ("favorites", "❤️ Избранное"),
    ("history", "🕘 История"), ("me", "👤 Профиль и лимиты"), ("help", "❓ Помощь"),
]


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return dp


async def on_startup(bot: Bot, dispatcher: Dispatcher) -> None:
    await init_db()
    commands = COMMANDS + ([("model", "🤖 Выбор модели")] if POLLINATIONS_KEY else [])
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in commands])
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}{WEBHOOK_PATH}", secret_token=WEBHOOK_SECRET,
                              allowed_updates=dispatcher.resolve_used_update_types())
        log.info("webhook set to %s%s", BASE_URL, WEBHOOK_PATH)
    log.info("providers: %s", ", ".join(name for name, _ in providers()))


async def on_shutdown() -> None:
    if _http and not _http.closed:
        await _http.close()


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        sys.exit("Не задан BOT_TOKEN")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    if not BASE_URL:
        log.info("Нет публичного URL — запускаю polling")
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
        return

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("listening on :%s", PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
