# -*- coding: utf-8 -*-
"""
BotForge Studio - creatore di bot Discord senza codice (file unico)

Avvio:
    pip install -U "discord.py>=2.4" aiohttp tzdata cryptography
    export DISCORD_TOKEN="token del bot BotForge (il manager)"
    export OWNER_IDS="il_tuo_id_discord"          # amministratori piattaforma
    export BOTFORGE_KEY="$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
    python botforge_studio.py

Variabili opzionali: DEV_GUILD_ID (sync immediato dei comandi in un server),
SUPPORT_CHANNEL_ID, DATA_FILE, MAX_RUNNING_BOTS.

Architettura: il bot "manager" espone i 41 comandi slash. Ogni bot creato dagli
utenti gira come client discord.py separato nello stesso processo (hosting integrato).
"""
import asyncio, io, json, math, os, re, secrets, sys, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

# ----------------------------------------------------------------- configurazione
TOKEN = os.getenv("DISCORD_TOKEN", "")
OWNER_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("OWNER_IDS", ""))}
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0") or 0)
SUPPORT_CHANNEL_ID = int(os.getenv("SUPPORT_CHANNEL_ID", "0") or 0)
DATA_FILE = os.getenv("DATA_FILE", "botforge_data.json")
SECRET_KEY = os.getenv("BOTFORGE_KEY", "")
MAX_COMMANDS = 500          # comandi personalizzati per bot
TOP_LEVEL_SLASH = 90        # Discord ammette max 100 slash per app: gli altri passano da /cmd
MAX_BOTS_PER_USER = 5
MAX_RUNNING = int(os.getenv("MAX_RUNNING_BOTS", "50"))
STARTED = time.time()
NAME_RE = re.compile(r"^[\w-]{1,32}$")
KNOWN = {"user", "username", "server", "channel", "target", "members"}
NO_PINGS = discord.AllowedMentions(everyone=False, roles=False, users=True)
INVITE_PERMS = discord.Permissions(view_channel=True, send_messages=True, embed_links=True, attach_files=True,
                                   read_message_history=True, manage_channels=True, manage_roles=True,
                                   kick_members=True, ban_members=True, moderate_members=True,
                                   manage_messages=True)

# ----------------------------------------------------------------- utilità
def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def ago(iso):
    try: return f"<t:{int(datetime.fromisoformat(iso).timestamp())}:R>"
    except Exception: return "mai"

def uptime(sec):
    sec = int(sec); d, r = divmod(sec, 86400); h, r = divmod(r, 3600); m, s = divmod(r, 60)
    return " ".join(x for x in (f"{d}g" if d else "", f"{h}h" if h else "", f"{m}m", f"{s}s") if x)

def enc(t):
    return "enc:" + Fernet(SECRET_KEY.encode()).encrypt(t.encode()).decode() if Fernet and SECRET_KEY else t
def dec(t):
    if t.startswith("enc:"): return Fernet(SECRET_KEY.encode()).decrypt(t[4:].encode()).decode()
    return t

def pc(s, default):
    try: return int(str(s).lstrip("#"), 16) if s else default
    except ValueError: return default

def valid_name(n):
    n = (n or "").strip().lower().lstrip("/")
    return n if NAME_RE.fullmatch(n) else None

# ----------------------------------------------------------------- archivio dati
class Store:
    def __init__(self, path):
        self.path, self.dirty = path, False
        self.d = {"bots": {}, "admins": [], "audit": [], "support": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f: self.d.update(json.load(f))
    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(self.d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path); self.dirty = False
    def mine(self, uid): return [b for b in self.d["bots"].values() if b["owner"] == uid]
    def find(self, uid, ref):
        ref = (ref or "").strip().lower()
        b = self.d["bots"].get(ref)
        if b and b["owner"] == uid: return b
        return next((b for b in self.mine(uid) if b["name"].lower() == ref), None)
    def audit(self, uid, op):
        self.d["audit"].append({"t": now(), "admin": uid, "op": op}); self.d["audit"] = self.d["audit"][-500:]; self.save()

store = Store(DATA_FILE)
RUNNING = {}      # id progetto -> ChildBot
COOLDOWNS = {}    # (pid, comando, utente) -> timestamp

def is_admin(uid): return uid in OWNER_IDS or uid in store.d["admins"]

def new_project(owner, name, desc, image, lang, guild_id, token, app_id):
    return {"id": secrets.token_hex(4), "owner": owner, "name": name, "description": desc, "image": image,
            "lang": lang, "guild_id": guild_id, "token": enc(token), "app_id": app_id, "prefix": "!",
            "embed_color": 0x5865F2, "style": "embed", "footer": "", "ephemeral_default": False,
            "status": "offline", "created": now(), "updated": now(), "last_start": None,
            "commands": {}, "components": {}, "modules": {}, "welcome": None, "log": None, "ticket": None,
            "scheduled": [], "versions": [], "stats": {"uses": {}, "errors": []}}

def record_error(pid, component, command, err):
    p = store.d["bots"].get(pid)
    if not p: return
    p["stats"]["errors"].append({"t": now(), "component": component, "command": command, "msg": str(err)[:300]})
    p["stats"]["errors"] = p["stats"]["errors"][-100:]; store.dirty = True

CONFIG_KEYS = ["description", "image", "lang", "prefix", "embed_color", "style", "footer", "ephemeral_default",
               "commands", "components", "modules", "welcome", "log", "ticket"]
def snapshot(p):
    return json.loads(json.dumps({k: p[k] for k in CONFIG_KEYS}))  # copia profonda, senza token

# ----------------------------------------------------------------- comandi personalizzati: modello
def new_cmd(name, desc, text, **kw):
    c = {"name": name, "description": desc, "type": "slash", "category": "generale",
         "response": {"text": text, "embed": None}, "fields": [], "roles": [], "users": [], "cooldown": 0,
         "ephemeral": False, "enabled": True, "action": None, "button_label": "Apri",
         "created": now(), "updated": now()}
    c.update(kw); return c

def clean_cmd(d):
    """Rimette in forma un comando importato (scarta chiavi sconosciute)."""
    base = new_cmd(str(d.get("name", "")).lower(), str(d.get("description", ""))[:100], "")
    for k in base:
        if k in d: base[k] = d[k]
    base["type"] = base["type"] if base["type"] in ("slash", "contestuale", "pulsante") else "slash"
    return base

def validate_command(c, guild=None):
    pr = []
    if not NAME_RE.fullmatch(c["name"]) or c["name"] != c["name"].lower():
        pr.append("Nome non valido (minuscole, numeri, - e _, max 32).")
    if not 1 <= len(c.get("description", "")) <= 100: pr.append("La descrizione deve avere 1-100 caratteri.")
    r = c.get("response") or {}; e = r.get("embed")
    if not (r.get("text") or (e and (e.get("title") or e.get("description")))): pr.append("Manca il messaggio di risposta.")
    if len(r.get("text") or "") > 4000: pr.append("Risposta oltre 4000 caratteri.")
    fl = c.get("fields", [])
    if len(fl) > 5: pr.append("Massimo 5 campi compilabili.")
    if any(not re.fullmatch(r"\w{1,40}", f) for f in fl): pr.append("Nomi dei campi: solo lettere/numeri/_ (max 40).")
    txt = (r.get("text") or "") + " " + (" ".join(str(e.get(k) or "") for k in ("title", "description", "footer")) if e else "")
    unk = set(re.findall(r"\{(\w+)\}", txt)) - KNOWN - set(fl)
    if unk: pr.append("Segnaposto sconosciuti: " + ", ".join("{" + u + "}" for u in sorted(unk)))
    if c.get("cooldown", 0) < 0: pr.append("Cooldown negativo.")
    if guild:
        for rid in c.get("roles", []) + [(c.get("action") or {}).get("role")]:
            if rid and not guild.get_role(int(rid)): pr.append(f"Ruolo {rid} non trovato nel server.")
    return pr

TEMPLATES = {
    "assistenza": {"modules": ["assistenza"], "commands": [
        ("faq", "Domande frequenti", "❓ Consulta le FAQ del server o apri un ticket."),
        ("contatti", "Come contattare lo staff", "📩 Apri un ticket per parlare con lo staff.")]},
    "moderazione": {"modules": ["moderazione"], "commands": [
        ("regolamento", "Mostra il regolamento", "📜 Rispetta tutti i membri.\nNiente spam né insulti.")]},
    "community": {"modules": ["benvenuto"], "commands": [
        ("social", "Collegamenti social", "Seguici sui nostri social!"),
        ("regole", "Regole del server", "1. Rispetto\n2. Niente spam\n3. Segui lo staff"),
        ("informazioni", "Informazioni sul server", "Benvenuto in **{server}**! Siamo in {members}.")]},
    "notifiche": {"modules": ["log"], "commands": [("annunci", "Ultimi annunci", "📢 Nessun annuncio al momento.")]},
    "server": {"modules": [], "commands": [
        ("serverinfo", "Informazioni sul server", "**{server}** - {members} membri"),
        ("ping", "Test di risposta", "🏓 Pong, {user}!")]},
}
TEMPLATE_CHOICES = [app_commands.Choice(name=k.capitalize(), value=k) for k in TEMPLATES]
MODULES = {"assistenza": "Ticket di assistenza (/configura-ticket)", "benvenuto": "Messaggio di benvenuto (/configura-benvenuto)",
           "log": "Registro eventi (/crea-log)", "moderazione": "Comandi /kick /ban /timeout /clear"}

def template_diff(p, key):
    t = TEMPLATES[key]
    new = [c for c in t["commands"] if c[0] not in p["commands"]]
    skip = [c[0] for c in t["commands"] if c[0] in p["commands"]]
    return new, skip, [m for m in t["modules"] if not p["modules"].get(m)]

def apply_template(p, key):
    new, _, mods = template_diff(p, key)
    for n, d, t in new:
        if len(p["commands"]) < MAX_COMMANDS: p["commands"][n] = new_cmd(n, d, t)
    for m in mods: p["modules"][m] = True

# ----------------------------------------------------------------- messaggi
def make_embed(d, p, f=lambda s: s):
    e = discord.Embed(title=(f(d.get("title") or "")[:256] or None), description=(f(d.get("description") or "")[:4000] or None),
                      color=d.get("color", p["embed_color"]))
    if d.get("image"): e.set_image(url=d["image"])
    ft = d.get("footer") or p.get("footer")
    if ft: e.set_footer(text=f(ft)[:2048])
    for n, v in (d.get("fields") or [])[:25]: e.add_field(name=f(n)[:256], value=f(v)[:1024], inline=False)
    if not (e.title or e.description or e.fields or d.get("image")): e.description = "\u200b"
    return e

def make_fmt(inter, extra=None, target=None):
    g = inter.guild
    rep = {"{user}": inter.user.mention, "{username}": inter.user.display_name, "{server}": g.name if g else "",
           "{channel}": getattr(inter.channel, "mention", ""), "{target}": (target or inter.user).mention,
           "{members}": str(g.member_count) if g else ""}
    for k, v in (extra or {}).items(): rep["{" + k + "}"] = v
    def f(t):
        for k, v in rep.items(): t = t.replace(k, v)
        return t
    return f

def build_message(p, resp, f):
    if resp.get("embed"): return None, make_embed(resp["embed"], p, f)
    text = f(resp.get("text", ""))
    if p.get("style", "embed") == "embed": return None, make_embed({"description": text}, p)
    return text, None

async def send(inter, content=None, *, embed=None, view=None, ephemeral=False, file=None):
    kw = {k: v for k, v in dict(content=content, embed=embed, view=view, file=file).items() if v is not None}
    if inter.response.is_done(): return await inter.followup.send(ephemeral=ephemeral, **kw)
    return await inter.response.send_message(ephemeral=ephemeral, **kw)

async def done(inter, text=None, embed=None):
    if inter.response.is_done(): await inter.edit_original_response(content=text, embed=embed, view=None)
    else: await inter.response.edit_message(content=text, embed=embed, view=None)

class Choices(discord.ui.View):
    def __init__(self, uid, options):
        super().__init__(timeout=180); self.uid = uid
        for label, style, fn in options:
            b = discord.ui.Button(label=label, style=style)
            async def cb(inter, fn=fn):
                if inter.user.id != self.uid:
                    return await inter.response.send_message("Questo menu non è tuo.", ephemeral=True)
                self.stop()
                if fn is None: return await done(inter, "Operazione annullata.")
                await fn(inter)
            b.callback = cb; self.add_item(b)

async def confirm(inter, text, yes, *, embed=None, label="Conferma", style=discord.ButtonStyle.danger, extra=None):
    opts = [(label, style, yes)] + (extra or []) + [("Annulla", discord.ButtonStyle.secondary, None)]
    await send(inter, text, embed=embed, view=Choices(inter.user.id, opts), ephemeral=True)

def allowed(member, obj):
    roles, users = obj.get("roles") or [], obj.get("users") or []
    if not roles and not users: return True
    if member.id in users: return True
    if isinstance(member, discord.Member):
        return member.guild_permissions.administrator or any(r.id in roles for r in member.roles)
    return False

# ----------------------------------------------------------------- esecuzione comandi personalizzati
async def run_custom(inter, pid, name, target=None, click=False):
    p = store.d["bots"].get(pid); c = p["commands"].get(name) if p else None
    if not c or not c["enabled"]: return await send(inter, "Comando non disponibile.", ephemeral=True)
    if not allowed(inter.user, c): return await send(inter, "⛔ Non hai i permessi per usare questo comando.", ephemeral=True)
    if c["cooldown"]:
        k = (pid, name, inter.user.id); left = COOLDOWNS.get(k, 0) + c["cooldown"] - time.time()
        if left > 0: return await send(inter, f"⏳ Riprova tra {int(left) + 1}s.", ephemeral=True)
        COOLDOWNS[k] = time.time()
    p["stats"]["uses"][name] = p["stats"]["uses"].get(name, 0) + 1; store.dirty = True
    if c["type"] == "pulsante" and not click:
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label=c.get("button_label", "Apri")[:80], custom_id=f"bf|cmd|{name}"))
        return await send(inter, "Premi il pulsante per continuare.", view=v, ephemeral=c["ephemeral"])
    if c["fields"] and not click: return await inter.response.send_modal(FieldsModal(pid, c, target))
    await respond_custom(inter, p, c, target, {})

