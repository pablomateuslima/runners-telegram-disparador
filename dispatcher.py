#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Runners Brasil — Disparador de Ofertas no Telegram (produção)
=============================================================
Um disparo por horário (07:00 / 12:00 / 16:00 / 19:00 BRT), rodado pelo
GitHub Actions. Cada produto = 1 post com FOTO + legenda; mais o "Guia de
Cupons" (Grupo 1 às 07:00, Grupo 2 às 16:00).

PRINCÍPIOS (inegociáveis):
  • A IA / o repositório NUNCA veem o token: vem do secret TELEGRAM_BOT_TOKEN.
  • "por" = preço real da loja (final_price). NUNCA preço de Pix.
  • Link de afiliado (coupons.link) sai INTOCADO, como botão "🛒 Comprar".
  • Sem "menor preço histórico" nesta versão (exige price_history via
    service_role — vem no upgrade da Edge Function). Aqui só selos honestos
    derivados de de/por reais.
  • Sem repetir o mesmo produto por DEDUPE_DAYS dias (janela móvel).
    Exceção: super ofertas (queda >= SUPER_DROP%) podem repetir.
  • FILA DE APROVAÇÃO: comece com TELEGRAM_CHANNEL_ID apontando para um canal
    PRIVADO de aprovação (só você). Quando aprovar, troque para @runnersbrasil.

Variáveis de ambiente (secrets/inputs do Actions):
  TELEGRAM_BOT_TOKEN   (secret)   token do bot — só você tem
  TELEGRAM_CHANNEL_ID  (secret)   @canal ou -100... (comece pelo canal de teste)
  SLOT                 07|12|16|19 (definido pelo workflow)
  SEND                 "1" envia de verdade; vazio = só prévia (log)
  N_PRODUCTS           padrão 10
  DEDUPE_DAYS          padrão 3
  SUPER_DROP           padrão 55   (queda% que pode repetir)
  LIVE_BONUS           padrão 15   (peso extra p/ marca LIVE — sua comissão)
  POOL_LIMIT           padrão 400  (tamanho do pool amplo lido do banco)
  STATE_DIR            padrão ./state
  DELAY                padrão 3    (segundos entre posts)
