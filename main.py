import os
import time
import logging
from openai import OpenAI
from supabase import create_client
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

# ── Config ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["ADMIN_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
ADMIN_IDS      = set(int(x) for x in os.environ.get("ADMIN_IDS", "0").split(",") if x.strip())

openai_client = OpenAI(api_key=OPENAI_API_KEY)
supabase      = create_client(SUPABASE_URL, SUPABASE_KEY)

# Histórico de conversa por usuário
historico: dict[int, list] = {}

# ── Helpers ───────────────────────────────────────────────────────────────

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS or 0 in ADMIN_IDS

def gerar_embedding(texto: str) -> list[float]:
    resp = openai_client.embeddings.create(
        model="text-embedding-ada-002",
        input=texto[:8000]
    )
    return resp.data[0].embedding

def salvar_estudo(titulo: str, conteudo: str) -> int:
    texto_completo = f"{titulo}\n\n{conteudo}"
    embedding = gerar_embedding(texto_completo)
    result = supabase.table("documents").insert({
        "content": texto_completo,
        "metadata": {"titulo": titulo, "source": "admin_bot"},
        "embedding": embedding
    }).execute()
    total = supabase.table("documents").select("id", count="exact").execute()
    return total.count or 0

def contar_estudos() -> int:
    resp = supabase.table("documents").select("id", count="exact").execute()
    return resp.count or 0

def chamar_agente(mensagem: str, hist: list) -> tuple[str, dict | None]:
    """
    Chama GPT-4o como agente organizador.
    Retorna (resposta_texto, estudo_para_salvar_ou_None)
    """
    system = """Você é um assistente organizador de estudos bíblicos do pastor Eli Oliveira.

Sua função é ajudar a organizar e salvar estudos bíblicos no banco de dados.

Quando o usuário enviar um estudo (pode ser texto bruto, anotações, reflexões, sermões, etc):
1. Identifique ou crie um título adequado
2. Organize o conteúdo de forma clara
3. Confirme com o usuário antes de salvar

Quando o usuário confirmar (disse "sim", "salva", "pode salvar", "ok", "confirmar", etc):
- Retorne um JSON no seguinte formato EXATO no final da sua resposta:
  [SALVAR]{"titulo": "Título do Estudo", "conteudo": "Conteúdo organizado completo"}[/SALVAR]

Quando o usuário estiver apenas conversando ou pedindo ajustes, responda normalmente sem o JSON.

Seja sempre amigável, pastoral e eficiente. Fale em português do Brasil.
Se o usuário mandar um texto longo, organize-o bem antes de confirmar.
Pergunte se quer ajustes antes de salvar."""

    msgs = [{"role": "system", "content": system}]
    msgs.extend(hist[-10:])
    msgs.append({"role": "user", "content": mensagem})

    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=msgs,
        temperature=0.3,
        max_tokens=2000
    )

    texto = resp.choices[0].message.content

    # Verifica se tem instrução de salvar
    estudo = None
    if "[SALVAR]" in texto and "[/SALVAR]" in texto:
        import json
        try:
            inicio = texto.index("[SALVAR]") + len("[SALVAR]")
            fim = texto.index("[/SALVAR]")
            json_str = texto[inicio:fim].strip()
            estudo = json.loads(json_str)
            # Remove o bloco JSON da resposta visível
            texto = texto[:texto.index("[SALVAR]")].strip()
        except Exception as e:
            log.error(f"Erro ao parsear JSON do agente: {e}")

    return texto, estudo

# ── Handlers ──────────────────────────────────────────────────────────────

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
        f"Pode começar a me enviar os estudos normalmente, como fazia no n8n.\n"
        f"Basta escrever ou colar o texto — eu organizo, confirmo com você e salvo!\n\n"
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
    try:
        resp = supabase.table("documents").select("id, content, metadata") \
            .order("id", desc=True).limit(10).execute()
        docs = resp.data or []
        if not docs:
            await update.message.reply_text("Nenhum estudo encontrado.")
            return
        linhas = []
        for d in docs:
            titulo = (d.get("metadata") or {}).get("titulo") or (d.get("content") or "")[:50]
            linhas.append(f"• *{d['id']}* — {titulo[:55]}")
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
            f"📊 *Estatísticas*\n\n"
            f"📚 Estudos indexados: *{total_docs}*\n"
            f"📨 Perguntas respondidas: *{total_logs}*\n"
            f"👥 Usuários únicos: *{unicos}*",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")

async def responder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
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

        # Atualiza histórico
        historico[uid].append({"role": "user",      "content": texto})
        historico[uid].append({"role": "assistant",  "content": resposta})
        historico[uid] = historico[uid][-20:]

        # Se tem estudo para salvar
        if estudo and estudo.get("titulo") and estudo.get("conteudo"):
            await update.message.reply_text(
                f"{resposta}\n\n⏳ Salvando e gerando embedding...",
                parse_mode="Markdown"
            )
            inicio = time.time()
            total = salvar_estudo(estudo["titulo"], estudo["conteudo"])
            tempo = round(time.time() - inicio, 1)

            await update.message.reply_text(
                f"✅ *Estudo salvo com sucesso!*\n\n"
                f"📌 *{estudo['titulo']}*\n"
                f"📚 Total no banco: *{total}*\n"
                f"⏱ Tempo: {tempo}s\n\n"
                f"Pode enviar o próximo estudo!",
                parse_mode="Markdown"
            )
        else:
            # Resposta normal do agente
            for i in range(0, len(resposta), 4000):
                await update.message.reply_text(resposta[i:i+4000], parse_mode="Markdown")

    except Exception as e:
        log.error(f"Erro [{uid}]: {e}")
        await update.message.reply_text(f"⚠️ Erro: {e}")

# ── Main ──────────────────────────────────────────────────────────────────

def main():
    log.info("Iniciando Admin Bot...")
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

