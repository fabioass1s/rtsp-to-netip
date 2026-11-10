# Conversor RTSP/ONVIF → NetIP

Faz câmeras **RTSP** ou **ONVIF** aparecerem como dispositivos **NetIP** (protocolo Xiongmai,
também chamado DVRIP/Sofia, porta 34567). Assim, NVRs, DVRs e softwares (CMS/VMS) que só falam
NetIP conseguem adicionar e gravar qualquer câmera IP.

```
 câmera RTSP/ONVIF ──RTSP──▶ [ netip-bridge (Docker) ] ──NetIP :34567──▶ NVR / CMS
                    ◀─ONVIF PTZ──                       ◀─comandos PTZ──
```

## Recursos

- Vídeo **H.264 e H.265** sem recodificar (cópia direta, quase sem uso de CPU).
- **Stream principal e substream** (Main / Extra1).
- **ONVIF**: descobre sozinho as URLs RTSP e repassa o **PTZ** da NVR (direções, zoom, presets).
- **Áudio** opcional, convertido para G.711 A-law 8 kHz (o formato que o NetIP usa).
- **Várias câmeras**: cada uma vira um dispositivo NetIP numa porta própria, ou várias
  câmeras viram canais de um mesmo dispositivo, no estilo DVR.
- **Busca automática**: responde à busca de dispositivos das NVRs (UDP 34569).
- Snapshot JPEG (comando de captura do CMS).
- Uma única conexão RTSP com a câmera é compartilhada por todos os clientes. Ela abre sob
  demanda e fecha quando ninguém está assistindo.
- Reconexão automática quando a câmera cai.

## Instalação

1. Crie a configuração:

   ```bash
   cp config/config.example.yaml config/config.yaml
   # edite config/config.yaml com suas câmeras
   ```

2. Suba o container:

   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```

3. Na NVR/CMS, adicione um dispositivo **NetIP** com estes dados:
   - **IP**: o IP da máquina que roda o Docker
   - **Porta**: 34567 para a 1ª câmera, 34568 para a 2ª, e assim por diante
   - **Usuário/senha**: os definidos em `netip.username` e `netip.password`

### Rede

| Cenário | Como configurar |
|---|---|
| **Servidor Linux** (recomendado) | No `docker-compose.yml`, use `network_mode: host` e remova `ports`. Isso habilita a busca automática da NVR e o `discover` ONVIF. |
| **Windows/Mac (Docker Desktop)** | Deixe o mapeamento `ports`. A busca automática por broadcast não atravessa o NAT do Docker Desktop, então adicione as câmeras na NVR digitando IP e porta. Defina `netip.advertise_ip` com o IP do PC. |

Abra no firewall as portas TCP 34567 em diante (uma por câmera) e a UDP 34569.

### Sem Docker (Windows)

Precisa do Python 3.12 ou mais novo. Dê dois cliques em `iniciar-windows.bat`. Na primeira vez
ele cria o ambiente `.venv` e instala as dependências. Depois inicia o conversor com
`config\config.yaml`.

Para a NVR conseguir conectar, libere as portas no firewall do Windows. Rode este comando
**uma vez**, num PowerShell aberto como administrador:

```powershell
New-NetFirewallRule -DisplayName "NetIP Bridge TCP" -Direction Inbound -Protocol TCP -LocalPort 34567-34576 -Action Allow -Profile Private
New-NetFirewallRule -DisplayName "NetIP Bridge UDP" -Direction Inbound -Protocol UDP -LocalPort 34569 -Action Allow -Profile Private
```

## Configuração

Veja [config/config.example.yaml](config/config.example.yaml). Um resumo:

```yaml
netip:
  username: admin
  password: ""

cameras:
  - name: Entrada                 # via ONVIF (URLs RTSP e PTZ automáticos)
    onvif: { host: 192.168.1.50, username: admin, password: "senha" }

  - name: Garagem                 # via RTSP direto
    rtsp:
      main: rtsp://admin:senha@192.168.1.51:554/stream1
      sub:  rtsp://admin:senha@192.168.1.51:554/stream2
```

| Opção (câmera) | Padrão | Descrição |
|---|---|---|
| `rtsp.main` / `rtsp.sub` | — | URLs RTSP. Se informadas junto com `onvif`, têm prioridade, e o ONVIF passa a ser usado só para PTZ. |
| `onvif.host/port/username/password` | porta 80 | Acesso ONVIF da câmera. |
| `onvif.main_profile` / `sub_profile` | 0 / 1 | Índice ou token do perfil ONVIF de cada stream. |
| `port` | automática | Porta NetIP. Câmeras com a mesma porta viram canais de um só dispositivo. |
| `audio` | `false` | Envia o áudio (convertido para G.711A). |
| `fps` | detectado | Força o FPS informado à NVR. |
| `rtsp_transport` | `tcp` | `tcp`, `udp`, `http` ou `auto`. |
| `ptz` | `true` | Repassa comandos PTZ via ONVIF. |
| `always_on` | `false` | Mantém o RTSP principal sempre conectado. |

Senhas com caracteres especiais dentro da URL RTSP devem ir codificadas (`@` → `%40`, `#` → `%23`).
Isso não vale para o bloco `onvif`, que é codificado automaticamente.

