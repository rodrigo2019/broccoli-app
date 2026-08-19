# Broccoli Desktop — integração com Listening local

## Objetivo

Conectar o Broccoli Desktop ao contrato Listening que já existe no backend
local, validar a captura e a transcrição de áudio do sistema com uma conta
administrativa local, e deixar explícitas no aplicativo as capacidades que o
contrato ainda não fornece.

O escopo de implementação é exclusivamente `C:\repos\broccoli-app`. O
backend em `C:\repos\broccoli` é a fonte de verdade do contrato e só é
iniciado, observado e usado para a aceitação manual. Uma mudança no backend só
é autorizada se uma rota ou comportamento já declarado por ele falhar com
evidência reproduzível; não se criam rotas novas para acomodar o desktop.

## Contrato confirmado

O desktop deve usar estes elementos do backend atual:

- `GET /api/auth/me/` com `Authorization: Token <key>` valida o token.
- `WS /ws/listening/` recebe o mesmo cabeçalho e as query strings opcionais
  `resume`, `device` e `language`.
- O handshake abre ou retoma a sessão. O cliente não envia `session.start`.
- O primeiro evento é `session.started`, com `uuid_code`, `resumed`,
  `next_seq` por canal e `max_duration_s`; o backend não envia
  `next_offset_ms`.
- O backend emite `transcript.segment` final por canal, sem `utterance_id`, e
  pode emitir `credit.warning`, `session.ended` e `error`.
- O frame PCM binário atual do desktop já é idêntico ao codec do backend:
  versão 1, canal, offset em milissegundos e PCM16.
- `session.end` encerra uma sessão. Fechamentos 4401, 4402 e 4403 representam,
  respectivamente, credencial inválida, crédito insuficiente e duração máxima.

O backend não oferece, autenticado por token, lista de sessões, leitura de
segmentos, título remoto ou histórico reutilizável. As rotas HTTP de
Listening existentes são intencionalmente autenticadas por cookie e não são
um contrato do cliente desktop.

## Design do aplicativo

### Adaptador remoto e sessão

`HttpListeningRemote` deixa de presumir a API de biblioteca. A validação de
credencial passa a chamar `/api/auth/me/`; a conexão monta a URL WebSocket
confirmada, incluindo `resume`, o rótulo do dispositivo e o idioma `en`.

O adaptador não envia controle ao abrir o socket. Ele mantém o próximo
sequencial por canal recebido em `session.started` e deriva um
`utterance_id` local estável como `<canal>:<sequência>` para cada segmento
final. Para uma sessão nova, a pipeline de áudio começa no offset zero. Em
uma reconexão, ela preserva o relógio local e reenvia apenas o buffer de
recuperação já limitado; nunca o reinicializa a partir de dados que o backend
não fornece.

A sessão ativa recebe um `SessionSummary` local: UUID e estado vêm do evento
do backend; título é um rótulo local da captura atual, inicialmente vazio;
contagem de segmentos é incrementada ao receber segmentos. Nenhum título ou
transcrição é persistido pelo desktop. Falha de autenticação no handshake ou
durante a leitura remove a credencial do Windows Credential Manager e retorna
à tela de login.

### Capacidades da interface local

O bootstrap local publica `capabilities`:

```json
{
  "history": false,
  "remote_title": false,
  "user_resume": false,
  "segment_history": false
}
```

Com essas capacidades, o Caderno de reunião mostra captura, seleção de
dispositivos, indicador de transmissão, código da sessão ativa, parar e a
timeline ao vivo. Biblioteca, busca, carregar mais, retomar, edição de título
e carregamento de segmentos anteriores aparecem desabilitados com a explicação
"Histórico não disponível neste backend". Uma nova captura sempre começa uma
sessão nova. A recuperação automática do mesmo socket continua usando
`resume`; ela não é a funcionalidade de retomar histórico da UI.

As rotas locais que representavam capacidades ausentes são removidas do uso da
UI e deixam de acionar o remoto. O servidor local ainda expõe login, bootstrap,
dispositivos, iniciar, parar e eventos. Respostas de capacidade indisponível
usam `409` com mensagem segura se chamadas antigas chegarem à API.

### Modo browser-only e aceitação real

O runtime ganha modo real browser-only que inicia o mesmo FastAPI, serviços
reais, Credential Manager, captura WASAPI e adaptador, mas não abre PyWebView
nem tray. Ele aceita uma porta local explícita para o `agent-browser` e só
escuta em `127.0.0.1`.

A aceitação manual usa um único navegador agent-browser headed. No backend
local, entra-se em `/admin/` com a conta local `admin`, abre-se `/profile/`,
cria-se um token com nome temporal e validade de 30 dias e copia-se o valor
por gesto da UI. O token é colado diretamente no desktop sem ser lido,
registrado, fotografado ou incluído em HAR. O teste seleciona um microfone e
um loopback do sistema, reproduz no Chrome um discurso público em inglês no
YouTube, inicia captura e espera um segmento não vazio. O teste não mede
fidelidade nem tradução; comprova a cadeia operacional captura → desktop →
WebSocket → transcrição → timeline.

Ao encerrar, sessão, segmentos, token local e a credencial do Windows são
mantidos deliberadamente. O relatório só registra estado, datas, UUID da
sessão e número de segmentos; nunca senha, token, texto transcrito, frames
PCM, áudio ou HAR.

## Falhas e segurança

- Ausência de Azure, crédito, usuário/tenant local, dispositivo WASAPI ou
  reprodução de mídia é pré-requisito não atendido: o relatório para e não
  pede uma alteração de rota.
- Um 404/501 de uma capacidade não declarada pelo backend é tratado como
  indisponibilidade da capacidade no app, não como bug de backend.
- Uma rota declarada que falhar sob este contrato gera relatório com URL,
  método, status sanitizado e passos de reprodução antes de qualquer proposta
  de mudança no backend.
- A automação usa `--session` isolada, `--headed`, allowlist de localhost e
  domínios indispensáveis do YouTube, sem profile, restore, HAR ou estado
  persistido do navegador.

## Critérios de aceite

1. Token válido autentica o desktop por `/api/auth/me/`; token inválido limpa
   a credencial e mostra login.
2. Uma sessão nova recebe `session.started`, abre a captura e envia frames que
   o backend decodifica.
3. Um `transcript.segment` real gera uma linha de timeline com timestamp e
   canal; a contagem local é atualizada.
4. Reconectar preserva offsets locais e recupera a sessão ativa sem vazar
   áudio de outra sessão.
5. A UI não tenta biblioteca, título ou histórico contra endpoints que não
   fazem parte do contrato do token.
6. A execução local com agent-browser completa o login de plataforma, a
   criação segura de token, o login do app, a captura de áudio do sistema e a
   comprovação de ao menos um segmento não vazio.
