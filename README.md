# Cardline
Pipeline de cartas, abra seu booster e acompanhe o resultado.

Começando por **Disney Lorcana**: você envia o vídeo abrindo o booster e o cardline

1. identifica cada carta revelada e o instante em que ela aparece;
2. busca o preço de mercado atual (TCGplayer, via [Lorcast](https://lorcast.com));
3. registra as cartas na sua coleção, vinculadas à pipeline que as abriu;
4. compara com o valor pago pelo booster (resultado da abertura);
5. renderiza o vídeo de volta com overlay: uma capa com o booster e o valor pago, o preço de cada carta quando
   ela aparece e o total do booster somando, num painel com o ícone do set;
6. (opcional) narra o vídeo: um narrador de trailer comenta a abertura sem dar spoiler, com voz em português;
7. (opcional) posta no YouTube, Instagram e TikTok e acompanha visualizações e reações de cada abertura.

Também dá para **cadastrar cartas que você já tem**: grave as cartas uma por uma e o cardline identifica,
precifica e registra na coleção, sem valor pago e sem vídeo de saída.

<p align="center">
  <img src="docs/demo.gif" width="360" alt="Abertura de um booster com o overlay do cardline: capa com o booster e o valor pago, o booster voando para o painel do topo, etiqueta com raridade e preço em cada carta revelada, total do booster somando e resumo final com todas as cartas">
</p>
<p align="center"><sub>Vídeo de exemplo processado pelo cardline (as cartas aceleradas; o valor pago é ilustrativo).</sub></p>

## Instalação

Requer [uv](https://docs.astral.sh/uv/). O ffmpeg vem embutido (imageio-ffmpeg), não precisa instalar.

```bash
uv sync
uv run cardline sync          # catálogo + preços + imagens + índices (~2 min na primeira vez, ~500 MB em data/cache)
```

Para a **narração** (opcional), instale o extra da voz. Ele é pesado: torch (só CPU) e, na primeira narração,
a voz XTTS-v2 (1,9 GB), que usa a [licença CPML](https://coqui.ai/cpml), só para uso não comercial:

```bash
uv sync --extra narracao
COQUI_TOS_AGREED=1 uv run cardline voz   # aceita a licença, baixa a voz e lista as 58 vozes disponíveis
```

Depois disso, rode sempre `uv sync --extra narracao`: um `uv sync` sem o extra desinstala a voz.
`uv run cardline voz --amostras` grava uma amostra de cada voz (~6 min), para ouvir e escolher na página. As piadas do
roteiro são escritas por um modelo do Ollama (`gemma3:4b` por padrão); sem Ollama, o roteiro sai sem elas.

`uv run cardline sync --sets 1,2` indexa só os sets que você abre. Os preços das cartas de cada pipeline
são atualizados no próprio passo de preços, então o `sync` só precisa rodar de novo quando sair um set novo
(também dá pela página: botão **Sincronizar** na aba **Sets**).

## Uso

```bash
uv run cardline serve         # abre a página em http://localhost:8000
```

Sem a página, pelo terminal: `uv run cardline process videos/abertura.mp4 --paid 34.90` para uma abertura,
`uv run cardline process videos/colecao.mp4 --cadastro` para um cadastro, e `cardline list` para ver as pipelines.

### Como gravar

O formato do vídeo de exemplo funciona bem: câmera de cima, cartas colocadas **uma por vez numa pilha**,
cada uma parada por ~1 s. Luz boa e sem reflexo forte ajudam. Pode abrir vários boosters no mesmo vídeo
(agrupados de `pack_size` em `pack_size` cartas). O set é detectado sozinho, ou pode ser escolhido no upload.

## Página web

A página fica em `cardline/web/` (HTML, CSS e JavaScript puros, sem build) e é servida pelo
`cardline/server.py` (FastAPI). O mesmo servidor expõe a API, recebe os uploads e roda as pipelines em fila,
uma por vez, cada uma num processo próprio.

A página tem quatro abas: **Resumo** (onde ela abre), **Coleção**, **Pipelines** e **Sets**. No topo ficam sempre o
seletor US$/R$ (pela cotação do dia), o tema claro/escuro e o botão **+ Nova pipeline**. As capturas
abaixo são do booster de exemplo.

### Resumo

![Aba Resumo: valor da coleção, cartas, únicas, foils, investido em boosters e resultado, com o gráfico de gasto vs valor das cartas](docs/pagina-resumo.jpg)

O valor da coleção (somando tudo, com o detalhe de quanto vem de aberturas, de cadastros e de cartas avulsas),
o total investido em boosters e o resultado das aberturas. O gráfico **Gasto vs valor das cartas** acumula,
abertura por abertura, quanto foi pago e quanto as cartas valiam na abertura e valem hoje. Ele tem tooltip
(também pelo teclado, com as setas) e uma tabela com os mesmos números em "Ver tabela".

O gráfico **Valor da coleção por data** ganha um ponto por dia sempre que os preços são atualizados (botão ou
sincronização) ou a coleção muda (pipeline registrada ou excluída, carta avulsa, lacrado); várias atualizações
no mesmo dia viram um ponto só, o da última. Com lacrados, eles aparecem como uma segunda linha. O painel
**Lacrados** mostra o valor de hoje, os itens, o pago e o resultado, e os itens de maior valor. Os dias de antes do gráfico existir vieram do histórico de preços que o
cardline já guardava.

O gráfico **Valor médio do booster por coleção** mostra, para cada set, quanto vale em média um booster aberto
pelo valor das suas cartas (não pelo preço de compra), na abertura e hoje. Cada pacote do vídeo conta como um
booster, e a coleção dele é a da maioria das cartas; embaixo de cada set aparece quantos boosters entraram na média.

Cadastros de coleção **não entram** no investido, no resultado nem nesses gráficos: são cartas que você já tinha,
sem custo, e entrariam como valor sem gasto, inflando o resultado das aberturas.

**↻ Atualizar preços**, no topo, ao lado da data dos preços, busca os preços de mercado de hoje das cartas da
coleção. Roda em segundo plano (dá para navegar enquanto isso) e o Resumo mostra em que set está: é uma
consulta ao Lorcast por set, que traz todas as cartas do set de uma vez, então o tempo depende de quantos sets a
coleção tem, não de quantas cartas. O valor de cada carta **no momento da abertura** fica guardado e não muda,
nem com esse botão nem ao reprocessar uma pipeline. É ele que aparece no vídeo e em "Na abertura".

### Nova pipeline

![Formulário de nova pipeline: área para arrastar o vídeo, valor pago com moeda, set, moeda do vídeo e opções de overlay e conferência com IA local](docs/pagina-nova.jpg)

1. Clique em **+ Nova pipeline** e escolha o tipo:
   - **Abertura de booster**: identifica, precifica e registra as cartas abertas, compara com o valor pago e
     gera o vídeo com overlay.
   - **Cadastro de coleção**: para cartas que você já tem. Identifica, precifica e registra na coleção, sem
     valor pago e sem vídeo. Como não há booster, a foil não é deduzida: marque as foils pela edição.
2. Arraste o vídeo (MP4 ou MOV, do jeito que sai do celular).
3. Na abertura, escolha a **moeda** uma vez (R$ ou US$): ela vale para o **valor pago** pelo(s) booster(s) e para
   os preços no vídeo. O valor pago é opcional e pode ser preenchido depois. Se o booster estava nos **lacrados**,
   escolha-o em **Booster dos lacrados** (e quantos, se tiver mais de um): o valor pago vem do valor de registro
   dele (o pago informado ou, sem ele, o preço de mercado quando foi registrado) e o set também; ele sai do
   estoque ao enviar e volta se a pipeline for excluída.
4. **Set das cartas**: deixe em "Detectar automaticamente" ou escolha o set. As opções ficam em chaves (liga/desliga):
   **vídeo com overlay**, **logo no vídeo** e **narrar o vídeo** (precisa do extra `narracao`; ~2 min a mais).
   **Logo no vídeo** põe o logo semitransparente num canto, da capa ao resumo: vem marcado com o logo padrão
   (`data/logos/padrao.png`), e **Trocar logo** usa outra imagem só nesta pipeline (PNG com fundo transparente
   fica melhor).
5. **Conferir com IA local** só fica habilitado com o Ollama rodando. Com `verify_model` no
   `cardline.toml`, ele já vem marcado.
6. **Enviar e processar** (ou **Enviar e cadastrar**): a barra mostra o envio. Ao terminar, a página abre a
   pipeline e acompanha o progresso sozinha.

Um vídeo que já foi processado é recusado, com um link para a pipeline original.

### Pipelines

![Lista de pipelines: cada abertura com status, miniaturas das cartas, valor pago, valor de hoje e resultado](docs/pagina-pipelines.jpg)

A lista mostra cada pipeline com o tipo, o status (na fila, rodando, concluída, falhou, interrompida ou
desatualizada), o progresso ao vivo, as miniaturas das cartas e os valores: pago, hoje e resultado numa
abertura; no cadastro e hoje num cadastro. O filtro no topo separa **Todas**, **Aberturas**, **Cadastros** e
**Atualizações**: cada **↻ Atualizar preços**, **↻ Atualizar números** e **Sincronizar** (Sets) disparado na
página também vira uma linha, com o andamento enquanto roda e, depois, o que mudou (valor das cartas antes e
depois, visualizações antes e depois, sets sincronizados). Clicar abre o detalhe, com o log da sincronização.
A leitura automática dos números ao abrir o Resumo não vira linha.
O detalhe de um cadastro tem o mesmo editar/remover por deslize e o mesmo reprocessar, mas sem vídeo.

**⚠ Repetida** marca a pipeline cujas cartas identificadas são as mesmas de outra pipeline (aberturas e
cadastros), sem contar a ordem, o foil nem as cartas repetidas dentro dela. Vale também depois de editar:
remover uma carta lida errado pode deixar a pipeline igual a outra, e restaurar desfaz o aviso. No detalhe, o
aviso tem o link da outra pipeline. O mesmo arquivo de vídeo enviado de novo já é recusado no envio; o aviso
pega o mesmo booster gravado de novo.

![Detalhe de uma pipeline: valor pago, valor das cartas e resultado; cartas com o recorte do vídeo ao lado da imagem oficial; passos com tempo de cada um; vídeo com overlay](docs/pagina-pipeline.jpg)

No detalhe de uma pipeline:

- **Valor pago** (com "editar"), **cartas na abertura**, **valor hoje** e **resultado** sobre o valor pago.
- **Cartas**: o recorte tirado do vídeo ao lado da imagem oficial, para conferir a identificação. A melhor
  carta fica destacada, e ⚠ marca uma divergência apontada pela IA local. Clicar abre os detalhes da carta.
- **Corrigir cartas**: deslize a carta para a **direita** para **Editar** ou para a **esquerda** para
  **Remover** (com o mouse, clique e arraste; pelo teclado, Tab chega nos dois botões).
  - **Editar** abre a carta com a chave **Foil**. Como um booster tem uma foil, marcar uma carta tira a
    marcação que o sistema tinha deduzido em outra do mesmo booster. Encantada, Épica e Icônica são sempre foil.
  - **Remover** tira uma carta identificada errada ou duplicada; ela vai para "Removidas", de onde pode ser
    restaurada.
  - **Sanitizar**, no topo das cartas, fica disponível quando a mesma carta aparece mais de uma vez (foil ou
    não; "↺ repete a #N" marca as que saem). Tira as repetidas de uma vez, deixando a primeira aparição de cada
    carta no vídeo; as tiradas também vão para "Removidas".
  - As edições ficam pendentes até **Reprocessar com as edições**, no fim da lista: preços, coleção e vídeo
    são refeitos. Cartas que não mudaram mantêm o preço da abertura, e uma carta que mudou de acabamento recebe
    o preço daquele acabamento no dia da abertura.

  <img src="docs/pagina-editar.jpg" width="560" alt="No celular: carta deslizada para a direita mostrando Editar, outra deslizada para a esquerda mostrando Remover, e a janela de edição com a chave Foil">
- **Passos** com status, mensagem e tempo de cada um. O passo em andamento mostra a barra de progresso.
- **Vídeo com overlay** para assistir ou baixar, e o **log** da execução. A **moeda do vídeo** (US$ ou R$) e o
  **logo no vídeo** (com/sem; "com" usa o logo padrão) podem ser trocados acima do player; o vídeo é refeito ao
  reprocessar. Com narração, **Com/Sem** escolhe a versão.
- **Narração**: **Narrar este vídeo** liga a narração numa pipeline já feita. O painel tem a **voz** (as 58 do
  XTTS-v2, agrupadas em graves, médias e agudas pelo tom; ▶ toca uma amostra) e o roteiro, e
  cada fala pode ser editada no texto e no instante (▶ leva o vídeo àquele momento), apagada ou criada (**+ Fala**).
  **Salvar e narrar de novo** grava de novo só as falas que mudaram; **Escrever outro roteiro** troca por um
  novo; **Desligar** apaga o vídeo narrado (as falas gravadas ficam guardadas).

  <img src="docs/pagina-narracao.jpg" width="360" alt="Painel da narração: vídeo com a escolha com ou sem narração e o roteiro com o instante e o texto de cada fala, editáveis">
- Ações: **Continuar** (depois de falha ou interrupção), **Rodar de novo daqui** (a partir de qualquer passo)
  e **Excluir** (remove a pipeline e as cartas que ela registrou na coleção).

### Coleção

![Coleção: grade de cartas com quantidade, selo foil, raridade e preço, e filtros por busca, set, raridade, acabamento, preço mínimo e tinta](docs/pagina-colecao.jpg)

Busca por nome, subtítulo ou número (`1/169`), várias ordenações e visualização em grade ou lista ficam sempre à
vista. O funil mostra os outros filtros (set, raridade, foil, preço mínimo e tinta) e, fechado, conta quantos
estão valendo. A tinta é escolhida pelo ícone de cada uma, desenhado a partir do símbolo oficial: escudo (Âmbar),
vórtice (Ametista), onda (Esmeralda), fogo (Rubi), olho (Safira) e fortaleza (Aço). Clicar numa carta abre a
imagem grande, os preços normal e foil, o link do TCGplayer e cada cópia: de qual pipeline veio (com link) e
quanto valia na abertura e hoje.

**Lacrados** (ao lado de **Cartas**, no topo da Coleção) guarda boosters, caixas, decks, baús e outros produtos
fechados. **+ Adicionar lacrado**: escolha o set e o produto (a lista vem do TCGplayer, via tcgcsv.com, já com o
preço de mercado), a quantidade e, se quiser, quanto pagou por unidade. Cada item mostra o valor de hoje
(preço × quantidade) e o resultado sobre o pago; a quantidade (só os fechados) muda no − / +. Um booster aberto
numa pipeline sai do estoque, mas continua registrado nela. Os preços dos lacrados são
atualizados junto com os das cartas (**↻ Atualizar preços**), e o Resumo tem um painel próprio para eles.

### Sets

![Aba Sets: cada set com a foto do booster, data de lançamento, cartas no catálogo e na coleção, se já é reconhecido em vídeo, e o botão Sincronizar](docs/pagina-sets.jpg)

A base de sets que o cardline conhece. Cada set mostra o ícone, a data de lançamento, quantas cartas tem no
catálogo e na sua coleção, e se já é **reconhecido em vídeo** (imagens baixadas e índice pronto). As cartas de
Lorcana não têm símbolo de expansão (o símbolo embaixo é a raridade), então o ícone de cada set é a **foto
oficial do booster** no TCGplayer, com o fundo branco removido. Sets sem booster avulso (promos, quests)
ficam com um selo hexagonal com o código do set.

- **Sincronizar** roda o `cardline sync` em segundo plano: sets novos, cartas, preços, imagens, índices e
  ícones. Saiu um set novo, é só clicar; a página mostra o andamento e se atualiza no fim.
- **Trocar ícone** põe uma imagem sua (o logo do set, por exemplo) no lugar da foto do booster. A
  sincronização não troca um ícone escolhido por você, e **Usar a foto do booster** desfaz a troca.

O ícone aparece na capa e no painel do vídeo que vai somando o booster, na lista e no detalhe das pipelines e no
detalhe de cada carta. A foto é guardada em resolução cheia (até 1000 px) para a capa, e a página usa uma
miniatura leve; ícones guardados pela versão anterior, menores, são baixados de novo na próxima sincronização.

### Redes: YouTube, Instagram e TikTok

O painel **Postar nas redes**, no detalhe de uma abertura, posta o vídeo e vincula cada post à pipeline. O
**Resumo** ganha o gráfico **visualizações e reações por abertura**: as visualizações num gráfico e as reações
(curtidas + comentários) no outro, uma barra por rede, com o resultado do booster embaixo de cada abertura.

- **Compartilhar o vídeo** funciona com qualquer rede e sem configurar nada. No celular, copia a legenda e abre
  o compartilhamento com o vídeo (escolha YouTube, Instagram ou TikTok); no computador, baixa o vídeo.
- **Já postou?** Cole o link do vídeo no YouTube, do Reel ou do TikTok: o cardline reconhece a rede e vincula.
  Com o YouTube conectado, a lista dos últimos vídeos do canal já aparece para vincular com um toque.
- **Números:** YouTube e Instagram conectados são lidos pela API ao abrir o Resumo (quando algum vídeo está com a
  leitura de mais de 30 min) e em **↻ Atualizar números**, no gráfico de redes do Resumo, que lê todos os vídeos
  vinculados de uma vez, em segundo plano, e mostra quando foi a leitura. **Atualizar números** no painel de uma pipeline lê só os
  vídeos dela. No TikTok, sem API, use **Informar números**. Cada leitura fica guardada.
- **Programar:** preencha **Programar a publicação** e os botões viram **Programar no YouTube / no Instagram**.
  No YouTube, o vídeo sobe na hora, fica privado e o próprio YouTube publica na data, mesmo com o PC desligado
  (sem a auditoria do Google, porém, o vídeo fica travado como privado e a data não vale). O Instagram não programa
  pela API: o cardline guarda o pedido e publica na hora marcada, então o PC precisa estar ligado e o túnel
  aberto. Se não der (túnel fechado, Instagram desconectado), tenta de novo a cada 30 s e, 1 h depois da hora,
  desiste e avisa na página. No TikTok, use o agendamento do próprio app. Na lista de pipelines, a rede
  programada aparece com o ícone apagado.

**YouTube (postar pela API e ler os números):**

1. No [Google Cloud](https://console.cloud.google.com), crie um projeto e ative a **YouTube Data API v3**.
2. Configure a **tela de consentimento OAuth** (tipo externo). Em **Público-alvo**, adicione a conta do canal
   como **usuário de teste**: sem isso, o Google bloqueia a conexão com "Erro 403: access_denied" (o app está em
   teste). Com o app em "Teste", a autorização vence a cada 7 dias; publicar o app (mesmo sem verificação)
   evita as duas coisas, com um aviso de "app não verificado" na hora de autorizar.
3. Em **Credenciais**, crie um **ID do cliente OAuth** do tipo **"TVs e dispositivos de entrada limitada"** e cole
   o ID e a chave na página (ficam em `data/youtube/`, fora do git).
4. **Conectar o canal do YouTube**: abra google.com/device (no celular ou no computador) e digite o código.

O vídeo vai com as **tags** de `youtube_tags` (lorcana, disney lorcana, booster, abertura de booster, tcg, br,
brasil) mais o nome do set; dá para editar no painel antes de postar.

Atenção: o YouTube trava como **privado** todo vídeo enviado por um projeto de API que não passou pela
[auditoria do Google](https://support.google.com/youtube/answer/7300965), e não dá para mudar depois. Até lá,
poste pelo app e vincule o link.

**Instagram (Reels pela API e os números):**

1. A conta do Instagram precisa ser **profissional** (Criador de conteúdo ou Comercial).
2. Em [Meta for Developers](https://developers.facebook.com/apps), crie um app com o caso de uso **"Gerenciar
   mensagens e conteúdo no Instagram"**. Em Funções do app → **Testadores do Instagram**, adicione a conta e aceite
   o convite no Instagram (Configurações → Apps e sites).
3. Em **Configuração da API com login do Instagram**, gere o token de acesso da conta com as permissões
   `instagram_business_basic`, `instagram_business_content_publish` e `instagram_business_manage_insights`, e cole
   na página (**Conectar o Instagram**). O token vale 60 dias e o cardline renova antes de vencer.

O gráfico **Visualizações por data**, no Resumo, soma as visualizações dos vídeos vinculados por rede em cada
dia em que os números foram lidos (a última leitura do dia de cada vídeo; o TikTok entra pelos números informados).

Publicar na própria conta não precisa de revisão da Meta, e o Reel sai público. O Instagram baixa o vídeo de um
endereço público: o cardline oferece o arquivo pelo túnel (ngrok) durante a publicação, então abra a página pelo
túnel ou configure `public_url`.

**TikTok:** a API do TikTok só publica em modo privado até o app passar pela auditoria deles (2 a 6 semanas), e
até para mandar aos rascunhos ou ler os números o app precisa ser aprovado. Por isso o TikTok vai pelo
compartilhamento, e os números são informados à mão.

Quem acessa a página pode postar nas suas redes: com alguma rede conectada, use senha no túnel.

### No celular e acesso remoto

<img src="docs/pagina-celular.jpg" width="400" alt="Página no celular: aba Resumo e detalhe de uma pipeline">

A página funciona no celular. Por padrão o servidor só aceita conexões da própria máquina. Para acessar de
outro aparelho:

- na rede local: `uv run cardline serve --host 0.0.0.0` e abra `http://IP-da-máquina:8000`;
- de qualquer lugar: um túnel, por exemplo `ngrok http 8000 --basic-auth "usuario:uma-senha-forte"`.

A página não tem login: quem tiver o endereço consegue enviar vídeos, rodar e excluir pipelines. Use senha
no túnel, ou só redes de confiança.

### API

| Método | Rota | Para quê |
|---|---|---|
| GET | `/api/meta` | moedas e cotação, passos, sets, raridades e se o Ollama está disponível |
| GET | `/api/collection` | cartas da coleção, agrupadas por carta e acabamento, com as cópias |
| GET | `/api/runs` | pipelines com status, progresso e valores; `duplicates` lista as pipelines com as mesmas cartas e `repeated` conta as cartas repetidas |
| GET | `/api/runs/{id}` | detalhe: passos, cartas e log |
| POST | `/api/runs?filename=…&kind=abertura&paid=…&paid_currency=BRL&narration=true&logo=padrao.png` | cria a pipeline (`kind`: `abertura` ou `cadastro`; `logo` vazio = sem logo); o corpo da requisição é o vídeo |
| POST | `/api/logos` | o corpo é a imagem: guarda o logo (PNG, até 600 px) e devolve o nome para `logo=` na criação |
| POST | `/api/runs/{id}/rerun` | `{"from_step": "prices"}`, ou `null` para continuar de onde parou |
| PATCH | `/api/runs/{id}` | `{"paid": 34.9, "paid_currency": "BRL"}`, `{"currency": "BRL"}` (moeda do vídeo), `{"narration": true}` e/ou `{"logo": "padrao.png"}` (`null` tira); só o que for enviado muda |
| POST | `/api/prices/refresh` | atualiza os preços de hoje dos sets da coleção (o preço na abertura não muda) |
| GET / POST | `/api/sealed` | lacrados com valor de hoje e pago; `{"set_code": "1", "product_id": 482406, "qty": 2, "paid": 35, "paid_currency": "BRL"}` adiciona |
| PATCH / DELETE | `/api/sealed/{id}` | `{"qty": 3}` e/ou `{"paid": 30}` (`null` apaga o pago); DELETE remove |
| GET | `/api/sealed/boosters` | os boosters fechados, para escolher numa abertura (`sealed_id` e `sealed_qty` na criação) |
| GET | `/api/sealed/products?set=1` | os lacrados do set no TCGplayer, com o preço de mercado de hoje |
| GET | `/api/history` | séries por dia do Resumo: `value` (valor da coleção) e `views` (visualizações por rede) |
| GET | `/api/jobs`, `/api/jobs/{id}` | as atualizações disparadas na página (preços, números das redes, sincronização), com o resultado e o log |
| GET | `/api/tasks` | tarefas de fundo do Resumo (`prices`, `social`): rodando, progresso e a mensagem do fim |
| POST | `/api/tasks/prices`, `/api/tasks/social?max_age=1800` | atualiza os preços / os números das redes em segundo plano (uma de cada vez) |
| PATCH | `/api/runs/{id}/cards/{uid}` | `{"foil": true}`: edita a carta (por enquanto, só o acabamento); fica pendente até reprocessar |
| DELETE | `/api/runs/{id}/cards/{uid}` | tira uma carta da identificação (fica pendente até reprocessar) |
| POST | `/api/runs/{id}/sanitize` | tira as cartas repetidas, deixando a primeira aparição de cada uma (pendente até reprocessar) |
| POST | `/api/runs/{id}/cards/{uid}/restore` | devolve uma carta removida |
| DELETE | `/api/runs/{id}` | exclui a pipeline e as cartas dela |
| PUT | `/api/runs/{id}/narration` | `{"lines": [{"t": 6.2, "texto": "Hakuna matata... sei."}]}`: salva o roteiro editado (vale na próxima narração) |
| POST | `/api/runs/{id}/narration/new` | descarta o roteiro: a próxima narração escreve outro |
| GET | `/api/youtube` | cliente configurado, canal conectado e a espera do código de conexão |
| POST | `/api/youtube/client` | `{"client_id": "…", "client_secret": "…"}`: salva o cliente OAuth |
| POST | `/api/youtube/connect` | pede o código para google.com/device; `/api/youtube/disconnect` revoga e esquece o canal |
| GET | `/api/youtube/recent` | últimos vídeos do canal (para vincular o que foi postado pelo app) |
| GET / POST | `/api/instagram`, `/api/instagram/token` | situação do Instagram; `{"token": "IG…"}` conecta (`/api/instagram/disconnect` esquece) |
| POST | `/api/social/stats?max_age=1800` | atualiza os números dos posts no YouTube e no Instagram |
| POST | `/api/runs/{id}/posts/{rede}` | `{"title", "caption", "privacy", "variant", "tags"}`: posta pela API (`youtube` ou `instagram`, em segundo plano); com `"publish_at": "2026-10-08T21:30:00Z"`, programa |
| DELETE | `/api/runs/{id}/scheduled/{rede}` | cancela a publicação que o cardline faria na hora marcada (ou descarta a que falhou) |
| PUT | `/api/runs/{id}/posts` | `{"url": "…"}`: vincula um post já feito (YouTube, Instagram ou TikTok, reconhecido pelo link) |
| PATCH / DELETE | `/api/runs/{id}/posts/{rede}` | `{"views": 1500, "likes": 120, …}` informa os números à mão; DELETE desvincula |
| GET | `/api/sets` | sets com ícone, cartas no catálogo e na coleção, e se já são reconhecidos em vídeo |
| POST | `/api/sets/sync` | inicia o `cardline sync` em segundo plano (um por vez); `GET` na mesma rota mostra o andamento e o log |
| POST | `/api/sets/{code}/icon` | troca o ícone do set; o corpo da requisição é a imagem |
| DELETE | `/api/sets/{code}/icon` | volta ao ícone automático (a foto do booster) |

## Pipeline

### Passos

| Passo | Faz | Artefato em `runs/<id>/` |
|---|---|---|
| Identificar cartas | casa os frames com as imagens oficiais e acha quando cada carta aparece | `scan.json`, `crops/` |
| Conferir com IA local | (opcional) um modelo de visão do Ollama lê nome e número de cada recorte | `scan.json` |
| Atualizar preços | busca os preços de hoje do set e fixa o preço da abertura de cada carta (foil ou não); ao reprocessar, o preço da abertura é mantido, e uma carta nova recebe o preço do dia da abertura | `scan.json` |
| Registrar na coleção | substitui as cartas que esta pipeline tinha registrado | banco |
| Gerar vídeo com overlay | vídeo 1080×1920: capa (o primeiro frame parado, com o booster e o valor pago), etiquetas com um "ka-ching" de caixa registradora a cada carta, aplausos quando a soma alcança o valor pago, painel do booster (ícone do set e total animado) e resumo (com valor pago e resultado) | `overlay.mp4`, `overlay.jpg` (a capa), `overlay.json` (tempos) |
| Narrar o vídeo | (opcional) escreve o roteiro, grava cada fala com a voz e mistura com o som do vídeo | `narrado.mp4`, `narracao/` |

Uma abertura passa pelos cinco passos; um **cadastro** para em "Registrar na coleção" (não tem vídeo).

Cada passo grava seu estado. Se algo falhar ou o servidor cair, a pipeline pode **continuar de onde parou**,
e qualquer pipeline pode **rodar de novo a partir de um passo** (na página ou com `cardline run ID --from passo`).

### Correções

Na página, a lixeira de cada carta resolve cartas erradas ou duplicadas. Pelo terminal dá para fazer
também, e mais (trocar a carta, marcar foil, inserir uma que faltou):

```bash
uv run cardline edit 4 3 --card 1/24          # na pipeline 4, a carta #3 é a 1/24 (também aceita nome)
uv run cardline edit 4 12 --foil              # a #12 é foil (--no-foil desfaz)
uv run cardline edit 4 5 --remove             # a #5 não é uma carta aberta
uv run cardline edit 4 --card 1/55 --at 9.2   # faltou uma carta aos 9,2 s
```

A correção muda só o resultado da identificação. Preço, coleção e vídeo ficam marcados como
desatualizados até você clicar **Reprocessar com as edições** na página (ou rodar `cardline run 4`). Aí só esses passos
rodam de novo, e as cartas registradas na coleção mudam junto.

Cartas obtidas fora de vídeo: `uv run cardline add 1/169 --foil --qty 2`.

## Configuração

`cardline.toml` (todas as chaves são opcionais e estão documentadas no arquivo): moeda padrão do vídeo
(`USD` ou `BRL`), cartas por booster, fps da análise, resolução de saída, duração do resumo, e o modelo do
Ollama para conferência. Com `verify_model` preenchido, a opção já vem marcada no upload. Para a narração,
`narration_voice` (a voz do XTTS-v2) e `narration_writer` (o modelo do Ollama que escreve as piadas).
`intro_seconds` é a duração da capa no começo do vídeo (3 s; 0 tira a capa). `logo_corner` e `logo_opacity` são o canto do logo
(`top-right`, padrão; `top-left`, `bottom-right` ou `bottom-left`) e a opacidade (0,6). `card_sound_volume` e
`celebration_volume` são os volumes do "ka-ching" de cada carta e dos aplausos (0,25 e 0,3), relativos à voz do
narrador: 1 é tão alto quanto ela, 0,25 fica ~12 dB abaixo e 0 tira o efeito. `youtube_tags` são as tags dos
vídeos postados no YouTube (o nome do set entra junto).

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
  Pillow desenha e o x264 codifica mantendo o áudio original. A capa congela o primeiro frame, escurecido e
  desfocado. O booster entra com um pop e um reflexo passando pelo pacote, e o painel com o set e o valor pago
  sobe logo depois. Na saída, o booster voa até o lugar do ícone no painel do topo, o frame volta ao normal e o
  vídeo começa sem corte. O som original entra atrasado pelo tempo da capa, e a capa vira a imagem do vídeo
  na página, sem spoiler. Os efeitos (`audio.py`) são de fundo: o "ka-ching" bate quando a etiqueta de cada
  carta aparece, e os aplausos entram no instante em que a soma das cartas alcança o valor pago (no meio da
  contagem animada do total; booster que não se paga não tem aplausos). Os arquivos estão no nível da fala do
  narrador (-20 LUFS, medido com EBU R128) e o volume é relativo a ela; na narração, o som original abaixa 14 dB
  durante a fala e os efeitos, 4 dB. Sons: um trecho de
  [Cash register.ogg](https://commons.wikimedia.org/wiki/File:Cash_register.ogg) (domínio público) e um de
  [277021 sandermotions applause-2.wav](https://commons.wikimedia.org/wiki/File:277021_sandermotions_applause-2.wav) (CC0).
- **Ícones dos sets** (`catalog.py`): o [tcgcsv](https://tcgcsv.com) espelha o catálogo do TCGplayer, e o
  grupo de cada set de Lorcana lá tem a mesma sigla do código do set no Lorcast. Do grupo sai o produto
  "Booster Pack" avulso (não o sleeved nem a caixa). Na foto em 1000×1000 só sai o branco ligado à borda (o
  branco da arte fica), e o resultado é recortado no booster.
- **Narração** (`narration.py`): o roteiro tem estrutura fixa, que garante o tempo e que o resultado só
  apareça no fim. A aposta (valor pago) abre, as raras ganham reação, a falsa esperança vem no meio e o
  desfecho ("Eu avisei.") só depois do resumo. As piadas das cartas vêm de um banco de falas aprovadas ou do
  modelo do Ollama, com exemplos; piada com número, spoiler ou o inglês da carta é descartada. A voz é o XTTS-v2
  na CPU (~1,7 s por segundo de fala). Como ele às vezes troca palavras, o Whisper transcreve cada tomada, e só
  vale a que diz o texto certo, comparando pela pronúncia ("atiçar" = "atissar"). Até 3 tomadas; piada que a voz
  não acerta sai. O som original abaixa enquanto o narrador fala, e o áudio sai em -16 LUFS.
- **Conferência com IA local** (`verify.py`): no vídeo de exemplo o `qwen3.5:4b` acertou 12/12 nomes e
  números (~1 s por carta). Ele só **sinaliza** divergências, nunca troca a carta, porque pode alucinar.

## Dados

- `data/cardline.db`: catálogo, preços, pipelines e **a sua coleção**. Fica fora do git (o repositório é
  público e o banco muda a cada atualização de preço), então faça backup desse arquivo.
- `runs/<id>/`: vídeo enviado, recortes, `scan.json`, `overlay.mp4` (+ capa `overlay.jpg` e tempos `overlay.json`),
  `narrado.mp4` (+ o roteiro e as falas gravadas em `narracao/`) e `pipeline.log` de cada pipeline.
- `data/youtube/` e `data/instagram/`: cliente OAuth e tokens das redes (só o seu usuário lê; fora do git).
- `data/logos/`: o logo padrão do vídeo (`padrao.png`) e os enviados na criação das pipelines (fora do git).
- `data/cache/`: imagens, índices e ícones dos sets (`sets/`), regeneráveis com `cardline sync`. Só um ícone
  que você enviou não volta: sem o arquivo, o set volta para a foto do booster.

## Testes

```bash
uv run pytest
```
