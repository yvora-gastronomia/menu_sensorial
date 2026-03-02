# Cardápio Sensorial | YVORA (Streamlit)

Este app publica o **cardápio** para qualquer pessoa e restringe o **envio de avaliações** apenas para quem estiver conectado ao **Wi-Fi do restaurante**, usando o IP público do roteador.

## 1) Estrutura do repositório

- `app.py`
- `requirements.txt`
- `.streamlit/config.toml`
- `.streamlit/secrets.example.toml` (modelo para copiar no Streamlit Cloud)
- `.gitignore`

## 2) Deploy no Streamlit Community Cloud

1. Suba este repositório no GitHub.
2. No Streamlit Cloud, clique em **New app**, conecte o repositório e selecione `app.py`.
3. Em **Settings -> Secrets**, cole o conteúdo de `.streamlit/secrets.example.toml` e preencha com seus valores.

## 3) Como descobrir o IP público do Wi-Fi do restaurante

Conecte um celular no Wi-Fi do restaurante e pesquise por “my ip”.  
Pegue o **IP público** e coloque em:

`RESTAURANT_PUBLIC_IPS = "SEU_IP"`

Se o provedor mudar seu IP com frequência, você pode:
- Solicitar IP fixo ao provedor (recomendado), ou
- Atualizar o Secret quando necessário.

## 4) Melhorias de integridade incluídas

- Restrição de envio por Wi-Fi (IP)
- Rate limit: mínimo de 20s entre envios por sessão
- Bloqueio de duplicidade: mesmo telefone não avalia o mesmo prato mais de 1 vez no mesmo dia
- Log técnico no Google Sheets: `client_ip` e `user_agent` (ajuda a auditar)

## 5) Observação

No Streamlit Community Cloud, a identificação do IP depende de headers da plataforma.  
Na prática isso funciona bem para impedir avaliações remotas casuais.  
Se você quiser nível “definitivo” (bloqueio na borda), a alternativa é usar VPS + Cloudflare/Nginx.
