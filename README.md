# Disparador de Ofertas — Runners Brasil (Telegram)

Automação que posta ofertas diárias no canal **@runnersbrasil** via GitHub Actions,
4x por dia (**07:00 / 12:00 / 16:00 / 19:00 BRT**). Cada produto vira 1 post com
**foto + legenda** (de/por real, cupom, loja, selo de queda e **link de afiliado
intocado** como botão). Inclui o **Guia de Cupons** (Grupo 1 às 07:00, Grupo 2 às 16:00).

> **Segurança:** o token do bot **nunca** fica no código — é um *secret* do repositório.
> A IA nunca vê nem digita o token.

---

## Como colocar no ar (5 minutos)

### 1. Criar o repositório
Suba esta pasta (`telegram-disparador/`) para um repositório no GitHub (pode ser privado).

### 2. Cadastrar os secrets
No repositório: **Settings → Secrets and variables → Actions → New repository secret**. Crie:

| Secret | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | o token do seu bot (o que o BotFather te deu) |
| `TELEGRAM_CHANNEL_ID` | **comece por um canal de TESTE**: `@seu_canal_teste` (ou id `-100...`) |

> O **bot precisa ser ADMINISTRADOR** do canal para poder postar.
> Comece apontando para um **canal privado de aprovação** (fila de aprovação).
> Quando aprovar o resultado, troque o secret para `@runnersbrasil`.

### 3. Ligar o Actions e testar
- Aba **Actions** → habilite os workflows.
- **Run workflow** (workflow_dispatch): escolha o `slot` (ex.: `12`) e deixe
  `dry_run = true` para ver a **prévia nos logs** (não envia nada).
- Rode de novo com `dry_run = false` para **enviar de verdade** ao canal de teste.

### 4. Ligar os 4 horários
Depois de aprovado, não precisa fazer nada: os 4 crons já estão no workflow e
disparam sozinhos todo dia. Para migrar do canal de teste para o oficial, basta
editar o secret `TELEGRAM_CHANNEL_ID` para `@runnersbrasil`.

---

## O que ele faz

- **Fonte:** as 3 vitrines do site (*Tênis em destaque*, *Caiu de preço*,
  *Essenciais do corredor*) + um **pool amplo** de produtos verificados, em estoque,
  com desconto e link de afiliado (chega a ~200+ itens para girar vários dias).
- **Prioriza:** maiores quedas e **marca LIVE** (peso extra — `LIVE_BONUS`).
- **Não repete** o mesmo produto por `DEDUPE_DAYS` (3) dias — estado salvo em `state/`
  e commitado de volta pelo próprio Actions. **Super ofertas** (queda ≥ `SUPER_DROP`%,
  padrão 55%) podem repetir.
- **Legenda honesta:** `por` = preço real da loja (nunca Pix). Sem "menor preço
  histórico" nesta versão (veja o upgrade abaixo). Selos: `🎟 cupom`,
  `💰 por menos de R$X`, `🔥 baita preço`.

### Ajustes finos (opcional)
No `.github/workflows/disparo.yml`, bloco `env:` do passo *Rodar disparador*:
`N_PRODUCTS` (padrão 10), `DEDUPE_DAYS` (3), `SUPER_DROP` (55), `LIVE_BONUS` (15).

---

## Rodar localmente (opcional)

```bash
# prévia (não envia, não precisa de token):
SLOT=12 python3 dispatcher.py

# envio real para um canal de teste:
export TELEGRAM_BOT_TOKEN="seu_token"
export TELEGRAM_CHANNEL_ID="@seu_canal_teste"
export SLOT=12 SEND=1
python3 dispatcher.py
```
Só usa a biblioteca padrão do Python 3 — nada para instalar.

---

## Upgrade: selo "🔻 MENOR PREÇO HISTÓRICO" (opcional)

Para o selo de mínimo histórico com dado que comprove, é preciso ler a tabela
`price_history` (restrita ao `service_role`). Isso vive **server-side**, na pasta
`upgrade-price-history/`:

- `sql/telegram_posts_log.sql` — tabela de log/dedupe no banco (alternativa ao `state/`).
- `edge-function/telegram-feed/index.ts` — Edge Function (padrão `awin-audit-sync`)
  que devolve os produtos já com os selos calculados (queda vs. maior preço recente
  e mínimo histórico) e registra o dedupe no banco.

Depois de publicar a Edge Function no Lovable/Supabase e cadastrar os secrets
`RB_FEED_URL` e `RB_FEED_TOKEN`, dá para trocar a fonte do dispatcher para ela.
Me avise que eu faço essa troca.
