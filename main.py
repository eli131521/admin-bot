import os
import time
import logging
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, filters, ContextTypes
)
from openai import OpenAI
from supabase import create_client

# ── Config ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["ADMIN_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]

# IDs autorizados a usar o bot admin (separe por vírgula no env)
ADMINS = set(int(x) for x in os.environ.get("ADMIN_IDS", "0").split(",") if x.strip())

openai_client = OpenAI(api_key=OPENAI_API_KEY)
supabase      = create_client(SUPABASE_URL, SUPABASE_KEY)

# Estados da conversa
AGUARDANDO_TITULO, AGUARDANDO_CONTEUDO, CONFIRMANDO = range(3)

# ── Helpers ───────────────────────────────────────────────────────────────

def is_admin(uid: int) -> bool:
    return uid in ADMINS or 0 in ADMINS  # 0 = sem restrição

def gerar_embedding(texto: str) -> list[float]:
    resp = openai_client.embeddings.create(
        model="text-embedding-ada-002",
        input=texto[:8000]
    )
    return resp.data[0].embedding

def salvar_estudo(titulo: str, conteudo: str) -> dict:
    texto_completo = f"{titulo}\n\n{conteudo}"
    embedding = gerar_embedding(texto_completo)
    result = supabase.table("documents").insert({
        "content": texto_completo,
        "metadata": {"titulo": titulo, "source": "admin_bot"},
        "embedding": embedding
    }).execute()
    return result.data[0] if result.data else {}

def contar_estudos() -> int:
    resp = supabase.table("documents").select("id", count="exact").execute()
    return resp.count or 0

# ── Handlers ──────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Acesso restrito. Você não é administrador.")
        return

    total = contar_estudos()
    await update.message.reply_text(
        f"✦ *Palavra Viva — Painel Admin*\n\n"
        f"📚 Estudos no banco: *{total}*\n\n"
        f"Comandos disponíveis:\n"
        f"/inserir — Adicionar novo estudo\n"
        f"/listar — Ver últimos estudos\n"
        f"/stats — Estatísticas do banco\n"
        f"/ajuda — Ver instruções",
        parse_mode="Markdown"
    )

async def cmd_ajuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Como inserir um estudo:*\n\n"
        "1. Digite /inserir\n"
        "2. Envie o *título* do estudo\n"
        "3. Envie o *texto completo* do estudo\n"
        "4. Confirme para salvar\n\n"
        "O embedding é gerado automaticamente! ✦",
        parse_mode="Markdown"
    )

async def cmd_listar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        resp = supabase.table("documents").select("id, content, metadata, criado_date") \
            .order("id", desc=True).limit(10).execute()
        docs = resp.data or []
        if not docs:
            await update.message.reply_text("Nenhum estudo encontrado.")
            return
        linhas = []
        for d in docs:
            titulo = (d.get("metadata") or {}).get("titulo") or (d.get("content") or "")[:50]
            linhas.append(f"• [{d['id']}] {titulo[:60]}")
        await update.message.reply_text(
            f"📚 *Últimos 10 estudos:*\n\n" + "\n".join(linhas),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        total_docs = contar_estudos()
        total_logs = supabase.table("bot_logs").select("id", count="exact").execute().count or 0
        usuarios   = supabase.table("bot_logs").select("user_id").execute()
        unicos     = len(set(d["user_id"] for d in (usuarios.data or [])))

        await update.message.reply_text(
            f"📊 *Estatísticas Gerais*\n\n"
            f"📚 Estudos indexados: *{total_docs}*\n"
            f"📨 Perguntas respondidas: *{total_logs}*\n"
            f"👥 Usuários únicos: *{unicos}*",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")

# ── Conversa para inserir estudo ──────────────────────────────────────────

async def cmd_inserir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Acesso restrito.")
        return ConversationHandler.END

    await update.message.reply_text(
        "✦ *Inserir Novo Estudo*\n\n"
        "Passo 1/2 — Envie o *título* do estudo:\n\n"
        "_(ou /cancelar para sair)_",
        parse_mode="Markdown"
    )
    return AGUARDANDO_TITULO

async def receber_titulo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    titulo = update.message.text.strip()
    if not titulo:
        await update.message.reply_text("Título não pode ser vazio. Tente novamente:")
        return AGUARDANDO_TITULO

    ctx.user_data["titulo"] = titulo
    await update.message.reply_text(
        f"✅ Título: *{titulo}*\n\n"
        f"Passo 2/2 — Agora envie o *texto completo* do estudo:\n\n"
        f"_(pode ser longo, envie tudo de uma vez)_",
        parse_mode="Markdown"
    )
    return AGUARDANDO_CONTEUDO

async def receber_conteudo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    conteudo = update.message.text.strip()
    if not conteudo:
        await update.message.reply_text("Conteúdo não pode ser vazio. Tente novamente:")
        return AGUARDANDO_CONTEUDO

    ctx.user_data["conteudo"] = conteudo
    titulo = ctx.user_data["titulo"]
    preview = conteudo[:200] + ("..." if len(conteudo) > 200 else "")
    palavras = len(conteudo.split())

    teclado = ReplyKeyboardMarkup([["✅ Confirmar", "❌ Cancelar"]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(
        f"📋 *Confirme o estudo:*\n\n"
        f"*Título:* {titulo}\n"
        f"*Palavras:* {palavras}\n"
        f"*Preview:* {preview}\n\n"
        f"Deseja salvar?",
        parse_mode="Markdown",
        reply_markup=teclado
    )
    return CONFIRMANDO

async def confirmar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    resposta = update.message.text.strip()

    if "Confirmar" in resposta:
        titulo  = ctx.user_data["titulo"]
        conteudo = ctx.user_data["conteudo"]

        await update.message.reply_text(
            "⏳ Gerando embedding e salvando...",
            reply_markup=ReplyKeyboardRemove()
        )

        try:
            inicio = time.time()
            salvar_estudo(titulo, conteudo)
            tempo = round(time.time() - inicio, 1)
            total = contar_estudos()

            await update.message.reply_text(
                f"✅ *Estudo salvo com sucesso!*\n\n"
                f"📚 Total no banco: *{total}*\n"
                f"⏱ Tempo: {tempo}s\n\n"
                f"Use /inserir para adicionar outro.",
                parse_mode="Markdown"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Erro ao salvar: {e}")

    else:
        await update.message.reply_text(
            "❌ Cancelado. Use /inserir para tentar novamente.",
            reply_markup=ReplyKeyboardRemove()
        )

    ctx.user_data.clear()
    return ConversationHandler.END

async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "❌ Operação cancelada.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

# ── Main ──────────────────────────────────────────────────────────────────

def main():
    log.info("Iniciando bot Admin...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("inserir", cmd_inserir)],
        states={
            AGUARDANDO_TITULO:   [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_titulo)],
            AGUARDANDO_CONTEUDO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_conteudo)],
            CONFIRMANDO:         [MessageHandler(filters.TEXT & ~filters.COMMAND, confirmar)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("ajuda",   cmd_ajuda))
    app.add_handler(CommandHandler("listar",  cmd_listar))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(conv)

    log.info("Admin bot rodando!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
