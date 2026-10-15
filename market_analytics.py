"""
Módulo de Inteligência e Estatísticas de Mercado da OLX.
Calcula mediana de preços limpa, remoção de ruídos (PCs completos em buscas de peças),
desconto percentual factual e liquidez de giro.
"""
import os
import sqlite3
import statistics

LIQUIDEZ_REFERENCIA = {
    "PS VITA": {"liquidez": "Altíssima", "tempo_giro": "24h a 48h", "demanda": "Colecionadores e emulação portátil"},
    "NINTENDO 3DS": {"liquidez": "Altíssima", "tempo_giro": "24h a 48h", "demanda": "Forte procura para desbloqueio e retrogaming"},
    "NINTENDO 2DS": {"liquidez": "Alta", "tempo_giro": "2 a 4 dias", "demanda": "Busca constante por custo-benefício"},
    "PSP": {"liquidez": "Alta", "tempo_giro": "2 a 5 dias", "demanda": "Alta procura nostálgica"},
    "NINTENDO SWITCH": {"liquidez": "Altíssima", "tempo_giro": "12h a 36h", "demanda": "Produto líder de giro no mercado usado"},
    "SSD SATA": {"liquidez": "Altíssima", "tempo_giro": "24h a 48h", "demanda": "Upgrade universal para PCs e notebooks antigos"},
    "MEMORIA RAM DDR3": {"liquidez": "Alta", "tempo_giro": "2 a 4 dias", "demanda": "Manutenção corporativa e PCs de entrada"},
    "GTX 750 TI": {"liquidez": "Alta", "tempo_giro": "2 a 4 dias", "demanda": "Placa clássica que não exige conector de energia"},
    "GTX 1050": {"liquidez": "Altíssima", "tempo_giro": "24h a 48h", "demanda": "Alta procura para eSports em PCs básicos"},
    "GTX 1050 TI": {"liquidez": "Altíssima", "tempo_giro": "24h a 48h", "demanda": "4GB VRAM com consumo baixo de 75W"},
    "RX 550": {"liquidez": "Média/Alta", "tempo_giro": "3 a 6 dias", "demanda": "Alternativa de entrada para vídeo e jogos leves"},
    "GTX 960": {"liquidez": "Média", "tempo_giro": "4 a 7 dias", "demanda": "Boa força bruta, mas consome 120W e exige conector 6 pinos"}
}

TERMOS_IGNORAR_PECA = ["pc gamer", "computador", "notebook", "laptop", "gabinete completo", "setup completo"]

def obter_estatisticas_mercado(query, preco_anuncio, max_price=None, db_path=None):
    if db_path is None:
        db_path = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "ofertas.db"))
    """
    Calcula as estatísticas de mercado com base no histórico minerado da OLX:
    - Preço mediano
    - Desconto percentual real
    - Grau de liquidez e tempo estimado de giro
    - Margem projetada de revenda rápida
    """
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    palavras = [p for p in query.split() if len(p) > 2]
    where_clauses = ["LOWER(title) LIKE ?" for _ in palavras]
    params = [f"%{p.lower()}%" for p in palavras]
    
    # Se a busca for de peça avulsa (placa de vídeo, memória, SSD), ignora anúncios de PCs/Notebooks inteiros
    q_lower = query.lower()
    eh_componente = any(x in q_lower for x in ["gtx", "rx", "rtx", "ram", "ddr3", "ddr4", "ssd", "placa"])
    if eh_componente:
        for t_ignora in TERMOS_IGNORAR_PECA:
            where_clauses.append("LOWER(title) NOT LIKE ?")
            params.append(f"%{t_ignora}%")
            
    where_str = " AND ".join(where_clauses) if where_clauses else "1=1"
    
    # Limite superior de corte para evitar distorções
    teto_corte = (max_price * 1.8) if max_price and max_price > 0 else (preco_anuncio * 2.5 if preco_anuncio > 0 else 5000.0)
    
    c.execute(f"""
        SELECT price 
        FROM ads 
        WHERE {where_str} 
          AND price >= 15 
          AND price <= ?
    """, params + [teto_corte])
    
    rows = c.fetchall()
    conn.close()
    
    precos = sorted([float(r[0]) for r in rows if r[0] is not None])
    
    if len(precos) < 3:
        # Se houver poucas amostras, estima mediana prudente
        mediana = round(preco_anuncio * 1.35, 2) if preco_anuncio > 0 else 100.0
        total_amostras = len(precos)
    else:
        # Filtro de Outliers IQR
        try:
            q1, q2, q3 = statistics.quantiles(precos, n=4)
            iqr = q3 - q1
            limite_inf = max(15.0, q1 - 1.5 * iqr)
            limite_sup = q3 + 1.5 * iqr
            precos_limpos = [p for p in precos if limite_inf <= p <= limite_sup]
            if not precos_limpos:
                precos_limpos = precos
            mediana = round(statistics.median(precos_limpos), 2)
            total_amostras = len(precos_limpos)
        except Exception:
            mediana = round(statistics.median(precos), 2)
            total_amostras = len(precos)
            
    # Desconto em relação à mediana
    if mediana > 0:
        desconto_pct = round(((mediana - preco_anuncio) / mediana) * 100, 1)
    else:
        desconto_pct = 0.0
        
    # Margem de revenda rápida estimada (venda a 90% da mediana)
    preco_revenda_rapida = round(mediana * 0.90, 2)
    margem_lucro = round(preco_revenda_rapida - preco_anuncio, 2)
    
    # Identifica perfil de liquidez da categoria
    chave_liquidez = "Média"
    tempo_giro = "3 a 7 dias"
    q_upper = query.upper()
    for item_key, info in LIQUIDEZ_REFERENCIA.items():
        if item_key in q_upper or any(palavra in q_upper for palavra in item_key.split()):
            chave_liquidez = info["liquidez"]
            tempo_giro = info["tempo_giro"]
            break
            
    return {
        "mediana": mediana,
        "total_amostras": total_amostras,
        "desconto_pct": desconto_pct,
        "liquidez": chave_liquidez,
        "tempo_giro": tempo_giro,
        "preco_revenda_rapida": preco_revenda_rapida,
        "margem_lucro": margem_lucro
    }
