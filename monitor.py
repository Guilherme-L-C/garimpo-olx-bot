import os
import re
import json
import sqlite3
import time
import urllib.parse
import threading
from datetime import datetime
from curl_cffi import requests as cffi_requests
import requests
from bs4 import BeautifulSoup
from google import genai
import benchmarks
import market_analytics

from dotenv import load_dotenv

# Carrega configurações e segredos do arquivo .env
load_dotenv()

# ================= CONFIGURAÇÕES =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "ofertas.db"))
INTERVALO_SEGUNDOS = int(os.getenv("INTERVALO_SEGUNDOS", "90"))

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("TELEGRAM_TOKEN e GEMINI_API_KEY precisam estar configurados no arquivo .env")

ai_client = genai.Client(api_key=GEMINI_API_KEY)


# ================= BANCO DE DADOS =================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    # Tabela de anúncios
    c.execute("""
        CREATE TABLE IF NOT EXISTS ads (
            id TEXT PRIMARY KEY,
            title TEXT,
            price REAL,
            url TEXT,
            score INTEGER,
            review TEXT,
            tier TEXT,
            benchmark_review TEXT,
            market_review TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migrações seguras de colunas
    for col in ["tier TEXT", "benchmark_review TEXT", "market_review TEXT"]:
        try:
            c.execute(f"ALTER TABLE ads ADD COLUMN {col}")
        except Exception:
            pass
            
    # Tabela de buscas dinâmicas por usuário
    c.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            query TEXT,
            max_price REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Tabela de notificações já enviadas para evitar duplicados por usuário
    c.execute("""
        CREATE TABLE IF NOT EXISTS sent_notifications (
            ad_id TEXT,
            chat_id TEXT,
            PRIMARY KEY (ad_id, chat_id)
        )
    """)
    # Tabela de estado para contar postagens e disparar tiers periódicos
    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value INTEGER
        )
    """)
    c.execute("INSERT OR IGNORE INTO bot_state (key, value) VALUES ('ads_since_last_digest', 0)")
    conn.commit()
    conn.close()

def ad_ja_notificado(ad_id, chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM sent_notifications WHERE ad_id = ? AND chat_id = ?", (ad_id, str(chat_id)))
    res = c.fetchone()
    conn.close()
    return res is not None

def registrar_notificacao(ad_id, chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO sent_notifications (ad_id, chat_id) VALUES (?, ?)", (ad_id, str(chat_id)))
    conn.commit()
    conn.close()

def salvar_ad_cache(ad_id, title, price, url, score=None, review=None, tier=None, pitaco_benchmark=None, pitaco_mercado=None):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO ads (id, title, price, url, score, review, tier, benchmark_review, market_review)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ad_id, title, price, url, score, review, tier, pitaco_benchmark, pitaco_mercado))
    conn.commit()
    conn.close()

