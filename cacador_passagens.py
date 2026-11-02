#!/usr/bin/env python3
"""
Caçador de passagens baratas (versão GitHub: grátis, sem servidor).

Roda sozinho no GitHub Actions, salva os preços em estado.json, gera os dados do
painel (docs/dados.json) e avisa no WhatsApp (Twilio) quando acha preço bem
abaixo do normal. Tudo que você ajusta fica em config.json.

Chaves (guardadas nos "Secrets" do GitHub, nunca no código):
  TRAVELPAYOUTS_TOKEN   Aviasales via Travelpayouts (grátis)
  SERPAPI_KEY           Google Flights via SerpApi (cota grátis pequena)
  AMADEUS_ID / AMADEUS_SECRET   Amadeus (opcional)
  TWILIO_SID / TWILIO_TOKEN / TWILIO_FROM / WHATSAPP_TO   alerta no WhatsApp

Uso local (opcional):
  python cacador_passagens.py            busca e alerta
  python cacador_passagens.py --teste    só manda uma mensagem de teste
"""
import json
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

try:  # evita erro de acentos/emoji em terminais do Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RAIZ = Path(__file__).resolve().parent
ARQ_CONFIG = RAIZ / "config.json"
ARQ_ESTADO = RAIZ / "estado.json"
ARQ_DADOS = RAIZ / "docs" / "dados.json"

PADRAO = {
    "origem": "BSB",
    "destinos": {
        "NAT": {"nome": "Natal", "aeroportos": "NAT", "limite": 700},
        "SAO": {"nome": "São Paulo", "aeroportos": "CGH,GRU,VCP", "limite": 400},
        "RIO": {"nome": "Rio de Janeiro", "aeroportos": "GIG,SDU", "limite": 400},
    },
    "dias_inicio": 14,
    "dias_fim": 120,
    "passo_dias": 14,
    "estadias": [4],
    "max_escalas": 1,
    "desconto_min": 0.30,
    "min_amostras": 15,
    "janela_historico_dias": 90,
    "max_alertas_por_mensagem": 5,
    "provedores_sempre": ["travelpayouts"],
    "provedores_semanais": ["serpapi"],
    "varredura_geral": True,
    "monitores": [],
    "provedores_diarios_monitores": ["serpapi"],
}

SEGREDOS = [
    "SERPAPI_KEY", "TRAVELPAYOUTS_TOKEN", "AMADEUS_ID", "AMADEUS_SECRET",
    "TWILIO_SID", "TWILIO_TOKEN",
]


# ------------------------------ utilidades ------------------------------
def ler_json(caminho, padrao):
    try:
        return json.loads(Path(caminho).read_text(encoding="utf-8"))
    except Exception:
        return padrao


def gravar_json(caminho, obj, indent=None):
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    separadores = None if indent else (",", ":")
    caminho.write_text(
        json.dumps(obj, ensure_ascii=False, indent=indent, separators=separadores),
        encoding="utf-8",
    )


def carregar_config():
    cfg = dict(PADRAO)
    cfg.update(ler_json(ARQ_CONFIG, {}))
    return cfg


def limpar(txt):
    """Esconde chaves em mensagens de erro (a URL do erro costuma conter a chave)."""
    txt = str(txt)
    for nome in SEGREDOS:
        valor = os.environ.get(nome)
        if valor:
            txt = txt.replace(valor, "***")
    return txt


def reais(v):
    return "R$ " + f"{v:,.0f}".replace(",", ".")


def link_google(origem, info, ida, volta):
    q = f"Flights from {origem} to {info['nome']} on {ida} through {volta}"
    return "https://www.google.com/travel/flights?q=" + quote(q)


# ------------------------------ PROVEDORES ------------------------------
# Cada função devolve uma lista de ofertas:
#   {"preco", "escalas", "fonte", "link" (opcional), "ida"/"volta" (opcional)}
_tok = {"v": None, "exp": 0.0}
_cache_tp = {}


def amadeus_host():
    return os.environ.get("AMADEUS_HOST") or "https://test.api.amadeus.com"


