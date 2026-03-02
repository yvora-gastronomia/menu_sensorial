import os
import re
import io
import csv
import hashlib
import urllib.request
import ipaddress
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

import streamlit as st

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials


# ===============================
# CONFIGURAÇÕES
# ===============================
APP_TITLE = "Cardápio Sensorial | YVORA"

ASSET_DIR = "asset"
LOGO_PATH = os.path.join(ASSET_DIR, "yvora_logo.png")
DISH_IMG_DIR = os.path.join(ASSET_DIR, "dishes")

COLOR_NAVY = "#0E2A47"
COLOR_CREAM = "#EFE7DD"
COLOR_GOLD = "#C6A96A"
COLOR_SILVER = "#C0C0C0"
COLOR_BRONZE = "#CD7F32"
COLOR_INK = "#0B2238"

DEFAULT_SHEETS_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1sM5MydAxcn5t0SeeU-cpRR9z3iFPeCYSBCgZ8GYArFk"
    "/gviz/tq?tqx=out:csv&sheet=menu.csv"
)

# Nomes das abas no Google Sheet (pode trocar via secrets)
DEFAULT_WS_EVALS = "evaluations"
DEFAULT_WS_INTERACTIONS = "interactions"
DEFAULT_WS_SETTINGS = "settings"

# Controle básico (anti-flood / anti-duplicidade)
RATE_LIMIT_SECONDS = 20  # tempo mínimo entre envios por sessão
ALLOW_DUPLICATE_SAME_DISH_PER_DAY = False  # recomenda-se False


# ===============================
# OPÇÕES SENSORIAIS
# ===============================
INTENTIONS = ["Brinde", "Conexão", "Descoberta", "Prazer", "Desacelerar"]
INTENT_DESC = {
    "Brinde": "Abriu a noite com leveza, clima de celebração e energia social.",
    "Conexão": "Favoreceu conversa, compartilhamento e presença à mesa.",
    "Descoberta": "Trouxe sensação de experimentar algo novo, curioso e diferente.",
    "Prazer": "Entregou prazer imediato, sabor direto e sensação de acerto fácil.",
    "Desacelerar": "Convidou a comer com calma, aproveitar o tempo e sentir as camadas do prato.",
}

AXIS_LABELS = [
    "Clássico e Sutil",
    "Clássico e Marcante",
    "Fora do obvio e Sutil",
    "Fora do obvio e Marcante",
]
AXIS_DESC = {
    "Clássico e Sutil": "Tradicional e elegante, com delicadeza e pouca intervenção.",
    "Clássico e Marcante": "Tradicional, com presença forte e assinatura clara de sabor.",
    "Fora do obvio e Sutil": "Diferente, mas delicado, surpreende sem chocar.",
    "Fora do obvio e Marcante": "Diferente e intenso, com impacto e personalidade.",
}

HARMONIES = ["Equilibrada", "Contrastante", "Surpreendente", "Provocativa"]
HARM_DESC = {
    "Equilibrada": "Encaixe natural, nada disputa atenção.",
    "Contrastante": "Alternância de sabores com contraste interessante.",
    "Surpreendente": "Combinação inesperada, sensação clara de descoberta.",
    "Provocativa": "Ousada e intensa, para quem gosta de personalidade.",
}


# ===============================
# UTILITÁRIOS
# ===============================
def ensure_dirs() -> None:
    os.makedirs(DISH_IMG_DIR, exist_ok=True)


def normalize_phone(phone: str) -> str:
    return re.sub(r"\D+", "", phone or "")


def phone_hash(phone: str) -> str:
    return hashlib.sha256(normalize_phone(phone).encode("utf-8")).hexdigest()


def find_image_by_id(dish_id: str) -> Optional[str]:
    for ext in ["webp", "jpg", "jpeg", "png"]:
        p = os.path.join(DISH_IMG_DIR, f"{dish_id}.{ext}")
        if os.path.exists(p):
            return p
    return None


def iso_now_seconds() -> str:
    return datetime.now().isoformat(timespec="seconds")


def iso_date_only(dt_str: str) -> str:
    if not dt_str:
        return ""
    return dt_str.split("T", 1)[0].strip()


def today_iso() -> str:
    return date.today().isoformat()



# ===============================
# LINKS DE IMAGEM (Google Drive / GitHub raw)
# ===============================
def _extract_drive_file_id(url: str) -> Optional[str]:
    """
    Aceita formatos comuns:
      - https://drive.google.com/file/d/<ID>/view?...
      - https://drive.google.com/open?id=<ID>
      - https://drive.google.com/uc?id=<ID>&export=download
    """
    if not url:
        return None
    u = url.strip()

    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", u)
    if m:
        return m.group(1)

    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", u)
    if m:
        return m.group(1)

    return None


def drive_to_direct_image(url: str) -> str:
    """
    Converte link do Google Drive em link direto que o <img> consegue carregar.
    Observação: o arquivo precisa estar compartilhado como 'Anyone with the link'.
    Também converte links do GitHub no formato /blob/ para raw.githubusercontent.com.
    """
    if not url:
        return url
    u = url.strip()

    # já é formato direto
    if "drive.google.com/uc" in u and "export=" in u:
        return u

    file_id = _extract_drive_file_id(u)
    if file_id:
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    if "github.com" in u and "/blob/" in u:
        return u.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")

    return u


