"""Coletor de ofertas da Pace & Preço.

Roda no GitHub Actions. Lê os feeds oficiais da Awin (um segredo AWIN_FEED_<awinmid>
por loja aprovada) e a API oficial do Mercado Livre (segredos ML_CLIENT_ID e
ML_CLIENT_SECRET), filtra pela curadoria em config/curadoria.json, mantém um
histórico de preços e grava data/ofertas.json para o agente montar os posts.

Só usa a biblioteca padrão do Python.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import re
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(RAIZ, "config", "curadoria.json")
DADOS = os.path.join(RAIZ, "data")
HIST = os.path.join(DADOS, "historico_precos.json")
SAIDA = os.path.join(DADOS, "ofertas.json")
BRT = timezone(timedelta(hours=-3))
AGORA = datetime.now(BRT)
HOJE = AGORA.strftime("%Y-%m-%d")
UA = "PacePreco-Coletor/1.0 (+https://instagram.com/pacepreco)"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def baixar(url: str, headers: dict | None = None, data: bytes | None = None, timeout=120) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def num(v) -> float | None:
    if v is None:
        return None
    s = re.sub(r"[^0-9,.]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        return None


def norm(s: str) -> str:
    return " " + (s or "").lower().replace("-", " ").replace("_", " ") + " "


# ---------------------------------------------------------------- curadoria

def classificar(cfg: dict, nome: str, marca: str, categoria: str = "") -> dict | None:
    """Devolve {marca, uso} se o produto entra na curadoria, ou None."""
    texto = norm(f"{marca} {nome} {categoria}")
    if any(norm(x).strip() in texto for x in cfg["excluir"]):
        return None
    for m in cfg["modelos"]:
        if norm(m["marca"]) in texto or norm(m["marca"]).strip() == (marca or "").lower().strip():
            if any(norm(t).strip() in texto for t in m["termos"]):
                return {"marca": m["marca"], "uso": m["uso"]}
    # marca da lista + palavra de corrida (pega modelos novos que ainda não estão na lista)
    marca_ok = next((b for b in cfg["marcas"] if norm(b) in texto), None)
    if marca_ok and any(p in texto for p in cfg["palavras_corrida"]):
        return {"marca": marca_ok, "uso": ""}
    return None


# ---------------------------------------------------------------- histórico

def carregar_hist() -> dict:
    if os.path.exists(HIST):
        with open(HIST, encoding="utf-8") as f:
            return json.load(f)
    return {}


def registrar(hist: dict, chave: str, preco: float, dias: int):
    serie = hist.setdefault(chave, {})
    serie[HOJE] = min(preco, serie.get(HOJE, preco))
    corte = (AGORA - timedelta(days=dias)).strftime("%Y-%m-%d")
    for d in [d for d in serie if d < corte]:
        del serie[d]


def referencia(hist: dict, chave: str, preco_tabela: float | None) -> tuple[float | None, str]:
    """Preço de referência para calcular o desconto real.

    Com 7+ dias de histórico, usa a mediana dos dias ANTERIORES (pega desconto inflado).
    Sem histórico, usa o preço de tabela do feed, avisando que ainda não há histórico.
    """
    serie = hist.get(chave, {})
    anteriores = [v for d, v in serie.items() if d < HOJE]
    if len(anteriores) >= 7:
        return statistics.median(anteriores), "mediana_historico"
    return preco_tabela, "preco_tabela_sem_historico"


# ---------------------------------------------------------------- Awin

def segredos() -> dict:
    """Lê os segredos do GitHub (passados como JSON) ou variáveis de ambiente soltas."""
    s = {}
    if os.environ.get("SECRETS_JSON"):
        try:
            s.update(json.loads(os.environ["SECRETS_JSON"]))
        except json.JSONDecodeError:
            pass
    for k, v in os.environ.items():
        if k.startswith(("AWIN_FEED", "ML_")):
            s.setdefault(k, v)
    return s


def ler_csv(bruto: bytes) -> list[dict]:
    if bruto[:2] == b"\x1f\x8b":
        bruto = gzip.decompress(bruto)
    texto = bruto.decode("utf-8-sig", errors="replace")
    amostra = texto[:5000]
    delim = "|" if amostra.count("|") > amostra.count(",") else ","
    return list(csv.DictReader(io.StringIO(texto), delimiter=delim))


COLUNAS_AWIN = ("aw_deep_link,product_name,aw_product_id,merchant_product_id,merchant_image_url,"
                 "aw_image_url,merchant_category,category_name,search_price,store_price,rrp_price,"
                 "product_price_old,brand_name,merchant_deep_link,in_stock,merchant_id,merchant_name")


def _col(linha: dict, *pedacos: str) -> str:
    """Valor da primeira coluna cujo nome contém todos os pedaços (ignora maiúsculas)."""
    for k, v in linha.items():
        nome = (k or "").lower()
        if all(p in nome for p in pedacos):
            return (v or "").strip()
    return ""


def feeds_awin(cfg: dict, seg: dict) -> list[tuple[str, str, str]]:
    """Lista (awinmid, nome da loja, url do feed) das lojas em que estamos inscritos.

    1) AWIN_FEEDLIST: o link geral "Download da Lista de Feed" da Awin. Pega sozinho
       toda loja aprovada (status de inscrição ativo) e troca as colunas pelas que usamos.
    2) AWIN_FEED_<id>: link de feed avulso, se existir (tem prioridade para aquela loja).
    """
    feeds: dict[str, tuple[str, str]] = {}
    lista = seg.get("AWIN_FEEDLIST")
    if lista:
        try:
            for l in ler_csv(baixar(lista)):
                mid = _col(l, "advertiser", "id")
                status = _col(l, "membership").lower()
                url = _col(l, "url")
                idioma = _col(l, "language").lower()
                if not (mid and url) or status not in ("active", "joined", "ativo", "inscrito"):
                    continue
                if idioma and not idioma.startswith(("pt", "portug")):
                    continue
                if "/datafeed/download/" in url:  # formato antigo (Create-a-Feed)
                    if "/columns/" in url:
                        url = re.sub(r"/columns/[^/]+/", f"/columns/{COLUNAS_AWIN}/", url)
                    if "/compression/" not in url:
                        url = url.rstrip("/") + "/compression/gzip/"
                nome = _col(l, "advertiser", "name") or f"Awin {mid}"
                feeds.setdefault(mid, (nome, url))
            log(f"[awin] lista de feeds: {len(feeds)} loja(s) inscrita(s)")
        except Exception as e:  # noqa: BLE001
            log(f"[awin] falha ao ler a lista de feeds: {e}")
    for chave, url in seg.items():
        if chave.startswith("AWIN_FEED_") and url:
            mid = chave.removeprefix("AWIN_FEED_")
            feeds[mid] = (feeds.get(mid, (f"Awin {mid}", ""))[0], url)
    return [(mid, nome, url) for mid, (nome, url) in feeds.items()]


def coletar_awin(cfg: dict, seg: dict, hist: dict) -> list[dict]:
    ofertas = []
    aff = cfg["awin"]["affiliate_id"]
    for mid, nome_feed, url in feeds_awin(cfg, seg):
        loja = cfg["awin"]["lojas"].get(mid, {"nome": nome_feed, "beneficio": ""})
        try:
            linhas = ler_csv(baixar(url, timeout=300))
        except Exception as e:  # noqa: BLE001
            log(f"[awin {mid}] falha ao baixar feed: {e}")
            continue
        log(f"[awin {mid}] {len(linhas)} produtos no feed")
        for p in linhas:
            # aceita o formato antigo da Awin e o novo, baseado no Google Shopping
            nome = p.get("product_name") or p.get("title") or ""
            marca = p.get("brand_name") or p.get("brand") or ""
            cat = " ".join(filter(None, [p.get("merchant_category"), p.get("category_name"),
                                         p.get("product_type"), p.get("google_product_category")]))
            c = classificar(cfg, nome, marca, cat)
            if not c:
                continue
            estoque = (p.get("in_stock") or p.get("availability") or "1").strip().lower()
            if estoque in ("0", "false", "no", "nao", "não", "out of stock", "out_of_stock", "outofstock"):
                continue
            venda = num(p.get("sale_price"))
            cheio = num(p.get("price"))
            preco = (num(p.get("search_price")) or num(p.get("store_price")) or venda or cheio
                     or num(p.get("display_price")))
            if not preco:
                continue
            tabela = max(filter(None, [num(p.get("rrp_price")), num(p.get("product_price_old")),
                                       num(p.get("base_price")), cheio if venda else None]), default=None)
            pid = (p.get("aw_product_id") or p.get("merchant_product_id") or p.get("id")
                   or p.get("item_group_id") or nome)
            k = f"awin:{mid}:{pid}"
            registrar(hist, k, preco, cfg["dias_historico"])
            ref, base = referencia(hist, k, tabela)
            url_loja = p.get("merchant_deep_link") or p.get("link") or ""
            if p.get("aw_deep_link"):
                link = p["aw_deep_link"]
            elif "awin1.com" in url_loja:
                link = url_loja
            else:
                link = (f"https://www.awin1.com/cread.php?awinmid={mid}&awinaffid={aff}&ued="
                        + urllib.parse.quote(url_loja, safe=""))
            ofertas.append({
                "id": k, "canal": "awin", "loja": loja["nome"], "beneficio_loja": loja.get("beneficio", ""),
                "marca": c["marca"], "uso_sugerido": c["uso"], "produto": nome.strip(),
                "preco": round(preco, 2), "preco_referencia": round(ref, 2) if ref else None,
                "base_desconto": base, "url_produto": url_loja, "link_afiliado": link,
                "link_pronto": True,
                "imagem": (p.get("merchant_image_url") or p.get("aw_image_url")
                           or p.get("image_link") or ""),
                "tamanhos": p.get("Fashion:size") or p.get("size") or "",
            })
    return ofertas


# ---------------------------------------------------------------- Mercado Livre

def token_ml(seg: dict) -> str | None:
    cid, sec = seg.get("ML_CLIENT_ID"), seg.get("ML_CLIENT_SECRET")
    if not (cid and sec):
        log("[ml] sem ML_CLIENT_ID/ML_CLIENT_SECRET: pulando Mercado Livre")
        return None
    corpo = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid,
                                    "client_secret": sec}).encode()
    try:
        r = json.loads(baixar("https://api.mercadolibre.com/oauth/token", data=corpo,
                              headers={"Content-Type": "application/x-www-form-urlencoded",
                                       "Accept": "application/json"}))
        return r.get("access_token")
    except Exception as e:  # noqa: BLE001
        log(f"[ml] falha no token: {e}")
        return None


def link_ml(cfg: dict, url: str) -> tuple[str, bool]:
    ml = cfg["mercadolivre"]
    limpo = url.split("?")[0].split("#")[0]
    if ml.get("link_pronto_confirmado"):
        return f"{limpo}?matt_word={ml['matt_word']}&matt_tool={ml['matt_tool']}", True
    return limpo, False


def coletar_ml(cfg: dict, seg: dict, hist: dict) -> list[dict]:
    if not cfg["mercadolivre"].get("ativo"):
        return []
    tok = token_ml(seg)
    if not tok:
        return []
    ofertas = []
    for q in cfg["buscas_mercadolivre"]:
        url = ("https://api.mercadolibre.com/sites/MLB/search?condition=new&limit=50&q="
               + urllib.parse.quote(q))
        try:
            r = json.loads(baixar(url, headers={"Authorization": f"Bearer {tok}"}))
        except Exception as e:  # noqa: BLE001
            log(f"[ml] busca '{q}' falhou: {e}")
            continue
        for it in r.get("results", []):
            nome = it.get("title", "")
            attrs = {a.get("id"): a.get("value_name") for a in it.get("attributes", [])}
            c = classificar(cfg, nome, attrs.get("BRAND") or "")
            if not c:
                continue
            oficial = bool(it.get("official_store_id"))
            rep = ((it.get("seller") or {}).get("seller_reputation") or {}).get("power_seller_status")
            if cfg["mercadolivre"].get("somente_lojas_oficiais_ou_lider") and not (oficial or rep):
                continue
            preco = num(it.get("price"))
            if not preco:
                continue
            k = f"ml:{it.get('id')}"
            registrar(hist, k, preco, cfg["dias_historico"])
            ref, base = referencia(hist, k, num(it.get("original_price")))
            link, pronto = link_ml(cfg, it.get("permalink", ""))
            ofertas.append({
                "id": k, "canal": "mercadolivre",
                "loja": "Mercado Livre" + (" (loja oficial)" if oficial else f" ({rep})" if rep else ""),
                "beneficio_loja": "", "marca": c["marca"], "uso_sugerido": c["uso"], "produto": nome,
                "preco": round(preco, 2), "preco_referencia": round(ref, 2) if ref else None,
                "base_desconto": base, "url_produto": it.get("permalink", "").split("?")[0],
                "link_afiliado": link, "link_pronto": pronto,
                "imagem": it.get("thumbnail", ""), "tamanhos": "",
            })
    return ofertas


# ---------------------------------------------------------------- saída

TAM_LETRA = ["PP", "P", "M", "G", "GG", "XG", "XGG", "EG", "EGG", "XS", "S", "L", "XL", "XXL", "U", "UN", "UNICO", "ÚNICO"]


def ordem_tamanho(t: str):
    u = t.upper()
    if u in TAM_LETRA:
        return (1, TAM_LETRA.index(u), u)
    m = re.match(r"\d+(?:[.,]\d)?", t)
    return (0, float(m.group().replace(",", ".")) if m else 0, t)


def main():
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    os.makedirs(DADOS, exist_ok=True)
    seg = segredos()
    hist = carregar_hist()

    todas = coletar_awin(cfg, seg, hist) + coletar_ml(cfg, seg, hist)

    # desconto real e filtro
    boas = []
    for o in todas:
        ref = o["preco_referencia"]
        if not ref or ref <= o["preco"]:
            continue
        o["desconto_pct"] = round(100 * (1 - o["preco"] / ref), 1)
        if o["desconto_pct"] >= cfg["desconto_minimo_pct"]:
            boas.append(o)

    # agrupa variações (tamanho) do mesmo modelo/cor pela página do produto:
    # fica o menor preço, o nome comum e a lista de tamanhos
    grupos = {}
    for o in boas:
        chave_url = (o.get("url_produto") or "").split("?")[0].lower()
        k = (o["canal"], chave_url or o["produto"].lower())
        grupos.setdefault(k, []).append(o)
    final = []
    for itens in grupos.values():
        g = dict(min(itens, key=lambda o: o["preco"]))
        tams = set()
        if len(itens) > 1:
            listas = [o["produto"].split() for o in itens]
            comum = []
            for col in zip(*listas):
                if len(set(w.lower() for w in col)) > 1:
                    break
                comum.append(col[0])
            if comum:
                g["produto"] = " ".join(comum).rstrip(" -/")
                tams = {" ".join(l[len(comum):]).strip("()") for l in listas} - {""}
        elif chave_url:
            # item único: tira o tamanho do fim do nome se ele não faz parte do endereço da página
            partes = g["produto"].rsplit(" ", 1)
            slug = re.split(r"[/-]", chave_url.rstrip("/").removesuffix("/p"))
            if (len(partes) == 2 and partes[1].lower() not in slug
                    and (re.fullmatch(r"\d{2}(?:[.,]5)?(?:/\d{2})?", partes[1])
                         or partes[1].upper() in TAM_LETRA)):
                g["produto"], t = partes
                tams.add(t)
        for o in itens:
            for t in str(o.get("tamanhos") or "").replace(";", ",").split(","):
                if t.strip():
                    tams.add(t.strip())
        g["tamanhos"] = ", ".join(sorted(tams, key=ordem_tamanho))
        g["variacoes"] = len(itens)
        final.append(g)
    final = sorted(final, key=lambda o: -o["desconto_pct"])[: cfg["max_ofertas"]]

    with open(HIST, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))
    with open(SAIDA, "w", encoding="utf-8") as f:
        json.dump({"gerado_em": AGORA.isoformat(timespec="minutes"),
                   "total_analisado": len(todas), "total_ofertas": len(final),
                   "ofertas": final}, f, ensure_ascii=False, indent=1)
    log(f"analisados {len(todas)} | ofertas {len(final)} -> {SAIDA}")


if __name__ == "__main__":
    main()