def amadeus_token():
    if _tok["v"] and time.time() < _tok["exp"] - 30:
        return _tok["v"]
    r = requests.post(
        f"{amadeus_host()}/v1/security/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["AMADEUS_ID"],
            "client_secret": os.environ["AMADEUS_SECRET"],
        },
        timeout=30,
    )
    r.raise_for_status()
    j = r.json()
    _tok["v"] = j["access_token"]
    _tok["exp"] = time.time() + int(j["expires_in"])
    return _tok["v"]


def buscar_amadeus(origem, codigo, info, ida, volta):
    r = requests.get(
        f"{amadeus_host()}/v2/shopping/flight-offers",
        headers={"Authorization": f"Bearer {amadeus_token()}"},
        params={
            "originLocationCode": origem,
            "destinationLocationCode": codigo,
            "departureDate": ida.isoformat(),
            "returnDate": volta.isoformat(),
            "adults": 1,
            "currencyCode": "BRL",
            "max": 30,
        },
        timeout=60,
    )
    r.raise_for_status()
    ofertas = []
    for o in r.json().get("data", []):
        escalas = max(len(it["segments"]) - 1 for it in o["itineraries"])
        ofertas.append(
            {"preco": float(o["price"]["grandTotal"]), "escalas": escalas, "fonte": "Amadeus"}
        )
    return ofertas


def buscar_serpapi(origem, codigo, info, ida, volta):
    r = requests.get(
        "https://serpapi.com/search.json",
        params={
            "engine": "google_flights",
            "departure_id": origem,
            "arrival_id": info["aeroportos"],
            "outbound_date": ida.isoformat(),
            "return_date": volta.isoformat(),
            "currency": "BRL",
            "hl": "pt",
            "gl": "br",
            "type": 1,
            "api_key": os.environ["SERPAPI_KEY"],
        },
        timeout=90,
    )
    r.raise_for_status()
    j = r.json()
    ofertas = []
    for o in j.get("best_flights", []) + j.get("other_flights", []):
        if "price" in o:
            ofertas.append(
                {
                    "preco": float(o["price"]),
                    "escalas": len(o.get("flights", [])) - 1,
                    "fonte": "Google Flights",
                    "link": j.get("search_metadata", {}).get("google_flights_url"),
                }
            )
    return ofertas


def buscar_travelpayouts(origem, codigo, info, ida, volta):
    """Aviasales via Travelpayouts. Consulta o mês inteiro (preços em cache de buscas
    recentes) e aproveita ofertas com datas próximas das pedidas."""
    chave = (origem, codigo, ida.strftime("%Y-%m"))
    if chave not in _cache_tp:
        r = requests.get(
            "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
            params={
                "origin": origem,
                "destination": codigo,
                "departure_at": ida.strftime("%Y-%m"),
                "currency": "brl",
                "one_way": "false",
                "sorting": "price",
                "limit": 1000,
                "token": os.environ["TRAVELPAYOUTS_TOKEN"],
            },
            timeout=30,
        )
        r.raise_for_status()
        _cache_tp[chave] = r.json().get("data", [])
    estadia = (volta - ida).days
    ofertas = []
    for o in _cache_tp[chave]:
        try:
            dep = date.fromisoformat(o["departure_at"][:10])
            ret = date.fromisoformat(o["return_at"][:10])
        except Exception:
            continue
        if abs((dep - ida).days) <= 3 and abs((ret - dep).days - estadia) <= 2:
            escalas = max(int(o.get("transfers") or 0), int(o.get("return_transfers") or 0))
            link = o.get("link")
            ofertas.append(
                {
                    "preco": float(o["price"]),
                    "escalas": escalas,
                    "fonte": "Aviasales",
                    "link": ("https://www.aviasales.com" + link) if link else None,
                    "ida": dep,
                    "volta": ret,
                }
            )
    return ofertas


