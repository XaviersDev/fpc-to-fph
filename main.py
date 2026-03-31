from fastapi import FastAPI, Request
from fastapi.responses import Response, FileResponse
import re
import io
import zipfile
import json
import httpx

app = FastAPI()

FPTOOLS_API_URL = "https://fptools.onrender.com/api/ai"
FPTOOLS_API_KEY = "fptoolsdim"

AI_SYSTEM_PROMPT = (
    "You are a Python code migration expert. "
    "Convert telebot/pyTelegramBotAPI code to aiogram 3. "
    "Return ONLY the converted Python code. No explanations, no markdown fences."
)

FSM_DETECTION_PATTERN = re.compile(
    r'StatesGroup|state\.set\(|state\.next\(|state\.finish\(|'
    r'dp\.register_message_handler|dp\.register_callback_query_handler|'
    r'State\(\)|@dp\.message_handler|@dp\.callback_query_handler',
    re.MULTILINE,
)


@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")


@app.post("/api/convert")
async def convert_plugin(request: Request):
    body = await request.json()
    code = body.get("code", "")

    if not code:
        return Response(content="No code provided", status_code=400)

    name     = extract_meta(code, r'NAME\s*=\s*["\']([^"\']+)["\']', "ConvertedPlugin")
    version  = extract_meta(code, r'VERSION\s*=\s*["\']([^"\']+)["\']', "1.0.0")
    desc     = extract_meta(code, r'DESCRIPTION\s*=\s*["\']([^"\']+)["\']', "Converted to FPH")
    credits_ = extract_meta(code, r'CREDITS\s*=\s*["\']([^"\']+)["\']', "AutoConverter")

    plugin_id = "com.converted." + re.sub(r'[^a-z0-9]', '', name.lower())

    manifest = {
        "manifest": 1,
        "plugin_id": plugin_id,
        "name": name,
        "description": desc,
        "plugin_version": version,
        "app_version": ">=0.5.0",
        "entry_point": "main.ConvertedPlugin",
        "author": {"name": credits_},
    }

    hooks = {
        "pre_init":     extract_list(code, r'BIND_TO_PRE_INIT\s*=\s*\[([^\]]+)\]'),
        "post_start":   extract_list(code, r'BIND_TO_POST_START\s*=\s*\[([^\]]+)\]'),
        "new_message":  extract_list(code, r'BIND_TO_NEW_MESSAGE\s*=\s*\[([^\]]+)\]'),
        "new_order":    extract_list(code, r'BIND_TO_NEW_ORDER\s*=\s*\[([^\]]+)\]'),
        "order_status": extract_list(code, r'BIND_TO_ORDER_STATUS_CHANGED\s*=\s*\[([^\]]+)\]'),
    }

    warnings = []
    clean_code = convert_code(code, plugin_id, warnings)

    if FSM_DETECTION_PATTERN.search(clean_code) or _has_unresolved_todos(clean_code):
        ai_result = await ai_fix_hard_cases(clean_code)
        if ai_result:
            clean_code = ai_result
        else:
            warnings.append("ИИ-доводка недоступна (сервер fptools) — проверьте FSM и # TODO вручную.")

    main_py = build_main_py(clean_code, hooks, plugin_id, name, warnings)

    plugin_folder_name = plugin_id.replace('.', '_')
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{plugin_folder_name}/manifest.json", json.dumps(manifest, indent=4))
        zf.writestr(f"{plugin_folder_name}/__init__.py", "")
        zf.writestr(f"{plugin_folder_name}/main.py", main_py)
        if warnings:
            zf.writestr(
                f"{plugin_folder_name}/CONVERSION_WARNINGS.txt",
                "ТРЕБУЕТ РУЧНОЙ ПРОВЕРКИ:\n\n" + "\n".join(f"- {w}" for w in warnings),
            )

    zip_buffer.seek(0)
    return Response(
        content=zip_buffer.read(),
        headers={
            "Content-Disposition": f'attachment; filename="{plugin_folder_name}_fph.zip"',
            "Content-Type": "application/zip",
        },
    )


