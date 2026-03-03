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
import requests

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials


# ===============================
# CONFIGURAÇÕES
# ===============================
APP_TITLE = "Cardápio Sensorial | YVORA"

ASSET_DIR = "asset"
LOGO_PATH = os.path.join(ASSET_DIR, "yvora_logo.png")
ROOT_LOGO_PATH = "yvora_logo.png"  # logo na raiz do repo
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

# Controle básico (anti flood / anti duplicidade)
RATE_LIMIT_SECONDS = 20
ALLOW_DUPLICATE_SAME_DISH_PER_DAY = False


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


# ===============================
# IP E USER AGENT (SOLUÇÃO DEFINITIVA)
# ===============================
def _is_valid_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except Exception:
        return False


def _first_ip_from_xff(xff: str) -> str:
    """
    X-Forwarded-For pode vir como: "client_ip, proxy1, proxy2"
    A regra correta é pegar o primeiro IP válido.
    """
    if not xff:
        return ""
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    for p in parts:
        if _is_valid_ip(p):
            return p
    return ""


def safe_client_ip() -> str:
    """
    Captura o IP do cliente pelo contexto do Streamlit (headers).
    Isso funciona no Streamlit Cloud porque ele coloca o IP público do usuário em headers.
    """
    headers = {}
    try:
        # Streamlit recente
        headers = dict(getattr(st, "context").headers)  # type: ignore[attr-defined]
    except Exception:
        headers = {}

    # Normaliza chaves para lower
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

    # Prioridade comum em reverse proxies
    xff = h.get("x-forwarded-for", "")
    ip = _first_ip_from_xff(xff)
    if ip:
        return ip

    xri = h.get("x-real-ip", "").strip()
    if _is_valid_ip(xri):
        return xri

    cfi = h.get("cf-connecting-ip", "").strip()
    if _is_valid_ip(cfi):
        return cfi

    # Se nada vier, retorna vazio (vai bloquear quando houver allowlist)
    return ""


def safe_user_agent() -> str:
    headers = {}
    try:
        headers = dict(getattr(st, "context").headers)  # type: ignore[attr-defined]
    except Exception:
        headers = {}
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    return (h.get("user-agent", "") or "")[:500]


# ===============================
# IMAGENS (SOLUÇÃO DEFINITIVA)
# ===============================
def _extract_drive_file_id(url: str):
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


def _is_drive_url(url: str) -> bool:
    u = (url or "").lower()
    return "drive.google.com" in u


def _to_github_raw(url: str) -> str:
    if "github.com" in url and "/blob/" in url:
        return url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    return url


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_image_bytes(url: str) -> Optional[bytes]:
    """
    Solução definitiva:
    Baixa a imagem no backend e retorna bytes.
    Isso evita o problema do Google Drive servir HTML, viewer, confirmações e redirects que quebram o <img>.
    """
    if not url:
        return None

    url = url.strip()

    # Google Drive: usa thumbnail que é o endpoint mais estável para embed de imagem
    if _is_drive_url(url):
        fid = _extract_drive_file_id(url)
        if not fid:
            return None
        url = f"https://drive.google.com/thumbnail?id={fid}&sz=w1600"

    url = _to_github_raw(url)

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
        if r.status_code != 200:
            return None

        ctype = (r.headers.get("Content-Type") or "").lower()
        if "image" not in ctype:
            return None

        return r.content
    except Exception:
        return None


# ===============================
# RESTRIÇÃO POR WIFI (IP)
# ===============================
def _parse_ip_list(raw: str) -> List[ipaddress._BaseNetwork]:
    nets: List[ipaddress._BaseNetwork] = []
    raw = (raw or "").strip()
    if not raw:
        return nets
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    for p in parts:
        try:
            if "/" in p:
                nets.append(ipaddress.ip_network(p, strict=False))
            else:
                ip = ipaddress.ip_address(p)
                nets.append(ipaddress.ip_network(f"{ip}/32", strict=False))
        except Exception:
            continue
    return nets