FUNCOES = {
    "serpapi": buscar_serpapi,
    "travelpayouts": buscar_travelpayouts,
    "amadeus": buscar_amadeus,
}
EXIGE = {
    "serpapi": ["SERPAPI_KEY"],
    "travelpayouts": ["TRAVELPAYOUTS_TOKEN"],
    "amadeus": ["AMADEUS_ID", "AMADEUS_SECRET"],
}


def escolher_provedores(cfg, agora):
    nomes = list(cfg["provedores_sempre"])
    # Os "semanais" (ex.: SerpApi, de cota pequena) rodam só na primeira rodada de
    # segunda-feira, ou sempre que a execução manual marcar "usar todos".
    if os.environ.get("FORCAR_TODOS") or (agora.weekday() == 0 and agora.hour < 6):
        nomes += [n for n in cfg["provedores_semanais"] if n not in nomes]
    usaveis = []
    for n in nomes:
        if n in FUNCOES and all(os.environ.get(v) for v in EXIGE[n]):
            usaveis.append(n)
        else:
            print(f"[aviso] {n} ignorado (nome inválido ou chave não configurada)")
    return usaveis


# ------------------------------- FILTRO ---------------------------------
def datas_busca(cfg):
    hoje = date.today()
    for d in range(cfg["dias_inicio"], cfg["dias_fim"] + 1, cfg["passo_dias"]):
        ida = hoje + timedelta(days=d)
        for e in cfg["estadias"]:
            yield ida, ida + timedelta(days=e)


def mediana_historica(precos, codigo, cfg):
    valores = [p[3] for p in precos if p[0] == codigo]
    if len(valores) < cfg["min_amostras"]:
        return None
    return statistics.median(valores)


def serie_diaria(precos, codigo, dias=60):
    por_dia = {}
    for destino, _ida, _volta, preco, _esc, em in precos:
        if destino == codigo:
            dia = em[:10]
            por_dia[dia] = min(por_dia.get(dia, preco), preco)
    return [{"d": d, "min": v} for d, v in sorted(por_dia.items())[-dias:]]


def eh_alerta_novo(alertas_antigos, chave, preco):
    antigo = alertas_antigos.get(chave)
    return antigo is None or preco < antigo * 0.97


# ------------------------------- WHATSAPP -------------------------------
def enviar_whatsapp(texto):
    sid = os.environ.get("TWILIO_SID")
    token = os.environ.get("TWILIO_TOKEN")
    de = os.environ.get("TWILIO_FROM")
    para = os.environ.get("WHATSAPP_TO")
    if not all([sid, token, de, para]):
        print("[whatsapp não configurado] mensagem:\n" + texto)
        return False
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, token),
            data={"From": f"whatsapp:{de}", "To": f"whatsapp:{para}", "Body": texto[:1500]},
            timeout=30,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print("[erro] whatsapp:", limpar(e))
        return False


# ------------------- BUSCAS COM DATAS ESPECÍFICAS (AJUSTES) -------------------
def oferta_painel(origem, info, i, v, o):
    return {
        "ida": i.isoformat(),
        "volta": v.isoformat(),
        "preco": o["preco"],
        "escalas": o["escalas"],
        "fonte": o["fonte"],
        "link": o.get("link") or link_google(origem, info, i, v),
    }


