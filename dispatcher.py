#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Runners Brasil — Disparador de Ofertas no Telegram (produção)
=============================================================
1 disparo por horário (07:00 / 12:00 / 16:00 / 19:00 BRT), pelo GitHub Actions.
Cada item = 1 post com FOTO + legenda. Guia de Cupons (Grupo 1 às 07:00, Grupo 2 às 16:00).

COMPOSIÇÃO DO DISPARO (15 itens, configurável):
  • 8 TÊNIS   — prioridade: "Caiu de preço" + "Tênis em destaque" (depois pool amplo)
  • 3 VARIADOS— "Essenciais do corredor" + produtos de todas as lojas
  • 1 VIAGEM  — turismo esportivo
  • 2 PROVAS  — provas com cupom RB
  • 1 SERVIÇO — serviços pro corredor (FOTOP / Foco Radical etc., rotativo)

PRINCÍPIOS:
  • A IA / o repositório NUNCA veem o token (secret TELEGRAM_BOT_TOKEN).
  • "por" = preço real da loja (final_price). NUNCA preço de Pix.
  • Link de afiliado (coupons.link) INTOCADO, como botão "🛒 Comprar".
  • Sem "menor preço histórico" aqui (vem no upgrade da Edge Function).
  • Sem repetir o mesmo produto por DEDUPE_DAYS dias (super ofertas podem repetir).
