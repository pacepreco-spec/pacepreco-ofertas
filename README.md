# pacepreco-ofertas

Coletor automático de ofertas da **Pace & Preço** (comunidade de ofertas de corrida com curadoria da Assessoria Evolução).

## Como funciona

1. O GitHub Actions roda `coletor/coletar.py` às **6h20, 11h20 e 17h20** (Brasília).
2. O script lê fontes **oficiais**:
   - **Awin:** o feed de produtos de cada loja aprovada (preço e estoque reais).
   - **Mercado Livre:** a API pública para desenvolvedores.
3. Filtra pela curadoria em `config/curadoria.json` (marcas, modelos, exclusões) e por **desconto real ≥ 20%**.
4. Guarda o histórico de preços em `data/historico_precos.json`. Com 7+ dias de histórico, o desconto passa a ser calculado sobre a **mediana dos dias anteriores**, e não sobre o "de" da loja (pega desconto inflado).
5. Grava `data/ofertas.json`, que o agente da Pace & Preço lê 30 minutos depois para montar os posts.

## Segredos (Settings → Secrets and variables → Actions → New repository secret)

Nunca coloque links de feed ou chaves no código: eles ficam só aqui.

| Nome do segredo | O que colar |
|---|---|
| `AWIN_FEED_118977` | Link do feed da **Lauri Esporte** gerado no Create-a-Feed da Awin (CSV) |
| `AWIN_FEED_<id da loja>` | Um por loja aprovada na Awin (o número é o ID da loja) |
| `ML_CLIENT_ID` | Client ID do app em developers.mercadolivre.com.br |
| `ML_CLIENT_SECRET` | Client Secret do mesmo app |

Ao aprovar uma loja nova na Awin: crie o segredo `AWIN_FEED_<id>` e adicione a loja em `config/curadoria.json` → `awin.lojas`.

## Rodar na mão

Aba **Actions → Coletar ofertas → Run workflow**.

## Campos de cada oferta (`data/ofertas.json`)

`canal` (awin / mercadolivre), `loja`, `beneficio_loja`, `marca`, `uso_sugerido` (tags), `produto`, `preco`, `preco_referencia`, `base_desconto` (`mediana_historico` ou `preco_tabela_sem_historico`), `desconto_pct`, `url_produto`, `link_afiliado`, `link_pronto` (false = gerar link ML na barra de afiliados), `imagem`, `tamanhos`.