async def ai_fix_hard_cases(code: str) -> str | None:
    prompt = (
        "The following Python plugin code has been partially migrated from telebot to aiogram 3.\n"
        "Complete the migration:\n"
        "- Convert any remaining FSM (StatesGroup, State, .set(), .next(), .finish()) to aiogram 3 FSM (StatesGroup, State, FSMContext)\n"
        "- Convert @dp.register_* to @tg_router.* decorators\n"
        "- Convert any remaining @dp.message_handler / @dp.callback_query_handler decorators\n"
        "- Replace # TODO comments with correct aiogram 3 code\n"
        "- Keep all FunPayHub, fp_router, tg_router references intact\n"
        "- Do NOT change imports block at the top\n\n"
        "CODE:\n"
        f"{code}"
    )

    payload = {
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "modelName": "ChatGPT 4o",
        "currentPagePath": "/chatgpt-4o",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                FPTOOLS_API_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {FPTOOLS_API_KEY}",
                },
            )
        if response.status_code == 200:
            data = response.json()
            if data.get("response"):
                result = data["response"].strip()
                result = re.sub(r'^```python\s*', '', result)
                result = re.sub(r'^```\s*', '', result)
                result = re.sub(r'\s*```$', '', result)
                return result.strip()
    except Exception:
        pass
    return None


def _has_unresolved_todos(code: str) -> bool:
    return bool(re.search(r'#\s*TODO:', code))


def convert_code(code: str, plugin_id: str, warnings: list) -> str:
    c = code
    c = re.sub(r'^\s*from\s+cardinal\s+import\s+Cardinal.*$', '', c, flags=re.MULTILINE)
    c = re.sub(r'^\s*import\s+telebot.*$', '', c, flags=re.MULTILINE)
    c = re.sub(r'^\s*from\s+telebot.*$', '', c, flags=re.MULTILINE)
    c = re.sub(r'^(NAME|VERSION|DESCRIPTION|CREDITS|UUID|SETTINGS_PAGE)\s*=.*$', '', c, flags=re.MULTILINE)
    c = re.sub(r'^BIND_TO_[A-Z_]+\s*=\s*\[.*?\]$', '', c, flags=re.MULTILINE)
    c = c.replace('time.sleep(', 'await asyncio.sleep(')
    c = re.sub(r'\bc\.send_message\(', 'await hub.funpay.send_message(', c)
    c = re.sub(r'\bc\.telegram\.bot\.send_message\(', 'await hub.telegram.bot.send_message(', c)
    c = re.sub(r'\bc\.telegram\.bot\.edit_message_text\(', 'await hub.telegram.bot.edit_message_text(', c)
    c = re.sub(r'\bc\.account\.get_order\(', 'await hub.funpay.try_method(hub.funpay.account.get_order, ', c)
    c = re.sub(
        r'def\s+([a-zA-Z0-9_]+)\s*\([^)]*(?:Cardinal|hub)[^)]*\)\s*:',
        r'async def \1(hub: "FunPayHub", *args, **kwargs):',
        c,
    )
    c = convert_telebot_to_aiogram(c, warnings)
    return c