def rodar_monitores(cfg, agora, estado, dados_ant):
    """Cada monitor = um destino, datas de ida/volta (com folga de ± dias) e um limite."""
    painel, alertas, total = [], [], 0
    forcar = bool(os.environ.get("FORCAR_TODOS"))
    nomes = list(cfg["provedores_sempre"])
    if forcar or agora.hour < 6:  # SerpApi: só na primeira rodada do dia
        nomes += [n for n in cfg["provedores_diarios_monitores"] if n not in nomes]
    if forcar:
        nomes += [n for n in cfg["provedores_semanais"] if n not in nomes]
    nomes = [n for n in nomes if n in FUNCOES and all(os.environ.get(v) for v in EXIGE[n])]
    anteriores = {m.get("id"): m for m in dados_ant.get("monitores", [])}

    for m in cfg.get("monitores", []):
        try:
            ida0 = date.fromisoformat(m["ida"])
            volta0 = date.fromisoformat(m["volta"])
            destino = str(m["destino"]).upper()
            limite = float(m["limite"])
            flex = max(0, min(3, int(m.get("flex", 0))))
        except Exception as e:
            print(f"[aviso] busca com datas ignorada (dados inválidos): {limpar(e)}")
            continue
        info = cfg["destinos"].get(destino) or {"nome": destino, "aeroportos": destino}
        achadas = []
        for d in range(-flex, flex + 1):
            ida, volta = ida0 + timedelta(days=d), volta0 + timedelta(days=d)
            for n in nomes:
                try:
                    for o in FUNCOES[n](cfg["origem"], destino, info, ida, volta):
                        oi, ov = o.get("ida", ida), o.get("volta", volta)
                        if (
                            o["escalas"] <= cfg["max_escalas"]
                            and abs((oi - ida0).days) <= flex
                            and abs((ov - volta0).days) <= flex
                        ):
                            achadas.append((oi, ov, o))
                except Exception as e:
                    print(f"[erro] {n} {destino} {ida}: {limpar(e)[:200]}")
            time.sleep(1)
        total += len(achadas)
        melhores = sorted(achadas, key=lambda a: a[2]["preco"])[:5]
        ofertas = [oferta_painel(cfg["origem"], info, i, v, o) for i, v, o in melhores]
        if not ofertas:
            ofertas = anteriores.get(m.get("id"), {}).get("ofertas", [])
        painel.append(
            {
                "id": m.get("id"), "destino": destino, "nome": info["nome"],
                "ida": m["ida"], "volta": m["volta"], "flex": flex,
                "limite": limite, "ofertas": ofertas,
            }
        )
        print(f"[ok] {info['nome']} {m['ida']}→{m['volta']}: {len(achadas)} ofertas")
        if melhores:
            i, v, o = melhores[0]
            chave = f"M|{m.get('id')}|{i}|{v}"
            if o["preco"] <= limite and eh_alerta_novo(estado["alertas"], chave, o["preco"]):
                esc = "direto" if o["escalas"] == 0 else f"{o['escalas']} escala"
                link = o.get("link") or link_google(cfg["origem"], info, i, v)
                texto = (
                    f"🎯 {cfg['origem']} → {info['nome']}: {reais(o['preco'])}\n"
                    f"Abaixo do seu limite de {reais(limite)}\n"
                    f"Ida {i:%d/%m} · Volta {v:%d/%m} · {esc} · {o['fonte']}\n{link}"
                )
                alertas.append((1 - o["preco"] / limite, o["preco"], chave, texto))
    return painel, alertas, total