async def respond_custom(inter, p, c, target, extra):
    content, embed = build_message(p, c["response"], make_fmt(inter, extra, target))
    priv = c["ephemeral"] or p.get("ephemeral_default", False)
    await send(inter, content, embed=embed, ephemeral=priv)
    a = c.get("action")
    if a and isinstance(inter.user, discord.Member):
        try:
            if a["type"] in ("dai_ruolo", "togli_ruolo") and a.get("role"):
                role = inter.guild.get_role(int(a["role"]))
                if role: await (inter.user.add_roles if a["type"] == "dai_ruolo" else inter.user.remove_roles)(role, reason="BotForge")
            elif a["type"] == "invia_dm":
                await inter.user.send(content=content, embed=embed)
        except discord.HTTPException as e:
            record_error(p["id"], "azione", c["name"], e)
            await inter.followup.send("⚠️ Non sono riuscito a completare l'azione (permessi/DM chiusi).", ephemeral=True)

class FieldsModal(discord.ui.Modal):
    def __init__(self, pid, c, target):
        super().__init__(title=c["name"][:45]); self.pid, self.c, self.target, self.inputs = pid, c, target, {}
        for f in c["fields"][:5]:
            ti = discord.ui.TextInput(label=f[:45], max_length=1000); self.inputs[f] = ti; self.add_item(ti)
    async def on_submit(self, inter):
        await respond_custom(inter, store.d["bots"][self.pid], self.c, self.target, {k: v.value for k, v in self.inputs.items()})

# ----------------------------------------------------------------- bot figli (hosting integrato)
def generic_cmd(pid):
    @app_commands.describe(nome="Nome del comando personalizzato da eseguire")
    async def cb(inter: discord.Interaction, nome: str):
        await run_custom(inter, pid, nome.lower().lstrip("/"))
    c = app_commands.Command(name="cmd", description="Esegue un comando personalizzato per nome", callback=cb)
    async def ac(inter, current):
        pp = store.d["bots"].get(pid)
        return [app_commands.Choice(name=n, value=n) for n, x in pp["commands"].items()
                if x["enabled"] and x["type"] != "contestuale" and current.lower() in n][:25]
    c.autocomplete("nome")(ac); return c