def convert_telebot_to_aiogram(code: str, warnings: list) -> str:
    c = code

    c = convert_keyboard_builders(c, warnings)

    c = re.sub(r'(?:telebot\.)?types\.InlineKeyboardButton\(', 'InlineKeyboardButton(', c)
    c = re.sub(r'(?:telebot\.)?types\.KeyboardButton\(([^,)]+)\)', r'KeyboardButton(text=\1)', c)
    c = re.sub(r'(?:telebot\.)?types\.ReplyKeyboardRemove\(\)', 'ReplyKeyboardRemove()', c)
    c = re.sub(r'(?:telebot\.)?types\.ParseMode\.(\w+)', r'"\1"', c)
    c = re.sub(r'parse_mode\s*=\s*telebot\.PARSE_MODE_(\w+)', r'parse_mode="\1"', c)
    c = re.sub(r'(?:telebot\.)?types\.ChatType\.(\w+)', r'ChatType.\1', c)
    c = re.sub(r'(?:telebot\.)?types\.ContentType\.(\w+)', r'ContentType.\1', c)

    c = re.sub(
        r'@(?:bot|c\.telegram\.bot)\.message_handler\(([^)]*)\)',
        lambda m: convert_message_handler(m.group(1), warnings),
        c,
    )
    c = re.sub(
        r'@(?:bot|c\.telegram\.bot)\.callback_query_handler\(([^)]*)\)',
        lambda m: convert_callback_handler(m.group(1), warnings),
        c,
    )
    c = re.sub(
        r'@(?:bot|c\.telegram\.bot)\.inline_handler\(([^)]*)\)',
        lambda m: convert_inline_handler(m.group(1), warnings),
        c,
    )

    for method in [
        'send_message', 'send_photo', 'send_document', 'send_video', 'send_audio',
        'send_sticker', 'send_animation', 'send_voice', 'send_invoice',
        'edit_message_text', 'edit_message_reply_markup', 'edit_message_caption',
        'delete_message', 'answer_callback_query', 'answer_inline_query',
        'pin_chat_message', 'unpin_chat_message', 'forward_message',
        'copy_message', 'get_chat_member', 'ban_chat_member', 'unban_chat_member',
        'restrict_chat_member', 'get_file', 'get_file_url',
    ]:
        c = re.sub(rf'(?<!await )\bbot\.{method}\(', f'await hub.telegram.bot.{method}(', c)

    c = re.sub(r'(?<!await )call\.answer\(', 'await call.answer(', c)
    c = re.sub(r'(?<!await )query\.answer\(', 'await query.answer(', c)
    c = re.sub(r'(?<!await )message\.answer\(', 'await message.answer(', c)
    c = re.sub(r'\.reply_text\(', '.answer(', c)

    c = re.sub(r'(?:telebot\.)?types\.Message\b', 'types.Message', c)
    c = re.sub(r'(?:telebot\.)?types\.CallbackQuery\b', 'types.CallbackQuery', c)
    c = re.sub(r'(?:telebot\.)?types\.User\b', 'types.User', c)
    c = re.sub(r'(?:telebot\.)?types\.Chat\b', 'types.Chat', c)

    remaining_telebot = re.findall(r'telebot\.\w+', c)
    if remaining_telebot:
        warnings.append("Остались ссылки на telebot: " + ", ".join(dict.fromkeys(remaining_telebot)))

    known_types = {
        'types.Message', 'types.CallbackQuery', 'types.User', 'types.Chat',
        'types.InlineQuery', 'types.ChosenInlineResult', 'types.Poll',
        'types.PollAnswer', 'types.BotCommand', 'types.InputFile',
    }
    unknown_types = [t for t in dict.fromkeys(re.findall(r'\btypes\.[A-Z]\w+', c)) if t not in known_types]
    if unknown_types:
        warnings.append("Проверьте types.*: " + ", ".join(unknown_types[:10]))

    return c


def convert_keyboard_builders(code: str, warnings: list) -> str:
    lines = code.split('\n')
    result = []
    kb_vars: dict[str, tuple] = {}

    for line in lines:
        m_inline = re.match(r'^(\s*)(\w+)\s*=\s*(?:telebot\.)?types\.InlineKeyboardMarkup\(([^)]*)\)', line)
        m_reply  = re.match(r'^(\s*)(\w+)\s*=\s*(?:telebot\.)?types\.ReplyKeyboardMarkup\(([^)]*)\)', line)

        if m_inline:
            indent, var, args = m_inline.group(1), m_inline.group(2), m_inline.group(3)
            row_width = _extract_row_width(args) or 1
            kb_vars[var] = ('inline', row_width)
            result.append(f"{indent}{var} = InlineKeyboardBuilder()")
            continue

        if m_reply:
            indent, var, args = m_reply.group(1), m_reply.group(2), m_reply.group(3)
            kb_vars[var] = ('reply', 1)
            extra = _convert_reply_kb_args(args)
            result.append(f"{indent}{var} = ReplyKeyboardBuilder()")
            if extra:
                result.append(f"{indent}# reply keyboard options: {extra}")
            continue

        converted = line
        for var, (kb_type, row_width) in kb_vars.items():
            converted = re.sub(
                rf'\b{re.escape(var)}\.add\(([^)]+)\)',
                lambda m, v=var, rw=row_width: f"{v}.add({m.group(1)}); {v}.adjust({rw})",
                converted,
            )
            converted = re.sub(
                rf'\b{re.escape(var)}\.row\(([^)]+)\)',
                lambda m, v=var: f"{v}.row({m.group(1)})",
                converted,
            )
            converted = re.sub(
                rf'(?<!["\'])\b{re.escape(var)}\b(?!\s*[=.(])',
                f"{var}.as_markup()",
                converted,
            )

        result.append(converted)

    return '\n'.join(result)