def get_logo_url() -> Optional[str]:
    """
    Prioridade:
      1) asset/yvora_logo.(png|jpg|jpeg|webp) no GitHub
      2) secrets: LOGO_URL
      3) settings (Google Sheets): logo_url
    """
    for ext in ["png", "jpg", "jpeg", "webp"]:
        p = os.path.join(ASSET_DIR, f"yvora_logo.{ext}")
        if os.path.exists(p):
            return p

    try:
        v = str(st.secrets.get("LOGO_URL", "")).strip()
        if v:
            return drive_to_direct_image(v)
    except Exception:
        pass

    v2 = get_setting("logo_url")
    if v2 and v2.strip():
        return drive_to_direct_image(v2.strip())

    return None

# ===============================
# RESTRIÇÃO POR WIFI (IP)
# ===============================
def _split_ips(csv_str: str) -> set:
    return {x.strip() for x in (csv_str or "").split(",") if x.strip()}


def get_client_ip_from_headers() -> Optional[str]:
    """
    Tenta obter IP do cliente via headers comuns quando há proxy na frente.
    No Streamlit Community Cloud pode variar dependendo da infra,
    mas normalmente funciona para bloquear avaliações fora do Wi-Fi.
    """
    try:
        headers = st.context.headers
    except Exception:
        return None

    candidates = [
        headers.get("x-forwarded-for"),
        headers.get("x-real-ip"),
        headers.get("cf-connecting-ip"),
        headers.get("true-client-ip"),
    ]

    for v in candidates:
        if not v:
            continue
        ip = str(v).split(",")[0].strip()
        try:
            ipaddress.ip_address(ip)
            return ip
        except ValueError:
            continue
    return None


def get_user_agent_from_headers() -> str:
    try:
        headers = st.context.headers
        return str(headers.get("user-agent") or "").strip()
    except Exception:
        return ""


def wifi_gate() -> Tuple[bool, str, str]:
    """
    Retorna:
      ok: bool
      mensagem: str
      client_ip: str (pode ser vazio)
    """
    allowed_ips = _split_ips(str(st.secrets.get("RESTAURANT_PUBLIC_IPS", "")).strip())
    client_ip = get_client_ip_from_headers() or ""

    if not allowed_ips:
        return (
            False,
            "Configuração ausente: defina RESTAURANT_PUBLIC_IPS nos Secrets do Streamlit.",
            client_ip,
        )

    if not client_ip:
        return (
            False,
            "Não foi possível identificar o IP. Conecte no Wi-Fi do restaurante e atualize a página.",
            client_ip,
        )

    if client_ip in allowed_ips:
        return True, f"OK (IP {client_ip})", client_ip

    return False, f"Fora do Wi-Fi do restaurante (IP {client_ip}). Conecte no Wi-Fi para avaliar.", client_ip


# ===============================
# ADMIN PASSWORD (robusto)
# ===============================
def _safe_get_admin_password() -> Optional[str]:
    """
    Lê a senha do Admin a partir de:
      1) st.secrets["admin_password"]
      2) variáveis de ambiente (YVORA_ADMIN_PASSWORD / ADMIN_PASSWORD)
    """
    try:
        pw = st.secrets.get("admin_password")  # type: ignore[attr-defined]
        if pw and str(pw).strip():
            return str(pw).strip()
    except Exception:
        pass

    for k in ("YVORA_ADMIN_PASSWORD", "ADMIN_PASSWORD"):
        v = os.getenv(k)
        if v and str(v).strip():
            return str(v).strip()

    return None


def _admin_config_message() -> str:
    return (
        "Senha do Admin não configurada em secrets.\n\n"
        "No Streamlit Cloud, abra Settings -> Secrets e adicione:\n\n"
        "admin_password = \"SUA_SENHA\""
    )


# ===============================
# GOOGLE SHEETS (persistência)
# ===============================
def _get_gsheets_conf() -> dict:
    """
    Espera em secrets:
      [gsheets]
      sheet_id = "..."
      evaluations_ws = "evaluations" (opcional)
      interactions_ws = "interactions" (opcional)
      settings_ws = "settings" (opcional)

    E credenciais:
      [gcp_service_account]
      type="service_account"
      project_id="..."
      private_key="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
      client_email="..."
      token_uri="https://oauth2.googleapis.com/token"
      (demais campos ok)
    """
    gs = {}
    try:
        gs = dict(st.secrets.get("gsheets", {}))  # type: ignore[attr-defined]
    except Exception:
        gs = {}

    sheet_id = gs.get("sheet_id") or st.secrets.get("sheet_id")  # type: ignore[attr-defined]
    if not sheet_id:
        raise RuntimeError("Faltou configurar o sheet_id em secrets (seção [gsheets]).")

    return {
        "sheet_id": str(sheet_id).strip(),
        "evaluations_ws": str(gs.get("evaluations_ws") or DEFAULT_WS_EVALS),
        "interactions_ws": str(gs.get("interactions_ws") or DEFAULT_WS_INTERACTIONS),
        "settings_ws": str(gs.get("settings_ws") or DEFAULT_WS_SETTINGS),
    }


