"""
Discord-бот + Google Таблицы: кураторы ОПГ и итоги бизваров.

Файлы, которые должны лежать рядом (на хостинге в одной папке):
  discord_bot.py, requirements.txt,
  creds.json  - ключ сервисного аккаунта Google,
  .env        - одна строка:  DISCORD_TOKEN=токен_бота
Вместо .env можно задать переменную DISCORD_TOKEN в панели хостинга.
Токен в код вставлять не нужно.

Команды:
  /add организация: ... ник: ... [организация2, 3, 4]   (один куратор в нескольких ОПГ)
  /del ник: ... [организация, организация2, организация3]
  /список
  /итоги тип: АЗС номер: 31 орг: Кавказская ОПГ
"""
import asyncio
import os
import re
from datetime import datetime
from typing import Optional

import discord
import gspread
from discord import app_commands
from gspread.utils import rowcol_to_a1


def load_dotenv(path=".env"):
    """Читает строки KEY=VALUE из файла .env (если он есть)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()
# токен берётся из переменной хостинга (подойдёт любое из этих имён) или из .env
TOKEN = next((os.getenv(n).strip() for n in ("DISCORD_TOKEN", "TOKEN", "BOT_TOKEN")
              if os.getenv(n)), "")
if not TOKEN:
    raise SystemExit("Нет токена: создай файл .env со строкой DISCORD_TOKEN=... "
                     "или задай переменную DISCORD_TOKEN в панели хостинга.")

# ============ НАСТРОЙКИ (не секретные, менять не нужно) ============
# ID таблицы = часть ссылки между /d/ и /edit
CURATORS_SHEET_ID = "1PaYB_xzndyzJPdtu-AWPWQDDQ86LAFcwTEMI_qWod6o"
CURATORS_TAB = None  # None = бот сам найдёт лист со словом «куратор» (или возьмёт первый)

BIZ_SHEET_ID = "104l-Mcsb-4BEKRgvIJSlVrvrGWBRWQfJyaEjpyuYZ-0"  # отдельная таблица
BIZ_TAB = None       # None = бот сам найдёт лист со словом «бизнес» (или возьмёт первый)

GUILD_ID = 1564595935504498728  # ID твоего сервера, бот работает только на нём
ADMIN_USER_IDS = [      # ID людей с полным доступом
    1469843330610495543,  # тех. админ
    498484253332471808,   # Главный Следящий
]
ADMIN_ROLE_IDS = []     # (необязательно) роли с полным доступом
CURATOR_ROLE_IDS = []   # (необязательно) роли, которым можно только /итоги
# ===================================================================

ADMIN_USERS = set(ADMIN_USER_IDS)
ADMIN_ROLES = set(ADMIN_ROLE_IDS)
RESULT_ROLES = ADMIN_ROLES | set(CURATOR_ROLE_IDS)
SLOTS = 12  # сколько строк под ники под каждым заголовком

# ключ -> (что искать в заголовке, красивое название)
ORGS = {
    "орех": ("орехов", "Ореховская ОПГ"),
    "кавказ": ("кавказ", "Кавказская ОПГ"),
    "курган": ("курган", "Курганская ОПГ"),
    "тамбов": ("тамбов", "Тамбовская ОПГ"),
}
ORG_CHOICES = [app_commands.Choice(name=t, value=k) for k, (_, t) in ORGS.items()]
ALL_CHOICES = ORG_CHOICES + [app_commands.Choice(name="Все", value="все")]

gc = gspread.service_account(filename="creds.json")


def pick_tab(book, name, keyword):
    """Лист по точному названию, иначе по слову в названии, иначе первый."""
    if name:
        return book.worksheet(name)
    for w in book.worksheets():
        if keyword in w.title.lower():
            return w
    return book.sheet1


curators_book = gc.open_by_key(CURATORS_SHEET_ID)
ws = pick_tab(curators_book, CURATORS_TAB, "куратор")

# таблица бизнесов: A = "Бизнес", B = "Контролирующая ОПГ"
biz_book = gc.open_by_key(BIZ_SHEET_ID)
biz = pick_tab(biz_book, BIZ_TAB, "бизнес")
try:
    history = biz_book.worksheet("Журнал итогов")
except gspread.WorksheetNotFound:
    history = biz_book.add_worksheet("Журнал итогов", 2000, 5)
    history.append_row(["Дата", "Бизнес", "Было", "Стало", "Кто внёс"])


# ---------- работа с таблицей ----------
def find_header(rows, key):
    """Ищет ячейку 'Куратор ... <орг>' -> (строка, столбец) с нуля."""
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            v = val.lower()
            if "куратор" in v and ORGS[key][0] in v:
                return r, c
    return None


def column_names(rows, r, c):
    names = []
    for i in range(1, SLOTS + 1):
        row = rows[r + i] if r + i < len(rows) else []
        names.append(row[c].strip() if c < len(row) else "")
    return names


def write_column(r, c, names):
    names = names + [""] * (SLOTS - len(names))
    start = rowcol_to_a1(r + 2, c + 1)
    end = rowcol_to_a1(r + 1 + SLOTS, c + 1)
    ws.update(range_name=f"{start}:{end}", values=[[n] for n in names])


def expand_orgs(values):
    """Выбранные организации -> ключи без повторов; «все» = все четыре."""
    keys = []
    for v in values:
        if not v:
            continue
        for k in (list(ORGS) if v == "все" else [v]):
            if k not in keys:
                keys.append(k)
    return keys


def do_add(nick, orgs):
    keys = expand_orgs(orgs)
    rows = ws.get_all_values()
    out = []
    for k in keys:
        title = ORGS[k][1]
        pos = find_header(rows, k)
        if not pos:
            out.append(f"❌ {title}: не нашёл заголовок в таблице")
            continue
        names = column_names(rows, *pos)
        if nick.lower() in [n.lower() for n in names]:
            out.append(f"➖ {title}: уже в списке")
        elif "" not in names:
            out.append(f"❌ {title}: список заполнен")
        else:
            i = names.index("")  # первая свободная ячейка под заголовком
            ws.update_cell(pos[0] + 2 + i, pos[1] + 1, nick)
            out.append(f"✅ {title}: добавлен")
    return f"**{nick}**\n" + "\n".join(out)


def do_del(nick, orgs):
    keys = expand_orgs(orgs) or list(ORGS)  # пусто = из всех организаций
    rows = ws.get_all_values()
    removed = []
    for k in keys:
        pos = find_header(rows, k)
        if not pos:
            continue
        filled = [n for n in column_names(rows, *pos) if n]
        keep = [n for n in filled if n.lower() != nick.lower()]
        if len(keep) != len(filled):
            write_column(pos[0], pos[1], keep)  # список сдвигается вверх
            removed.append(ORGS[k][1])
    if not removed:
        return f"❌ **{nick}** не найден"
    return f"✅ **{nick}** удалён: " + ", ".join(removed)


def do_list():
    rows = ws.get_all_values()
    out = []
    for k, (_, title) in ORGS.items():
        pos = find_header(rows, k)
        names = [n for n in column_names(rows, *pos) if n] if pos else []
        out.append(f"**{title}:** " + (", ".join(names) if names else "—"))
    return "\n".join(out)


def do_result(biz_type, number, org, who):
    """Находит бизнес по номеру в скобках, (№31) или (51), и меняет владельца."""
    title = ORGS[org][1]
    rows = biz.get_all_values()
    for r, row in enumerate(rows[1:], start=2):
        name = row[0].strip() if row else ""
        m = re.search(r"\(\s*№?\s*(\d+)\s*\)\s*$", name)
        if not m or int(m.group(1)) != number:
            continue
        if biz_type.lower() not in name.lower():
            return f"❌ Бизнес №{number} — это «{name}», а не «{biz_type}»"
        old = row[1].strip() if len(row) > 1 else ""
        if old.rstrip(" .").lower() == title.lower():
            return f"➖ {name} уже у {title}"
        biz.update_cell(r, 2, title)
        history.append_row([
            datetime.now().strftime("%d.%m.%Y %H:%M"), name, old, title, who,
        ])
        text = f"✅ **{name}**\n{old or '—'} → **{title}**"
        try:  # свежие итоги из формул справа в таблице
            stats = [f"{s[0]}: {s[1]}" for s in biz.get("C2:D10") if len(s) > 1 and s[0]]
            if stats:
                text += "\n\n" + "\n".join(stats)
        except Exception:
            pass
        return text
    return f"❌ Бизнес №{number} не найден"


# ---------- Discord ----------
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)  # команды видны только на твоём сервере
        await self.tree.sync(guild=guild)

    async def on_ready(self):
        for g in self.guilds:  # на чужих серверах бот не остаётся
            if g.id != GUILD_ID:
                await g.leave()

    async def on_guild_join(self, g: discord.Guild):
        if g.id != GUILD_ID:
            await g.leave()


bot = Bot()


def is_allowed(i: discord.Interaction, roles):
    if i.guild_id != GUILD_ID:
        return False
    return i.user.id in ADMIN_USERS or any(
        r.id in roles for r in getattr(i.user, "roles", [])
    )


async def run(i: discord.Interaction, fn, *args, roles=ADMIN_ROLES):
    if not is_allowed(i, roles):
        return await i.response.send_message("⛔ Нет доступа", ephemeral=True)
    await i.response.defer()  # таблица может отвечать дольше 3 секунд
    try:
        text = await asyncio.to_thread(fn, *args)
    except Exception as e:
        text = f"⚠️ Ошибка: {e}"
    await i.followup.send(text)


@bot.tree.command(name="add", description="Добавить куратора в организацию")
@app_commands.describe(org="Организация", nick="Ник куратора",
                       org2="Ещё одна организация (необязательно)",
                       org3="И ещё одна (необязательно)",
                       org4="И ещё одна (необязательно)")
@app_commands.rename(org="организация", nick="ник", org2="организация2",
                     org3="организация3", org4="организация4")
@app_commands.choices(org=ALL_CHOICES, org2=ORG_CHOICES, org3=ORG_CHOICES, org4=ORG_CHOICES)
async def add(i: discord.Interaction, org: app_commands.Choice[str], nick: str,
              org2: Optional[app_commands.Choice[str]] = None,
              org3: Optional[app_commands.Choice[str]] = None,
              org4: Optional[app_commands.Choice[str]] = None):
    orgs = [o.value for o in (org, org2, org3, org4) if o]
    await run(i, do_add, nick.strip(), orgs)


@bot.tree.command(name="del", description="Удалить куратора")
@app_commands.describe(nick="Ник куратора", org="Организация (пусто = из всех)",
                       org2="Ещё одна организация (необязательно)",
                       org3="И ещё одна (необязательно)")
@app_commands.rename(nick="ник", org="организация", org2="организация2",
                     org3="организация3")
@app_commands.choices(org=ALL_CHOICES, org2=ORG_CHOICES, org3=ORG_CHOICES)
async def delete(i: discord.Interaction, nick: str,
                 org: Optional[app_commands.Choice[str]] = None,
                 org2: Optional[app_commands.Choice[str]] = None,
                 org3: Optional[app_commands.Choice[str]] = None):
    orgs = [o.value for o in (org, org2, org3) if o]
    await run(i, do_del, nick.strip(), orgs)


@bot.tree.command(name="список", description="Показать всех кураторов")
async def show_list(i: discord.Interaction):
    await run(i, do_list)


@bot.tree.command(name="итоги", description="Внести итог бизвара: кто теперь контролирует бизнес")
@app_commands.describe(biz_type="Тип бизнеса, например АЗС", number="Номер бизнеса",
                       org="Какая ОПГ взяла бизнес")
@app_commands.rename(biz_type="тип", number="номер", org="орг")
@app_commands.choices(org=ORG_CHOICES)
async def results(i: discord.Interaction, biz_type: str, number: int,
                  org: app_commands.Choice[str]):
    await run(i, do_result, biz_type.strip(), number, org.value,
              i.user.display_name, roles=RESULT_ROLES)


bot.run(TOKEN)
