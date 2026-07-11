// Edge Function: telegram-feed  (Deno / Supabase)
// -------------------------------------------------------------------------
// Mesmo padrão do robô de auditoria (awin-audit-sync): função pública
// protegida por header. Devolve os produtos do disparo JÁ com os selos
// calculados a partir de price_history (server-side, service_role), e
// registra o dedupe em telegram_posts_log.
//
// Protegida pelo header:  x-rb-token: <RB_FEED_TOKEN>
//
// Chamadas:
//   GET  ?slot=07&n=10   -> seleciona e devolve os produtos do disparo
//   POST {slot, coupon_ids:[...]}  -> grava no telegram_posts_log (dedupe)
//
// Selos honestos (só quando o dado comprova):
//   • historic_low  : final_price <= menor price_history já registrado
//   • drop_pct_recent: queda vs. maior price_history recente (janela)
// -------------------------------------------------------------------------

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const FEED_TOKEN   = Deno.env.get("RB_FEED_TOKEN")!;      // segredo do header
const DEDUPE_DAYS  = Number(Deno.env.get("DEDUPE_DAYS") ?? "3");
const SUPER_DROP   = Number(Deno.env.get("SUPER_DROP") ?? "55");
const LIVE_BONUS   = Number(Deno.env.get("LIVE_BONUS") ?? "15");

const db = createClient(SUPABASE_URL, SERVICE_ROLE, { auth: { persistSession: false } });

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function pct(orig: number, final: number) {
  return orig > 0 ? Math.round(((orig - final) / orig) * 100) : 0;
}

Deno.serve(async (req) => {
  // auth por header (nunca por conteúdo observado)
  if (req.headers.get("x-rb-token") !== FEED_TOKEN) return json({ error: "unauthorized" }, 401);

  const url = new URL(req.url);

  // ---- POST: registrar dedupe --------------------------------------------
  if (req.method === "POST") {
    const { slot, coupon_ids } = await req.json().catch(() => ({}));
    if (!slot || !Array.isArray(coupon_ids)) return json({ error: "bad request" }, 400);
    const rows = coupon_ids.map((id: string) => ({ coupon_id: id, slot }));
    const { error } = await db.from("telegram_posts_log").insert(rows);
    if (error) return json({ error: error.message }, 500);
    return json({ ok: true, logged: rows.length });
  }

  // ---- GET: montar o disparo ---------------------------------------------
  const slot = url.searchParams.get("slot") ?? "07";
  const n = Number(url.searchParams.get("n") ?? "10");

  // 1) vitrines em evidência
  const vit: any[] = [];
  for (const fn of ["home_hero_shoes", "home_price_drops", "home_essentials"]) {
    const { data } = await db.rpc(fn);
    if (Array.isArray(data)) vit.push(...data);
  }
  // 2) pool amplo
  const { data: pool } = await db
    .from("coupons")
    .select("id,name,product_brand,brand_name,original_price,final_price,discount,coupon_code,link,image_url,category_slug")
    .eq("archived", false).eq("is_verified", true)
    .not("original_price", "is", null).not("final_price", "is", null).not("link", "is", null)
    .order("updated_at", { ascending: false }).limit(400);

  // dedupe da janela
  const since = new Date(Date.now() - DEDUPE_DAYS * 864e5).toISOString();
  const { data: recentRows } = await db
    .from("telegram_posts_log").select("coupon_id").gte("posted_at", since);
  const recent = new Set((recentRows ?? []).map((r: any) => r.coupon_id));

  // normaliza + dedupe por id
  const map = new Map<string, any>();
  for (const p of [...vit, ...(pool ?? [])]) {
    if (!p?.id || map.has(p.id)) continue;
    const orig = Number(p.original_price), fin = Number(p.final_price);
    if (!(orig > 0) || !(fin > 0) || !p.link) continue;
    if (pct(orig, fin) <= 0) continue;
    map.set(p.id, p);
  }
  const candidates = [...map.values()];

  // selos via price_history (só os candidatos, para não varrer a tabela toda)
  const ids = candidates.map((c) => c.id);
  const { data: ph } = await db
    .from("price_history").select("coupon_id,price,seen_at")
    .in("coupon_id", ids);
  const hist = new Map<string, number[]>();
  for (const r of ph ?? []) {
    const arr = hist.get(r.coupon_id) ?? [];
    arr.push(Number(r.price)); hist.set(r.coupon_id, arr);
  }

  const enrich = (p: any) => {
    const fin = Number(p.final_price);
    const h = hist.get(p.id) ?? [];
    const minHist = h.length ? Math.min(...h) : null;
    const maxHist = h.length ? Math.max(...h) : null;
    const historic_low = minHist !== null && fin <= minHist;      // dado comprova
    const drop_recent = maxHist ? Math.round(((maxHist - fin) / maxHist) * 100) : null;
    const is_live = ((p.product_brand || p.brand_name || "") + "").toLowerCase().includes("live");
    return { ...p, pct: pct(Number(p.original_price), fin), historic_low, drop_recent, is_live };
  };

  const elig = candidates.map(enrich)
    .filter((p) => !recent.has(p.id) || p.pct >= SUPER_DROP)
    .sort((a, b) => (b.pct + (b.is_live ? LIVE_BONUS : 0)) - (a.pct + (a.is_live ? LIVE_BONUS : 0)));

  // evita quase-duplicados (mesmo nome base)
  const used = new Set<string>(); const picks: any[] = [];
  for (const p of elig) {
    const base = p.name.toLowerCase().replace(/\s+(masculino|feminino|unissex).*$/, "");
    if (used.has(base)) continue;
    used.add(base); picks.push(p);
    if (picks.length >= n) break;
  }

  return json({ slot, count: picks.length, products: picks });
});
