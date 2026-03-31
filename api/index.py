from fastapi import FastAPI, Request
from fastapi.responses import Response
import re
import io
import zipfile
import json

app = FastAPI()

def extract_meta(code: str, pattern: str, fallback: str) -> str:
    match = re.search(pattern, code, re.IGNORECASE)
    return match.group(1).strip() if match else fallback

def extract_list(code: str, pattern: str) -> list:
    match = re.search(pattern, code)
    if not match: return []
    items = match.group(1).split(',')
    return [i.strip().strip('"').strip("'") for i in items if i.strip()]

@app.post("/api/convert")
async def convert_plugin(request: Request):
    body = await request.json()
    code = body.get("code", "")
    
    if not code:
        return Response(content="No code provided", status_code=400)

    
    name = extract_meta(code, r'NAME\s*=\s*["\']([^"\']+)["\']', "ConvertedPlugin")
    version = extract_meta(code, r'VERSION\s*=\s*["\']([^"\']+)["\']', "1.0.0")
    desc = extract_meta(code, r'DESCRIPTION\s*=\s*["\']([^"\']+)["\']', "Converted to FPH")
    credits = extract_meta(code, r'CREDITS\s*=\s*["\']([^"\']+)["\']', "AutoConverter")
    
    plugin_id = "com.converted." + re.sub(r'[^a-z0-9]', '', name.lower())

    
    manifest = {
        "manifest": 1,
        "plugin_id": plugin_id,
        "name": name,
        "description": desc,
        "plugin_version": version,
        "app_version": ">=0.5.0",
        "entry_point": "main.ConvertedPlugin",
        "author": {"name": credits}
    }

    
    hooks = {
        "pre_init": extract_list(code, r'BIND_TO_PRE_INIT\s*=\s*\[([^\]]+)\]'),
        "post_start": extract_list(code, r'BIND_TO_POST_START\s*=\s*\[([^\]]+)\]'),
        "new_message": extract_list(code, r'BIND_TO_NEW_MESSAGE\s*=\s*\[([^\]]+)\]'),
        "new_order": extract_list(code, r'BIND_TO_NEW_ORDER\s*=\s*\[([^\]]+)\]')
    }

    
    main_py = '"""\nАВТОМАТИЧЕСКИ СКОНВЕРТИРОВАНО ДЛЯ FUNPAY HUB\n'
    main_py += 'Требуется ручная адаптация логики Telebot (кнопки, интерфейсы).\n"""\n'
    main_py += "from __future__ import annotations\nimport asyncio\nimport logging\nfrom typing import TYPE_CHECKING\n"
    main_py += "from funpayhub.app.plugin import Plugin\nfrom funpaybotengine import Router as FPRouter\n"
    main_py += "from aiogram import Router as TGRouter\nfrom aiogram import types\n\n"
    main_py += "if TYPE_CHECKING:\n    from funpayhub.app.main import FunPayHub\n"
    main_py += "    from funpaybotengine.dispatching.events import NewMessageEvent, NewSaleEvent\n\n"
    main_py += f"logger = logging.getLogger('fph.plugin.{plugin_id}')\n\n"
    main_py += "fp_router = FPRouter()\ntg_router = TGRouter()\n\n"

    
    clean_code = re.sub(r'from\s+cardinal\s+import\s+Cardinal', '', code)
    clean_code = re.sub(r'import\s+telebot', '
    clean_code = re.sub(r'NAME\s*=.*?\n|VERSION\s*=.*?\n|DESCRIPTION\s*=.*?\n|CREDITS\s*=.*?\n|UUID\s*=.*?\n|SETTINGS_PAGE\s*=.*?\n', '', clean_code)
    clean_code = re.sub(r'BIND_TO_[A-Z_]+\s*=\s*\[.*?\]', '', clean_code)

    
    clean_code = re.sub(r'def\s+([a-zA-Z0-9_]+)\s*\(.*?(Cardinal|hub).*?\):', r'async def \1(hub: FunPayHub, *args, **kwargs):', clean_code)
    clean_code = clean_code.replace('time.sleep(', 'await asyncio.sleep(')
    clean_code = clean_code.replace('c.send_message(', 'await hub.funpay.send_message(')
    clean_code = clean_code.replace('c.telegram.bot.send_message(', 'await hub.telegram.bot.send_message(')
    clean_code = clean_code.replace('c.telegram.bot.edit_message_text(', 'await hub.telegram.bot.edit_message_text(')
    clean_code = clean_code.replace('c.account.get_order(', 'await hub.funpay.try_method(hub.funpay.account.get_order, ')

    main_py += clean_code + "\n\n"

    
    for func in hooks["new_message"]:
        main_py += f"@fp_router.on_new_message()\nasync def fph_{func}(event: NewMessageEvent, hub: FunPayHub):\n    await {func}(hub, event)\n\n"
    for func in hooks["new_order"]:
        main_py += f"@fp_router.on_new_sale()\nasync def fph_{func}(event: NewSaleEvent, hub: FunPayHub):\n    await {func}(hub, event)\n\n"

    
    main_py += "class ConvertedPlugin(Plugin):\n"
    main_py += "    async def funpay_routers(self):\n        return fp_router\n\n"
    main_py += "    async def telegram_routers(self):\n        return tg_router\n\n"
    main_py += "    async def pre_setup(self):\n"
    main_py += f"        logger.info('Инициализация {name}...')\n"
    for func in hooks["pre_init"]:
        main_py += f"        await {func}(self.hub)\n"
    if not hooks["pre_init"]: main_py += "        pass\n"

    main_py += "\n    async def post_setup(self):\n"
    for func in hooks["post_start"]:
        main_py += f"        await {func}(self.hub)\n"
    if not hooks["post_start"]: main_py += "        pass\n"

    
    zip_buffer = io.BytesIO()
    plugin_folder_name = plugin_id.replace('.', '_')
    
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr(f"{plugin_folder_name}/manifest.json", json.dumps(manifest, indent=4))
        zip_file.writestr(f"{plugin_folder_name}/__init__.py", "")
        zip_file.writestr(f"{plugin_folder_name}/main.py", main_py)

    zip_buffer.seek(0)

    
    headers = {
        "Content-Disposition": f'attachment; filename="{plugin_folder_name}_fph.zip"',
        "Content-Type": "application/zip"
    }
    
    return Response(content=zip_buffer.read(), headers=headers)