def obter_ad_cache(ad_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM ads WHERE id = ?", (ad_id,))
    res = c.fetchone()
    conn.close()
    return res


# ================= AVALIAÇÃO COM IA MULTI-AGENTE =================
def avaliar_com_ia(title, description, price, query, max_price=None):
    hw_nome, hw_tipo, hw_info = benchmarks.identificar_hardware(title)
    stats_mercado = market_analytics.obter_estatisticas_mercado(query, price, max_price)
    
    texto_benchmark = ""
    if hw_tipo == "gpu" and hw_info:
        custo_pt = benchmarks.calcular_custo_por_ponto(price, hw_info.get("score", 0))
        texto_benchmark = (
            f"- PassMark G3D Score: {hw_info['score']} pts\n"
            f"- Consumo / Conector: {hw_info['tdp']}W ({hw_info['power_pin']})\n"
            f"- Memória: {hw_info['vram']}\n"
            f"- Índice Custo por Ponto: R$ {custo_pt:.3f} por ponto G3D"
        )
    elif hw_tipo == "console" and hw_info:
        texto_benchmark = (
            f"- Tela: {hw_info['tela']}\n"
            f"- Ecossistema & Desbloqueio: {hw_info['ecossistema']}\n"
            f"- Capacidade de Emulação: {hw_info['emulacao']}\n"
            f"- Pontos Críticos de Inspeção: {hw_info['pontos_atencao']}"
        )
    else:
        texto_benchmark = "- Sem ficha de benchmark pré-catalogada; use conhecimento geral de mercado e hardware."

    prompt = f"""Você é o Juiz de um Comitê de Especialistas em Garimpo e Arbitragem de Usados no Brasil.
Dois agentes consultores prepararam dados factuais para sua decisão:

ANÚNCIO AVALIADO:
- Busca: "{query}"
- Título: "{title}"
- Preço: R$ {price:.2f}
- Descrição: {description}

DADOS FACTUAIS DO ESPECIALISTA TÉCNICO (BENCHMARK & SPECS):
- Modelo Identificado: {hw_nome or 'Genérico'} ({hw_tipo or 'Geral'})
{texto_benchmark}

DADOS FACTUAIS DO ESPECIALISTA EM MERCADO (OLX & GIRO):
- Mediana de Mercado na OLX: R$ {stats_mercado['mediana']:.2f} (baseada em {stats_mercado['total_amostras']} anúncios reais)
- Desconto em relação à Mediana: {stats_mercado['desconto_pct']:.1f}%
- Grau de Liquidez: {stats_mercado['liquidez']} (Giro estimado em {stats_mercado['tempo_giro']})
- Projeção de Revenda Rápida: R$ {stats_mercado['preco_revenda_rapida']:.2f} (Margem de R$ {stats_mercado['margem_lucro']:.2f})

MISSÃO DO COMITÊ:
Produza o parecer dos dois especialistas e o seu veredito final.
Responda ESTRITAMENTE em formato JSON:
{{
  "aprovado": true ou false,
  "nota": número inteiro de 0 a 10,
  "tier": "Tier S" | "Tier A" | "Tier B" | "Tier C",
  "pitaco_benchmark": "1 a 2 frases objetivas do Especialista Técnico sobre poder de fogo, R$/ponto e specs",
  "pitaco_mercado": "1 a 2 frases objetivas do Especialista Comercial sobre liquidez, desconto e revenda",
  "parecer_resumo": "1 frase curta com a conclusão final"
}}

Regras Críticas de Eliminação ("aprovado": false, "nota": 0):
- Sucata, defeito ("para retirada", "não liga", "tela quebrada", "sem teste", "no estado").
- Anúncios de acessórios avulsos quando a busca era pelo aparelho.
- PC incompatível: Se a busca for memória RAM para PC, o usuário exige EXCLUSIVAMENTE DDR3 Desktop Unbuffered (Non-ECC). Reprove ECC/Servidor/SODIMM.
- Anúncio falso, golpe ou preço R$ 1.

Classificação de Tiers:
- Tier S (Nota 9 a 10): Desconto >= 35%, item funcionando perfeitamente, ótima entrega técnica e alta liquidez. Oportunidade rara!
- Tier A (Nota 7 a 8): Desconto 20% a 35%, bom custo-benefício.
- Tier B (Nota 5 a 6): Preço normal de mercado / justo.
- Tier C (Nota < 5): Caro para usado ou produto defasado.
"""
    for model_name in ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.5-flash"]:
        try:
            res = ai_client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            text = res.text.strip()
            text = re.sub(r"^```json\s*", "", text)
            text = re.sub(r"^```\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            data = json.loads(text)
            return {
                "aprovado": bool(data.get("aprovado", False)),
                "nota": int(data.get("nota", 5)),
                "tier": str(data.get("tier", "Tier B")),
                "parecer": str(data.get("parecer_resumo", "Sem parecer adicional.")),
                "pitaco_benchmark": str(data.get("pitaco_benchmark", "")),
                "pitaco_mercado": str(data.get("pitaco_mercado", "")),
                "stats_mercado": stats_mercado
            }
        except Exception:
            continue
            
    return {
        "aprovado": True,
        "nota": 6,
        "tier": "Tier B",
        "parecer": "Preço dentro da média de mercado.",
        "pitaco_benchmark": "Item funcional para a categoria.",
        "pitaco_mercado": f"Preço próximo da mediana de R$ {stats_mercado['mediana']:.2f}.",
        "stats_mercado": stats_mercado
    }

def interpretar_intencao_usuario(texto):
    """Usa o Gemini para entender pedidos em linguagem natural e extrair termo e preço."""
    prompt = f"""O usuário enviou uma mensagem para um bot de monitoramento da OLX:
"{texto}"

Identifique a ação desejada e responda ESTRITAMENTE em formato JSON:
{{
  "acao": "adicionar" | "listar" | "remover" | "ajuda" | "desconhecido",
  "termo": "nome limpo do produto a buscar na OLX (apenas se acao for adicionar)",
  "preco_maximo": número float com o teto de preço (se o usuário não disser, estime um valor razoável de mercado, ou null),
  "resposta": "mensagem amigável confirmando a ação ou pedindo detalhes"
}}

Exemplos:
- "Quero um nintendo switch até 1200" -> {{"acao": "adicionar", "termo": "Nintendo Switch", "preco_maximo": 1200.0, "resposta": "Beleza! Cadastrei o monitoramento de 'Nintendo Switch' por até R$ 1200.00."}}
- "me avisa se achar controle de ps5 por menos de 250" -> {{"acao": "adicionar", "termo": "Controle PS5", "preco_maximo": 250.0, "resposta": "Feito! Estou monitorando 'Controle PS5' por até R$ 250.00."}}
- "quais minhas buscas?" -> {{"acao": "listar", "termo": null, "preco_maximo": null, "resposta": ""}}
"""
    for model_name in ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]:
        try:
            res = ai_client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            text = res.text.strip()
            text = re.sub(r"^```json\s*", "", text)
            text = re.sub(r"^```\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            return json.loads(text)
        except Exception:
            continue
    return {"acao": "desconhecido", "resposta": "Não entendi bem. Diga o que procura e o valor máximo (ex: 'Monitorar SSD 480GB até 150')."}


# ================= TELEGRAM =================
def send_telegram(chat_id, title, price, url, description, score, review, pitaco_benchmark=None, pitaco_mercado=None, stats_mercado=None, image_url=None):
    if score >= 9:
        badge = f"🔥 *NOTA DA IA: {score}/10 (IMPERDÍVEL - TIER S)* 🔥"
    elif score >= 7:
        badge = f"⭐ *NOTA DA IA: {score}/10 (ÓTIMA OFERTA - TIER A)*"
    else:
        badge = f"📊 *NOTA DA IA: {score}/10 (TIER B)*"

    mercado_linha = ""
    if stats_mercado and stats_mercado.get("mediana"):
        desconto = stats_mercado.get("desconto_pct", 0)
        sinal = "📉" if desconto > 0 else "📈"
        mercado_linha = f"📊 *Mercado OLX:* Mediana R$ {stats_mercado['mediana']:.2f} ({sinal} {abs(desconto):.0f}% do mercado)\n"

    pareceres = ""
    if pitaco_benchmark:
        pareceres += f"\n⚡ *Pitaco de Benchmark (Técnico):*\n_{pitaco_benchmark}_\n"
    if pitaco_mercado:
        pareceres += f"\n📈 *Pitaco de Mercado (Comercial):*\n_{pitaco_mercado}_\n"

    caption = (
        f"{badge}\n\n"
        f"📦 *Item:* {title}\n"
        f"💰 *Preço:* R$ {price:.2f}\n"
        f"{mercado_linha}"
        f"🧠 *Veredito:* _{review}_\n"
        f"{pareceres}\n"
        f"🔗 [Abrir Anúncio na OLX]({url})\n\n"
        f"📝 *Descrição:*\n_{description[:180]}..._"
    )
    
    try:
        if image_url:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                json={
                    "chat_id": chat_id,
                    "photo": image_url,
                    "caption": caption,
                    "parse_mode": "Markdown"
                },
                timeout=15
            )
            if r.status_code == 200:
                return
        
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": caption,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False
            },
            timeout=15
        )
    except Exception as e:
        print(f"Erro ao enviar telegram: {e}")

