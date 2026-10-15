"""
Módulo de Banco de Dados Factual de Benchmarks e Especificações Técnicas.
Usado pelo Agente de Benchmark para fundamentar as análises com números reais (Ground Truth).
"""
import re

# Base factual de GPUs com PassMark G3D Score, TDP e conectores
GPUS_DATABASE = {
    "GT 710": {"score": 640, "tdp": 19, "power_pin": "Nenhum (Slot PCIe)", "vram": "1GB-2GB DDR3", "categoria": "Básica / Vídeo"},
    "GT 730": {"score": 820, "tdp": 38, "power_pin": "Nenhum (Slot PCIe)", "vram": "2GB DDR3/GDDR5", "categoria": "Básica / Vídeo"},
    "GT 1030": {"score": 2600, "tdp": 30, "power_pin": "Nenhum (Slot PCIe)", "vram": "2GB GDDR5", "categoria": "Entrada / eSports Leve"},
    "RX 550": {"score": 2800, "tdp": 50, "power_pin": "Nenhum (Slot PCIe)", "vram": "2GB-4GB GDDR5", "categoria": "Entrada / eSports Leve"},
    "GTX 750 TI": {"score": 3900, "tdp": 60, "power_pin": "Nenhum (Slot PCIe)", "vram": "2GB GDDR5", "categoria": "Entrada Clássica"},
    "GTX 960": {"score": 6050, "tdp": 120, "power_pin": "1x 6 pinos", "vram": "2GB-4GB GDDR5", "categoria": "Intermediária Antiga"},
    "GTX 1050": {"score": 5300, "tdp": 75, "power_pin": "Nenhum (Slot PCIe)", "vram": "2GB GDDR5", "categoria": "Entrada Eficiente"},
    "GTX 1050 TI": {"score": 6400, "tdp": 75, "power_pin": "Nenhum / 1x 6 pinos", "vram": "4GB GDDR5", "categoria": "Entrada Recomendada"},
    "RX 570": {"score": 7000, "tdp": 150, "power_pin": "1x 8 pinos", "vram": "4GB-8GB GDDR5", "categoria": "Custo-Benefício 1080p"},
    "RX 580": {"score": 8800, "tdp": 185, "power_pin": "1x 8 pinos", "vram": "8GB GDDR5", "categoria": "Custo-Benefício 1080p"},
    "GTX 1060 3GB": {"score": 9600, "tdp": 120, "power_pin": "1x 6 pinos", "vram": "3GB GDDR5", "categoria": "Intermediária 1080p"},
    "GTX 1060 6GB": {"score": 10200, "tdp": 120, "power_pin": "1x 6 pinos", "vram": "6GB GDDR5", "categoria": "Intermediária 1080p"},
    "GTX 1650": {"score": 7800, "tdp": 75, "power_pin": "Nenhum (Slot PCIe)", "vram": "4GB GDDR5/GDDR6", "categoria": "Entrada Eficiente"},
    "GTX 1660 SUPER": {"score": 12700, "tdp": 125, "power_pin": "1x 8 pinos", "vram": "6GB GDDR6", "categoria": "Intermediária Forte"},
    "RTX 2060": {"score": 14100, "tdp": 160, "power_pin": "1x 8 pinos", "vram": "6GB-12GB GDDR6", "categoria": "Intermediária DLSS/RT"},
    "RTX 3060": {"score": 17200, "tdp": 170, "power_pin": "1x 8 pinos", "vram": "12GB GDDR6", "categoria": "Intermediária Moderna"},
    "RTX 4060": {"score": 19700, "tdp": 115, "power_pin": "1x 8 pinos", "vram": "8GB GDDR6", "categoria": "Geração Atual Eficiente"},
    "RTX 5070": {"score": 28000, "tdp": 220, "power_pin": "16 pinos / 2x8 pinos", "vram": "12GB GDDR7", "categoria": "High-End Próxima Geração"}
}