@st.cache_resource
def _gs_client() -> gspread.Client:
    try:
        sa_info = dict(st.secrets["gcp_service_account"])  # type: ignore[index]
    except Exception:
        raise RuntimeError("Faltou configurar [gcp_service_account] nos Secrets do Streamlit.")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(creds)


def _open_sheet():
    conf = _get_gsheets_conf()
    return _gs_client().open_by_key(conf["sheet_id"])


def _ensure_headers_compat(ws, headers: List[str]) -> None:
    """
    Evita apagar dados.
    Se estiver vazio: cria cabeçalho.
    Se existir cabeçalho diferente: garante que todas as colunas de 'headers' existam, adicionando as que faltarem ao final.
    """
    try:
        first_row = ws.row_values(1)
    except Exception:
        first_row = []

    if not first_row:
        ws.update("A1", [headers])
        return

    normalized = [str(c).strip() for c in first_row]
    wanted = [str(c).strip() for c in headers]

    # se já contém todos, ok
    missing = [h for h in wanted if h not in normalized]
    if not missing:
        return

    # adiciona colunas faltantes ao fim
    new_header = normalized + missing
    ws.update("A1", [new_header])


def _ensure_worksheet(sheet, title: str, headers: List[str]):
    try:
        ws = sheet.worksheet(title)
    except Exception:
        ws = sheet.add_worksheet(title=title, rows=2000, cols=max(10, len(headers) + 5))

    _ensure_headers_compat(ws, headers)
    return ws


@st.cache_resource
def _ws_handles():
    conf = _get_gsheets_conf()
    sh = _open_sheet()

    # Nota: adicionamos colunas extras ao final SEM limpar dados
    ws_evals = _ensure_worksheet(
        sh,
        conf["evaluations_ws"],
        headers=[
            "created_at",
            "dish_id",
            "dish_name",
            "user_name",
            "user_phone",
            "user_hash",
            "consent_marketing",
            "intention",
            "axis",
            "harmony",
            "client_ip",
            "user_agent",
        ],
    )

    ws_inter = _ensure_worksheet(
        sh,
        conf["interactions_ws"],
        headers=[
            "created_at",
            "dish_id",
            "user_hash",
            "interaction_type",
            "value",
        ],
    )

    ws_set = _ensure_worksheet(
        sh,
        conf["settings_ws"],
        headers=["key", "value"],
    )

    return {"evaluations": ws_evals, "interactions": ws_inter, "settings": ws_set}


@st.cache_data(ttl=15)
def _read_ws_records(kind: str) -> List[dict]:
    ws = _ws_handles()[kind]
    try:
        return ws.get_all_records()
    except Exception:
        return []


def _clear_ws_cache():
    try:
        _read_ws_records.clear()
    except Exception:
        pass
    try:
        load_menu_from_url.clear()
    except Exception:
        pass


def set_setting(key: str, value: str) -> None:
    ws = _ws_handles()["settings"]
    rows = _read_ws_records("settings")
    key = str(key).strip()
    value = str(value).strip()

    for i, r in enumerate(rows, start=2):
        if str(r.get("key", "")).strip() == key:
            ws.update(f"B{i}", value)
            _clear_ws_cache()
            return

    ws.append_row([key, value], value_input_option="RAW")
    _clear_ws_cache()


def get_setting(key: str) -> Optional[str]:
    key = str(key).strip()
    rows = _read_ws_records("settings")
    for r in rows:
        if str(r.get("key", "")).strip() == key:
            v = r.get("value")
            return str(v).strip() if v is not None else None
    return None


def get_menu_url() -> str:
    val = get_setting("menu_csv_url")
    if val and val.strip():
        return val.strip()
    return DEFAULT_SHEETS_CSV_URL


def save_interaction(dish_id: str, user_hash: str, itype: str, value: str) -> None:
    ws = _ws_handles()["interactions"]
    ws.append_row(
        [iso_now_seconds(), str(dish_id), str(user_hash), str(itype), str(value)],
        value_input_option="RAW",
    )
    _clear_ws_cache()


def already_voted_today(dish_id: str, user_hash: str) -> bool:
    """
    Bloqueia duplicidade por prato por dia (mesmo telefone -> user_hash).
    """
    if ALLOW_DUPLICATE_SAME_DISH_PER_DAY:
        return False

    did = str(dish_id).strip()
    uhash = str(user_hash).strip()
    today = today_iso()

    rows = _read_ws_records("evaluations")
    for r in rows:
        if str(r.get("dish_id", "")).strip() != did:
            continue
        if str(r.get("user_hash", "")).strip() != uhash:
            continue
        created = iso_date_only(str(r.get("created_at", "")).strip())
        if created == today:
            return True
    return False