def send_msg(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"Erro ao mandar mensagem: {e}")


# ================= SCRAPING =================
def extrair_detalhes_ad(url):
    try:
        r = cffi_requests.get(url, impersonate="chrome120", timeout=12)
        if r.status_code != 200:
            return None
        
        soup = BeautifulSoup(r.text, "html.parser")
        for s in soup.find_all("script", type="application/ld+json"):
            if not s.string:
                continue
            try:
                data = json.loads(s.string)
                if data.get("@type") == "Product" or "offers" in data:
                    name = data.get("name", "")
                    description = data.get("description", "")
                    offers = data.get("offers", {})
                    price = float(offers.get("price", 0))
                    
                    images = data.get("image", [])
                    image_url = None
                    if isinstance(images, list) and images:
                        first = images[0]
                        if isinstance(first, dict):
                            image_url = first.get("contentUrl")
                        elif isinstance(first, str):
                            image_url = first
                    elif isinstance(images, str):
                        image_url = images
                    
                    return {
                        "title": name,
                        "description": description,
                        "price": price,
                        "image_url": image_url
                    }
            except Exception:
                continue
    except Exception as e:
        print(f"Erro ao detalhar {url}: {e}")
    return None

def monitorar_rodada():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando varredura das buscas ativas...")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, chat_id, query, max_price FROM searches")
    buscas = c.fetchall()
    conn.close()
    
    if not buscas:
        print("Nenhuma busca ativa cadastrada.")
        return

    for b in buscas:
        search_id = b["id"]
        chat_id = str(b["chat_id"])
        query = b["query"]
        teto_preco = float(b["max_price"] or 999999)
        
        url_busca = f"https://www.olx.com.br/brasil?q={urllib.parse.quote(query)}&sf=1"
        
        try:
            r = cffi_requests.get(url_busca, impersonate="chrome120", timeout=15)
            if r.status_code != 200:
                continue
                
            soup = BeautifulSoup(r.text, "html.parser")
            links_ad = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                m = re.search(r"-(\d+)$", href)
                if m and "olx.com.br" in href:
                    ad_id = m.group(1)
                    if href not in [x[1] for x in links_ad]:
                        links_ad.append((ad_id, href))
                        
            # Analisa até 15 anúncios mais recentes
            for ad_id, href in links_ad[:15]:
                # Se este usuário específico já recebeu esse anúncio, pula
                if ad_ja_notificado(ad_id, chat_id):
                    continue
                
                # Checa cache para não reprocessar na IA se outro usuário já puxou
                cached = obter_ad_cache(ad_id)
                if cached and cached["score"] is not None:
                    titulo = cached["title"]
                    preco = cached["price"]
                    desc = "Anúncio verificado em cache."
                    score = cached["score"]
                    parecer = cached["review"]
                    tier = cached["tier"] if "tier" in cached.keys() else "Tier B"
                    pitaco_benchmark = cached["benchmark_review"] if "benchmark_review" in cached.keys() else None
                    pitaco_mercado = cached["market_review"] if "market_review" in cached.keys() else None
                    stats_mercado = None
                    img = None
                else:
                    detalhes = extrair_detalhes_ad(href)
                    if not detalhes:
                        salvar_ad_cache(ad_id, "Desconhecido", 0, href)
                        continue
                    
                    titulo = detalhes["title"]
                    preco = detalhes["price"]
                    desc = detalhes["description"]
                    img = detalhes["image_url"]
                    
                    if preco > teto_preco or preco <= 5:
                        salvar_ad_cache(ad_id, titulo, preco, href, score=0, review="Preço fora do limite")
                        registrar_notificacao(ad_id, chat_id)
                        continue
                    
                    print(f"🧠 Consultando IA para [{chat_id}]: '{titulo}' por R$ {preco:.2f}...")
                    avaliacao = avaliar_com_ia(titulo, desc, preco, query, teto_preco)
                    score = avaliacao["nota"]
                    parecer = avaliacao["parecer"]
                    aprovado = avaliacao["aprovado"]
                    tier = avaliacao.get("tier", "Tier B")
                    pitaco_benchmark = avaliacao.get("pitaco_benchmark")
                    pitaco_mercado = avaliacao.get("pitaco_mercado")
                    stats_mercado = avaliacao.get("stats_mercado")
                    
                    if not aprovado:
                        score = 0
                        
                    salvar_ad_cache(ad_id, titulo, preco, href, score, parecer, tier, pitaco_benchmark, pitaco_mercado)
                
                # Se tiver nota boa (>= 6) e preço dentro do teto do usuário, notifica
                if score >= 6 and preco <= teto_preco:
                    print(f"🎯 Enviando para {chat_id}: {titulo} [{score}/10 - {tier}]")
                    send_telegram(chat_id, titulo, preco, href, desc, score, parecer, pitaco_benchmark, pitaco_mercado, stats_mercado, img)
                    
                    # Atualiza contador de postagens para disparar o resumo em Tiers a cada 15
                    verificar_e_enviar_digest_periodico(chat_id)
                    
                registrar_notificacao(ad_id, chat_id)
                time.sleep(1.5)
                
        except Exception as e:
            print(f"Erro ao buscar '{query}': {e}")
            
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Varredura concluída.")