def _client_ip_allowed() -> bool:
    """
    Modo simples:
      - Se secrets tiver RESTAURANT_ALLOWED_IP_RANGES, permite somente IPs nesses ranges.
      - Caso não esteja configurado, não bloqueia.
    """
    try:
        raw = str(st.secrets.get("RESTAURANT_ALLOWED_IP_RANGES", "")).strip()
    except Exception:
        raw = ""

    if not raw:
        return True

    nets = _parse_ip_list(raw)
    if not nets:
        return True

    ip_str = safe_client_ip()
    if not ip_str:
        return False

    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in n for n in nets)
    except Exception:
        return False


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
        pw = st.secrets.get("admin_password")
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
        'admin_password = "SUA_SENHA"'
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
    """
    gs = {}
    try:
        gs = dict(st.secrets.get("gsheets", {}))
    except Exception:
        gs = {}

    sheet_id = gs.get("sheet_id") or st.secrets.get("sheet_id")
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
        sa_info = dict(st.secrets["gcp_service_account"])
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
    try:
        first_row = ws.row_values(1)
    except Exception:
        first_row = []

    if not first_row:
        ws.update("A1", [headers])
        return

    normalized = [str(c).strip() for c in first_row]
    wanted = [str(c).strip() for c in headers]

    missing = [h for h in wanted if h not in normalized]
    if not missing:
        return

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
    if ALLOW_DUPLICATE_SAME_DISH_PER_DAY:
        return False

    did = str(dish_id).strip()
    uhash = str(user_hash).strip()
    today = date.today().isoformat()

    rows = _read_ws_records("evaluations")
    for r in rows:
        if str(r.get("dish_id", "")).strip() != did:
            continue
        if str(r.get("user_hash", "")).strip() != uhash:
            continue
        created_at = str(r.get("created_at", "")).strip()
        if created_at[:10] == today:
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
            str(user_agent or "")[:500],
        ],
        value_input_option="RAW",
    )

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
            if r["Ativo"] in ("1", "true", "True", "SIM", "Sim", "sim", "ATIVO", "Ativo", "ativo")
        ]
    except Exception:
        return []


def load_menu() -> List[dict]:
    url = get_menu_url()
    return load_menu_from_url(url)


def get_dish_image(dish: dict):
    url = str(dish.get("ImagemURL", "") or "").strip()
    if url:
        return fetch_image_bytes(url)
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
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_header() -> None:
    col1, col2 = st.columns([1, 3])
    with col1:
        if os.path.exists(ROOT_LOGO_PATH):
            st.image(ROOT_LOGO_PATH, use_container_width=True)
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
            if rank_map[dish["Id"]] == 1:
                st.caption("👑 Top 1")
            elif rank_map[dish["Id"]] == 2:
                st.caption("👑 Top 2")
            else:
                st.caption("👑 Top 3")

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
    if not _client_ip_allowed():
        st.error("A avaliação está disponível apenas para conexões autorizadas do restaurante.")
        st.stop()

    if not menu:
        st.warning("Sem itens ativos no menu. Verifique o Google Sheets e a coluna Ativo.")
        return

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

    if "session_voted_dishes" not in st.session_state:
        st.session_state["session_voted_dishes"] = set()

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
                        client_ip=safe_client_ip(),
                        user_agent=safe_user_agent(),
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


def admin_reports_screen(menu: List[dict]) -> None:
    if not st.session_state.get("is_admin"):
        st.warning("Acesso restrito. Entre como Admin na barra lateral.")
        return

    st.subheader("Relatórios")
    st.caption("Tela de Admin mantida conforme estrutura atual.")
    st.info("Se você quiser evoluir esta página com ranking, filtros e exportação, eu adapto sem mudar a navegação.")


# ===============================
# MAIN
# ===============================
def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="🍽️", layout="wide")
    ensure_dirs()

    try:
        _ = _ws_handles()
    except Exception as e:
        st.error(f"Erro de configuração do Google Sheets: {e}")
        st.stop()

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