"""

import os, re, sys, json, time, html, base64, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

SUPA_URL = "https://jpjhgetlhlnryalplqcf.supabase.co"
SITE = "https://cupons-rb.com/"

BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
SLOT       = os.environ.get("SLOT", "07").strip()
SEND       = os.environ.get("SEND", "").strip() in ("1", "true", "yes")
N_PRODUCTS = int(os.environ.get("N_PRODUCTS", "10"))
DEDUPE_DAYS= int(os.environ.get("DEDUPE_DAYS", "3"))
SUPER_DROP = float(os.environ.get("SUPER_DROP", "55"))
LIVE_BONUS = float(os.environ.get("LIVE_BONUS", "15"))
POOL_LIMIT = int(os.environ.get("POOL_LIMIT", "400"))
STATE_DIR  = os.environ.get("STATE_DIR", "state")
DELAY      = float(os.environ.get("DELAY", "3"))

# BRT = UTC-3 (Brasil sem horário de verão desde 2019)
BRT = timezone(timedelta(hours=-3))
TODAY = datetime.now(BRT).date()

# ---------------------------------------------------------------- HTTP / dados
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

def http_get(url, headers=None, timeout=30):
    h = {"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def get_anon_key():
    """Chave pública (anon) extraída do bundle do site — sem hardcode."""
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
        headers={"apikey": key, "Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))

def rest(path, key):
    req = urllib.request.Request(f"{SUPA_URL}/rest/v1/{path}",
        headers={"apikey": key, "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))

# ----------------------------------------------------------------- helpers
def brl(n):
    return "R$ " + f"{float(n):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def gender(name):
    n = (name or "").lower()
    return ("Feminino" if "femin" in n else "Masculino" if "mascul" in n
            else "Unissex" if "unissex" in n else None)

def image_proxy(u):
    if not u: return None
    return f"{SUPA_URL}/functions/v1/image-proxy?url=" + urllib.parse.quote(u.split("?")[0], safe="")

def is_live(p):
    return "live" in ((p.get("product_brand") or p.get("brand_name") or "").lower())

def pct_of(p):
    v = p.get("discount_pct") or p.get("drop_pct")
    if v: return float(v)
    de, por = p.get("original_price"), p.get("final_price")
    if de and por and float(de) > 0:
        return round((float(de) - float(por)) / float(de) * 100)
    return 0

ROUND_STEPS = [100, 150, 200, 250, 300, 400, 500, 700, 1000, 1500, 2000]
def crossed(de, por):
    for t in ROUND_STEPS:
        if por < t <= de: return t
    return None

def headline(p):
    marca = (p.get("product_brand") or p.get("brand_name") or "").upper()
    if p.get("coupon_code"):
        return f"🎟 CUPOM DA {marca}"
    t = crossed(float(p["original_price"]), float(p["final_price"]))
    if t:
        return f"💰 {marca} POR MENOS DE R$ " + f"{t:,}".replace(",", ".")
    if pct_of(p) >= SUPER_DROP:
        return "🔥 OFERTA DO DIA"
    return "🔥 BAITA PREÇO"

def caption(p):
    de, por = float(p["original_price"]), float(p["final_price"])
    g = gender(p["name"]); name = html.escape(p["name"])
    L = ["<b>" + html.escape(headline(p)) + "</b>",
         f"💥 <b>{name}</b>" + (f" ({g})" if g else ""),
         f"<s>de {brl(de)}</s> por <b>{brl(por)}</b>",
         f"▼ caiu {int(pct_of(p))}%"]
    if p.get("coupon_code"):
        L.append(f"🎟 Cupom: <code>{html.escape(p['coupon_code'])}</code>")
    if p.get("brand_name"):
        L.append(f"🛒 vendido por <b>{html.escape(p['brand_name'])}</b>")
    return "\n".join(L)

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
    p = state_path(TODAY)
    cur = []
    if os.path.exists(p):
        try: cur = json.load(open(p))
        except Exception: cur = []
    json.dump(sorted(set(cur) | set(new_ids)), open(p, "w"))
    # limpa arquivos antigos (> DEDUPE_DAYS+2 dias)
    for f in os.listdir(STATE_DIR) if os.path.isdir(STATE_DIR) else []:
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
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))

def send_photo(photo, cap, buy):
    payload = {"chat_id": CHANNEL_ID, "photo": photo, "caption": cap, "parse_mode": "HTML"}
    if buy:
        payload["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": "🛒 Comprar com desconto", "url": buy}]]})
    return tg("sendPhoto", payload)

def send_text(text):
    return tg("sendMessage", {"chat_id": CHANNEL_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True})

# ---------------------------------------------------------------- seleção
def build_pool(key):
    # base em evidência: as 3 vitrines do site
    base = []
    for fn in ("home_hero_shoes", "home_price_drops", "home_essentials"):
        try: base += rpc(fn, key)
        except Exception as e: print(f"  (aviso: {fn} falhou: {e})")
    # pool amplo: coupons verificados, em estoque, com link e desconto
    cols = "id,name,product_brand,brand_name,original_price,final_price,discount,coupon_code,link,image_url,category_slug"
    q = (f"coupons?select={cols}&archived=eq.false&is_verified=eq.true"
         f"&original_price=not.is.null&final_price=not.is.null&link=not.is.null"
         f"&order=updated_at.desc&limit={POOL_LIMIT}")
    try: base += rest(q, key)
    except Exception as e: print(f"  (aviso: pool falhou: {e})")
    # normaliza + dedupe por id
    seen, out = set(), []
    for p in base:
        pid = p.get("id")
        if not pid or pid in seen: continue
        if not p.get("final_price") or float(p["final_price"]) <= 0: continue
        if not p.get("original_price") or float(p["original_price"]) <= 0: continue
        if not p.get("link"): continue
        if pct_of(p) <= 0: continue
        seen.add(pid); out.append(p)
    return out

def score(p, recent):
    s = pct_of(p)
    if is_live(p): s += LIVE_BONUS
    if p["id"] not in recent: s += 5
    return s

def main():
    print(f"== Disparo {SLOT}:00 BRT — {TODAY.isoformat()} — {'ENVIO REAL' if SEND else 'PRÉVIA'} ==")
    key = get_anon_key()
    pool = build_pool(key)
    recent = load_recent_ids()
    print(f"  pool={len(pool)} · já postados (janela {DEDUPE_DAYS}d)={len(recent)}")
    elig = [p for p in pool if p["id"] not in recent or pct_of(p) >= SUPER_DROP]
    elig.sort(key=lambda p: score(p, recent), reverse=True)
    picks, used = [], set()
    for p in elig:
        base_name = re.sub(r"\s+(masculino|feminino|unissex).*$", "", p["name"].lower())
        if base_name in used: continue
        used.add(base_name); picks.append(p)
        if len(picks) >= N_PRODUCTS: break
    print(f"  selecionados={len(picks)} (LIVE={sum(1 for x in picks if is_live(x))})\n")
    posted_ids = []
    for i, p in enumerate(picks, 1):
        cap = caption(p); photo = image_proxy(p.get("image_url")); buy = p.get("link")
        print(f"── POST {i} ──\n{cap}\n[foto] {photo}\n[🛒] {buy}\n")
        if SEND:
            try:
                r = send_photo(photo, cap, buy)
                ok = r.get("ok")
                print("   ✓ enviado" if ok else f"   ✗ {r}")
                if ok: posted_ids.append(p["id"])
            except Exception as e:
                print(f"   ✗ falha: {e}")
            time.sleep(DELAY)
    g = guia_text(SLOT)
    if g:
        print("── GUIA DE CUPONS ──\n" + g + "\n")
        if SEND:
            try:
                r = send_text(g); print("   ✓ guia enviado" if r.get("ok") else f"   ✗ {r}")
            except Exception as e:
                print(f"   ✗ falha guia: {e}")
    if SEND and posted_ids:
        record_posted(posted_ids)
        print(f"\n  dedupe: {len(posted_ids)} ids gravados em {state_path(TODAY)}")
    if not SEND:
        print("== PRÉVIA: nada foi enviado. (Defina SEND=1 e os secrets do bot para enviar.) ==")

if __name__ == "__main__":
    main()