def _extract_row_width(args: str) -> int | None:
    m = re.search(r'row_width\s*=\s*(\d+)', args)
    return int(m.group(1)) if m else None


def _convert_reply_kb_args(args: str) -> str:
    parts = []
    for k in ('resize_keyboard', 'one_time_keyboard', 'selective', 'is_persistent'):
        m = re.search(rf'{k}\s*=\s*(\w+)', args)
        if m:
            parts.append(f"{k}={m.group(1)}")
    return ', '.join(parts)


def convert_message_handler(args_str: str, warnings: list) -> str:
    commands      = re.findall(r"commands\s*=\s*\[([^\]]+)\]", args_str)
    content_types = re.findall(r"content_types\s*=\s*\[([^\]]+)\]", args_str)
    func_filter   = re.findall(r"func\s*=\s*(.+)", args_str)
    state         = re.search(r"state\s*=\s*(\S+)", args_str)

    filters = []

    if commands:
        cmds = [c.strip().strip("'\"") for c in commands[0].split(',')]
        filters.append("Command(" + ", ".join(f'"{c}"' for c in cmds) + ")")

    if content_types:
        type_map = {
            "text": "F.text", "photo": "F.photo", "document": "F.document",
            "voice": "F.voice", "video": "F.video", "audio": "F.audio",
            "sticker": "F.sticker", "animation": "F.animation",
            "location": "F.location", "contact": "F.contact",
        }
        for ct in [t.strip().strip("'\"") for t in content_types[0].split(',')]:
            filters.append(type_map.get(ct, f"F.content_type == ContentType.{ct.upper()}"))

    if func_filter:
        raw = func_filter[0].strip()
        inline = re.match(r'lambda\s+\w+\s*:\s*(.+)', raw)
        if inline:
            filters.append(inline.group(1).strip())
        else:
            warnings.append(f"func={raw} в message_handler — отправлено в ИИ-доводку.")
            filters.append(f"# TODO: func={raw}")

    if state:
        filters.append(f"StateFilter({state.group(1)})")

    return f"@tg_router.message({', '.join(filters)})"


def convert_callback_handler(args_str: str, warnings: list) -> str:
    data_eq = re.search(r"lambda\s+\w+\s*:\s*\w+\.data\s*==\s*(['\"].+?['\"])", args_str)
    data_sw = re.search(r"lambda\s+\w+\s*:\s*\w+\.data\.startswith\((['\"].+?['\"])\)", args_str)
    data_re = re.search(r"lambda\s+\w+\s*:\s*re\.match\(['\"](.+?)['\"]", args_str)
    state   = re.search(r"state\s*=\s*(\S+)", args_str)

    filters = []

    if data_eq:
        filters.append(f"F.data == {data_eq.group(1)}")
    elif data_sw:
        filters.append(f"F.data.startswith({data_sw.group(1)})")
    elif data_re:
        filters.append(f"F.data.regexp(r'{data_re.group(1)}')")
    else:
        raw = re.search(r"func\s*=\s*(.+)", args_str)
        if raw:
            inline = re.match(r'lambda\s+\w+\s*:\s*(.+)', raw.group(1).strip())
            if inline:
                filters.append(inline.group(1).strip())
            else:
                warnings.append(f"callback_query_handler func={raw.group(1)} — отправлено в ИИ-доводку.")
                filters.append(f"# TODO: func={raw.group(1)}")

    if state:
        filters.append(f"StateFilter({state.group(1)})")

    return f"@tg_router.callback_query({', '.join(filters)})"


