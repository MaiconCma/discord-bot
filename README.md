# Discord Bot (SET / BAÚ / VENDAS / ECONOMIA)

## 1) Instalar dependências
```bash
pip install -r requirements.txt
```

## 2) Configurar .env
Copie `.env.example` para `.env` e coloque seu TOKEN.

## 3) Rodar
```bash
python main.py
```

## 4) Comandos
### Painéis
- `/painelset` (admin) — painel de SET (alterar nickname)
- `/painelbau` (admin) — painel de BAÚ (criar canal privado)
- `/painelvendas` — painel passo a passo de vendas

### Vendas
- `/listavendas` — mostra as últimas vendas (ephemeral)
- `/exportvendas` — baixa CSV (e XLSX se habilitado)

### Economia
- `/saldo` — seu saldo
- `/pagar` — transfere saldo
- `/caixa` — mostra o caixa (somatório das vendas)
- `/addsaldo` (admin) — adiciona saldo
- `/remsaldo` (admin) — remove saldo

## Notas
- A pasta `data/` é criada automaticamente (CSV e economia.json).
- Se der erro, veja `bot.log`.