def verificar_e_enviar_digest_periodico(chat_id):
    """A cada 15 ofertas postadas, gera um boletim estruturado com os maiores Tiers agrupados por categoria."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM bot_state WHERE key = 'ads_since_last_digest'")
    row = c.fetchone()
    count = (row["value"] if row else 0) + 1
    
    if count >= 15:
        c.execute("UPDATE bot_state SET value = 0 WHERE key = 'ads_since_last_digest'")
        conn.commit()
        
        # Puxa as melhores ofertas recentes (Tiers 7 a 10)
        c.execute("""
            SELECT title, price, score, url 
            FROM ads 
            WHERE score >= 7 
            ORDER BY score DESC, created_at DESC 
            LIMIT 15
        """)
        top_ads = c.fetchall()
        conn.close()
        
        if top_ads:
            # Agrupa por categoria
            ram_list = []
            ssd_list = []
            gpu_list = []
            games_list = []
            outros_list = []
            
            for ad in top_ads:
                t = ad["title"].lower()
                linha = f"• [{ad['title'][:40]}]({ad['url']}) - *R$ {ad['price']:.2f}* (Nota {ad['score']}/10)"
                if "ram" in t or "ddr" in t or "memória" in t or "memoria" in t:
                    ram_list.append(linha)
                elif "ssd" in t or "sata" in t:
                    ssd_list.append(linha)
                elif any(g in t for g in ["gtx", "rx", "placa de vídeo", "placa de video", "geforce", "radeon", "rtx"]):
                    gpu_list.append(linha)
                elif any(g in t for g in ["vita", "3ds", "2ds", "psp", "switch", "nintendo", "playstation", "xbox", "game boy", "gameboy", "console"]):
                    games_list.append(linha)
                else:
                    outros_list.append(linha)
                    
            msg = "🏆 *BOLETIM DE TIERS - TOP OFERTAS GARIMPADAS*\n\n"
            if ram_list:
                msg += "🧠 *MEMÓRIA RAM:*\n" + "\n".join(ram_list[:4]) + "\n\n"
            if ssd_list:
                msg += "💾 *SSD SATA:*\n" + "\n".join(ssd_list[:4]) + "\n\n"
            if gpu_list:
                msg += "🎮 *PLACAS DE VÍDEO:*\n" + "\n".join(gpu_list[:4]) + "\n\n"
            if games_list:
                msg += "🕹️ *GAMES & PORTÁTEIS (TIER S/A):*\n" + "\n".join(games_list[:4]) + "\n\n"
            if outros_list:
                msg += "📦 *OUTROS PRODUTOS:*\n" + "\n".join(outros_list[:4]) + "\n\n"
                
            msg += "_Boletim automático gerado a cada 15 ofertas analisadas._"
            send_msg(chat_id, msg)
    else:
        c.execute("UPDATE bot_state SET value = ? WHERE key = 'ads_since_last_digest'", (count,))
        conn.commit()
        conn.close()


# ================= BOT TELEGRAM INTERATIVO =================
def processar_comandos_telegram():
    last_update_id = 0
    print("Iniciando escuta de mensagens do Telegram...")
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 20}
            r = requests.get(url, params=params, timeout=25)
            
            if r.status_code == 200:
                data = r.json()
                for update in data.get("result", []):
                    last_update_id = update["update_id"]
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    texto = msg.get("text", "").strip()
                    
                    if not chat_id or not texto:
                        continue
                    
                    # Remove menção do bot em grupos (ex: /start@garimpo123321_guilherme_bot -> /start)
                    texto = re.sub(r"@garimpo123321_guilherme_bot\b", "", texto, flags=re.IGNORECASE).strip()
                    print(f"Mensagem recebida de {chat_id}: '{texto}'")
                    
                    # Comando /start ou /help
                    if texto.lower() in ["/start", "/help", "ajuda", "oi", "ola"]:
                        help_msg = (
                            "👋 *Olá! Eu sou o Garimpo Ofertas Bot com IA.*\n\n"
                            "Eu monitoro a *OLX Brasil* 24 horas por dia e aviso você assim que alguém postar o que você procura por um preço excelente (Tier S / Oportunidade)!\n\n"
                            "💡 *Como usar (você pode falar naturalmente ou em grupos):*\n"
                            "• _\"Quero monitorar PS Vita até 500 reais\"_\n"
                            "• _\"Me avisa se achar Nintendo 3DS por menos de 450\"_\n"
                            "• _\"Procura SSD 480GB até 250\"_\n\n"
                            "📋 *Comandos Rápidos:*\n"
                            "• `/tierlist` - 📊 Mostra a Tierlist das melhores ofertas das SUAS buscas ativas\n"
                            "• `/top` ou `/tiers` - 🏆 Mostra o ranking geral de melhores ofertas (8 a 10/10)\n"
                            "• `/list` - Mostra tudo o que estou procurando para você\n"
                            "• `/del [número]` - Para de monitorar uma busca\n"
                            "• `/help` - Exibe esta mensagem de ajuda"
                        )
                        send_msg(chat_id, help_msg)
                        continue

                    # Comando /tierlist (Organizado com base nas categorias ativas do próprio usuário)
                    if texto.startswith("/tierlist") or "tierlist" in texto.lower() or "tier list" in texto.lower():
                        conn = get_db()
                        c = conn.cursor()
                        # Pega as buscas cadastradas pelo próprio usuário
                        c.execute("SELECT query, max_price FROM searches WHERE chat_id = ?", (chat_id,))
                        buscas_usuario = c.fetchall()
                        
                        if not buscas_usuario:
                            conn.close()
                            send_msg(chat_id, "Você não tem buscas ativas cadastradas ainda.\nEnvie o que procura (ex: _'Monitorar SSD até 250'_ ou _'RAM até 100'_).")
                            continue
                            
                        resp = "📊 *SUA TIERLIST DE OFERTAS GARIMPADAS*\n\n"
                        tem_alguma_oferta = False
                        
                        for b in buscas_usuario:
                            termo = b["query"]
                            preco_limite = b["max_price"] or 999999.0
                            
                            # Busca anúncios que combinem com o termo e tenham nota alta
                            palavras = [p for p in termo.split() if len(p) > 2]
                            where_clauses = " AND ".join(["LOWER(title) LIKE ?" for _ in palavras]) or "1=1"
                            params = [f"%{p.lower()}%" for p in palavras] + [preco_limite]
                            
                            query_sql = f"""
                                SELECT title, price, score, url 
                                FROM ads 
                                WHERE {where_clauses} 
                                  AND price <= ? 
                                  AND score >= 6 
                                ORDER BY score DESC, created_at DESC 
                                LIMIT 3
                            """
                            c.execute(query_sql, params)
                            ofertas_termo = c.fetchall()
                            
                            resp += f"🏷️ *{termo.upper()}:*\n"
                            if ofertas_termo:
                                tem_alguma_oferta = True
                                for i, o in enumerate(ofertas_termo, 1):
                                    resp += f"  *{i}.* [{o['title'][:38]}]({o['url']}) - *R$ {o['price']:.2f}* (Tier {o['score']}/10)\n"
                            else:
                                resp += "  _Nenhum achado com nota alta ainda para este item._\n"
                            resp += "\n"
                            
                        conn.close()
                        resp += "_Atualizado dinamicamente com base nas suas buscas ativas._"
                        send_msg(chat_id, resp)
                        continue

                    # Comando /top ou /tiers (Maiores Tiers da IA globais)
                    if texto.startswith("/top") or texto.startswith("/tiers") or "melhores ofertas" in texto.lower():
                        conn = get_db()
                        c = conn.cursor()
                        c.execute("""
                            SELECT title, price, score, review, url 
                            FROM ads 
                            WHERE score >= 8 
                            ORDER BY score DESC, created_at DESC 
                            LIMIT 8
                        """)
                        rows = c.fetchall()
                        conn.close()

                        if not rows:
                            send_msg(chat_id, "Ainda não encontrei ofertas nos Tiers mais altos (8 a 10).\nAssim que surgirem, elas aparecerão aqui!")
                        else:
                            resp = "🏆 *MAIORES TIERS CLASSIFICADOS PELA IA (8 a 10/10):*\n\n"
                            for r in rows:
                                badge = "🔥 TIER 10/10 (ACHADO)" if r["score"] == 10 else f"⭐ TIER {r['score']}/10"
                                resp += (
                                    f"*{badge}*\n"
                                    f"📦 *{r['title']}*\n"
                                    f"💰 R$ {r['price']:.2f}\n"
                                    f"🧠 _{r['review']}_\n"
                                    f"🔗 [Ver Anúncio na OLX]({r['url']})\n\n"
                                )
                            send_msg(chat_id, resp)
                        continue
                        
                    # Comando /list
                    if texto.startswith("/list") or "minhas buscas" in texto.lower() or "listar" in texto.lower():
                        conn = get_db()
                        c = conn.cursor()
                        c.execute("SELECT id, query, max_price FROM searches WHERE chat_id = ?", (chat_id,))
                        rows = c.fetchall()
                        conn.close()
                        
                        if not rows:
                            send_msg(chat_id, "Você ainda não tem nenhuma busca ativa.\nEnvie o que quer procurar (ex: _'Monitorar RX 580 até 450'_).")
                        else:
                            resp = "📋 *Suas Buscas Ativas:*\n\n"
                            for i, row in enumerate(rows, 1):
                                preco_str = f"R$ {row['max_price']:.2f}" if row['max_price'] else "Sem limite"
                                resp += f"*{i}.* `{row['query']}` (Até {preco_str})\n   _Para apagar, envie: `/del {row['id']}`_\n\n"
                            send_msg(chat_id, resp)
                        continue
                        
                    # Comando /del
                    if texto.startswith("/del") or texto.startswith("/remover"):
                        partes = texto.split()
                        if len(partes) > 1 and partes[1].isdigit():
                            del_id = int(partes[1])
                            conn = get_db()
                            c = conn.cursor()
                            c.execute("DELETE FROM searches WHERE id = ? AND chat_id = ?", (del_id, chat_id))
                            deletado = conn.total_changes > 0
                            conn.commit()
                            conn.close()
                            
                            if deletado:
                                send_msg(chat_id, f"✅ Busca #{del_id} removida com sucesso!")
                            else:
                                send_msg(chat_id, f"❌ Não encontrei a busca #{del_id} nas suas buscas.")
                        else:
                            send_msg(chat_id, "Use `/del [número]` para remover. Ex: `/del 2`.")
                        continue

                    # Comando explícito /add ou /monitorar (ótimo para grupos)
                    if texto.startswith("/add") or texto.startswith("/monitorar"):
                        partes = texto.split()
                        if len(partes) > 1:
                            ultimo = partes[-1].replace("R$", "").replace("$", "").replace(",", ".").strip()
                            try:
                                preco_val = float(ultimo)
                                termo_val = " ".join(partes[1:-1]).strip()
                            except ValueError:
                                preco_val = 999999.0
                                termo_val = " ".join(partes[1:]).strip()

                            if termo_val:
                                conn = get_db()
                                c = conn.cursor()
                                c.execute("INSERT INTO searches (chat_id, query, max_price) VALUES (?, ?, ?)",
                                          (chat_id, termo_val, preco_val))
                                conn.commit()
                                conn.close()
                                preco_msg = f"R$ {preco_val:.2f}" if preco_val < 999999 else "Sem limite de preço"
                                send_msg(chat_id, f"🎯 *Busca cadastrada!*\n\n📦 *Produto:* `{termo_val}`\n💰 *Preço Máximo:* {preco_msg}\n\nAssim que surgir um anúncio com nota alta na IA, eu te aviso aqui!")
                                continue
                        else:
                            send_msg(chat_id, "Use: `/add [termo] [preço opcional]`\nExemplo: `/add PS Vita 450` ou `/add Nintendo 3DS`")
                            continue

                    # Processamento com Linguagem Natural (Gemini)
                    interpretacao = interpretar_intencao_usuario(texto)
                    acao = interpretacao.get("acao")
                    
                    if acao == "adicionar" and interpretacao.get("termo"):
                        termo = interpretacao["termo"]
                        preco = interpretacao.get("preco_maximo") or 999999.0
                        
                        conn = get_db()
                        c = conn.cursor()
                        c.execute("INSERT INTO searches (chat_id, query, max_price) VALUES (?, ?, ?)",
                                  (chat_id, termo, preco))
                        conn.commit()
                        conn.close()
                        
                        preco_msg = f"R$ {preco:.2f}" if preco < 999999 else "Sem limite de preço"
                        send_msg(chat_id, f"🎯 *Busca cadastrada!*\n\n📦 *Produto:* `{termo}`\n💰 *Preço Máximo:* {preco_msg}\n\nAssim que surgir um anúncio com nota alta na IA, eu te aviso aqui!")
                        
                    elif acao == "listar":
                        send_msg(chat_id, "Digite `/list` para ver suas buscas cadastradas.")
                    else:
                        send_msg(chat_id, interpretacao.get("resposta", "Não entendi bem. Diga o que você quer monitorar (ex: 'Monitorar teclado mecânico até 150')."))
                        
        except Exception as e:
            print(f"Erro no loop de mensagens do Telegram: {e}")
            time.sleep(3)


# ================= LOOP PRINCIPAL =================
def loop_scraping():
    while True:
        try:
            monitorar_rodada()
        except Exception as e:
            print(f"Erro na rodada de scraping: {e}")
        time.sleep(INTERVALO_SEGUNDOS)

if __name__ == "__main__":
    init_db()
    # Inicia a escuta de mensagens do Telegram em uma thread dedicada
    t_telegram = threading.Thread(target=processar_comandos_telegram, daemon=True)
    t_telegram.start()
    
    # Inicia o monitor de garimpo contínuo
    loop_scraping()