def convert_inline_handler(args_str: str, warnings: list) -> str:
    warnings.append("inline_handler — отправлено в ИИ-доводку.")
    return "@tg_router.inline_query()  # TODO: check filters"


def build_main_py(clean_code: str, hooks: dict, plugin_id: str, name: str, warnings: list) -> str:
    lines = []
    lines.append('"""\nАВТОМАТИЧЕСКИ СКОНВЕРТИРОВАНО ДЛЯ FUNPAY HUB\n"""')
    lines.append("from __future__ import annotations")
    lines.append("import asyncio")
    lines.append("import logging")
    lines.append("from typing import TYPE_CHECKING")
    lines.append("")
    lines.append("from funpayhub.app.plugin import Plugin")
    lines.append("from funpaybotengine import Router as FPRouter")
    lines.append("from aiogram import Router as TGRouter, F")
    lines.append("from aiogram import types")
    lines.append("from aiogram.enums import ChatType, ContentType")
    lines.append("from aiogram.filters import Command, StateFilter")
    lines.append("from aiogram.fsm.context import FSMContext")
    lines.append("from aiogram.fsm.state import State, StatesGroup")
    lines.append("from aiogram.types import InlineKeyboardButton, ReplyKeyboardRemove")
    lines.append("from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder")
    lines.append("")
    lines.append("if TYPE_CHECKING:")
    lines.append("    from funpayhub.app.main import FunPayHub")
    lines.append("    from funpaybotengine.dispatching.events import (")
    lines.append("        NewMessageEvent, NewSaleEvent, SaleStatusChangedEvent,")
    lines.append("    )")
    lines.append("")
    lines.append(f"logger = logging.getLogger('fph.plugin.{plugin_id}')")
    lines.append("")
    lines.append("fp_router = FPRouter()")
    lines.append("tg_router = TGRouter()")
    lines.append("")

    if warnings:
        lines.append("# " + "─" * 60)
        for w in warnings:
            lines.append(f"#  ⚠  {w}")
        lines.append("# " + "─" * 60)
        lines.append("")

    lines.append(clean_code)
    lines.append("")

    for func in hooks["new_message"]:
        lines.append("@fp_router.on_new_message()")
        lines.append(f"async def fph_{func}(event: NewMessageEvent, hub: FunPayHub):")
        lines.append(f"    await {func}(hub, event)")
        lines.append("")

    for func in hooks["new_order"]:
        lines.append("@fp_router.on_new_sale()")
        lines.append(f"async def fph_{func}(event: NewSaleEvent, hub: FunPayHub):")
        lines.append(f"    await {func}(hub, event)")
        lines.append("")

    for func in hooks["order_status"]:
        lines.append("@fp_router.on_sale_status_change()")
        lines.append(f"async def fph_{func}(event: SaleStatusChangedEvent, hub: FunPayHub):")
        lines.append(f"    await {func}(hub, event)")
        lines.append("")

    lines.append("class ConvertedPlugin(Plugin):")
    lines.append("    async def funpay_routers(self):")
    lines.append("        return fp_router")
    lines.append("")
    lines.append("    async def telegram_routers(self):")
    lines.append("        return tg_router")
    lines.append("")
    lines.append("    async def pre_setup(self):")
    lines.append(f"        logger.info('Инициализация {name}...')")
    if hooks["pre_init"]:
        for func in hooks["pre_init"]:
            lines.append(f"        await {func}(self.hub)")
    else:
        lines.append("        pass")
    lines.append("")
    lines.append("    async def post_setup(self):")
    if hooks["post_start"]:
        for func in hooks["post_start"]:
            lines.append(f"        await {func}(self.hub)")
    else:
        lines.append("        pass")

    return "\n".join(lines) + "\n"


def extract_meta(code: str, pattern: str, fallback: str) -> str:
    m = re.search(pattern, code, re.IGNORECASE)
    return m.group(1).strip() if m else fallback


def extract_list(code: str, pattern: str) -> list:
    m = re.search(pattern, code)
    if not m:
        return []
    return [i.strip().strip('"').strip("'") for i in m.group(1).split(',') if i.strip()]