def mod_commands():
    out = []
    def hier(inter, m): return inter.guild.owner_id == inter.user.id or m.top_role < inter.user.top_role
    @app_commands.describe(utente="Utente", motivo="Motivo")
    @app_commands.guild_only()
    async def kick(inter: discord.Interaction, utente: discord.Member, motivo: str = "Nessun motivo"):
        if not inter.permissions.kick_members or not hier(inter, utente): return await send(inter, "⛔ Permessi insufficienti.", ephemeral=True)
        try: await utente.kick(reason=f"{inter.user}: {motivo}"); await send(inter, f"👢 {utente} espulso.")
        except discord.HTTPException: await send(inter, "❌ Non posso espellere questo utente.", ephemeral=True)
    @app_commands.describe(utente="Utente", motivo="Motivo")
    @app_commands.guild_only()
    async def ban(inter: discord.Interaction, utente: discord.Member, motivo: str = "Nessun motivo"):
        if not inter.permissions.ban_members or not hier(inter, utente): return await send(inter, "⛔ Permessi insufficienti.", ephemeral=True)
        try: await utente.ban(reason=f"{inter.user}: {motivo}"); await send(inter, f"🔨 {utente} bannato.")
        except discord.HTTPException: await send(inter, "❌ Non posso bannare questo utente.", ephemeral=True)
    @app_commands.describe(utente="Utente", minuti="Durata in minuti", motivo="Motivo")
    @app_commands.guild_only()
    async def timeout(inter: discord.Interaction, utente: discord.Member, minuti: app_commands.Range[int, 1, 40320], motivo: str = "Nessun motivo"):
        if not inter.permissions.moderate_members or not hier(inter, utente): return await send(inter, "⛔ Permessi insufficienti.", ephemeral=True)
        try: await utente.timeout(timedelta(minutes=minuti), reason=f"{inter.user}: {motivo}"); await send(inter, f"🔇 {utente} in timeout per {minuti} min.")
        except discord.HTTPException: await send(inter, "❌ Non posso sospendere questo utente.", ephemeral=True)
    @app_commands.describe(quantita="Messaggi da eliminare")
    @app_commands.guild_only()
    async def clear(inter: discord.Interaction, quantita: app_commands.Range[int, 1, 100]):
        if not inter.permissions.manage_messages: return await send(inter, "⛔ Permessi insufficienti.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        n = len(await inter.channel.purge(limit=quantita)); await inter.followup.send(f"🧹 Eliminati {n} messaggi.", ephemeral=True)
    for fn, d in ((kick, "Espelle un utente"), (ban, "Banna un utente"), (timeout, "Sospende un utente"), (clear, "Elimina messaggi")):
        out.append(app_commands.Command(name=fn.__name__, description=d, callback=fn))
    return out

class ChildBot(discord.Client):
    def __init__(self, pid, members=True):
        intents = discord.Intents.default(); intents.members = members
        super().__init__(intents=intents, allowed_mentions=NO_PINGS)
        self.pid, self.started_at, self.conn_task, self.sched_task = pid, None, None, None
        self.tree = app_commands.CommandTree(self); self.tree.on_error = self.on_tree_error

    @property
    def p(self): return store.d["bots"][self.pid]

    def in_scope(self, guild):
        gid = self.p.get("guild_id"); return not gid or (guild and guild.id == int(gid))

    async def setup_hook(self):
        await self.rebuild_tree()
        self.sched_task = asyncio.create_task(self.scheduler())

    async def rebuild_tree(self):
        p = self.p; guild = discord.Object(int(p["guild_id"])) if p.get("guild_id") else None
        self.tree.clear_commands(guild=guild)
        self.tree.add_command(generic_cmd(self.pid), guild=guild)
        if p["modules"].get("moderazione"):
            for c in mod_commands(): self.tree.add_command(c, guild=guild)
        slash = ctx = 0
        for n, c in p["commands"].items():
            if not c["enabled"]: continue
            if c["type"] == "contestuale":
                if ctx >= 5: continue
                async def ccb(inter: discord.Interaction, member: discord.Member, n=n): await run_custom(inter, self.pid, n, target=member)
                self.tree.add_command(app_commands.ContextMenu(name=n[:32], callback=ccb), guild=guild); ctx += 1
            elif slash < TOP_LEVEL_SLASH:
                async def scb(inter: discord.Interaction, n=n): await run_custom(inter, self.pid, n)
                self.tree.add_command(app_commands.Command(name=n, description=c["description"][:100] or "Comando", callback=scb), guild=guild); slash += 1
        await self.tree.sync(guild=guild)

    async def on_tree_error(self, inter, error):
        err = getattr(error, "original", error)
        record_error(self.pid, "comando", inter.command.name if inter.command else "?", err)
        try: await send(inter, "⚠️ Si è verificato un errore. Il proprietario può vederlo con /log-errori.", ephemeral=True)
        except Exception: pass

    async def scheduler(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                due = [s for s in self.p["scheduled"] if s["ts"] <= time.time()]
                for s in due:
                    self.p["scheduled"].remove(s)
                    ch = self.get_channel(s["channel"])
                    if not ch or not ch.permissions_for(ch.guild.me).send_messages:
                        record_error(self.pid, "programma-messaggio", s["id"], "Canale non raggiungibile o permessi mancanti"); continue
                    await ch.send(s["content"])
                if due: store.save()
            except Exception as e: record_error(self.pid, "programma-messaggio", "-", e)
            await asyncio.sleep(20)

    async def log(self, guild, event, title, desc):
        l = self.p.get("log")
        if not l or not self.p["modules"].get("log") or event not in l["events"] or not self.in_scope(guild): return
        ch = self.get_channel(l["channel"])
        if ch and ch.permissions_for(ch.guild.me).send_messages:
            await ch.send(embed=discord.Embed(title=title, description=desc[:4000], color=self.p["embed_color"], timestamp=datetime.now(timezone.utc)))

    async def on_message_delete(self, m):
        if m.guild and not m.author.bot: await self.log(m.guild, "messaggi_eliminati", "🗑️ Messaggio eliminato", f"{m.author.mention} in {m.channel.mention}\n{m.content or '*(vuoto)*'}")
    async def on_message_edit(self, b, a):
        if a.guild and not a.author.bot and b.content != a.content: await self.log(a.guild, "messaggi_modificati", "✏️ Messaggio modificato", f"{a.author.mention} in {a.channel.mention}\n**Prima:** {b.content}\n**Dopo:** {a.content}")
    async def on_member_remove(self, m): await self.log(m.guild, "uscite", "📤 Uscita", f"{m} ha lasciato il server.")
    async def on_member_ban(self, g, u): await self.log(g, "ban", "🔨 Ban", f"{u} è stato bannato.")

    async def on_member_join(self, m):
        await self.log(m.guild, "ingressi", "📥 Ingresso", f"{m.mention} è entrato nel server.")
        p = self.p; w = p.get("welcome")
        if not w or not p["modules"].get("benvenuto") or not self.in_scope(m.guild): return
        ch = m.guild.get_channel(w["channel"])
        if not ch or not ch.permissions_for(m.guild.me).send_messages: return record_error(self.pid, "benvenuto", "-", "Canale non raggiungibile")
        f = lambda s: s.replace("{user}", m.mention).replace("{username}", m.display_name).replace("{server}", m.guild.name).replace("{members}", str(m.guild.member_count))
        view = None
        if w.get("button_label"):
            view = discord.ui.View(timeout=None); view.add_item(discord.ui.Button(label=w["button_label"][:80], custom_id="bf|welcome"))
        if w.get("embed", True): await ch.send(embed=make_embed({"title": w.get("title"), "description": w["text"], "image": w.get("image")}, p, f), view=view)
        else: await ch.send(f(w["text"]), view=view)

    async def on_interaction(self, inter):
        if inter.type != discord.InteractionType.component: return
        parts = (inter.data.get("custom_id") or "").split("|")
        if parts[0] != "bf": return
        try: await self.component(inter, parts)
        except Exception as e:
            record_error(self.pid, "componente", "|".join(parts), e)
            try: await send(inter, "⚠️ Errore nell'esecuzione dell'azione.", ephemeral=True)
            except Exception: pass

    async def component(self, inter, parts):
        p, kind = self.p, parts[1]; f = make_fmt(inter)
        if kind in ("btn", "menu"):
            comp = p["components"].get(parts[2])
            if not comp: return await send(inter, "Elemento non più disponibile.", ephemeral=True)
            if not allowed(inter.user, comp): return await send(inter, "⛔ Non hai i permessi per usare questo elemento.", ephemeral=True)
            idx = int(parts[3]) if kind == "btn" else int(inter.data["values"][0])
            content, embed = build_message(p, {"text": comp["items"][idx]["response"]}, f)
            await send(inter, content, embed=embed, ephemeral=True)
        elif kind == "cmd": await run_custom(inter, self.pid, parts[2], click=True)
        elif kind == "welcome":
            w = p.get("welcome") or {}; content, embed = build_message(p, {"text": w.get("button_text") or "Benvenuto!"}, f)
            await send(inter, content, embed=embed, ephemeral=True)
        elif kind == "ticket" and parts[2] == "open": await self.ticket_open(inter)
        elif kind == "ticket" and parts[2] == "close": await self.ticket_close(inter)

    async def ticket_open(self, inter):
        p = self.p; t = p.get("ticket"); g = inter.guild
        if not t or not g: return await send(inter, "Ticket non configurati.", ephemeral=True)
        old = discord.utils.get(g.text_channels, topic=f"bf-ticket:{inter.user.id}")
        if old: return await send(inter, f"Hai già un ticket aperto: {old.mention}", ephemeral=True)
        sup = g.get_role(t["role"]) if t.get("role") else None
        ow = {g.default_role: discord.PermissionOverwrite(view_channel=False),
              inter.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
              g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)}
        if sup: ow[sup] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        try:
            ch = await g.create_text_channel("ticket-" + (re.sub(r"[^a-z0-9]", "", inter.user.name.lower())[:20] or "utente"),
                                             overwrites=ow, category=g.get_channel(t["category"]) if t.get("category") else None,
                                             topic=f"bf-ticket:{inter.user.id}", reason="Ticket BotForge")
        except discord.Forbidden:
            record_error(self.pid, "ticket", "open", "Permesso Gestisci canali mancante")
            return await send(inter, "❌ Il bot non può creare canali: serve il permesso *Gestisci canali*.", ephemeral=True)
        v = discord.ui.View(timeout=None); v.add_item(discord.ui.Button(label="Chiudi ticket", style=discord.ButtonStyle.danger, custom_id="bf|ticket|close"))
        await ch.send(f"{inter.user.mention} {sup.mention if sup else ''}", embed=make_embed({"title": "🎫 Ticket aperto", "description": "Descrivi il problema: lo staff ti risponderà qui."}, p), view=v)
        await send(inter, f"✅ Ticket creato: {ch.mention}", ephemeral=True)
        await self.ticket_log(g, f"🎫 Ticket aperto da {inter.user.mention}: {ch.mention}")

    async def ticket_close(self, inter):
        t = self.p.get("ticket") or {}; ch = inter.channel
        owner = (getattr(ch, "topic", "") or "") == f"bf-ticket:{inter.user.id}"
        sup = any(r.id == t.get("role") for r in getattr(inter.user, "roles", []))
        if not (owner or sup or inter.permissions.manage_channels): return await send(inter, "⛔ Non puoi chiudere questo ticket.", ephemeral=True)
        await send(inter, "🔒 Ticket chiuso, il canale verrà eliminato tra 5 secondi.")
        await self.ticket_log(inter.guild, f"🔒 Ticket **{ch.name}** chiuso da {inter.user.mention}")
        await asyncio.sleep(5)
        try: await ch.delete(reason="Ticket chiuso")
        except discord.HTTPException as e: record_error(self.pid, "ticket", "close", e)

    async def ticket_log(self, guild, text):
        t = self.p.get("ticket") or {}; ch = guild.get_channel(t.get("log")) if t.get("log") else None
        if ch and ch.permissions_for(guild.me).send_messages: await ch.send(text)

async def fetch_bot_info(token):
    try:
        async with aiohttp.ClientSession() as s, s.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {token}"}) as r:
            return await r.json() if r.status == 200 else None
    except aiohttp.ClientError: return None

async def start_child(p):
    """Avvia un bot. Ritorna None se ok, altrimenti il testo dell'errore da correggere."""
    pid = p["id"]
    if pid in RUNNING: return "Il bot è già online."
    if len(RUNNING) >= MAX_RUNNING: return "Capacità dell'hosting esaurita: riprova più tardi."
    try: token = dec(p["token"])
    except Exception: return "Token non leggibile: reimpostalo con /configura-bot (cambia_token) o verifica BOTFORGE_KEY."
    for members in (True, False):
        c = ChildBot(pid, members)
        try: await c.login(token)
        except discord.LoginFailure: await c.close(); return "Token non valido: reimpostalo con /configura-bot (cambia_token)."
        except discord.Forbidden:
            await c.close(); return "Il bot non è nel server di destinazione o manca lo scope *applications.commands*: usa il link d'invito di /stato-bot."
        except Exception as e: await c.close(); return f"Errore di avvio: {e}"
        c.conn_task = asyncio.create_task(c.connect(reconnect=True))
        ready = asyncio.ensure_future(c.wait_until_ready())
        await asyncio.wait({c.conn_task, ready}, timeout=40, return_when=asyncio.FIRST_COMPLETED)
        if ready.done() and not c.conn_task.done(): break
        ready.cancel()
        exc = c.conn_task.exception() if c.conn_task.done() else None
        await c.close()
        if isinstance(exc, discord.PrivilegedIntentsRequired) and members: continue  # riprova senza intent privilegiato
        return f"Connessione a Discord non riuscita: {exc or 'timeout'}"
    c.started_at = time.time(); RUNNING[pid] = c
    def _end(t, c=c):
        if RUNNING.get(pid) is c:
            RUNNING.pop(pid, None); p["status"] = "offline"
            if not t.cancelled() and t.exception(): record_error(pid, "hosting", "connessione", t.exception())
            store.dirty = True
    c.conn_task.add_done_callback(_end)
    p["status"], p["last_start"] = "online", now(); store.save()
    return None

async def stop_child(p):
    c = RUNNING.pop(p["id"], None)
    if c:
        if c.sched_task: c.sched_task.cancel()
        await c.close()
    p["status"] = "offline"; store.save()

async def commit(p, reload=False):
    p["updated"] = now(); store.save()
    c = RUNNING.get(p["id"])
    if reload and c:
        try: await c.rebuild_tree()
        except Exception as e: record_error(p["id"], "sync", "-", e)

async def child_send(p, channel_id, **kw):
    c = RUNNING.get(p["id"])
    if not c: raise RuntimeError("Il bot non è online: usa prima /metti-online.")
    ch = c.get_channel(channel_id)
    if ch is None:
        try: ch = await c.fetch_channel(channel_id)
        except discord.HTTPException: raise RuntimeError("Il bot non vede quel canale: è nello stesso server?")
    pm = ch.permissions_for(ch.guild.me)
    if not (pm.view_channel and pm.send_messages): raise RuntimeError("Il bot non può scrivere in quel canale (permessi).")
    return await ch.send(**kw)

async def try_send(inter, p, channel_id, **kw):
    try: return await child_send(p, channel_id, **kw)
    except RuntimeError as e: await send(inter, f"❌ {e}", ephemeral=True)
    except discord.HTTPException as e: await send(inter, f"❌ Invio non riuscito: {e}", ephemeral=True)

# ----------------------------------------------------------------- manager
class Manager(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=NO_PINGS)
        self.tree = app_commands.CommandTree(self)
    async def setup_hook(self):
        if DEV_GUILD_ID:
            g = discord.Object(DEV_GUILD_ID); self.tree.copy_global_to(guild=g); await self.tree.sync(guild=g)
        else: await self.tree.sync()
        asyncio.create_task(self.autosave()); asyncio.create_task(self.restore())
    async def autosave(self):
        while True:
            await asyncio.sleep(15)
            if store.dirty: store.save()
    async def restore(self):  # riavvia i bot che erano online (24/7 dopo un riavvio dell'hosting)
        await self.wait_until_ready()
        for p in list(store.d["bots"].values()):
            if p["status"] == "online":
                err = await start_child(p)
                if err: p["status"] = "offline"; record_error(p["id"], "hosting", "ripristino", err)
        store.save()
    async def on_ready(self): print(f"BotForge Studio online come {self.user}")

manager = Manager()
def cmd(name, desc): return manager.tree.command(name=name, description=desc)

async def ac_bot(inter, current):
    return [app_commands.Choice(name=f"{b['name']} ({'online' if b['id'] in RUNNING else 'offline'})"[:100], value=b["id"])
            for b in store.mine(inter.user.id) if current.lower() in b["name"].lower()][:25]

async def ac_cmd(inter, current):
    p = store.find(inter.user.id, getattr(inter.namespace, "bot", None))
    return [app_commands.Choice(name=n, value=n) for n in (p["commands"] if p else []) if current.lower() in n][:25]

async def get_p(inter, ref):
    p = store.find(inter.user.id, ref)
    if not p: await send(inter, "❌ Bot non trovato (o non sei il proprietario). Usa /miei-bot.", ephemeral=True)
    return p

async def get_c(inter, p, name):
    c = p["commands"].get((name or "").lower())
    if not c: await send(inter, f"❌ Comando `{name}` non trovato in **{p['name']}**.", ephemeral=True)
    return c

def guild_of(p):
    c = RUNNING.get(p["id"]); return c.get_guild(int(p["guild_id"])) if c and p.get("guild_id") else (c.guilds[0] if c and c.guilds else None)

def invite_url(p):
    return discord.utils.oauth_url(p["app_id"], permissions=INVITE_PERMS, scopes=("bot", "applications.commands")) if p.get("app_id") else "-"

def status_embed(p):
    c = RUNNING.get(p["id"]); on = c is not None
    e = discord.Embed(title=f"{'🟢' if on else '🔴'} {p['name']}", description=p["description"] or None, color=p["embed_color"])
    e.add_field(name="Stato", value="Online" if on else "Offline")
    e.add_field(name="Ultimo avvio", value=ago(p["last_start"]) if p["last_start"] else "mai")
    e.add_field(name="Attività", value=uptime(time.time() - c.started_at) if on else "—")
    if on: e.add_field(name="Connessione Discord", value=f"{round(c.latency * 1000)} ms" if math.isfinite(c.latency) else "in corso")
    errs = p["stats"]["errors"]
    e.add_field(name="Errori registrati", value=str(len(errs)) + (f"\nUltimo: {errs[-1]['msg'][:80]}" if errs else ""))
    e.add_field(name="Hosting", value=f"{len(RUNNING)}/{MAX_RUNNING} processi attivi")
    e.add_field(name="Comandi", value=f"{len(p['commands'])}/{MAX_COMMANDS}")
    e.add_field(name="Link d'invito", value=f"[Invita il bot]({invite_url(p)})", inline=False)
    return e

# ===== 1. CREAZIONE E GESTIONE DEI BOT =====
class TokenModal(discord.ui.Modal):
    def __init__(self, mode, data):
        super().__init__(title="Token del bot"); self.mode, self.data = mode, data
        self.token = discord.ui.TextInput(label="Token del bot (visibile solo a te)", min_length=50, max_length=120)
        self.add_item(self.token)
    async def on_submit(self, inter):
        await inter.response.defer(ephemeral=True)
        tok = self.token.value.strip(); info = await fetch_bot_info(tok)
        if not info: return await inter.followup.send("❌ Token non valido. Copialo di nuovo dal Developer Portal (Bot → Reset Token).", ephemeral=True)
        if self.mode == "change":
            p = store.d["bots"][self.data]; p["token"] = enc(tok); p["app_id"] = info["id"]; await commit(p)
            return await inter.followup.send("✅ Token aggiornato. Usa /riavvia-bot per applicarlo.", ephemeral=True)
        d = self.data
        p = new_project(inter.user.id, d["nome"], d["descrizione"], d["immagine"], d["lingua"], d["server_id"], tok, info["id"])
        if d["template"]: apply_template(p, d["template"])
        store.d["bots"][p["id"]] = p; store.save()
        e = discord.Embed(title="✅ Progetto creato", color=p["embed_color"], description=f"**{p['name']}** (`{info.get('username')}`) è pronto.")
        e.add_field(name="1. Invita il bot", value=f"[Link d'invito]({invite_url(p)})", inline=False)
        e.add_field(name="2. Aggiungi comandi", value="/crea-comando, /importa-template, /crea-embed…", inline=False)
        e.add_field(name="3. Pubblica", value="/prova-comando → /metti-online", inline=False)
        await inter.followup.send(embed=e, ephemeral=True)

@cmd("crea-bot", "Crea un nuovo bot Discord con configurazione guidata")
@app_commands.describe(nome="Nome del bot", descrizione="Descrizione", immagine="URL dell'immagine/avatar", lingua="Lingua",
                       server_id="ID del server di destinazione (vuoto = globale)", funzione_iniziale="Funzioni iniziali (template)")
@app_commands.choices(lingua=[app_commands.Choice(name="Italiano", value="it"), app_commands.Choice(name="English", value="en")],
                      funzione_iniziale=TEMPLATE_CHOICES)
async def crea_bot(inter: discord.Interaction, nome: app_commands.Range[str, 2, 32], descrizione: str = "", immagine: str = "",
                   lingua: str = "it", server_id: str = "", funzione_iniziale: str = ""):
    if len(store.mine(inter.user.id)) >= MAX_BOTS_PER_USER: return await send(inter, f"❌ Limite di {MAX_BOTS_PER_USER} bot per utente raggiunto.", ephemeral=True)
    if store.find(inter.user.id, nome): return await send(inter, "❌ Hai già un bot con questo nome.", ephemeral=True)
    if immagine and not immagine.startswith("http"): return await send(inter, "❌ L'immagine deve essere un URL http(s).", ephemeral=True)
    if server_id and not server_id.isdigit(): return await send(inter, "❌ L'ID server deve essere numerico.", ephemeral=True)
    await inter.response.send_modal(TokenModal("create", dict(nome=nome, descrizione=descrizione[:300], immagine=immagine, lingua=lingua,
                                                              server_id=server_id, template=funzione_iniziale)))

@cmd("configura-bot", "Modifica le impostazioni generali di un bot")
@app_commands.describe(bot="Bot", nome="Nuovo nome", descrizione="Descrizione", lingua="Lingua", prefisso="Prefisso per comandi tradizionali",
                       colore="Colore embed (esadecimale, es. 5865F2)", risposte_private="Risposte private di default", cambia_token="Imposta un nuovo token")
@app_commands.choices(lingua=[app_commands.Choice(name="Italiano", value="it"), app_commands.Choice(name="English", value="en")])
@app_commands.autocomplete(bot=ac_bot)
async def configura_bot(inter: discord.Interaction, bot: str, nome: str = None, descrizione: str = None, lingua: str = None,
                        prefisso: str = None, colore: str = None, risposte_private: bool = None, cambia_token: bool = False):
    p = await get_p(inter, bot)
    if not p: return
    if nome and nome != p["name"]:
        if store.find(inter.user.id, nome): return await send(inter, "❌ Nome già in uso.", ephemeral=True)
        p["name"] = nome
    if descrizione is not None: p["description"] = descrizione[:300]
    if lingua: p["lang"] = lingua
    if prefisso: p["prefix"] = prefisso[:5]
    if colore: p["embed_color"] = pc(colore, p["embed_color"])
    if risposte_private is not None: p["ephemeral_default"] = risposte_private
    await commit(p)
    if cambia_token: return await inter.response.send_modal(TokenModal("change", p["id"]))
    await send(inter, "✅ Impostazioni salvate (nessuna ricreazione necessaria).", ephemeral=True)

@cmd("metti-online", "Pubblica il bot sull'hosting BotForge e lo avvia")
@app_commands.describe(bot="Bot da avviare")
@app_commands.autocomplete(bot=ac_bot)
async def metti_online(inter: discord.Interaction, bot: str):
    p = await get_p(inter, bot)
    if not p: return
    await inter.response.defer(ephemeral=True)
    if not await fetch_bot_info(dec(p["token"])): return await inter.followup.send("❌ Token rifiutato da Discord: reimpostalo con /configura-bot.", ephemeral=True)
    err = await start_child(p)
    if err: return await inter.followup.send(f"❌ Non pubblicato: {err}", ephemeral=True)
    bad = [t for ok, t in await run_checks(p) if not ok]
    await inter.followup.send(f"🟢 **{p['name']}** è online." + ("\n⚠️ Da correggere:\n" + "\n".join("• " + b for b in bad) if bad else ""), ephemeral=True)

@cmd("metti-offline", "Arresta il bot senza eliminare nulla")
@app_commands.autocomplete(bot=ac_bot)
async def metti_offline(inter: discord.Interaction, bot: str):
    p = await get_p(inter, bot)
    if not p: return
    if p["id"] not in RUNNING: return await send(inter, "Il bot è già offline.", ephemeral=True)
    await inter.response.defer(ephemeral=True); await stop_child(p)
    await inter.followup.send("🔴 Bot offline. Progetto, comandi e impostazioni sono salvati.", ephemeral=True)

@cmd("riavvia-bot", "Riavvia il bot salvando prima la configurazione")
@app_commands.autocomplete(bot=ac_bot)
async def riavvia_bot(inter: discord.Interaction, bot: str):
    p = await get_p(inter, bot)
    if not p: return
    await inter.response.defer(ephemeral=True); store.save(); await stop_child(p)
    err = await start_child(p)
    await inter.followup.send(f"❌ Riavvio fallito: {err}" if err else "🔄 Riavviato: il bot è di nuovo operativo.", ephemeral=True)

@cmd("stato-bot", "Mostra lo stato effettivo del bot")
@app_commands.autocomplete(bot=ac_bot)
async def stato_bot(inter: discord.Interaction, bot: str):
    p = await get_p(inter, bot)
    if p: await send(inter, embed=status_embed(p), ephemeral=True)

class MyBotsView(discord.ui.View):
    def __init__(self, uid):
        super().__init__(timeout=300); self.uid, self.sel = uid, None
        s = discord.ui.Select(placeholder="Scegli un bot", options=[discord.SelectOption(label=b["name"][:100], value=b["id"],
                              description="Online" if b["id"] in RUNNING else "Offline") for b in store.mine(uid)[:25]])
        s.callback = self.pick; self.s = s; self.add_item(s)
    async def interaction_check(self, inter):
        if inter.user.id != self.uid: await inter.response.send_message("Non è il tuo menu.", ephemeral=True); return False
        return True
    async def pick(self, inter):
        self.sel = self.s.values[0]; await inter.response.send_message(f"Selezionato: **{store.d['bots'][self.sel]['name']}**", ephemeral=True)
    async def cur(self, inter):
        p = store.d["bots"].get(self.sel)
        if not p: await inter.response.send_message("Seleziona prima un bot dal menu.", ephemeral=True)
        return p
    @discord.ui.button(label="Avvia", style=discord.ButtonStyle.success)
    async def b_start(self, inter, _):
        p = await self.cur(inter)
        if p: await inter.response.defer(ephemeral=True); err = await start_child(p); await inter.followup.send(f"❌ {err}" if err else "🟢 Online.", ephemeral=True)
    @discord.ui.button(label="Ferma", style=discord.ButtonStyle.secondary)
    async def b_stop(self, inter, _):
        p = await self.cur(inter)
        if p: await inter.response.defer(ephemeral=True); await stop_child(p); await inter.followup.send("🔴 Offline.", ephemeral=True)
    @discord.ui.button(label="Impostazioni", style=discord.ButtonStyle.primary)
    async def b_set(self, inter, _):
        p = await self.cur(inter)
        if p: await inter.response.send_message(embed=status_embed(p), ephemeral=True)
    @discord.ui.button(label="Comandi", style=discord.ButtonStyle.primary)
    async def b_cmds(self, inter, _):
        p = await self.cur(inter)
        if p: await inter.response.send_message(embed=cmd_list_embed(p, "", "", 1), ephemeral=True)

@cmd("miei-bot", "Mostra tutti i tuoi bot")
async def miei_bot(inter: discord.Interaction):
    bots = store.mine(inter.user.id)
    if not bots: return await send(inter, "Non hai ancora bot. Inizia con **/crea-bot**.", ephemeral=True)
    e = discord.Embed(title="I tuoi bot", color=0x5865F2)
    for b in bots: e.add_field(name=f"{'🟢' if b['id'] in RUNNING else '🔴'} {b['name']}", value=f"{len(b['commands'])} comandi • modificato {ago(b['updated'])}", inline=False)
    await send(inter, embed=e, view=MyBotsView(inter.user.id), ephemeral=True)

@cmd("elimina-bot", "Elimina un progetto dopo conferma esplicita")
@app_commands.autocomplete(bot=ac_bot)
async def elimina_bot(inter: discord.Interaction, bot: str):
    p = await get_p(inter, bot)
    if not p: return
    async def yes(i):
        await stop_child(p); store.d["bots"].pop(p["id"], None); store.save(); await done(i, f"🗑️ **{p['name']}** eliminato.")
    await confirm(inter, f"⚠️ Stai per eliminare **{p['name']}** definitivamente.\nVerranno rimossi: {len(p['commands'])} comandi, impostazioni, "
                         f"{len(p['versions'])} versioni salvate, messaggi programmati, statistiche e token. Il bot Discord resterà sul tuo account Developer.", yes, label="Elimina definitivamente")

# ===== 2. COMANDI PERSONALIZZATI =====
ACTIONS = [app_commands.Choice(name="Nessuna", value="nessuna"), app_commands.Choice(name="Assegna ruolo", value="dai_ruolo"),
           app_commands.Choice(name="Rimuovi ruolo", value="togli_ruolo"), app_commands.Choice(name="Invia in DM", value="invia_dm")]
TYPES = [app_commands.Choice(name="Slash", value="slash"), app_commands.Choice(name="Menu contestuale (utente)", value="contestuale"),
         app_commands.Choice(name="Pulsante", value="pulsante")]

def split_fields(s): return [x for x in re.split(r"[,\s]+", s or "") if x]

@cmd("crea-comando", "Crea un comando personalizzato senza programmare")
@app_commands.describe(bot="Bot", nome="Nome (minuscolo)", descrizione="Descrizione", risposta="Testo di risposta ({user}, {server}, {members}, {campo}…)",
                       tipo="Tipo", titolo_embed="Se impostato, risponde con un embed", colore="Colore embed", immagine="URL immagine embed",
                       ruolo="Ruolo autorizzato", cooldown="Secondi tra due utilizzi", privata="Risposta visibile solo a chi lo usa",
                       campi="Campi compilabili (separati da virgola): apre un modulo", categoria="Categoria", azione="Azione extra",
                       ruolo_azione="Ruolo per l'azione", etichetta_pulsante="Testo del pulsante (tipo pulsante)")
@app_commands.choices(tipo=TYPES, azione=ACTIONS)
@app_commands.autocomplete(bot=ac_bot)
async def crea_comando(inter: discord.Interaction, bot: str, nome: str, descrizione: app_commands.Range[str, 1, 100], risposta: str,
                       tipo: str = "slash", titolo_embed: str = "", colore: str = "", immagine: str = "", ruolo: discord.Role = None,
                       cooldown: app_commands.Range[int, 0, 86400] = 0, privata: bool = False, campi: str = "", categoria: str = "generale",
                       azione: str = "nessuna", ruolo_azione: discord.Role = None, etichetta_pulsante: str = "Apri"):
    p = await get_p(inter, bot)
    if not p: return
    n = valid_name(nome)
    if not n: return await send(inter, "❌ Nome non valido: minuscole, numeri, - e _ (max 32).", ephemeral=True)
    if n in p["commands"] or n in ("cmd", "kick", "ban", "timeout", "clear"): return await send(inter, "❌ Nome già usato o riservato.", ephemeral=True)
    if len(p["commands"]) >= MAX_COMMANDS: return await send(inter, f"❌ Limite di {MAX_COMMANDS} comandi raggiunto.", ephemeral=True)
    if tipo == "contestuale" and sum(c["type"] == "contestuale" for c in p["commands"].values()) >= 5:
        return await send(inter, "❌ Discord ammette al massimo 5 menu contestuali utente per bot.", ephemeral=True)
    emb = {"title": titolo_embed or None, "description": risposta, "color": pc(colore, p["embed_color"]), "image": immagine or None} if (titolo_embed or immagine or colore) else None
    act = {"type": azione, "role": ruolo_azione.id if ruolo_azione else None} if azione != "nessuna" else None
    if act and act["type"] != "invia_dm" and not act["role"]: return await send(inter, "❌ L'azione scelta richiede `ruolo_azione`.", ephemeral=True)
    c = new_cmd(n, descrizione, risposta, type=tipo, category=categoria.lower()[:30], response={"text": risposta, "embed": emb},
                fields=split_fields(campi), roles=[ruolo.id] if ruolo else [], cooldown=cooldown, ephemeral=privata, action=act, button_label=etichetta_pulsante)
    pr = validate_command(c)
    if pr: return await send(inter, "❌ " + "\n".join(pr), ephemeral=True)
    p["commands"][n] = c; await commit(p, reload=True)
    await send(inter, f"✅ Comando `/{n}` creato ({len(p['commands'])}/{MAX_COMMANDS})." + (" Dopo il sync Discord può impiegare qualche istante." if p["id"] in RUNNING else " Sarà disponibile con /metti-online."), ephemeral=True)

def cmd_list_embed(p, cerca, categoria, pagina):
    items = [c for c in p["commands"].values() if cerca.lower() in c["name"] and (not categoria or c["category"] == categoria.lower())]
    per = 15; tot = max(1, math.ceil(len(items) / per)); pagina = min(max(pagina, 1), tot)
    e = discord.Embed(title=f"Comandi di {p['name']} ({len(items)})", color=p["embed_color"])
    for c in items[(pagina - 1) * per: pagina * per]:
        perm = "ruoli" if c["roles"] or c["users"] else "tutti"
        e.add_field(name=f"{'✅' if c['enabled'] else '⏸️'} {c['name']} • {c['type']}", value=f"{c['description'][:70]}\n📁 {c['category']} • 🔐 {perm} • 📊 {p['stats']['uses'].get(c['name'], 0)} usi", inline=False)
    e.set_footer(text=f"Pagina {pagina}/{tot}"); return e

@cmd("lista-comandi", "Elenca i comandi del bot con ricerca e filtri")
@app_commands.describe(bot="Bot", cerca="Cerca per nome", categoria="Filtra per categoria", pagina="Pagina")
@app_commands.autocomplete(bot=ac_bot)
async def lista_comandi(inter: discord.Interaction, bot: str, cerca: str = "", categoria: str = "", pagina: int = 1):
    p = await get_p(inter, bot)
    if p: await send(inter, embed=cmd_list_embed(p, cerca, categoria, pagina), ephemeral=True)

@cmd("modifica-comando", "Modifica un comando esistente senza ricrearlo")
@app_commands.describe(bot="Bot", comando="Comando", descrizione="Nuova descrizione", risposta="Nuovo testo", titolo_embed="Titolo embed", colore="Colore",
                       immagine="URL immagine", cooldown="Cooldown (s)", privata="Risposta privata", campi="Campi compilabili", ruolo="Ruolo autorizzato (sostituisce)",
                       etichetta_pulsante="Testo pulsante", categoria="Categoria")
@app_commands.autocomplete(bot=ac_bot, comando=ac_cmd)
async def modifica_comando(inter: discord.Interaction, bot: str, comando: str, descrizione: str = None, risposta: str = None, titolo_embed: str = None,
                           colore: str = None, immagine: str = None, cooldown: app_commands.Range[int, 0, 86400] = None, privata: bool = None,
                           campi: str = None, ruolo: discord.Role = None, etichetta_pulsante: str = None, categoria: str = None):
    p = await get_p(inter, bot)
    if not p: return
    c = await get_c(inter, p, comando)
    if not c: return
    new = json.loads(json.dumps(c)); r = new["response"]
    if descrizione is not None: new["description"] = descrizione
    if risposta is not None:
        r["text"] = risposta
        if r["embed"]: r["embed"]["description"] = risposta
    if titolo_embed is not None or colore or immagine is not None:
        r["embed"] = r["embed"] or {"description": r["text"], "title": None, "color": p["embed_color"], "image": None}
        if titolo_embed is not None: r["embed"]["title"] = titolo_embed or None
        if colore: r["embed"]["color"] = pc(colore, p["embed_color"])
        if immagine is not None: r["embed"]["image"] = immagine or None
    if cooldown is not None: new["cooldown"] = cooldown
    if privata is not None: new["ephemeral"] = privata
    if campi is not None: new["fields"] = split_fields(campi)
    if ruolo: new["roles"] = [ruolo.id]
    if etichetta_pulsante: new["button_label"] = etichetta_pulsante
    if categoria: new["category"] = categoria.lower()[:30]
    pr = validate_command(new, guild_of(p))
    if pr: return await send(inter, "❌ Modifiche non salvate:\n" + "\n".join("• " + x for x in pr), ephemeral=True)
    new["updated"] = now(); p["commands"][c["name"]] = new; await commit(p, reload=True)
    await send(inter, f"✅ `/{c['name']}` aggiornato.", ephemeral=True)

def duplicate_cmd(p, src, new_name):
    n = valid_name(new_name)
    if not n: return "Nome non valido."
    if n in p["commands"] or n in ("cmd", "kick", "ban", "timeout", "clear"): return "Nome già usato o riservato."
    if len(p["commands"]) >= MAX_COMMANDS: return f"Limite di {MAX_COMMANDS} comandi raggiunto."
    c = json.loads(json.dumps(p["commands"][src])); c.update(name=n, created=now(), updated=now())
    if c["type"] == "contestuale": c["type"] = "slash"
    p["commands"][n] = c; return None

@cmd("duplica-comando", "Crea una copia di un comando con un nuovo nome")
@app_commands.autocomplete(bot=ac_bot, comando=ac_cmd)
async def duplica_comando(inter: discord.Interaction, bot: str, comando: str, nuovo_nome: str):
    p = await get_p(inter, bot)
    if not p or not await get_c(inter, p, comando): return
    err = duplicate_cmd(p, comando.lower(), nuovo_nome)
    if err: return await send(inter, f"❌ {err}", ephemeral=True)
    await commit(p, reload=True); await send(inter, f"✅ `/{comando}` duplicato come `/{valid_name(nuovo_nome)}`.", ephemeral=True)

async def delete_cmd_flow(inter, p, name):
    async def yes(i):
        p["commands"].pop(name, None); await commit(p, reload=True); await done(i, f"🗑️ `/{name}` eliminato e registrazione Discord aggiornata.")
    await confirm(inter, f"Eliminare `/{name}` da **{p['name']}**? ({p['stats']['uses'].get(name, 0)} utilizzi registrati)", yes, label="Elimina")

@cmd("elimina-comando", "Rimuove un comando dopo conferma")
@app_commands.autocomplete(bot=ac_bot, comando=ac_cmd)
async def elimina_comando(inter: discord.Interaction, bot: str, comando: str):
    p = await get_p(inter, bot)
    if p and await get_c(inter, p, comando): await delete_cmd_flow(inter, p, comando.lower())

async def toggle_cmd(inter, p, name, on):
    c = p["commands"][name]
    if on:
        pr = validate_command(c, guild_of(p))
        if pr: return await send(inter, f"❌ `/{name}` non riattivabile:\n" + "\n".join("• " + x for x in pr), ephemeral=True)
    c["enabled"] = on; await commit(p, reload=True)
    await send(inter, f"{'✅ Attivato' if on else '⏸️ Disattivato'}: `/{name}`", ephemeral=True)

@cmd("attiva-comando", "Riattiva un comando disabilitato")
@app_commands.autocomplete(bot=ac_bot, comando=ac_cmd)
async def attiva_comando(inter: discord.Interaction, bot: str, comando: str):
    p = await get_p(inter, bot)
    if p and await get_c(inter, p, comando): await toggle_cmd(inter, p, comando.lower(), True)

@cmd("disattiva-comando", "Disabilita temporaneamente un comando")
@app_commands.autocomplete(bot=ac_bot, comando=ac_cmd)
async def disattiva_comando(inter: discord.Interaction, bot: str, comando: str):
    p = await get_p(inter, bot)
    if p and await get_c(inter, p, comando): await toggle_cmd(inter, p, comando.lower(), False)

class DupModal(discord.ui.Modal, title="Duplica comando"):
    nome = discord.ui.TextInput(label="Nuovo nome", max_length=32)
    def __init__(self, pid, src): super().__init__(); self.pid, self.src = pid, src
    async def on_submit(self, inter):
        p = store.d["bots"][self.pid]; err = duplicate_cmd(p, self.src, self.nome.value)
        if err: return await send(inter, f"❌ {err}", ephemeral=True)
        await commit(p, reload=True); await send(inter, "✅ Comando duplicato.", ephemeral=True)

class ManageView(discord.ui.View):
    def __init__(self, uid, pid, cerca=""):
        super().__init__(timeout=300); self.uid, self.pid, self.sel = uid, pid, None
        p = store.d["bots"][pid]; names = [n for n in p["commands"] if cerca.lower() in n][:25]
        self.s = discord.ui.Select(placeholder="Scegli un comando", options=[discord.SelectOption(label=n, description=("attivo" if p["commands"][n]["enabled"] else "disattivato")) for n in names])
        self.s.callback = self.pick; self.add_item(self.s)
    async def interaction_check(self, inter):
        if inter.user.id != self.uid: await inter.response.send_message("Non è il tuo menu.", ephemeral=True); return False
        return True
    async def pick(self, inter): self.sel = self.s.values[0]; await inter.response.send_message(f"Selezionato: `/{self.sel}`", ephemeral=True)
    async def ok(self, inter):
        if not self.sel or self.sel not in store.d["bots"][self.pid]["commands"]: await inter.response.send_message("Seleziona prima un comando.", ephemeral=True); return False
        return True
    @discord.ui.button(label="Attiva/Disattiva", style=discord.ButtonStyle.primary)
    async def b_tog(self, inter, _):
        if await self.ok(inter): p = store.d["bots"][self.pid]; await toggle_cmd(inter, p, self.sel, not p["commands"][self.sel]["enabled"])
    @discord.ui.button(label="Duplica", style=discord.ButtonStyle.secondary)
    async def b_dup(self, inter, _):
        if await self.ok(inter): await inter.response.send_modal(DupModal(self.pid, self.sel))
    @discord.ui.button(label="Elimina", style=discord.ButtonStyle.danger)
    async def b_del(self, inter, _):
        if await self.ok(inter): await delete_cmd_flow(inter, store.d["bots"][self.pid], self.sel)

@cmd("gestisci-comandi", "Menu interattivo per gestire i comandi del bot")
@app_commands.describe(bot="Bot", cerca="Filtra per nome (max 25 risultati mostrati)")
@app_commands.autocomplete(bot=ac_bot)
async def gestisci_comandi(inter: discord.Interaction, bot: str, cerca: str = ""):
    p = await get_p(inter, bot)
    if not p: return
    if not any(cerca.lower() in n for n in p["commands"]): return await send(inter, "Nessun comando trovato. Creane uno con /crea-comando.", ephemeral=True)
    await send(inter, f"Gestione comandi di **{p['name']}** (modifica i dettagli con /modifica-comando).", view=ManageView(inter.user.id, p["id"], cerca), ephemeral=True)

@cmd("limite-comandi", "Mostra quanti comandi hai creato e quanti ne restano")
@app_commands.autocomplete(bot=ac_bot)
async def limite_comandi(inter: discord.Interaction, bot: str):
    p = await get_p(inter, bot)
    if not p: return
    n = len(p["commands"]); bar = "█" * round(n / MAX_COMMANDS * 20) + "░" * (20 - round(n / MAX_COMMANDS * 20))
    extra = max(0, sum(c["enabled"] and c["type"] != "contestuale" for c in p["commands"].values()) - TOP_LEVEL_SLASH)
    await send(inter, f"**{p['name']}**: {n} usati • {MAX_COMMANDS - n} disponibili su {MAX_COMMANDS}\n`{bar}`" +
               (f"\nℹ️ Discord mostra max {TOP_LEVEL_SLASH} slash per bot: {extra} comandi si usano con `/cmd nome:`." if extra else ""), ephemeral=True)

@cmd("prova-comando", "Testa un comando prima della pubblicazione")
@app_commands.autocomplete(bot=ac_bot, comando=ac_cmd)
async def prova_comando(inter: discord.Interaction, bot: str, comando: str):
    p = await get_p(inter, bot)
    if not p: return
    c = await get_c(inter, p, comando)
    if not c: return
    pr = validate_command(c, guild_of(p))
    if pr: return await send(inter, f"❌ Test fallito per `/{c['name']}`:\n" + "\n".join(f"• {x}" for x in pr) + "\n\nCorreggi con /modifica-comando.", ephemeral=True)
    content, embed = build_message(p, c["response"], make_fmt(inter, {f: f"<{f}>" for f in c["fields"]}))
    await send(inter, f"✅ Test superato: campi, segnaposto, permessi e azioni sono validi. Anteprima:" + ("\n⚠️ Il comando è disattivato." if not c["enabled"] else ""), ephemeral=True)
    await send(inter, content, embed=embed, ephemeral=True)

# ===== 3. TEMPLATE, SALVATAGGI, TRASFERIMENTO =====
@cmd("importa-template", "Importa un template predefinito in un bot")
@app_commands.describe(bot="Bot", template="Template")
@app_commands.choices(template=TEMPLATE_CHOICES)
@app_commands.autocomplete(bot=ac_bot)
async def importa_template(inter: discord.Interaction, bot: str, template: str):
    p = await get_p(inter, bot)
    if not p: return
    new, skip, mods = template_diff(p, template)
    if not new and not mods: return await send(inter, "Nulla da aggiungere: il bot ha già tutto.", ephemeral=True)
    txt = f"**Template {template}** verrà aggiunto a **{p['name']}**:\n• Nuovi comandi: {', '.join('/' + c[0] for c in new) or 'nessuno'}\n• Moduli: {', '.join(mods) or 'nessuno'}" + \
          (f"\n• Già presenti (non sovrascritti): {', '.join(skip)}" if skip else "")
    async def yes(i): apply_template(p, template); await commit(p, reload=True); await done(i, "✅ Template importato.")
    await confirm(inter, txt, yes, label="Importa", style=discord.ButtonStyle.success)

@cmd("salva-configurazione", "Salva una versione della configurazione")
@app_commands.describe(bot="Bot", nota="Descrizione delle modifiche")
@app_commands.autocomplete(bot=ac_bot)
async def salva_configurazione(inter: discord.Interaction, bot: str, nota: str = "Salvataggio manuale"):
    p = await get_p(inter, bot)
    if not p: return
    vid = (p["versions"][-1]["id"] + 1) if p["versions"] else 1
    p["versions"].append({"id": vid, "t": now(), "author": inter.user.id, "note": nota[:200], "snap": snapshot(p)}); p["versions"] = p["versions"][-20:]
    await commit(p)
    await send(inter, f"💾 Versione **v{vid}** salvata (token escluso). Il bot online non è cambiato: usa /riavvia-bot per applicare modifiche già presenti.", ephemeral=True)

@cmd("cronologia-configurazioni", "Mostra le versioni salvate e ripristinane una")
@app_commands.autocomplete(bot=ac_bot)
async def cronologia(inter: discord.Interaction, bot: str):
    p = await get_p(inter, bot)
    if not p: return
    if not p["versions"]: return await send(inter, "Nessuna versione salvata. Usa /salva-configurazione.", ephemeral=True)
    e = discord.Embed(title=f"Versioni di {p['name']}", color=p["embed_color"])
    for v in reversed(p["versions"][-10:]): e.add_field(name=f"v{v['id']} • {ago(v['t'])}", value=f"<@{v['author']}> — {v['note']} ({len(v['snap']['commands'])} comandi)", inline=False)
    view = discord.ui.View(timeout=180)
    sel = discord.ui.Select(placeholder="Ripristina una versione…", options=[discord.SelectOption(label=f"v{v['id']} - {v['note'][:60]}", value=str(v["id"])) for v in reversed(p["versions"][-25:])])
    async def pick(i):
        if i.user.id != inter.user.id: return await i.response.send_message("Non è il tuo menu.", ephemeral=True)
        v = next(x for x in p["versions"] if str(x["id"]) == sel.values[0])
        async def yes(j):
            for k, val in json.loads(json.dumps(v["snap"])).items(): p[k] = val
            await commit(p, reload=True); await done(j, f"⏪ Ripristinata la versione v{v['id']}.")
        await confirm(i, f"Ripristinare **v{v['id']}**? La configurazione attuale verrà sostituita (salvala prima se serve).", yes, label="Ripristina")
    sel.callback = pick; view.add_item(sel)
    await send(inter, embed=e, view=view, ephemeral=True)

@cmd("esporta-configurazione", "Esporta la configurazione in JSON (senza token)")
@app_commands.autocomplete(bot=ac_bot)
async def esporta(inter: discord.Interaction, bot: str):
    p = await get_p(inter, bot)
    if not p: return
    data = {"botforge": 1, "name": p["name"], "exported": now(), **snapshot(p)}
    f = discord.File(io.BytesIO(json.dumps(data, ensure_ascii=False, indent=1).encode()), filename=f"{p['name']}.botforge.json")
    await send(inter, "📤 Esportazione pronta. Token e segreti non sono inclusi.", file=f, ephemeral=True)

@cmd("importa-configurazione", "Importa una configurazione esportata")
@app_commands.describe(bot="Bot di destinazione", file="File .json esportato")
@app_commands.autocomplete(bot=ac_bot)
async def importa_conf(inter: discord.Interaction, bot: str, file: discord.Attachment):
    p = await get_p(inter, bot)
    if not p: return
    if file.size > 1_000_000: return await send(inter, "❌ File troppo grande (max 1 MB).", ephemeral=True)
    try: data = json.loads((await file.read()).decode("utf-8"))
    except Exception: return await send(inter, "❌ Il file non è un JSON valido.", ephemeral=True)
    if not isinstance(data, dict) or data.get("botforge") != 1 or not isinstance(data.get("commands"), dict): return await send(inter, "❌ Formato non riconosciuto (manca la firma BotForge).", ephemeral=True)
    cmds = {}
    for n, d in data["commands"].items():
        if not isinstance(d, dict): continue
        c = clean_cmd({**d, "name": n}); pr = validate_command(c)
        if pr: return await send(inter, f"❌ Comando `{n}` non valido: {pr[0]}", ephemeral=True)
        cmds[c["name"]] = c
    conflicts = [n for n in cmds if n in p["commands"]]
    if len(set(p["commands"]) | set(cmds)) > MAX_COMMANDS: return await send(inter, f"❌ L'importazione supererebbe il limite di {MAX_COMMANDS} comandi.", ephemeral=True)
    def apply(over):
        async def go(i):
            for n, c in cmds.items():
                if over or n not in p["commands"]: p["commands"][n] = c
            for k in ("lang", "prefix", "embed_color", "style", "footer"):
                if k in data: p[k] = data[k]
            await commit(p, reload=True); await done(i, f"✅ Importati {len(cmds) - (0 if over else len(conflicts))} comandi.")
        return go
    txt = f"Il file contiene {len(cmds)} comandi. Moduli e canali legati a un altro server non vengono importati."
    if conflicts:
        await confirm(inter, txt + f"\n⚠️ Conflitti ({len(conflicts)}): {', '.join(conflicts[:15])}", apply(True), label="Sovrascrivi",
                      extra=[("Mantieni esistenti", discord.ButtonStyle.primary, apply(False))])
    else: await confirm(inter, txt, apply(True), label="Importa", style=discord.ButtonStyle.success)

# ===== 4. ASPETTO E MESSAGGI =====
@cmd("personalizza-bot", "Personalizza aspetto e stile delle risposte")
@app_commands.describe(bot="Bot", colore="Colore embed (esadecimale)", immagine="URL avatar", stile="Stile risposte", footer="Testo nel piè di pagina")
@app_commands.choices(stile=[app_commands.Choice(name="Embed", value="embed"), app_commands.Choice(name="Testo semplice", value="testo")])
@app_commands.autocomplete(bot=ac_bot)
async def personalizza(inter: discord.Interaction, bot: str, colore: str = None, immagine: str = None, stile: str = None, footer: str = None):
    p = await get_p(inter, bot)
    if not p: return
    await inter.response.defer(ephemeral=True); notes = []
    if colore: p["embed_color"] = pc(colore, p["embed_color"])
    if stile: p["style"] = stile
    if footer is not None: p["footer"] = footer[:200]
    if immagine:
        if not immagine.startswith("http"): return await inter.followup.send("❌ L'immagine deve essere un URL.", ephemeral=True)
        p["image"] = immagine; c = RUNNING.get(p["id"])
        if c:
            try:
                async with aiohttp.ClientSession() as s, s.get(immagine) as r: img = await r.read()
                await c.user.edit(avatar=img); notes.append("avatar aggiornato")
            except Exception as e: notes.append(f"avatar non aggiornato ({str(e)[:60]})")
        else: notes.append("l'avatar verrà applicato al prossimo avvio con /personalizza-bot online")
    await commit(p); await inter.followup.send("🎨 Aspetto salvato" + (f" ({'; '.join(notes)})" if notes else "") + ". Le funzioni interne non sono cambiate.", ephemeral=True)

def parse_embed_fields(s):
    out = []
    for part in (s or "").split(";"):
        if "|" in part: n, v = part.split("|", 1); out.append([n.strip(), v.strip()])
    return out[:25]

@cmd("crea-embed", "Crea e invia un embed con anteprima")
@app_commands.describe(bot="Bot che invierà il messaggio", canale="Canale di destinazione", titolo="Titolo", descrizione="Descrizione", colore="Colore",
                       immagine="URL immagine", footer="Piè di pagina", campi="Campi: Titolo|testo;Titolo|testo")
@app_commands.autocomplete(bot=ac_bot)
async def crea_embed(inter: discord.Interaction, bot: str, canale: discord.TextChannel, titolo: str = "", descrizione: str = "", colore: str = "",
                     immagine: str = "", footer: str = "", campi: str = ""):
    p = await get_p(inter, bot)
    if not p: return
    d = {"title": titolo or None, "description": descrizione or None, "color": pc(colore, p["embed_color"]), "image": immagine or None, "footer": footer or None, "fields": parse_embed_fields(campi)}
    if not (titolo or descrizione or d["fields"]): return await send(inter, "❌ Inserisci almeno titolo, descrizione o campi.", ephemeral=True)
    async def yes(i):
        m = await try_send(i, p, canale.id, embed=make_embed(d, p))
        if m: await done(i, f"✅ Embed inviato in {canale.mention}.")
    await confirm(inter, f"Anteprima — verrà inviato in {canale.mention} (il bot deve essere online):", yes, embed=make_embed(d, p), label="Invia", style=discord.ButtonStyle.success)

async def post_components(inter, bot, kind, canale, messaggio, pairs, ruolo):
    p = await get_p(inter, bot)
    if not p: return
    items = [{"label": l[:80], "response": r} for l, r in pairs if l and r]
    if not items: return await send(inter, "❌ Serve almeno una coppia etichetta + risposta.", ephemeral=True)
    cid = secrets.token_hex(3); view = discord.ui.View(timeout=None)
    if kind == "btn":
        for i, it in enumerate(items): view.add_item(discord.ui.Button(label=it["label"], custom_id=f"bf|btn|{cid}|{i}"))
    else:
        view.add_item(discord.ui.Select(placeholder="Scegli un'opzione…", custom_id=f"bf|menu|{cid}", options=[discord.SelectOption(label=it["label"], value=str(i)) for i, it in enumerate(items)]))
    m = await try_send(inter, p, canale.id, embed=make_embed({"description": messaggio}, p), view=view)
    if not m: return
    p["components"][cid] = {"type": kind, "channel": canale.id, "items": items, "roles": [ruolo.id] if ruolo else [], "users": []}
    await commit(p); await send(inter, f"✅ Pubblicato in {canale.mention}. Ogni scelta esegue solo le risposte configurate.", ephemeral=True)

@cmd("crea-menu", "Crea un menu interattivo a tendina")
@app_commands.describe(bot="Bot", canale="Canale", messaggio="Testo sopra il menu", opzione1="Etichetta 1", risposta1="Risposta 1", opzione2="Etichetta 2", risposta2="Risposta 2",
                       opzione3="Etichetta 3", risposta3="Risposta 3", opzione4="Etichetta 4", risposta4="Risposta 4", ruolo="Ruolo autorizzato")
@app_commands.autocomplete(bot=ac_bot)
async def crea_menu(inter: discord.Interaction, bot: str, canale: discord.TextChannel, messaggio: str, opzione1: str, risposta1: str,
                    opzione2: str = "", risposta2: str = "", opzione3: str = "", risposta3: str = "", opzione4: str = "", risposta4: str = "", ruolo: discord.Role = None):
    await post_components(inter, bot, "menu", canale, messaggio, [(opzione1, risposta1), (opzione2, risposta2), (opzione3, risposta3), (opzione4, risposta4)], ruolo)

@cmd("crea-pulsanti", "Crea un messaggio con pulsanti interattivi")
@app_commands.describe(bot="Bot", canale="Canale", messaggio="Testo del messaggio", pulsante1="Etichetta 1", risposta1="Risposta 1", pulsante2="Etichetta 2", risposta2="Risposta 2",
                       pulsante3="Etichetta 3", risposta3="Risposta 3", ruolo="Ruolo autorizzato")
@app_commands.autocomplete(bot=ac_bot)
async def crea_pulsanti(inter: discord.Interaction, bot: str, canale: discord.TextChannel, messaggio: str, pulsante1: str, risposta1: str,
                        pulsante2: str = "", risposta2: str = "", pulsante3: str = "", risposta3: str = "", ruolo: discord.Role = None):
    await post_components(inter, bot, "btn", canale, messaggio, [(pulsante1, risposta1), (pulsante2, risposta2), (pulsante3, risposta3)], ruolo)

# ===== 5. STRUMENTI PER AMMINISTRATORI DI SERVER =====
MODULE_CHOICES = [app_commands.Choice(name=k, value=k) for k in MODULES]

def module_missing(p, m):
    need = {"assistenza": ("ticket", "/configura-ticket"), "benvenuto": ("welcome", "/configura-benvenuto"), "log": ("log", "/crea-log")}
    if m in need and not p.get(need[m][0]): return need[m][1]
    return None

@cmd("attiva-modulo", "Attiva o disattiva un gruppo di funzioni predefinite")
@app_commands.describe(bot="Bot", modulo="Modulo", attiva="Attiva (sì) o disattiva (no)")
@app_commands.choices(modulo=MODULE_CHOICES)
@app_commands.autocomplete(bot=ac_bot)
async def attiva_modulo(inter: discord.Interaction, bot: str, modulo: str, attiva: bool = True):
    p = await get_p(inter, bot)
    if not p: return
    miss = module_missing(p, modulo) if attiva else None
    if miss: return await send(inter, f"⚠️ Prima completa le impostazioni con **{miss}**, poi il modulo si attiva da solo.", ephemeral=True)
    perms = {"assistenza": "Gestisci canali", "benvenuto": "Invia messaggi + intent Server Members", "log": "Invia messaggi (+ intent Server Members)", "moderazione": "Espellere, Bannare, Moderare membri, Gestire messaggi"}[modulo]
    async def yes(i): p["modules"][modulo] = attiva; await commit(p, reload=True); await done(i, f"✅ Modulo **{modulo}** {'attivato' if attiva else 'disattivato'}.")
    await confirm(inter, f"{'Attivare' if attiva else 'Disattivare'} **{modulo}**?\nIncluso: {MODULES[modulo]}\nPermessi richiesti al bot: {perms}", yes, label="Conferma", style=discord.ButtonStyle.success)

@cmd("permessi-comando", "Imposta chi può usare un comando")
@app_commands.describe(bot="Bot", comando="Comando", accesso="Chi può usarlo", ruolo1="Ruolo", ruolo2="Ruolo", ruolo3="Ruolo", utente1="Utente", utente2="Utente")
@app_commands.choices(accesso=[app_commands.Choice(name="Tutti", value="tutti"), app_commands.Choice(name="Ruoli o utenti selezionati", value="selezionati")])
@app_commands.autocomplete(bot=ac_bot, comando=ac_cmd)
async def permessi(inter: discord.Interaction, bot: str, comando: str, accesso: str, ruolo1: discord.Role = None, ruolo2: discord.Role = None,
                   ruolo3: discord.Role = None, utente1: discord.User = None, utente2: discord.User = None):
    p = await get_p(inter, bot)
    if not p: return
    c = await get_c(inter, p, comando)
    if not c: return
    roles = [r.id for r in (ruolo1, ruolo2, ruolo3) if r]; users = [u.id for u in (utente1, utente2) if u]
    if accesso == "selezionati" and not (roles or users): return await send(inter, "❌ Indica almeno un ruolo o un utente.", ephemeral=True)
    c["roles"], c["users"] = ([], []) if accesso == "tutti" else (roles, users)
    await commit(p); await send(inter, f"🔐 `/{c['name']}`: " + ("tutti possono usarlo." if accesso == "tutti" else f"{len(roles)} ruoli e {len(users)} utenti autorizzati (vale anche per pulsanti e menu).") + " Gli admin del server passano sempre.", ephemeral=True)

@cmd("crea-log", "Configura il registro degli eventi del server")
@app_commands.describe(bot="Bot", canale="Canale dei log", messaggi_eliminati="Messaggi eliminati", messaggi_modificati="Messaggi modificati",
                       ingressi="Ingressi", uscite="Uscite", ban="Ban")
@app_commands.autocomplete(bot=ac_bot)
async def crea_log(inter: discord.Interaction, bot: str, canale: discord.TextChannel, messaggi_eliminati: bool = True, messaggi_modificati: bool = True,
                   ingressi: bool = True, uscite: bool = True, ban: bool = True):
    p = await get_p(inter, bot)
    if not p: return
    ev = [k for k, v in dict(messaggi_eliminati=messaggi_eliminati, messaggi_modificati=messaggi_modificati, ingressi=ingressi, uscite=uscite, ban=ban).items() if v]
    if not ev: return await send(inter, "❌ Seleziona almeno un evento.", ephemeral=True)
    p["log"] = {"channel": canale.id, "events": ev}; p["modules"]["log"] = True; await commit(p)
    await send(inter, f"📋 Log attivo in {canale.mention}: {', '.join(ev)}.\nIl bot deve poter scrivere nel canale; ingressi/uscite richiedono l'intent *Server Members*.", ephemeral=True)

@cmd("configura-benvenuto", "Imposta il messaggio di benvenuto")
@app_commands.describe(bot="Bot", canale="Canale", testo="Testo ({user}, {username}, {server}, {members})", titolo="Titolo embed", immagine="URL immagine",
                       embed="Usa un embed", etichetta_pulsante="Testo di un pulsante opzionale", risposta_pulsante="Risposta al pulsante")
@app_commands.autocomplete(bot=ac_bot)
async def benvenuto(inter: discord.Interaction, bot: str, canale: discord.TextChannel, testo: str, titolo: str = "", immagine: str = "", embed: bool = True,
                    etichetta_pulsante: str = "", risposta_pulsante: str = ""):
    p = await get_p(inter, bot)
    if not p: return
    p["welcome"] = {"channel": canale.id, "text": testo, "title": titolo or None, "image": immagine or None, "embed": embed,
                    "button_label": etichetta_pulsante or None, "button_text": risposta_pulsante or None}
    p["modules"]["benvenuto"] = True; await commit(p)
    await send(inter, f"👋 Benvenuto attivo in {canale.mention}. Richiede l'intent *Server Members* attivo nel Developer Portal.", ephemeral=True)

@cmd("configura-ticket", "Configura il sistema di ticket e pubblica il pannello")
@app_commands.describe(bot="Bot (online)", canale="Canale del pannello", ruolo_supporto="Ruolo del supporto", categoria="Categoria dei ticket", titolo="Titolo", messaggio="Messaggio", canale_log="Canale log ticket")
@app_commands.autocomplete(bot=ac_bot)
async def ticket(inter: discord.Interaction, bot: str, canale: discord.TextChannel, ruolo_supporto: discord.Role, categoria: discord.CategoryChannel = None,
                 titolo: str = "🎫 Assistenza", messaggio: str = "Premi il pulsante per aprire un ticket privato con lo staff.", canale_log: discord.TextChannel = None):
    p = await get_p(inter, bot)
    if not p: return
    v = discord.ui.View(timeout=None); v.add_item(discord.ui.Button(label="Apri ticket", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="bf|ticket|open"))
    m = await try_send(inter, p, canale.id, embed=make_embed({"title": titolo, "description": messaggio}, p), view=v)
    if not m: return
    p["ticket"] = {"channel": canale.id, "role": ruolo_supporto.id, "category": categoria.id if categoria else None, "log": canale_log.id if canale_log else None}
    p["modules"]["assistenza"] = True; await commit(p)
    await send(inter, "✅ Pannello pubblicato. I ticket sono visibili solo a chi li apre e al ruolo di supporto; il bot ha bisogno di *Gestisci canali*.", ephemeral=True)

@cmd("programma-messaggio", "Pianifica un messaggio futuro")
@app_commands.describe(bot="Bot", canale="Canale", contenuto="Testo", data="Data AAAA-MM-GG", ora="Ora HH:MM", fuso="Fuso orario (es. Europe/Rome)")
@app_commands.autocomplete(bot=ac_bot)
async def programma(inter: discord.Interaction, bot: str, canale: discord.TextChannel, contenuto: app_commands.Range[str, 1, 2000], data: str, ora: str, fuso: str = "Europe/Rome"):
    p = await get_p(inter, bot)
    if not p: return
    try: dt = datetime.strptime(f"{data} {ora}", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(fuso))
    except Exception: return await send(inter, "❌ Data/ora/fuso non validi. Esempio: `2026-12-24` `18:30` `Europe/Rome`.", ephemeral=True)
    if dt.timestamp() <= time.time() + 30: return await send(inter, "❌ Scegli un momento futuro.", ephemeral=True)
    if len(p["scheduled"]) >= 50: return await send(inter, "❌ Massimo 50 messaggi programmati.", ephemeral=True)
    p["scheduled"].append({"id": secrets.token_hex(3), "channel": canale.id, "content": contenuto, "ts": int(dt.timestamp())}); await commit(p)
    await send(inter, f"🗓️ Programmato per <t:{int(dt.timestamp())}:F> in {canale.mention}. Il bot deve essere online e avere ancora i permessi all'invio (verificato in quel momento).", ephemeral=True)

@cmd("statistiche-bot", "Mostra le statistiche operative")
@app_commands.autocomplete(bot=ac_bot)
async def statistiche(inter: discord.Interaction, bot: str):
    p = await get_p(inter, bot)
    if not p: return
    uses = sorted(p["stats"]["uses"].items(), key=lambda x: -x[1]); c = RUNNING.get(p["id"])
    e = discord.Embed(title=f"📊 {p['name']}", color=p["embed_color"])
    e.add_field(name="Utilizzi totali", value=str(sum(p["stats"]["uses"].values())))
    e.add_field(name="Errori registrati", value=str(len(p["stats"]["errors"])))
    e.add_field(name="Hosting", value=f"online da {uptime(time.time() - c.started_at)}" if c else "offline")
    e.add_field(name="Più usati", value="\n".join(f"`/{n}` — {k}" for n, k in uses[:10]) or "Nessun dato", inline=False)
    e.set_footer(text="Dati basati sugli eventi effettivamente raccolti dal bot."); await send(inter, embed=e, ephemeral=True)

# ===== 6. DIAGNOSTICA, SICUREZZA, ASSISTENZA =====
async def run_checks(p):
    out = []; ok = lambda t: out.append((True, t)); ko = lambda t: out.append((False, t))
    try: dec(p["token"]); ok("Token presente e leggibile")
    except Exception: ko("Token non leggibile: reimpostalo con /configura-bot (cambia_token) o verifica BOTFORGE_KEY")
    c = RUNNING.get(p["id"]); g = guild_of(p)
    ok("Bot connesso a Discord") if c else ko("Bot offline: usa /metti-online")
    if c and p.get("guild_id") and not g: ko("Il bot non è nel server di destinazione: usa il link d'invito di /stato-bot")
    if c and not c.intents.members and (p["modules"].get("benvenuto") or (p.get("log") and p["modules"].get("log"))):
        ko("Intent *Server Members* non attivo: benvenuto e log ingressi/uscite non funzioneranno (attivalo nel Developer Portal)")
    en = [x for x in p["commands"].values() if x["enabled"]]
    ok(f"{len(en)} comandi attivi su {len(p['commands'])}")
    bad = [x["name"] for x in p["commands"].values() if validate_command(x, g)]
    ko(f"Comandi con problemi: {', '.join(bad[:10])} (usa /prova-comando)") if bad else ok("Tutti i comandi sono validi")
    def chan(cid):
        ch = g.get_channel(cid) if g else None
        return bool(ch and ch.permissions_for(g.me).send_messages)
    if g:
        for key, mod, label in (("welcome", "benvenuto", "Benvenuto"), ("log", "log", "Log")):
            if p.get(key) and p["modules"].get(mod): ok(f"{label}: canale raggiungibile") if chan(p[key]["channel"]) else ko(f"{label}: canale non trovato o senza permesso di scrittura")
        if p.get("ticket") and p["modules"].get("assistenza"):
            t = p["ticket"]; ok("Ticket: ruolo e permessi ok") if g.get_role(t["role"]) and g.me.guild_permissions.manage_channels else ko("Ticket: ruolo mancante o permesso *Gestisci canali* assente")
    late = [s for s in p["scheduled"] if s["ts"] < time.time() - 60]
    ko(f"{len(late)} messaggi programmati in ritardo (bot offline?)") if late else ok(f"{len(p['scheduled'])} messaggi programmati in coda")
    recent = [x for x in p["stats"]["errors"] if x["t"] > (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()]
    ko(f"{len(recent)} errori nelle ultime 24h (vedi /log-errori)") if recent else ok("Nessun errore nelle ultime 24h")
    return out

@cmd("diagnostica-bot", "Esegue controlli sul funzionamento del bot")
@app_commands.autocomplete(bot=ac_bot)
async def diagnostica(inter: discord.Interaction, bot: str):
    p = await get_p(inter, bot)
    if not p: return
    await inter.response.defer(ephemeral=True); res = await run_checks(p)
    e = discord.Embed(title=f"🩺 Diagnostica di {p['name']}", color=0x57F287 if all(o for o, _ in res) else 0xED4245,
                      description="\n".join(("✅ " if o else "❌ ") + t for o, t in res))
    await inter.followup.send(embed=e, ephemeral=True)

@cmd("log-errori", "Mostra gli errori recenti del bot")
@app_commands.autocomplete(bot=ac_bot)
async def log_errori(inter: discord.Interaction, bot: str):
    p = await get_p(inter, bot)
    if not p: return
    errs = p["stats"]["errors"][-10:][::-1]
    if not errs: return await send(inter, "✅ Nessun errore registrato.", ephemeral=True)
    e = discord.Embed(title=f"Errori recenti di {p['name']}", color=0xED4245)
    hints = {"hosting": "Controlla token e intent, poi /riavvia-bot.", "ticket": "Verifica il permesso Gestisci canali.", "benvenuto": "Controlla canale e permessi del bot.",
             "programma-messaggio": "Verifica che il bot sia online e abbia accesso al canale.", "azione": "Controlla gerarchia dei ruoli e DM aperti."}
    for x in errs: e.add_field(name=f"{ago(x['t'])} • {x['component']} • {x['command']}", value=f"{x['msg'][:200]}\n💡 {hints.get(x['component'], 'Esegui /diagnostica-bot.')}", inline=False)
    await send(inter, embed=e, ephemeral=True)  # visibile solo al proprietario (get_p)

@cmd("aiuto", "Guida di BotForge Studio")
async def aiuto(inter: discord.Interaction):
    bots = store.mine(inter.user.id)
    e = discord.Embed(title="📘 BotForge Studio", color=0x5865F2, description="Crea e gestisci bot Discord senza codice, interamente da Discord.")
    if not bots: e.add_field(name="Inizia da qui", value="**/crea-bot** → crea il progetto (ti servirà il token dal Developer Portal)\nPoi: /crea-comando → /prova-comando → /metti-online")
    else:
        step = "Aggiungi comandi con **/crea-comando** o **/importa-template**" if not any(b["commands"] for b in bots) else "Prova con **/prova-comando**, poi **/metti-online**"
        e.add_field(name="Prossimo passo", value=step, inline=False)
    e.add_field(name="Bot", value="/crea-bot /configura-bot /metti-online /metti-offline /riavvia-bot /stato-bot /miei-bot /elimina-bot", inline=False)
    e.add_field(name="Comandi", value="/crea-comando /lista-comandi /modifica-comando /duplica-comando /elimina-comando /attiva-comando /disattiva-comando /gestisci-comandi /limite-comandi /prova-comando", inline=False)
    e.add_field(name="Template e backup", value="/importa-template /salva-configurazione /cronologia-configurazioni /esporta-configurazione /importa-configurazione", inline=False)
    e.add_field(name="Aspetto", value="/personalizza-bot /crea-embed /crea-menu /crea-pulsanti", inline=False)
    e.add_field(name="Moduli server", value="/attiva-modulo /permessi-comando /crea-log /configura-benvenuto /configura-ticket /programma-messaggio /statistiche-bot", inline=False)
    e.add_field(name="Diagnostica", value="/diagnostica-bot /log-errori /aiuto /supporto", inline=False)
    e.set_footer(text=f"Ogni bot può avere fino a {MAX_COMMANDS} comandi personalizzati."); await send(inter, embed=e, ephemeral=True)

class SupportModal(discord.ui.Modal, title="Richiesta di supporto"):
    testo = discord.ui.TextInput(label="Descrivi il problema", style=discord.TextStyle.paragraph, max_length=1500)
    def __init__(self, cat, p): super().__init__(); self.cat, self.p = cat, p
    async def on_submit(self, inter):
        ref = "SUP-" + secrets.token_hex(3).upper(); diag = None
        if self.p: diag = {"bot": self.p["name"], "online": self.p["id"] in RUNNING, "comandi": len(self.p["commands"]), "errori": len(self.p["stats"]["errors"]), "moduli": [m for m, v in self.p["modules"].items() if v]}
        store.d["support"][ref] = {"user": inter.user.id, "cat": self.cat, "text": self.testo.value, "diag": diag, "status": "aperta", "t": now()}; store.save()
        ch = manager.get_channel(SUPPORT_CHANNEL_ID) if SUPPORT_CHANNEL_ID else None
        if ch: await ch.send(f"🆘 **{ref}** [{self.cat}] da <@{inter.user.id}>\n{self.testo.value[:1500]}\nDiagnostica (non sensibile): `{json.dumps(diag, ensure_ascii=False)}`")
        await inter.response.send_message(f"✅ Richiesta registrata. Riferimento: **{ref}** (controlla lo stato con `/supporto riferimento:{ref}`).", ephemeral=True)

@cmd("supporto", "Richiedi assistenza su BotForge Studio")
@app_commands.describe(categoria="Categoria", bot="Bot interessato (allega solo dati diagnostici non sensibili)", riferimento="Controlla lo stato di una richiesta")
@app_commands.choices(categoria=[app_commands.Choice(name=n, value=n) for n in ("hosting", "comandi", "permessi", "errore", "altro")])
@app_commands.autocomplete(bot=ac_bot)
async def supporto(inter: discord.Interaction, categoria: str = "altro", bot: str = "", riferimento: str = ""):
    if riferimento:
        r = store.d["support"].get(riferimento.upper())
        if not r or r["user"] != inter.user.id: return await send(inter, "❌ Riferimento non trovato.", ephemeral=True)
        return await send(inter, f"**{riferimento.upper()}** — stato: **{r['status']}** • categoria {r['cat']} • aperta {ago(r['t'])}", ephemeral=True)
    p = store.find(inter.user.id, bot) if bot else None
    await inter.response.send_modal(SupportModal(categoria, p))

# ===== 7. AMMINISTRAZIONE PIATTAFORMA =====
async def need_admin(inter):
    if not is_admin(inter.user.id): await send(inter, "⛔ Comando riservato agli amministratori di BotForge.", ephemeral=True); return False
    return True

@cmd("stato-hosting", "Stato dei servizi di hosting (solo admin)")
async def stato_hosting(inter: discord.Interaction):
    if not await need_admin(inter): return
    e = discord.Embed(title="🖥️ Hosting BotForge", color=0x5865F2)
    e.add_field(name="Processi attivi", value=f"{len(RUNNING)}/{MAX_RUNNING}"); e.add_field(name="Progetti totali", value=str(len(store.d["bots"])))
    e.add_field(name="Uptime piattaforma", value=uptime(time.time() - STARTED)); e.add_field(name="Latenza manager", value=f"{round(manager.latency * 1000)} ms")
    e.add_field(name="Python / discord.py", value=f"{sys.version.split()[0]} / {discord.__version__}")
    e.add_field(name="Errori hosting (24h)", value=str(sum(1 for b in store.d["bots"].values() for x in b["stats"]["errors"] if x["component"] == "hosting" and x["t"] > (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat())))
    e.set_footer(text="Nessun dato privato degli utenti viene mostrato."); await send(inter, embed=e, ephemeral=True)

@cmd("registro-attivita", "Registro delle operazioni amministrative (solo admin)")
async def registro(inter: discord.Interaction):
    if not await need_admin(inter): return
    rows = store.d["audit"][-15:][::-1]
    if not rows: return await send(inter, "Registro vuoto.", ephemeral=True)
    await send(inter, embed=discord.Embed(title="📜 Registro attività", color=0x5865F2, description="\n".join(f"{ago(r['t'])} • {r['op']} • <@{r['admin']}>" for r in rows)), ephemeral=True)

@cmd("gestisci-accessi", "Assegna o revoca accessi amministrativi (solo proprietari)")
@app_commands.describe(azione="Operazione", utente="Utente")
@app_commands.choices(azione=[app_commands.Choice(name=n, value=n) for n in ("lista", "assegna", "revoca")])
async def accessi(inter: discord.Interaction, azione: str, utente: discord.User = None):
    if not await need_admin(inter): return
    if azione == "lista":
        store.audit(inter.user.id, "consultazione accessi")
        return await send(inter, "**Proprietari (env):** " + (", ".join(f"<@{i}>" for i in OWNER_IDS) or "—") + "\n**Admin:** " + (", ".join(f"<@{i}>" for i in store.d["admins"]) or "—"), ephemeral=True)
    if inter.user.id not in OWNER_IDS: return await send(inter, "⛔ Solo i proprietari (OWNER_IDS) possono modificare gli accessi.", ephemeral=True)
    if not utente or utente.bot or utente.id in OWNER_IDS: return await send(inter, "❌ Indica un utente valido (non bot né proprietario).", ephemeral=True)
    async def yes(i):
        if azione == "assegna" and utente.id not in store.d["admins"]: store.d["admins"].append(utente.id)
        if azione == "revoca" and utente.id in store.d["admins"]: store.d["admins"].remove(utente.id)
        store.audit(inter.user.id, f"{azione} accesso a {utente.id}"); await done(i, f"✅ Accesso {azione}: {utente.mention}")
    await confirm(inter, f"Confermi: **{azione}** accesso amministrativo a {utente.mention}? L'operazione viene registrata.", yes, label="Conferma")

if __name__ == "__main__":
    if not TOKEN: sys.exit("Imposta la variabile d'ambiente DISCORD_TOKEN.")
    if not (Fernet and SECRET_KEY): print("⚠️  BOTFORGE_KEY non impostata: i token dei bot verranno salvati in chiaro.")
    manager.run(TOKEN)