# Base factual de Consoles e Portáteis (Métricas de Ecossistema)
CONSOLES_DATABASE = {
    "PS VITA": {
        "tipo": "Portátil",
        "tela": "OLED 5' (modelo 1000) ou LCD (modelo 2000 Slim)",
        "ecossistema": "Desbloqueio fácil (Henkaku/Enso), expansão micro SD via adaptador SD2Vita",
        "emulacao": "Excelente para PS1, PSP nativo, retroconsoles 8/16-bit e ports da comunidade",
        "pontos_atencao": "Verificar se analógicos têm drift e se tela tem burn-in (OLED)"
    },
    "NINTENDO 3DS": {
        "tipo": "Portátil",
        "tela": "Dual Screen, superior 3D estereoscópico sem óculos",
        "ecossistema": "Desbloqueio via CFW Luma3DS, loja alternativa hShop direta no console",
        "emulacao": "100% nativo para DS e GBA por injeção de hardware; ampla biblioteca",
        "pontos_atencao": "Verificar se as duas telas funcionam, calibração do touch e dobradiça firme"
    },
    "NINTENDO 2DS": {
        "tipo": "Portátil",
        "tela": "Dual Screen 2D sem efeito 3D (modelo wedge reto ou New 2DS XL dobrável)",
        "ecossistema": "Mesma compatibilidade e biblioteca total do 3DS e DS",
        "emulacao": "Excelente custo-benefício para crianças ou quem não usa o 3D",
        "pontos_atencao": "Construção sólida; verificar estado das telas e botões de ombro (L/R)"
    },
    "PSP": {
        "tipo": "Portátil",
        "tela": "LCD 4.3' widescreen (modelos 1000/2000/3000)",
        "ecossistema": "Desbloqueio definitivo PRO/Infinity, usa adaptador MicroSD para Memory Stick Pro Duo",
        "emulacao": "Ótimo para PS1 e emuladores clássicos (SNES, GBA, Mega Drive)",
        "pontos_atencao": "Baterias originais costumam estufar; verificar se bateria segura carga"
    },
    "NINTENDO SWITCH": {
        "tipo": "Híbrido / Portátil",
        "tela": "LCD 6.2' (Padrão) / LCD 5.5' (Lite) / OLED 7.0'",
        "ecossistema": "Biblioteca gigantesca atual; modelos V1 antigos podem ter desbloqueio por software (RCM)",
        "emulacao": "Nativa da geração atual",
        "pontos_atencao": "Verificar Joy-Con drift, se não está banido da Nintendo Network e se dock/carregador original acompanham"
    }
}

def identificar_hardware(titulo):
    """Identifica o modelo de GPU ou console presente no título do anúncio."""
    t = titulo.upper()
    
    # 1. Verifica Consoles
    if "VITA" in t or "PSVITA" in t:
        return "PS VITA", "console", CONSOLES_DATABASE["PS VITA"]
    if "3DS" in t:
        return "NINTENDO 3DS", "console", CONSOLES_DATABASE["NINTENDO 3DS"]
    if "2DS" in t:
        return "NINTENDO 2DS", "console", CONSOLES_DATABASE["NINTENDO 2DS"]
    if re.search(r"\bPSP\b", t):
        return "PSP", "console", CONSOLES_DATABASE["PSP"]
    if "SWITCH" in t:
        return "NINTENDO SWITCH", "console", CONSOLES_DATABASE["NINTENDO SWITCH"]
        
    # 2. Verifica GPUs em ordem de especificidade
    # Ordem das mais específicas para as mais gerais
    gpus_ordenadas = sorted(GPUS_DATABASE.keys(), key=lambda x: len(x), reverse=True)
    for gpu_name in gpus_ordenadas:
        padrao = r"\b" + re.escape(gpu_name) + r"\b"
        if re.search(padrao, t):
            return gpu_name, "gpu", GPUS_DATABASE[gpu_name]
            
    return None, None, None

def calcular_custo_por_ponto(preco, score_g3d):
    """Calcula o índice R$ por Ponto de Força Bruta (menor é melhor)."""
    if not score_g3d or score_g3d <= 0 or preco <= 0:
        return 0.0
    return round(preco / score_g3d, 3)