### Descobrir câmeras ONVIF da rede

```bash
docker run --rm --network host netip-bridge discover --username admin --password senha
```

O comando lista as câmeras, os perfis e as URLs RTSP de cada uma. Precisa de `--network host`, então só funciona num host Linux.

## Testar

O container traz um cliente NetIP de teste. Ele também serve para testar câmeras Xiongmai reais:

```bash
# login, informações e 5 s de vídeo salvos em arquivo
docker compose exec netip-bridge python -m netip_bridge client \
    --host 127.0.0.1 --port 34567 --user admin --password "" --seconds 5 --out /tmp/teste.h264

# substream, usando conexão de mídia separada (como as NVRs fazem)
docker compose exec netip-bridge python -m netip_bridge client --stream sub --separate

# snapshot e PTZ
docker compose exec netip-bridge python -m netip_bridge client --seconds 0 --snapshot /tmp/foto.jpg --ptz DirectionLeft
```

Os testes unitários rodam com:

```bash
docker run --rm --entrypoint python netip-bridge -m unittest discover -s tests -t .
```

### Teste ponta a ponta (sem câmera real)

[tests/e2e](tests/e2e) sobe uma câmera falsa (MediaMTX com padrões de teste H.264+AAC e H.265),
o conversor e o [go2rtc](https://github.com/AlexxIT/go2rtc) como cliente NetIP independente:

```bash
cd tests/e2e
docker compose -f docker-compose.e2e.yml up -d --build

# cliente embutido: H.264 com áudio, substream e H.265
docker compose -f docker-compose.e2e.yml exec bridge python -m netip_bridge client --host 127.0.0.1 --port 34567 --password teste123
docker compose -f docker-compose.e2e.yml exec bridge python -m netip_bridge client --host 127.0.0.1 --port 34567 --password teste123 --stream sub --separate
docker compose -f docker-compose.e2e.yml exec bridge python -m netip_bridge client --host 127.0.0.1 --port 34568 --password teste123

# go2rtc lendo o conversor via DVRIP e reexpondo em RTSP
docker compose -f docker-compose.e2e.yml exec camera ffprobe -v error -rtsp_transport tcp \
    -show_entries stream=codec_name,width,height -of compact rtsp://go2rtc:8554/netip_main

docker compose -f docker-compose.e2e.yml down
```

## Como funciona

- **Vídeo**: o PyAV (FFmpeg) puxa o RTSP. Cada quadro H.264/H.265 é reempacotado no formato de
  mídia Xiongmai: quadro I `00 00 01 FC` com codec, FPS, resolução e data/hora; quadro P
  `00 00 01 FD`; áudio `00 00 01 FA`. Os quadros são enviados em mensagens NetIP 1412.
  SPS/PPS/VPS são reinseridos em todo quadro-chave. Clientes novos recebem o GOP atual, então
  a imagem aparece na hora.
- **Protocolo**: login (1000, hash "Sofia" MD5), keepalive (1006), SystemInfo (1020),
  configurações (1042: `Simplify.Encode`, `General.General`, `NetWork.NetCommon`,
  `AVEnc.VideoWidget` e outras), capacidades (1360: `SystemFunction`, `EncodeCapability`),
  títulos dos canais (1048), monitor ao vivo (1410/1413, Claim e Start na mesma conexão ou em
  conexão separada), PTZ (1400), hora (1452), snapshot (1560) e busca UDP (1530).
- **PTZ**: o NetIP manda `Preset: 65535` ao apertar e `Preset: -1` ao soltar. Isso vira
  `ContinuousMove` e `Stop` no ONVIF. `GotoPreset`, `SetPreset` e `ClearPreset` viram as
  operações de preset do ONVIF.

## Problemas comuns

- **Imagem com blocos coloridos ou tela preta no DVR**: confira no DVR a tela *status do canal*,
  que mostra a "resolução máxima" aceita por canal (DVRs antigos costumam ir até 1080P). Se a
  câmera manda mais que isso (ex.: 2304x1296), reduza o stream principal dela para 1080p, pelo
  app da câmera ou via ONVIF.
- **DVR conecta e cai a cada poucos segundos**: rode com `LOG_LEVEL=DEBUG` e veja quais
  mensagens ficaram sem resposta. Com `DEBUG`, o log também mostra a cada 10 s quantos quadros
  cada cliente recebeu.

## Limitações

- Somente vídeo ao vivo. Não há reprodução de gravações nem busca de arquivos: a gravação fica
  na NVR.
- Câmeras MJPEG não são suportadas, porque o NetIP só aceita H.264/H.265.
- Intercom (falar pela NVR) não é suportado.
- O acesso pela nuvem XMEye (P2P/serial) não existe. O acesso é direto por IP e porta.
- Configurações enviadas pela NVR (mudar resolução, OSD etc.) são aceitas e ignoradas.
- Comandos ou configurações que o conversor não conhece aparecem no log (nível INFO). Se alguma
  NVR específica não aceitar o dispositivo, rode com `LOG_LEVEL=DEBUG` e veja o que ela pede.

## Licença

MIT. Veja [LICENSE](LICENSE).