"""

import os, re, sys, json, time, html, base64, unicodedata, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

SUPA_URL = "https://jpjhgetlhlnryalplqcf.supabase.co"
SITE = "https://cupons-rb.com/"

BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
SLOT       = os.environ.get("SLOT", "07").strip()
SEND       = os.environ.get("SEND", "").strip() in ("1", "true", "yes")

N_SHOES    = int(os.environ.get("N_SHOES", "8"))
N_VARIED   = int(os.environ.get("N_VARIED", "3"))
N_VIAGEM   = int(os.environ.get("N_VIAGEM", "1"))
N_PROVAS   = int(os.environ.get("N_PROVAS", "2"))
N_SERVICO  = int(os.environ.get("N_SERVICO", "1"))

DEDUPE_DAYS= int(os.environ.get("DEDUPE_DAYS", "3"))
SUPER_DROP = float(os.environ.get("SUPER_DROP", "55"))
LIVE_BONUS = float(os.environ.get("LIVE_BONUS", "15"))
POOL_LIMIT = int(os.environ.get("POOL_LIMIT", "400"))
STATE_DIR  = os.environ.get("STATE_DIR", "state")
DELAY      = float(os.environ.get("DELAY", "3"))

BRT = timezone(timedelta(hours=-3))
TODAY = datetime.now(BRT).date()
SLOT_IDX = {"07": 0, "12": 1, "16": 2, "19": 3}.get(SLOT, 0)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- HTTP / dados
def http_get(url, headers=None, timeout=30):
    h = {"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def get_anon_key():
    home = http_get(SITE)
    for src in re.findall(r'<script[^>]+src="([^"]+)"', home):
        if src.startswith("/"):
            src = SITE.rstrip("/") + src
        try:
            js = http_get(src)
        except Exception:
            continue
        for jwt in re.findall(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', js):
            try:
                pl = jwt.split(".")[1]; pl += "=" * (-len(pl) % 4)
                if json.loads(base64.urlsafe_b64decode(pl)).get("role") == "anon":
                    return jwt
            except Exception:
                pass
    raise RuntimeError("Não achei a chave pública no site.")

def rpc(fn, key):
    req = urllib.request.Request(f"{SUPA_URL}/rest/v1/rpc/{fn}", data=b"{}",
        headers={"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))

def rest(path, key):
    req = urllib.request.Request(f"{SUPA_URL}/rest/v1/{path}",
        headers={"apikey": key, "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))

def safe(fn, *a, label=""):
    try:
        return fn(*a)
    except Exception as e:
        print(f"  (aviso: {label} falhou: {e})"); return []

# ---------------------------------------------------------------- helpers
def brl(n):
    return "R$ " + f"{float(n):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def gender(name):
    n = (name or "").lower()
    return ("Feminino" if "femin" in n else "Masculino" if "mascul" in n
            else "Unissex" if "unissex" in n else None)

def image_proxy(u):
    return f"{SUPA_URL}/functions/v1/image-proxy?url=" + urllib.parse.quote(u.split("?")[0], safe="")

def image_for(u):
    if not u: return None
    if "supabase.co" in u:          # imagem já no nosso storage (URL assinada)
        return u
    return image_proxy(u)

def is_live(p):
    return "live" in ((p.get("product_brand") or p.get("brand_name") or "").lower())

def pct_of(p):
    v = p.get("discount_pct") or p.get("drop_pct")
    if v: return float(v)
    de, por = p.get("original_price"), p.get("final_price")
    if de and por and float(de) > 0:
        return round((float(de) - float(por)) / float(de) * 100)
    return 0

_STOP = {"masculino", "feminino", "unissex", "tenis", "de", "para"}
def base_name(name):
    # chave do MODELO: sem acentos/cores/gênero -> "qiaodan feiying pro 3"
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    toks = [t for t in n.split() if t not in _STOP]
    return " ".join(toks[:4])

def fmt_date(d):
    try:
        y, m, dd = d.split("-"); return f"{dd}/{m}"
    except Exception:
        return d or ""

ROUND_STEPS = [100, 150, 200, 250, 300, 400, 500, 700, 1000, 1500, 2000]
def crossed(de, por):
    for t in ROUND_STEPS:
        if por < t <= de: return t
    return None

# ---------------------------------------------------------------- legendas
VITRINE_LABEL = {
    "caiu":       "📉 CAIU DE PREÇO",
    "destaque":   "👟 TÊNIS EM DESTAQUE",
    "essenciais": "🎒 ESSENCIAIS DO CORREDOR",
    "pool":       "🛍 ACHADINHOS DO CORREDOR",
}

def cap_produto(p):
    marca = (p.get("product_brand") or p.get("brand_name") or "").upper()
    de, por = float(p["original_price"]), float(p["final_price"])
    pct = int(pct_of(p))
    vit = VITRINE_LABEL.get(p.get("vitrine"), "🛍 OFERTA RUNNERS BRASIL")
    t = crossed(de, por)
    if pct >= SUPER_DROP:
        hl = f"🚨 BAIXOU DEMAIS: {pct}% OFF"
    elif t:
        hl = f"💰 {marca} POR MENOS DE R$ " + f"{t:,}".replace(",", ".")
    elif p.get("coupon_code"):
        hl = f"🎟 CUPOM DA {marca} ATIVADO"
    else:
        hl = f"🔥 OFERTA DO DIA: {pct}% OFF"
    g = gender(p["name"]); name = html.escape(p["name"])
    L = ["<b>" + html.escape(vit) + "</b>",
         "<b>" + html.escape(hl) + "</b>",
         f"💥 <b>{name}</b>" + (f" ({g})" if g else ""),
         f"<s>de {brl(de)}</s> por <b>{brl(por)}</b>",
         f"▼ caiu {int(pct_of(p))}%"]
    if p.get("coupon_code"):
        L.append(f"🎟 Cupom: <code>{html.escape(p['coupon_code'])}</code>")
    if p.get("brand_name"):
        L.append(f"🛒 vendido por <b>{html.escape(p['brand_name'])}</b>")
    return "\n".join(L), "🛒 Comprar com desconto"

def cap_prova(p):
    name = html.escape(p.get("name") or "")
    loc = "/".join(x for x in [p.get("city"), p.get("state")] if x)
    L = ["<b>🏁 PROVA COM CUPOM RB</b>", f"🏃 <b>{name}</b>"]
    linha2 = " · ".join(x for x in [f"📍 {html.escape(loc)}" if loc else "",
                                    f"📅 {fmt_date(p.get('date'))}" if p.get("date") else ""] if x)
    if linha2: L.append(linha2)
    disc = (p.get("discount") or "").replace("OFF", "").strip()
    if p.get("coupon_code"):
        L.append(f"🎟 {disc} OFF na inscrição — cupom <code>{html.escape(p['coupon_code'])}</code>")
    return "\n".join(L), "🏁 Garantir inscrição"

def cap_viagem(p):
    name = html.escape(p.get("name") or "")
    L = ["<b>✈️ VIAGEM DO CORREDOR</b>", f"🌎 <b>{name}</b>"]
    if p.get("subtitle"): L.append(html.escape(p["subtitle"]))
    disc = (p.get("discount") or "").strip()
    if disc: L.append(f"🎟 {disc}" + (f" — cupom <code>{html.escape(p['coupon_code'])}</code>" if p.get("coupon_code") else ""))
    return "\n".join(L), "✈️ Ver pacote"

def cap_servico(p):
    name = html.escape(p.get("name") or "")
    L = ["<b>🛎 SERVIÇO PRO CORREDOR</b>", f"📸 <b>{name}</b>"]
    disc = (p.get("discount") or "").strip()
    if disc or p.get("coupon_code"):
        L.append(f"🎟 {disc}" + (f" — cupom <code>{html.escape(p['coupon_code'])}</code>" if p.get("coupon_code") else ""))
    return "\n".join(L), "🔗 Aproveitar"

CAPS = {"produto": cap_produto, "prova": cap_prova, "viagem": cap_viagem, "servico": cap_servico}

# ---------------------------------------------------------------- Guia de Cupons
GUIA = {
 "07": [("361°","RUNNERSBRASIL10","361sport.com.br"),("Bora Nutrition","RUNNERSBRASIL","boranutrition.com.br"),
        ("COROS","RUNNERSBRASIL","coros.shop"),("Flets","RUNNERSBRASIL15","flets.com.br"),
        ("GUMM","RUNNERSBRASIL15","gumm.com.br"),("Hardyn","RUNNERSBRASIL","hardyn.com.br"),
        ("Housewhey","RUNNERSBRASIL10","housewhey.com.br"),("La Ganexa","RUNNERSBRASIL10","lojalaganexa.com.br")],
 "16": [("MoveOn","RUNNERSBRASIL12","lojamoveon.com.br"),("My Safe Sport","RUNNERSBRASIL12","mysafesport.com.br"),
        ("Pink Cheeks","PNK-RUNNERSBRASILOFICIAL","pinkcheeks.com.br"),("Qiaodan","RUNNERSBRASIL10","qiaodanbrasil.com.br"),
        ("Rikam","RUNNERSBRASIL10","lojarikam.com.br"),("Safe Runners","RUNNERSBRASIL10","runnershop.com.br"),
        ("Sub4 Turismo Esportivo","RUNNERSBRASIL5","sub4.com.br"),("YOPP","RUNNERSBRASIL12","yopp.com.br")],
}
GUIA_FOOTER = ("🏬 <b>Grandes lojas</b> (desconto direto no link): Netshoes · Centauro · "
               "Decathlon · Asics · Adidas · Mizuno · New Balance · Under Armour · Olympikus\n"
               "👉 Tudo em https://runnersbrasil.com/cupons")
def guia_text(slot):
    grupo = GUIA.get(slot)
    if not grupo: return None
    linhas = "\n".join(f"• <b>{m}</b> — <code>{c}</code> 👉 {u}" for m, c, u in grupo)
    return ("🎟️ <b>GUIA DE CUPONS RUNNERS BRASIL</b>\n"
            "Use o cupom em qualquer produto da loja oficial 👇\n\n" + linhas + "\n\n" + GUIA_FOOTER)

# ---------------------------------------------------------------- dedupe (arquivos no repo)
def state_path(d):  return os.path.join(STATE_DIR, f"posted_{d.isoformat()}.json")
def load_recent_ids():
    ids = set()
    for i in range(DEDUPE_DAYS):
        p = state_path(TODAY - timedelta(days=i))
        if os.path.exists(p):
            try: ids.update(json.load(open(p)))
            except Exception: pass
    return ids
def record_posted(new_ids):
    os.makedirs(STATE_DIR, exist_ok=True)
    p = state_path(TODAY); cur = []
    if os.path.exists(p):
        try: cur = json.load(open(p))
        except Exception: cur = []
    json.dump(sorted(set(cur) | set(new_ids)), open(p, "w"))
    for f in (os.listdir(STATE_DIR) if os.path.isdir(STATE_DIR) else []):
        m = re.match(r"posted_(\d{4}-\d{2}-\d{2})\.json", f)
        if m:
            try:
                fd = datetime.fromisoformat(m.group(1)).date()
                if (TODAY - fd).days > DEDUPE_DAYS + 2:
                    os.remove(os.path.join(STATE_DIR, f))
            except Exception: pass

# ---------------------------------------------------------------- Telegram
def tg(method, payload):
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read().decode("utf-8"))   # mostra a razão do Telegram
        except Exception: return {"ok": False, "error": str(e)}

def send_item(it):
    cap, btn = CAPS[it["kind"]](it)
    kb = json.dumps({"inline_keyboard": [[{"text": btn, "url": it["link"]}]]}) if it.get("link") else None
    # candidatos de foto: proxy -> URL original (se diferente)
    photos, u = [], it.get("image_url")
    if u:
        pu = image_for(u)
        photos.append(pu)
        if pu != u:
            photos.append(u)
    last = None
    for ph in photos:
        payload = {"chat_id": CHANNEL_ID, "photo": ph, "caption": cap, "parse_mode": "HTML"}
        if kb: payload["reply_markup"] = kb
        r = tg("sendPhoto", payload)
        if r.get("ok"): return r
        last = r
        time.sleep(1)
    # fallback final: texto com botão (o post NUNCA deixa de sair)
    payload = {"chat_id": CHANNEL_ID, "text": cap, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if kb: payload["reply_markup"] = kb
    r = tg("sendMessage", payload)
    if r.get("ok") and last:
        r["nota"] = f"foto falhou ({(last.get('description') or '?')}), enviado como texto"
    return r

def send_text(text):
    return tg("sendMessage", {"chat_id": CHANNEL_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True})

# ---------------------------------------------------------------- coleta
def valid_product(p):
    try:
        return (p.get("id") and p.get("link") and p.get("original_price") and p.get("final_price")
                and float(p["final_price"]) > 0 and float(p["original_price"]) > 0 and pct_of(p) > 0)
    except Exception:
        return False

def dedupe_by_id(items):
    seen, out = set(), []
    for p in items:
        i = p.get("id")
        if i and i not in seen:
            seen.add(i); out.append(p)
    return out

def fetch_pool(key):
    # tenta do mais completo ao mais simples (colunas/filtros podem variar no schema)
    cols = "id,name,product_brand,brand_name,original_price,final_price,discount,coupon_code,link,image_url,category_slug"
    cols2 = "id,name,brand_name,original_price,final_price,discount,coupon_code,link,image_url,category_slug"
    tries = [
        f"coupons?select={cols}&archived=eq.false&is_verified=eq.true&limit={POOL_LIMIT}",
        f"coupons?select={cols}&is_verified=eq.true&limit={POOL_LIMIT}",
        f"coupons?select={cols2}&is_verified=eq.true&limit={POOL_LIMIT}",
        f"coupons?select=*&is_verified=eq.true&limit={POOL_LIMIT}",
    ]
    for q in tries:
        try:
            r = rest(q, key)
            if isinstance(r, list):
                return r
        except Exception as e:
            print(f"  (pool: tentativa falhou: {e})")
    return []

def collect(key):
    d = {}
    d["price_drops"] = safe(rpc, "home_price_drops", key, label="price_drops")
    d["hero_shoes"]  = safe(rpc, "home_hero_shoes", key, label="hero_shoes")
    d["essentials"]  = safe(rpc, "home_essentials", key, label="essentials")
    d["pool"] = fetch_pool(key)
    # marca a vitrine de origem (1ª ocorrência vence no dedupe)
    for src, lst in (("caiu", d["price_drops"]), ("destaque", d["hero_shoes"]),
                     ("essenciais", d["essentials"]), ("pool", d["pool"])):
        for p in lst:
            if isinstance(p, dict):
                p.setdefault("vitrine", src)
    pc = "id,name,city,state,date,modality,discount,coupon_code,link,image_url,subtitle,valid_until,category_slug"
    d["provas"] = safe(rest, (f"coupons?select={pc}&is_verified=eq.true&category_slug=eq.provas"
                              f"&coupon_code=not.is.null&date=gte.{TODAY.isoformat()}&order=date.asc.nullslast&limit=25"),
                       key, label="provas")
    d["viagem"] = safe(rest, (f"coupons?select={pc}&is_verified=eq.true&category_slug=eq.turismo"
                              f"&order=is_featured.desc&limit=25"), key, label="viagem")
    d["servico"] = safe(rest, (f"coupons?select={pc}&is_verified=eq.true&category_slug=eq.servicos"
                              f"&limit=25"), key, label="servico")
    return d

def pick_shoes(d, recent):
    # prioridade: caiu de preço -> tênis em destaque -> pool (nomes com "tênis")
    pool_shoes = [p for p in d["pool"] if "tênis" in (p.get("name") or "").lower() or "tenis" in (p.get("name") or "").lower()]
    cand = dedupe_by_id([p for p in (d["price_drops"] + d["hero_shoes"] + pool_shoes) if valid_product(p)])
    cand.sort(key=lambda p: pct_of(p) + (LIVE_BONUS if is_live(p) else 0), reverse=True)
    out, used = [], set()
    for p in cand:
        if p["id"] in recent and pct_of(p) < SUPER_DROP: continue
        bn = base_name(p["name"])
        if bn in used: continue
        used.add(bn); p["kind"] = "produto"; out.append(p)
        if len(out) >= N_SHOES: break
    return out

def pick_varied(d, recent, taken):
    cand = dedupe_by_id([p for p in (d["essentials"] + d["pool"]) if valid_product(p)])
    cand.sort(key=lambda p: pct_of(p) + (LIVE_BONUS if is_live(p) else 0), reverse=True)
    out, used_store = [], set()
    for p in cand:
        if p["id"] in taken: continue
        if p["id"] in recent and pct_of(p) < SUPER_DROP: continue
        store = (p.get("brand_name") or p.get("product_brand") or "").lower()
        if store in used_store: continue          # variar as lojas
        used_store.add(store); p["kind"] = "produto"; out.append(p)
        if len(out) >= N_VARIED: break
    return out

def rotate(items, n):
    items = [p for p in items if p.get("id")]
    if not items: return []
    start = (TODAY.toordinal() * 4 + SLOT_IDX) % len(items)
    return [items[(start + i) % len(items)] for i in range(min(n, len(items)))]

def main():
    print(f"== Disparo {SLOT}:00 BRT — {TODAY.isoformat()} — {'ENVIO REAL' if SEND else 'PRÉVIA'} ==")
    key = get_anon_key()
    d = collect(key)
    recent = load_recent_ids()
    print(f"  fontes: drops={len(d['price_drops'])} hero={len(d['hero_shoes'])} "
          f"ess={len(d['essentials'])} pool={len(d['pool'])} provas={len(d['provas'])} "
          f"viagem={len(d['viagem'])} servico={len(d['servico'])} · janela={len(recent)}")

    shoes  = pick_shoes(d, recent)
    taken  = {p["id"] for p in shoes}
    varied = pick_varied(d, recent, taken)
    for p in d["viagem"]:  p["kind"] = "viagem"
    for p in d["provas"]:  p["kind"] = "prova"
    for p in d["servico"]: p["kind"] = "servico"
    viagem  = rotate(d["viagem"], N_VIAGEM)
    provas  = rotate([p for p in d["provas"] if p.get("link")], N_PROVAS)
    servico = rotate([p for p in d["servico"] if p.get("link")], N_SERVICO)

    # ordem do disparo: tênis -> variados -> viagem -> provas -> serviço
    items = shoes + varied + viagem + provas + servico
    print(f"  montado: {len(shoes)} tênis + {len(varied)} variados + {len(viagem)} viagem "
          f"+ {len(provas)} provas + {len(servico)} serviço = {len(items)} itens (LIVE={sum(1 for x in shoes+varied if is_live(x))})\n")

    posted_ids, sent = [], 0
    for i, it in enumerate(items, 1):
        cap, _ = CAPS[it["kind"]](it)
        print(f"── {i}. [{it['kind']}] ──\n{cap}\n[foto] {image_for(it.get('image_url'))}\n[🔗] {it.get('link')}\n")
        if SEND:
            r = send_item(it)
            ok = r.get("ok")
            nota = f" ({r['nota']})" if r.get("nota") else ""
            print(f"   ✓ enviado{nota}" if ok else f"   ✗ {r}")
            if ok:
                sent += 1
                if it["kind"] == "produto": posted_ids.append(it["id"])
            time.sleep(DELAY)
    if SEND:
        print(f"\n  == ENVIADOS: {sent}/{len(items)} ==")

    g = guia_text(SLOT)
    if g:
        print("── GUIA DE CUPONS ──")
        if SEND:
            r = send_text(g); print("   ✓ guia enviado" if r.get("ok") else f"   ✗ {r}")

    if SEND and posted_ids:
        record_posted(posted_ids)
        print(f"\n  dedupe: {len(posted_ids)} produtos gravados em {state_path(TODAY)}")
    if not SEND:
        print("== PRÉVIA: nada foi enviado. ==")

if __name__ == "__main__":
    main()