# --------------------------------- MAIN ---------------------------------
def executar():
    cfg = carregar_config()
    agora = datetime.now(timezone.utc)
    nomes = escolher_provedores(cfg, agora)
    if not nomes:
        sys.exit("Nenhum provedor disponível. Cadastre a chave (Secret) de pelo menos um.")
    print("Provedores desta rodada:", ", ".join(nomes))

    estado = ler_json(ARQ_ESTADO, {})
    estado.setdefault("precos", [])
    estado.setdefault("alertas", {})
    dados_ant = ler_json(ARQ_DADOS, {})

    corte = (agora - timedelta(days=cfg["janela_historico_dias"])).strftime("%Y-%m-%dT%H:%M")
    estado["precos"] = [p for p in estado["precos"] if p[5] >= corte]
    agora_txt = agora.strftime("%Y-%m-%dT%H:%M")

    dados = {
        "atualizado": agora.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "origem": cfg["origem"],
        "desconto_min": cfg["desconto_min"],
        "destinos": {},
    }
    alertas = []
    total = 0

    varredura = cfg["destinos"].items() if cfg.get("varredura_geral", True) else []
    for codigo, info in varredura:
        mediana = mediana_historica(estado["precos"], codigo, cfg)  # antes de somar os de hoje
        coletados = []
        for ida, volta in datas_busca(cfg):
            ofertas = []
            for n in nomes:
                try:
                    achadas = FUNCOES[n](cfg["origem"], codigo, info, ida, volta)
                    ofertas += [o for o in achadas if o["escalas"] <= cfg["max_escalas"]]
                except Exception as e:
                    print(f"[erro] {n} {codigo} {ida}: {limpar(e)[:200]}")
            if ofertas:
                melhor = min(ofertas, key=lambda o: o["preco"])
                coletados.append((melhor.get("ida", ida), melhor.get("volta", volta), melhor))
            time.sleep(1)

        total += len(coletados)
        for i, v, o in coletados:
            p = o["preco"]
            if mediana:
                desconto = 1 - p / mediana
                barato = p <= mediana * (1 - cfg["desconto_min"])
                ref = f"{desconto:.0%} abaixo da mediana de {reais(mediana)}"
            else:
                desconto = 0
                barato = p <= info["limite"]
                ref = f"abaixo do seu limite de {reais(info['limite'])}"
            chave = f"{codigo}|{i}|{v}"
            if barato and eh_alerta_novo(estado["alertas"], chave, p):
                esc = "direto" if o["escalas"] == 0 else f"{o['escalas']} escala"
                link = o.get("link") or link_google(cfg["origem"], info, i, v)
                texto = (
                    f"✈️ {cfg['origem']} → {info['nome']}: {reais(p)}\n"
                    f"{ref}\n"
                    f"Ida {i:%d/%m} · Volta {v:%d/%m} · {esc} · {o['fonte']}\n"
                    f"{link}"
                )
                alertas.append((desconto, p, chave, texto))

        melhores = sorted(coletados, key=lambda c: c[2]["preco"])[:6]
        ofertas_painel = [
            {
                "ida": i.isoformat(),
                "volta": v.isoformat(),
                "preco": o["preco"],
                "escalas": o["escalas"],
                "fonte": o["fonte"],
                "link": o.get("link") or link_google(cfg["origem"], info, i, v),
            }
            for i, v, o in melhores
        ]
        if not ofertas_painel:  # sem dados novos: mantém o que o painel já mostrava
            ofertas_painel = dados_ant.get("destinos", {}).get(codigo, {}).get("ofertas", [])

        estado["precos"] += [
            [codigo, i.isoformat(), v.isoformat(), o["preco"], o["escalas"], agora_txt]
            for i, v, o in coletados
        ]
        dados["destinos"][codigo] = {
            "nome": info["nome"],
            "limite": info["limite"],
            "mediana": mediana,
            "ofertas": ofertas_painel,
            "serie": serie_diaria(estado["precos"], codigo),
        }
        print(f"[ok] {info['nome']}: {len(coletados)} datas coletadas")

    mon_painel, mon_alertas, mon_total = rodar_monitores(cfg, agora, estado, dados_ant)
    dados["monitores"] = mon_painel
    alertas += mon_alertas
    total += mon_total

    gravar_json(ARQ_ESTADO, estado)
    gravar_json(ARQ_DADOS, dados, indent=1)

    if alertas:
        alertas.sort(key=lambda a: (-a[0], a[1]))
        top = alertas[: cfg["max_alertas_por_mensagem"]]
        msg = "🔔 Passagens baratas encontradas\n\n" + "\n\n".join(a[3] for a in top)
        if enviar_whatsapp(msg):
            for a in top:
                estado["alertas"][a[2]] = a[1]
            gravar_json(ARQ_ESTADO, estado)
    else:
        print("Nenhuma promoção nova desta vez.")

    if total == 0:
        if cfg.get("monitores") or not cfg.get("varredura_geral", True):
            print("[aviso] Nenhuma oferta coletada nesta rodada.")
        else:
            sys.exit("Nenhuma oferta coletada: confira as chaves e os erros acima.")


if __name__ == "__main__":
    if "--teste" in sys.argv:
        enviar_whatsapp("✈️ Teste do caçador de passagens: WhatsApp funcionando!")
    else:
        executar()
