-- Tabela de log/dedupe dos disparos do Telegram.
-- Registra cada produto postado, para não repetir o mesmo dentro da janela
-- móvel (padrão 3 dias). Super ofertas podem repetir (decisão fica no dispatcher).

create table if not exists public.telegram_posts_log (
  id          bigint generated always as identity primary key,
  coupon_id   uuid not null references public.coupons(id) on delete cascade,
  slot        text not null check (slot in ('07','12','16','19')),
  posted_at   timestamptz not null default now(),
  headline    text,
  price       numeric
);

create index if not exists idx_tpl_coupon   on public.telegram_posts_log (coupon_id);
create index if not exists idx_tpl_postedat  on public.telegram_posts_log (posted_at desc);

-- Só o service_role (usado pela Edge Function) lê/escreve.
alter table public.telegram_posts_log enable row level security;

-- (RLS sem policy pública = ninguém com anon/authenticated acessa;
--  a Edge Function usa a service_role_key e ignora RLS.)

-- Ids postados nos últimos N dias (para o dedupe):
--   select distinct coupon_id from public.telegram_posts_log
--   where posted_at >= now() - interval '3 days';