def save_evaluation(
    dish_id: str,
    dish_name: str,
    user_name: str,
    user_phone: str,
    consent_marketing: bool,
    intention: str,
    axis_label: str,
    harmony: str,
    client_ip: str,
    user_agent: str,
) -> Tuple[bool, str]:
    created_at = iso_now_seconds()
    uhash = phone_hash(user_phone)

    if already_voted_today(dish_id, uhash):
        return False, "Você já avaliou este prato hoje. Obrigado."

    ws = _ws_handles()["evaluations"]
    ws.append_row(
        [
            created_at,
            str(dish_id),
            str(dish_name),
            user_name.strip(),
            normalize_phone(user_phone),
            uhash,
            1 if consent_marketing else 0,
            intention,
            axis_label,
            harmony,
            str(client_ip or ""),
            str(user_agent or "")[:500],  # limita tamanho
        ],
        value_input_option="RAW",
    )

    # Contagens (mantém compatibilidade com telas atuais)
    save_interaction(str(dish_id), uhash, "intention", intention)
    save_interaction(str(dish_id), uhash, "axis", axis_label)
    save_interaction(str(dish_id), uhash, "harmony", harmony)

    _clear_ws_cache()
    return True, "Avaliação enviada com sucesso."


def fetch_counts(dish_id: str, itype: str) -> Dict[str, int]:
    rows = _read_ws_records("interactions")
    out: Dict[str, int] = {}
    did = str(dish_id)
    it = str(itype)
    for r in rows:
        if str(r.get("dish_id", "")) != did:
            continue
        if str(r.get("interaction_type", "")) != it:
            continue
        v = str(r.get("value", "")).strip()
        if not v:
            continue
        out[v] = out.get(v, 0) + 1
    return out


def top_choice(counts: Dict[str, int]) -> Optional[str]:
    if not counts:
        return None
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)[0][0]


def dish_review_counts() -> Dict[str, int]:
    rows = _read_ws_records("evaluations")
    out: Dict[str, int] = {}
    for r in rows:
        did = str(r.get("dish_id", "")).strip()
        if not did:
            continue
        out[did] = out.get(did, 0) + 1
    return out


def top3_dishes_by_reviews() -> List[str]:
    counts = dish_review_counts()
    return [k for k, _ in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:3]]


# ===============================
# MENU (Google Sheets CSV)
# ===============================
def _normalize_colname(s: str) -> str:
    return (s or "").strip().lower()


@st.cache_data(ttl=30)
def load_menu_from_url(menu_url: str) -> List[dict]:
    try:
        with urllib.request.urlopen(menu_url) as r:
            content = r.read().decode("utf-8", errors="replace")

        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames:
            return []

        field_map = {_normalize_colname(f): f for f in reader.fieldnames if f}

        def get(row: dict, key: str) -> str:
            real = field_map.get(_normalize_colname(key))
            if not real:
                return ""
            return (row.get(real) or "").strip()

        rows: List[dict] = []
        for row in reader:
            rid = get(row, "Id")
            prato = get(row, "Prato")
            desc = get(row, "Descrição") or get(row, "Descricao")
            carne = get(row, "Carne")
            queijo = get(row, "Queijo")
            etapa = get(row, "Etapa")
            ativo = (get(row, "Ativo") or "1").strip()

            # usa coluna existente ImagemURL (opcional)
            imagem_url = (
                get(row, "ImagemURL")
                or get(row, "Imagem Url")
                or get(row, "Imagem URL")
                or get(row, "ImagemURL ")
            )

            if not rid or not prato:
                continue

            rows.append(
                {
                    "Id": rid,
                    "Prato": prato,
                    "Descrição": desc,
                    "Carne": carne,
                    "Queijo": queijo,
                    "Etapa": etapa,
                    "Ativo": ativo,
                    "ImagemURL": imagem_url,
                }
            )

        return [
            r
            for r in rows
            if r["Ativo"]
            in ("1", "true", "True", "SIM", "Sim", "sim", "ATIVO", "Ativo", "ativo")
        ]
    except Exception:
        return []


def load_menu() -> List[dict]:
    url = get_menu_url()
    return load_menu_from_url(url)


def get_dish_image(dish: dict) -> Optional[str]:
    url = str(dish.get("ImagemURL", "") or "").strip()
    if url:
        return drive_to_direct_image(url)
    return find_image_by_id(str(dish.get("Id", "")))


# ===============================
# TEXTO PARA DECISÃO (Explorar)
# ===============================
def build_decision_sentence(
    intention: Optional[str], harmony: Optional[str], axis_label: Optional[str]
) -> str:
    if not intention and not harmony and not axis_label:
        return "Ainda não há avaliações suficientes para orientar sua escolha."

    parts: List[str] = []

    intent_map = {
        "Brinde": "Se você quer começar com leveza e clima de celebração, este prato costuma funcionar muito bem.",
        "Conexão": "Se você quer um prato que favoreça conversa e presença à mesa, este tende a ser uma escolha forte.",
        "Descoberta": "Se você busca algo diferente, este prato costuma ser percebido como uma boa porta de entrada para descobrir.",
        "Prazer": "Se você quer prazer imediato e uma escolha fácil de gostar, este prato costuma agradar com facilidade.",
        "Desacelerar": "Se você quer comer com calma e aproveitar as camadas do prato, este tende a combinar com esse momento.",
    }

    if intention:
        parts.append(intent_map.get(intention, "Este prato ajuda a compor o clima do seu jantar."))

    if harmony:
        harm_map = {
            "Equilibrada": "A harmonia foi percebida como equilibrada, com sensação de encaixe natural.",
            "Contrastante": "A harmonia foi percebida como contrastante, criando alternância de sabores.",
            "Surpreendente": "A harmonia foi percebida como surpreendente, com efeito claro de descoberta.",
            "Provocativa": "A harmonia foi percebida como provocativa, para quem gosta de escolhas com personalidade.",
        }
        parts.append(harm_map.get(harmony, "A harmonia foi percebida como consistente para o perfil do prato."))

    if axis_label:
        axis_map = {
            "Clássico e Sutil": "O perfil foi descrito como clássico e sutil, elegante e sem exagero.",
            "Clássico e Marcante": "O perfil foi descrito como clássico e marcante, com presença e assinatura clara.",
            "Fora do obvio e Sutil": "O perfil foi descrito como fora do obvio e sutil, diferente sem chocar.",
            "Fora do obvio e Marcante": "O perfil foi descrito como fora do obvio e marcante, com impacto.",
        }
        parts.append(axis_map.get(axis_label, "O perfil do prato foi descrito de forma consistente."))

    return " ".join(parts)


