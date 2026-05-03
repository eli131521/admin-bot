import os, time, logging, json
from datetime import datetime, timezone
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from openai import OpenAI
from supabase import create_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["ADMIN_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
ADMIN_IDS      = set(int(x) for x in os.environ.get("ADMIN_IDS", "0").split(",") if x.strip())

openai_client = OpenAI(api_key=OPENAI_API_KEY)
supabase      = create_client(SUPABASE_URL, SUPABASE_KEY)
historico: dict[int, list] = {}

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS or 0 in ADMIN_IDS

def gerar_embedding(texto: str) -> list[float]:
    resp = openai_client.embeddings.create(model="text-embedding-ada-002", input=texto[:8000])
    return resp.data[0].embedding

def salvar_estudo(titulo: str, conteudo: str) -> int:
    texto_completo = f"{titulo}\n\n{conteudo}"
    embedding = gerar_embedding(texto_completo)
    agora = datetime.now(timezone.utc)
    supabase.table("documents").insert({
        "content": texto_completo,
        "metadata": {"titulo": titulo, "source": "admin_bot"},
        "embedding": embedding,
        "criado_date": agora.strftime("%Y-%m-%d"),
        "criado_ts": agora.isoformat(),
    }).execute()
    total = supabase.table("documents").select("id", count="exact").execute()
    return total.count or 0

def contar_estudos() -> int:
    return supabase.table("documents").select("id", count="exact").execute().count or 0

def chamar_agente(mensagem: str, hist: list) -> tuple[str, dict | None]:
    system = """Você é um assistente organizador de estudos bíblicos do Eli Oliveira.

Quando o usuário enviar um estudo (texto bruto, anotações, reflexões, sermão):
1. Identifique ou crie um título adequado
2. Organize o conteúdo de forma clara mantendo as palavras e expressões do Eli
3. Mostre como ficou organizado e pergunte se quer ajustes ou pode salvar

Quando o usuário confirmar para salvar (disse "sim", "salva", "pode salvar", "ok", "confirmar", "salve"):
Retorne OBRIGATORIAMENTE este bloco no final da resposta:
[SALVAR]{"titulo": "Título aqui", "conteudo": "Conteúdo completo organizado aqui"}[/SALVAR]

Quando o usuário pedir ajustes, faça os ajustes e mostre novamente sem o bloco [SALVAR].
Quando for apenas conversa ou dúvida, responda normalmente.

Seja direto e eficiente. Português do Brasil."""

    msgs = [{"role": "system", "content": system}]
    msgs.extend(hist[-12:])
    msgs.append({"role": "user", "content": mensagem})

    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini", messages=msgs, temperature=0.3, max_tokens=2000
    )
    texto = resp.choices[0].message.content
    estudo = None

    if "[SALVAR]" in texto and "[/SALVAR]" in texto:
        try:
            ini = texto.index("[SALVAR]") + len("[SALVAR]")
            fim = texto.index("[/SALVAR]")
            estudo = json.loads(texto[ini:fim].strip())
            texto  = texto[:texto.index("[SALVAR]")].strip()
        except Exception as e:
            log.error(f"Erro parse JSON: {e}")

    return texto, estudo

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Acesso restrito.")
        return
    total = contar_estudos()
    historico[uid] = []
    await update.message.reply_text(
        f"✦ *Palavra Viva — Admin*\n\n"
        f"Olá, Eli! 👋\n"
        f"📚 Estudos no banco: *{total}*\n\n"
        f"Pode me enviar os estudos normalmente — eu organizo, confirmo com você e salvo com embedding!\n\n"
        f"Comandos:\n"
        f"/novo — Limpar conversa\n"
        f"/listar — Ver últimos estudos\n"
        f"/stats — Estatísticas",
        parse_mode="Markdown"
    )

async def cmd_novo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    historico[update.effective_user.id] = []
    await update.message.reply_text("✦ Conversa limpa! Pode enviar o próximo estudo.")

async def cmd_listar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    resp = supabase.table("documents").select("id, content, metadata, criado_date") \
        .order("id", desc=True).limit(10).execute()
    docs = resp.data or []
    if not docs:
        await update.message.reply_text("Nenhum estudo encontrado.")
        return
    linhas = []
    for d in docs:
        titulo = (d.get("metadata") or {}).get("titulo") or (d.get("content") or "")[:50]
        data   = d.get("criado_date") or "sem data"
        linhas.append(f"• *{d['id']}* [{data}] — {titulo[:50]}")
    await update.message.reply_text(
        "📚 *Últimos 10 estudos:*\n\n" + "\n".join(linhas),
        parse_mode="Markdown"
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_docs = contar_estudos()
    total_logs = supabase.table("bot_logs").select("id", count="exact").execute().count or 0
    usuarios   = supabase.table("bot_logs").select("user_id").execute()
    unicos     = len(set(d["user_id"] for d in (usuarios.data or [])))
    await update.message.reply_text(
        f"📊 *Estatísticas*\n\n"
        f"📚 Estudos indexados: *{total_docs}*\n"
        f"📨 Perguntas respondidas: *{total_logs}*\n"
        f"👥 Usuários únicos: *{unicos}*",
        parse_mode="Markdown"
    )

async def responder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    texto = update.message.text.strip()
    if not is_admin(uid):
        await update.message.reply_text("⛔ Acesso restrito.")
        return
    if not texto:
        return
    if uid not in historico:
        historico[uid] = []

    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        resposta, estudo = chamar_agente(texto, historico[uid])

        historico[uid].append({"role": "user",      "content": texto})
        historico[uid].append({"role": "assistant",  "content": resposta})
        historico[uid] = historico[uid][-20:]

        if estudo and estudo.get("titulo") and estudo.get("conteudo"):
            await update.message.reply_text(f"{resposta}\n\n⏳ Salvando e gerando embedding...")
            inicio = time.time()
            total  = salvar_estudo(estudo["titulo"], estudo["conteudo"])
            tempo  = round(time.time() - inicio, 1)
            await update.message.reply_text(
                f"✅ *Estudo salvo!*\n\n"
                f"📌 *{estudo['titulo']}*\n"
                f"📚 Total no banco: *{total}*\n"
                f"⏱ Tempo: {tempo}s\n\n"
                f"Pode enviar o próximo!",
                parse_mode="Markdown"
            )
        else:
            for i in range(0, len(resposta), 4000):
                await update.message.reply_text(resposta[i:i+4000])

    except Exception as e:
        log.error(f"Erro [{uid}]: {e}")
        await update.message.reply_text(f"⚠️ Erro: {e}")

def main():
    log.info("Iniciando Admin Bot v3...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("novo",   cmd_novo))
    app.add_handler(CommandHandler("listar", cmd_listar))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))
    log.info("Admin bot rodando!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
