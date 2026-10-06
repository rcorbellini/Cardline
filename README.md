# Cardline
Pipeline de cartas, abra seu booster e acompanhe o resultado.

Começando por **Disney Lorcana**: você envia o vídeo abrindo o booster e o cardline

1. identifica cada carta revelada e o instante em que ela aparece;
2. busca o preço de mercado atual (TCGplayer, via [Lorcast](https://lorcast.com));
3. registra as cartas na sua coleção, vinculadas à pipeline que as abriu;
4. compara com o valor pago pelo booster (resultado da abertura);
5. renderiza o vídeo de volta com overlay: preço de cada carta quando ela aparece e o total do booster somando.

## Instalação

Requer [uv](https://docs.astral.sh/uv/). O ffmpeg vem embutido (imageio-ffmpeg), não precisa instalar.

```bash
uv sync
uv run cardline sync          # catálogo + preços + imagens + índices (~2 min na primeira vez, ~500 MB em data/cache)
```

`uv run cardline sync --sets 1,2` indexa só os sets que você abre. Os preços das cartas de cada pipeline
são atualizados no próprio passo de preços, então o `sync` só precisa rodar de novo quando sair um set novo.

## Uso

```bash
uv run cardline serve         # http://localhost:8000
```

A página tem:

- **Coleção**: todas as cartas, com filtros (busca, set, raridade, tinta, foil, preço), grade ou lista, e
  de qual pipeline veio cada cópia.
- **Pipelines**: cada abertura com status, progresso ao vivo, valor pago, valor das cartas e resultado.
  No detalhe ficam os passos, o recorte de cada carta tirado do vídeo ao lado da imagem oficial (para
  conferir), o vídeo com overlay para assistir/baixar e o log.
- **+ Nova pipeline**: upload do vídeo e valor pago pelo booster (em R$ ou US$; pode ser preenchido depois).

Os valores podem ser vistos em US$ ou R$ (cotação do dia), pelo seletor no topo. `serve --host 0.0.0.0`
abre a página para outros aparelhos da rede (sem senha: só faça isso numa rede de confiança).

Sem a página, pelo terminal: `uv run cardline process videos/abertura.mp4 --paid 34.90` (e `cardline list`).

### Passos da pipeline

| Passo | Faz | Artefato em `runs/<id>/` |
|---|---|---|
| Identificar cartas | casa os frames com as imagens oficiais e acha quando cada carta aparece | `scan.json`, `crops/` |
| Conferir com IA local | (opcional) um modelo de visão do Ollama lê nome e número de cada recorte | `scan.json` |
| Atualizar preços | busca os preços atuais do set e precifica cada carta (foil ou não) | `scan.json` |
| Registrar na coleção | substitui as cartas que esta pipeline tinha registrado | banco |
| Gerar vídeo com overlay | vídeo 1080×1920 com etiquetas, total animado e resumo (com valor pago e resultado) | `overlay.mp4` |

Cada passo grava seu estado. Se algo falhar ou o servidor cair, a pipeline pode **continuar de onde parou**,
e qualquer pipeline pode **rodar de novo a partir de um passo** (página ou `cardline run ID --from passo`).
Um vídeo já processado é recusado no upload, para as cartas não entrarem duas vezes na coleção.

### Correções

Se uma carta foi identificada errado:

```bash
uv run cardline edit 4 3 --card 1/24          # na pipeline 4, a carta #3 é a 1/24 (também aceita nome)
uv run cardline edit 4 12 --foil              # a #12 é foil (--no-foil desfaz)
uv run cardline edit 4 5 --remove             # a #5 não é uma carta aberta
uv run cardline edit 4 --card 1/55 --at 9.2   # faltou uma carta aos 9,2 s
```

A correção muda só o resultado da identificação. Preço, coleção e vídeo ficam marcados como
desatualizados até você clicar **Rodar o resto** na página (ou `cardline run 4`). Aí só esses passos
rodam de novo, e as cartas registradas na coleção mudam junto.

Cartas obtidas fora de vídeo: `uv run cardline add 1/169 --foil --qty 2`.

### Como gravar

O formato do vídeo de exemplo funciona bem: câmera de cima, cartas colocadas **uma por vez numa pilha**,
cada uma parada por ~1 s. Luz boa e sem reflexo forte ajudam. Pode abrir vários boosters no mesmo vídeo
(agrupados de `pack_size` em `pack_size` cartas). O set é detectado sozinho, ou pode ser escolhido no upload.

## Configuração

`cardline.toml` (todas as chaves são opcionais e estão documentadas no arquivo): moeda padrão do vídeo
(`USD` ou `BRL`), cartas por booster, fps da análise, resolução de saída, duração do resumo, e o modelo do
Ollama para conferência. Com `verify_model` preenchido, a opção já vem marcada no upload.

## Como funciona

- **Identificação** (`index.py`, `matcher.py`): as imagens oficiais de cada set viram um índice de features
  SIFT. A arte tem um orçamento próprio de features, porque as mais fortes de uma carta ficam no texto,
  que usa a mesma fonte em todas as cartas. Cada frame (10 fps) é casado contra o índice com FLANN, teste
  de razão e votação por carta, e o resultado é verificado por homografia (RANSAC). A homografia diz *qual*
  carta é e *onde* ela está, e é dela que saem o recorte retificado e a posição da etiqueta no overlay.
- **Segmentação** (`scan.py`): a carta com mais pontos casados é a do topo da pilha. Cada sequência estável
  de uma nova carta no topo é uma carta revelada.
- **Foil** (`foil.py`): o brilho foil não é confiável no vídeo, então ele é inferido pela estrutura do booster.
  Um booster tem 6 comuns, 3 incomuns, 2 raras+ e 1 foil. A classe de raridade que sobra contém a foil, e
  ela fica na ponta do booster. Enchanted, Epic e Iconic são sempre foil.
- **Pipeline** (`pipeline.py`): passos com estado no banco (`runs`, `run_steps`) e artefatos na pasta do run.
  O servidor (`server.py`, FastAPI) roda uma pipeline por vez, cada uma num processo próprio
  (`cardline run ID`), e a página acompanha o progresso por polling.
- **Overlay** (`overlay.py`): o ffmpeg decodifica (com tone mapping HDR→SDR para os vídeos HLG do Pixel), o
  Pillow desenha e o x264 codifica mantendo o áudio original.
- **Conferência com IA local** (`verify.py`): no vídeo de exemplo o `qwen3.5:4b` acertou 12/12 nomes e
  números (~1 s por carta). Ele só **sinaliza** divergências, nunca troca a carta, porque pode alucinar.

## Dados

- `data/cardline.db`: catálogo, preços, pipelines e **a sua coleção**. Fica fora do git (o repositório é
  público e o banco muda a cada atualização de preço), então faça backup desse arquivo.
- `runs/<id>/`: vídeo enviado, recortes, `scan.json`, `overlay.mp4` e `pipeline.log` de cada pipeline.
- `data/cache/`: imagens e índices, regeneráveis com `cardline sync`.

## Testes

```bash
uv run pytest
```