# ===============================
# VISUAL
# ===============================
def inject_css() -> None:
    st.markdown(
        f"""
        <style>
        .stApp {{
            background: {COLOR_CREAM};
        }}
        .yv-card {{
            background: rgba(255,255,255,0.75);
            border-radius: 22px;
            padding: 16px;
            margin-bottom: 16px;
            border: 1px solid rgba(14,42,71,0.08);
        }}
        .yv-title {{
            font-weight: 900;
            color: {COLOR_NAVY};
            font-size: 28px;
            line-height: 1.05;
        }}
        .yv-sub {{
            color: rgba(11,34,56,0.70);
            font-size: 13px;
            margin-top: 6px;
        }}
        .yv-h {{
            font-weight: 900;
            color: {COLOR_INK};
            font-size: 18px;
            margin-top: 6px;
        }}
        .yv-p {{
            color: rgba(11,34,56,0.72);
            font-size: 13px;
            margin-top: 4px;
        }}
        .yv-submit button {{
            background: {COLOR_GOLD} !important;
            color: {COLOR_NAVY} !important;
            font-weight: 900 !important;
            width: 100% !important;
            border-radius: 14px !important;
            padding: 0.7rem 1rem !important;
        }}
        .yv-badge {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 800;
            border: 1px solid rgba(14,42,71,0.12);
            background: rgba(255,255,255,0.55);
            color: {COLOR_NAVY};
            margin-right: 8px;
        }}
        .yv-crown {{
            font-size: 14px;
            line-height: 1;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_header() -> None:
    col1, col2 = st.columns([1, 3])
    with col1:
        # Logo primeiro na raiz do repo (como você salvou: yvora_logo.png)
        if os.path.exists(ROOT_LOGO_PATH):
            st.image(ROOT_LOGO_PATH, use_container_width=True)
        # Alternativa: logo dentro de asset/yvora_logo.png
        elif os.path.exists(LOGO_PATH):
            st.image(LOGO_PATH, use_container_width=True)
        else:
            st.markdown(
                f"<div style='font-weight:900;color:{COLOR_NAVY};font-size:18px'>YVORA</div>",
                unsafe_allow_html=True,
            )
    with col2:
        st.markdown(
            "<div class='yv-title'>Cardápio Sensorial</div>"
            "<div class='yv-sub'>Guia de escolha dos pratos orientado pela percepção do público</div>",
            unsafe_allow_html=True,
        )
    st.divider()


def crown_badge(rank: int) -> str:
    if rank == 1:
        color = COLOR_GOLD
        label = "Top 1"
    elif rank == 2:
        color = COLOR_SILVER
        label = "Top 2"
    else:
        color = COLOR_BRONZE
        label = "Top 3"
    return f"""
    <span class="yv-badge">
        <span class="yv-crown" style="color:{color};">👑</span>
        {label}
    </span>
    """


# ===============================
# SIDEBAR
# ===============================
def render_sidebar() -> str:
    st.sidebar.markdown("## Acesso")

    current_page = st.session_state.get("page", "Explorar")

    st.sidebar.markdown("### Ir para")

    if "nav_choice" not in st.session_state:
        st.session_state["nav_choice"] = "Explorar"
    if "nav_choice_prev" not in st.session_state:
        st.session_state["nav_choice_prev"] = st.session_state["nav_choice"]

    st.sidebar.radio(
        "Navegação",
        ["Explorar", "Avaliar"],
        index=0 if st.session_state["nav_choice"] == "Explorar" else 1,
        label_visibility="collapsed",
        key="nav_choice",
    )

    if current_page == "Admin":
        if st.session_state["nav_choice"] != st.session_state["nav_choice_prev"]:
            st.session_state["page"] = st.session_state["nav_choice"]
            current_page = st.session_state["page"]
    else:
        st.session_state["page"] = st.session_state["nav_choice"]
        current_page = st.session_state["page"]

    st.session_state["nav_choice_prev"] = st.session_state["nav_choice"]

    st.sidebar.divider()
    st.sidebar.markdown("## Admin")

    admin_pw = _safe_get_admin_password()
    is_admin = bool(st.session_state.get("is_admin", False))

    if is_admin:
        st.sidebar.success("Admin autenticado.")
        if st.sidebar.button("Sair do Admin"):
            st.session_state["is_admin"] = False
            st.session_state["page"] = "Explorar"
            st.session_state["nav_choice"] = "Explorar"
            st.session_state["nav_choice_prev"] = "Explorar"
            st.rerun()

        if st.sidebar.button("Ir para Admin"):
            st.session_state["page"] = "Admin"
            st.rerun()

    else:
        if not admin_pw:
            st.sidebar.info(_admin_config_message())

        pw_in = st.sidebar.text_input("Senha", type="password", key="admin_pw_input")

        if st.sidebar.button("Entrar"):
            admin_pw_now = _safe_get_admin_password()
            if not admin_pw_now:
                st.sidebar.error("Defina a senha em Secrets para habilitar o Admin.")
            elif (pw_in or "") == admin_pw_now:
                st.session_state["is_admin"] = True
                st.session_state["page"] = "Admin"
                st.rerun()
            else:
                st.sidebar.error("Senha incorreta.")

    return st.session_state.get("page", "Explorar")


# ===============================
# TELAS
# ===============================
def explore_screen(menu: List[dict]) -> None:
    msg = st.session_state.pop("flash_success", None)
    if msg:
        st.success(str(msg))

    if not menu:
        st.warning("Sem itens ativos no menu. Verifique o Google Sheets e a coluna Ativo.")
        return

    top3 = top3_dishes_by_reviews()
    rank_map = {dish_id: (i + 1) for i, dish_id in enumerate(top3)}

    etapas = sorted({m.get("Etapa", "") for m in menu if m.get("Etapa", "")})
    etapa = st.selectbox("Etapa do menu", ["Todas"] + etapas)

    for dish in menu:
        if etapa != "Todas" and dish.get("Etapa") != etapa:
            continue

        st.markdown("<div class='yv-card'>", unsafe_allow_html=True)

        img = get_dish_image(dish)
        if img:
            st.image(img, use_container_width=True)

        if dish["Id"] in rank_map:
            st.markdown(crown_badge(rank_map[dish["Id"]]), unsafe_allow_html=True)

        st.markdown(f"<div class='yv-h'>{dish['Prato']}</div>", unsafe_allow_html=True)
        if dish.get("Descrição"):
            st.markdown(f"<div class='yv-p'>{dish.get('Descrição')}</div>", unsafe_allow_html=True)

        meta = []
        if dish.get("Etapa"):
            meta.append(dish["Etapa"])
        if dish.get("Carne"):
            meta.append(f"Carne: {dish['Carne']}")
        if dish.get("Queijo"):
            meta.append(f"Queijo: {dish['Queijo']}")
        if meta:
            st.caption(" | ".join(meta))

        intent = top_choice(fetch_counts(dish["Id"], "intention"))
        harm = top_choice(fetch_counts(dish["Id"], "harmony"))
        axis = top_choice(fetch_counts(dish["Id"], "axis"))

        frase = build_decision_sentence(intent, harm, axis)
        st.markdown(
            f"<div class='yv-p'><b>Percepção predominante do público</b><br>{frase}</div>",
            unsafe_allow_html=True,
        )

        st.markdown("</div>", unsafe_allow_html=True)


def _rate_limit_ok() -> Tuple[bool, int]:
    now = int(datetime.now().timestamp())
    last = int(st.session_state.get("last_submit_ts", 0))
    delta = now - last
    if delta < RATE_LIMIT_SECONDS:
        return False, RATE_LIMIT_SECONDS - delta
    return True, 0


def evaluate_screen(menu: List[dict]) -> None:
    if not menu:
        st.warning("Sem itens ativos no menu. Verifique o Google Sheets e a coluna Ativo.")
        return

    # Gate Wi-Fi
    wifi_ok, wifi_msg, client_ip = wifi_gate()
    user_agent = get_user_agent_from_headers()

    if not wifi_ok:
        st.warning("Avaliação restrita ao Wi-Fi do restaurante.")
        st.caption(wifi_msg)
        st.info("Você pode visualizar o cardápio normalmente. Para avaliar, conecte no Wi-Fi do restaurante e atualize a página.")

    dish_names = [m["Prato"] for m in menu]
    selected = st.selectbox("Escolha o prato", dish_names, key="dish_select")
    dish = next(m for m in menu if m["Prato"] == selected)

    st.markdown("<div class='yv-card'>", unsafe_allow_html=True)

    img = get_dish_image(dish)
    if img:
        st.image(img, use_container_width=True)

    st.markdown(f"<div class='yv-h'>{dish['Prato']}</div>", unsafe_allow_html=True)
    if dish.get("Descrição"):
        st.markdown(f"<div class='yv-p'>{dish.get('Descrição')}</div>", unsafe_allow_html=True)

    st.subheader("1) Que tipo de experiência combina mais com este prato?")
    intention = st.radio("Intenção", INTENTIONS, horizontal=True, label_visibility="collapsed", key="q_intention")
    st.caption(f"Significado: {INTENT_DESC[intention]}")

    st.subheader("2) Como você descreve o perfil do prato?")
    axis_label = st.radio("Perfil", AXIS_LABELS, horizontal=True, label_visibility="collapsed", key="q_axis")
    st.caption(f"Significado: {AXIS_DESC[axis_label]}")

    st.subheader("3) Como foi a harmonia entre carne e queijo?")
    harmony = st.radio("Harmonia", HARMONIES, horizontal=True, label_visibility="collapsed", key="q_harmony")
    st.caption(f"Significado: {HARM_DESC[harmony]}")

    st.divider()

    st.subheader("Seus dados")
    col1, col2 = st.columns(2)
    with col1:
        name = st.text_input("Nome", key="user_name")
    with col2:
        phone = st.text_input("Telefone (WhatsApp)", key="user_phone")

    consent = st.checkbox(
        "Aceito receber promoções e ofertas especiais exclusivas neste número, conforme a Lei Geral de Proteção de Dados.",
        value=True,
        key="user_consent",
    )

    # Anti-duplicidade por sessão (rápido)
    if "session_voted_dishes" not in st.session_state:
        st.session_state["session_voted_dishes"] = set()

    st.markdown("<div class='yv-submit'>", unsafe_allow_html=True)

    # Se não está no Wi-Fi, não permite o submit (mas mantém toda a UI)
    if not wifi_ok:
        st.button("Enviar avaliação", key="btn_submit_eval_disabled", disabled=True)
        st.markdown("</div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)
        return

    # Caso esteja no Wi-Fi, libera envio com rate limit e dedup
    if st.button("Enviar avaliação", key="btn_submit_eval"):
        if not name.strip() or not normalize_phone(phone):
            st.error("Preencha Nome e Telefone corretamente para registrar a avaliação.")
        else:
            ok_rl, wait_s = _rate_limit_ok()
            if not ok_rl:
                st.warning(f"Aguarde {wait_s}s para enviar outra avaliação.")
            else:
                did = str(dish["Id"])
                if did in st.session_state["session_voted_dishes"] and not ALLOW_DUPLICATE_SAME_DISH_PER_DAY:
                    st.warning("Você já avaliou este prato nesta sessão. Obrigado.")
                else:
                    ok, msg = save_evaluation(
                        dish_id=did,
                        dish_name=str(dish["Prato"]),
                        user_name=name.strip(),
                        user_phone=phone,
                        consent_marketing=bool(consent),
                        intention=intention,
                        axis_label=axis_label,
                        harmony=harmony,
                        client_ip=client_ip,
                        user_agent=user_agent,
                    )
                    if ok:
                        st.session_state["session_voted_dishes"].add(did)
                        st.session_state["last_submit_ts"] = int(datetime.now().timestamp())

                        st.session_state["flash_success"] = msg
                        st.session_state["goto_page"] = "Explorar"
                        st.rerun()
                    else:
                        st.warning(msg)

    st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


def _fetch_evaluations_df(dt_ini, dt_fim, dish_id: Optional[str]):
    import pandas as pd

    rows = _read_ws_records("evaluations")
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    if "created_at" in df.columns:
        df["created_date"] = df["created_at"].astype(str).apply(iso_date_only)
    else:
        df["created_date"] = ""

    if dt_ini:
        df = df[df["created_date"] >= dt_ini.isoformat()]
    if dt_fim:
        df = df[df["created_date"] <= dt_fim.isoformat()]
    if dish_id and dish_id != "ALL":
        df = df[df["dish_id"].astype(str) == str(dish_id)]

    if "created_at" in df.columns:
        df = df.sort_values("created_at", ascending=False)

    expected = [
        "created_at",
        "dish_id",
        "dish_name",
        "user_name",
        "user_phone",
        "consent_marketing",
        "intention",
        "axis",
        "harmony",
        "client_ip",
        "user_agent",
    ]
    for c in expected:
        if c not in df.columns:
            df[c] = ""

    return df[expected]


def _render_heatmap(df):
    import pandas as pd

    if df.empty:
        st.info("Nenhuma avaliação encontrada para os filtros selecionados.")
        return

    long_rows: List[dict] = []

    for v in df["intention"].dropna().astype(str).tolist():
        if v.strip():
            long_rows.append({"Categoria": "Experiência", "Opção": v.strip()})
    for v in df["axis"].dropna().astype(str).tolist():
        if v.strip():
            long_rows.append({"Categoria": "Perfil", "Opção": v.strip()})
    for v in df["harmony"].dropna().astype(str).tolist():
        if v.strip():
            long_rows.append({"Categoria": "Harmonia", "Opção": v.strip()})

    if not long_rows:
        st.info("Ainda não há dados suficientes para o heatmap.")
        return

    dfl = pd.DataFrame(long_rows)
    dfl["Contagem"] = 1
    agg = dfl.groupby(["Categoria", "Opção"], as_index=False)["Contagem"].sum()

    option_order = INTENTIONS + AXIS_LABELS + HARMONIES
    agg["ord"] = agg["Opção"].apply(lambda x: option_order.index(x) if x in option_order else 999)

    try:
        import altair as alt

        chart = (
            alt.Chart(agg)
            .mark_rect(cornerRadius=6)
            .encode(
                x=alt.X("Categoria:N", sort=["Experiência", "Perfil", "Harmonia"], title=None),
                y=alt.Y("Opção:N", sort=alt.SortField(field="ord", order="ascending"), title=None),
                color=alt.Color("Contagem:Q", title="Avaliações"),
                tooltip=["Categoria:N", "Opção:N", "Contagem:Q"],
            )
            .properties(height=480)
        )

        st.altair_chart(chart, use_container_width=True)

    except Exception:
        pivot = agg.pivot_table(index="Opção", columns="Categoria", values="Contagem", fill_value=0)
        st.dataframe(pivot, use_container_width=True)


def admin_reports_screen(menu: List[dict]) -> None:
    if not st.session_state.get("is_admin"):
        st.warning("Acesso restrito. Entre como Admin na barra lateral.")
        return

    st.subheader("Relatórios")
    st.caption("Filtros, tabela completa, ranking e heatmap de percepções.")

    colf1, colf2, colf3 = st.columns([1, 1, 1.2])

    with colf1:
        dt_ini = st.date_input("Data inicial", value=None, key="rep_dt_ini")
    with colf2:
        dt_fim = st.date_input("Data final", value=None, key="rep_dt_fim")

    dish_options = [("ALL", "Todos os pratos")]
    for m in menu:
        dish_options.append((str(m.get("Id")), str(m.get("Prato"))))

    with colf3:
        dish_label_list = [label for _, label in dish_options]
        dish_idx = st.selectbox("Prato (filtro)", dish_label_list, index=0, key="rep_dish_filter")
        dish_id = dish_options[dish_label_list.index(dish_idx)][0]

    df_raw = _fetch_evaluations_df(dt_ini, dt_fim, dish_id)

    st.markdown("### Top 10 pratos mais avaliados")
    if df_raw.empty:
        st.info("Sem avaliações para os filtros selecionados.")
    else:
        top10 = (
            df_raw.assign(dish_name=df_raw["dish_name"].fillna(""))
            .groupby(["dish_id", "dish_name"], as_index=False)
            .size()
            .rename(columns={"size": "Avaliações", "dish_id": "Id", "dish_name": "Prato"})
            .sort_values("Avaliações", ascending=False)
            .head(10)
        )
        top10["#"] = list(range(1, len(top10) + 1))
        top10 = top10[["#", "Prato", "Avaliações", "Id"]]
        st.dataframe(top10, use_container_width=True, hide_index=True)

    st.markdown("### Heatmap de percepções")
    _render_heatmap(df_raw)

    st.markdown("### Tabela completa de avaliações")
    st.caption("Inclui data, contato, preferência de promoções e classificações.")

    if df_raw.empty:
        st.info("Nenhuma avaliação encontrada para os filtros selecionados.")
    else:
        df = df_raw.copy()
        df["consent_marketing"] = df["consent_marketing"].apply(lambda x: "Sim" if str(x).strip() == "1" else "Não")
        df = df.rename(
            columns={
                "created_at": "Data",
                "dish_id": "Id Prato",
                "dish_name": "Prato",
                "user_name": "Nome",
                "user_phone": "Telefone",
                "consent_marketing": "Aceita promoções",
                "intention": "Experiência",
                "axis": "Perfil",
                "harmony": "Harmonia",
                "client_ip": "IP",
                "user_agent": "User Agent",
            }
        )
        df["Data"] = df["Data"].astype(str)

        st.dataframe(df, use_container_width=True, hide_index=True)

        csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "Baixar CSV (filtro aplicado)",
            data=csv_bytes,
            file_name="yvora_avaliacoes_filtradas.csv",
            mime="text/csv",
        )

    st.divider()
    st.markdown("### Configurações")
    st.caption("Altere o link CSV do menu sem editar o código.")

    current_url = get_menu_url()
    new_url = st.text_input("Link CSV do menu (Google Sheets)", value=current_url, key="cfg_menu_url")

    colc1, colc2 = st.columns([1, 2])
    with colc1:
        if st.button("Salvar link do menu"):
            if not new_url.strip():
                st.error("Informe um link válido.")
            else:
                set_setting("menu_csv_url", new_url.strip())
                st.success("Link do menu atualizado.")
                st.rerun()

    with colc2:
        if st.button("Voltar para Explorar"):
            st.session_state["page"] = "Explorar"
            st.session_state["nav_choice"] = "Explorar"
            st.session_state["nav_choice_prev"] = "Explorar"
            st.rerun()


# ===============================
# MAIN
# ===============================
def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="🍽️", layout="wide")
    ensure_dirs()

    # valida configuração do Sheets logo no início (erro claro)
    try:
        _ = _ws_handles()
    except Exception as e:
        st.error(f"Erro de configuração do Google Sheets: {e}")
        st.stop()

    # redirecionamento deve acontecer ANTES do sidebar criar o widget nav_choice
    goto = st.session_state.pop("goto_page", None)
    if goto in ("Explorar", "Avaliar", "Admin"):
        if goto in ("Explorar", "Avaliar"):
            st.session_state["nav_choice"] = goto
            st.session_state["nav_choice_prev"] = goto
            st.session_state["page"] = goto
        else:
            st.session_state["page"] = "Admin"

    inject_css()
    render_header()

    menu = load_menu()

    page = render_sidebar()

    if page == "Explorar":
        explore_screen(menu)
    elif page == "Avaliar":
        evaluate_screen(menu)
    else:
        admin_reports_screen(menu)


if __name__ == "__main__":
    main()
