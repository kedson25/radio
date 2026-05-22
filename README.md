# Radio SSP21

Radio web em Python/Flask para tocar pedidos vindos da API, baixar audio com `yt-dlp` e transmitir pelo navegador.

## Instalar na VPS Ubuntu

Envie esta pasta para a VPS e execute:

```bash
sudo bash instalar.sh
```

O instalador cria o usuario `radio`, instala dependencias, copia o projeto para `/opt/radio`, configura o `systemd` e inicia o servico.

## Arquivos sensiveis

Coloque o arquivo `cookies.txt` na pasta do projeto antes de rodar o instalador, se precisar usar cookies do YouTube. Esse arquivo nao deve ser enviado ao GitHub.

## Comandos uteis

```bash
sudo systemctl status radio.service
sudo journalctl -u radio.service -f
sudo systemctl restart radio.service
```

A interface fica disponivel na porta `8000`.
