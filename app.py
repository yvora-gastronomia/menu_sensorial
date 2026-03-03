import hashlib
import io
from pathlib import Path
import unicodedata
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

# [AJUSTE PONTUAL] Restrição por IP (somente adiciona, sem alterar fluxo existente)
import ipaddress


APP_TITLE = "YVORA Wine Pairing"
BRAND_BG = "#EFE7DD"
BRAND_BLUE = "#0E2A47"
BRAND_MUTED = "#6B7785"
BRAND_CARD = "#F5EFE7"
BRAND_WARN = "#F3D6CF"

# Logo can live either at repo root (as in your current GitHub layout)
# or inside an assets/ folder. The app will auto-detect.
BASE_DIR = Path(__file__).resolve().parent

POSSIBLE_LOGOS = [
    BASE_DIR / "yvora_logo.png",
    BASE_DIR / "assets" / "yvora_logo.png",
]


def _find_logo_path() -> Path:
    for p in POSSIBLE_LOGOS:
        try:
            if p.exists():
                return p
        except Exception:
            continue
    # Default to root path (keeps a stable absolute path string even if missing)
    return POSSIBLE_LOGOS[0]


LOGO_LOCAL_PATH = _find_logo_path()


def _get_secret(key: str, default: str = "") -> str:
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def norm_text(x) -> str:
    if pd.isna(x):
        return ""
    s = str(x)
    # evita alguns caracteres que às vezes viram símbolos em fontes/ambientes específicos
    s = s.replace("—", "-").replace("–", "-").replace("•", "-")
    s = unicodedata.normalize("NFC", s)
    return s.strip()


@st.cache_data(ttl=3600, show_spinner=False)
def get_asset_bytes(local_path: Path, fallback_url: str = "") -> Optional[bytes]:
    """Load an asset from repo (preferred) or from a public URL (fallback).
    This avoids broken relative paths when deploying on Streamlit Cloud.
    """
    try:
        if local_path.exists():
            return local_path.read_bytes()
    except Exception:
        pass

    fb = norm_text(fallback_url)
    if fb:
        try:
            r = requests.get(fb, timeout=30)
            r.raise_for_status()
            return r.content
        except Exception:
            return None
    return None


def render_logo(width: Optional[int] = None, use_container_width: bool = False):
    """Renders the logo robustly.
    Configure one of these in Streamlit secrets:
    - LOGO_URL: public URL (recommended: GitHub raw URL)
    """
    logo_url = _get_secret("LOGO_URL", "")
    b = get_asset_bytes(LOGO_LOCAL_PATH, logo_url)
    if b:
        st.image(b, width=width, use_container_width=use_container_width)
    else:
        st.caption("Logo não encontrada. Inclua em assets/ ou configure LOGO_URL em secrets.")


# ===============================
# [AJUSTE PONTUAL] Restrição por IP do restaurante
# Secrets esperado:
# RESTAURANT_ALLOWED_IP_RANGES = "191.250.250.41/32"
# Opcionalmente múltiplos separados por vírgula:
# "191.250.250.41/32, 200.10.0.0/16"
# ===============================
def safe_client_ip() -> str:
    """
    Obtém o IP do cliente via headers do proxy do Streamlit Cloud.
    Se não conseguir, retorna string vazia.
    """
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        ctx = get_script_run_ctx()
        if not ctx or not hasattr(ctx, "request"):
            return ""

        req = ctx.request
        headers = req.headers or {}

        xff = headers.get("X-Forwarded-For") or headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()

        xrip = headers.get("X-Real-IP") or headers.get("x-real-ip")
        if xrip:
            return xrip.strip()

        return ""
    except Exception:
        return ""


def _parse_ip_ranges(raw: str) -> List[ipaddress._BaseNetwork]:
    raw = (raw or "").strip()
    if not raw:
        return []

    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    nets: List[ipaddress._BaseNetwork] = []
    for p in parts:
        try:
            nets.append(ipaddress.ip_network(p, strict=False))
        except Exception:
            # Se vier um IP puro sem /32, tenta converter automaticamente
            try:
                ip = ipaddress.ip_address(p)
                if ip.version == 4:
                    nets.append(ipaddress.ip_network(f"{p}/32", strict=False))
                else:
                    nets.append(ipaddress.ip_network(f"{p}/128", strict=False))
            except Exception:
                continue
    return nets


def is_restaurant_ip_allowed() -> bool:
    """
    Se RESTAURANT_ALLOWED_IP_RANGES não estiver configurado, não bloqueia nada.
    Se estiver configurado e não conseguir identificar IP do cliente, bloqueia.
    """
    raw = _get_secret("RESTAURANT_ALLOWED_IP_RANGES", "")
    raw = norm_text(raw)
    if not raw:
        return True

    nets = _parse_ip_ranges(raw)
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


def normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    for c in df.columns:
        df[c] = df[c].apply(norm_text)
    return df


def to_int(x, default: int = 0) -> int:
    s = norm_text(x)
    if s == "":
        return default
    try:
        return int(float(s))
    except Exception:
        return default


def to_float(x) -> Optional[float]:
    s = norm_text(x).replace("R$", "").replace(".", "").replace(",", ".").strip()
    if s == "":
        return None
    try:
        return float(s)
    except Exception:
        return None


def sheet_hash(df: pd.DataFrame) -> str:
    payload = df.fillna("").astype(str).to_csv(index=False)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:10]


def _decode_csv_bytes(raw: bytes) -> str:
    # Google Sheets frequentemente vem como UTF-8 (às vezes com BOM)
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    # fallback (evita crash, mas o ideal é nunca chegar aqui)
    return raw.decode("cp1252", errors="replace")


@st.cache_data(ttl=45)
def load_csv_from_url(url: str) -> pd.DataFrame:
    if not url or "docs.google.com/spreadsheets" not in url:
        raise ValueError("URL inválida ou não configurada.")

    r = requests.get(url, timeout=30)
    r.raise_for_status()

    # IMPORTANTE: não use r.text (encoding pode ser inferido errado).
    csv_text = _decode_csv_bytes(r.content)

    # dtype=str e keep_default_na=False evitam NaN quebrando textos
    return pd.read_csv(io.StringIO(csv_text), dtype=str, keep_default_na=False)


def make_key_for_pratos(prato_ids: List[str]) -> str:
    ids_sorted = sorted([norm_text(x) for x in prato_ids if norm_text(x)])
    return "|".join(ids_sorted)


def is_wine_available_now(w: Dict) -> bool:
    ativo = to_int(w.get("ativo", w.get("active", 0)), 0)
    est = to_int(w.get("estoque", 0), 0)
    return ativo == 1 and est > 0


def set_page_style():
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon="🍷",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(
        f"""
        <style>
        .stApp {{
            background: {BRAND_BG};
        }}
        h1, h2, h3, h4 {{
            color: {BRAND_BLUE};
        }}
        .yvora-subtitle {{
            color: {BRAND_MUTED};
            font-size: 1.05rem;
            margin-top: -8px;
        }}
        .yvora-card {{
            background: {BRAND_CARD};
            border-radius: 16px;
            padding: 18px 18px;
            border: 1px solid rgba(14,42,71,0.10);
        }}
        .yvora-warn {{
            background: {BRAND_WARN};
            border-radius: 12px;
            padding: 14px 16px;
            border: 1px solid rgba(14,42,71,0.08);
        }}
        .yvora-pill {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 999px;
            border: 1px solid rgba(14,42,71,0.20);
            color: {BRAND_BLUE};
            font-size: 0.85rem;
            margin-right: 6px;
            background: rgba(255,255,255,0.50);
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def sidebar_brand():
    with st.sidebar:
        render_logo(use_container_width=True)
        st.caption("YVORA - Meat & Cheese Lab")


def dm_login_block() -> bool:
    admin_password = _get_secret("ADMIN_PASSWORD", "")
    if "dm" not in st.session_state:
        st.session_state.dm = False

    with st.sidebar:
        st.markdown("### Acesso DM")

        # [AJUSTE PONTUAL] Restringe o login DM ao IP do restaurante quando a regra estiver configurada
        # Não muda o resto da lógica: apenas bloqueia o botão Entrar fora do IP permitido.
        ip_rule_on = bool(norm_text(_get_secret("RESTAURANT_ALLOWED_IP_RANGES", "")))
        allowed_here = is_restaurant_ip_allowed()

        if ip_rule_on and not allowed_here and not st.session_state.dm:
            st.error("Acesso DM permitido apenas na rede do restaurante.")
            st.caption(f"IP detectado: {safe_client_ip() or 'indisponível'}")
            return False

        if st.session_state.dm:
            st.success("Modo DM ativo")
            if st.button("Sair do DM", use_container_width=True):
                st.session_state.dm = False
                st.rerun()
        else:
            pwd = st.text_input("Senha", type="password", placeholder="Digite a senha do DM")
            if st.button("Entrar", use_container_width=True):
                if pwd and admin_password and pwd == admin_password:
                    st.session_state.dm = True
                    st.rerun()
                else:
                    st.error("Senha inválida.")

    return bool(st.session_state.dm)


def header_area():
    col1, col2 = st.columns([1, 3], vertical_alignment="center")
    with col1:
        render_logo(width=120)
    with col2:
        st.markdown("# Wine Pairing")
        st.markdown(
            "<div class='yvora-subtitle'>Harmonização de vinhos com carnes e queijos, no padrão YVORA.</div>",
            unsafe_allow_html=True,
        )


def load_all_data() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    menu_url = _get_secret("MENU_SHEET_URL", "")
    wines_url = _get_secret("WINES_SHEET_URL", "")
    pairings_url = _get_secret("PAIRINGS_SHEET_URL", "")

    if not menu_url:
        raise ValueError("MENU_SHEET_URL não configurado.")
    if not wines_url:
        raise ValueError("WINES_SHEET_URL não configurado.")
    if not pairings_url:
        raise ValueError("PAIRINGS_SHEET_URL não configurado.")

    menu_df = normalize_cols(load_csv_from_url(menu_url))
    wines_df = normalize_cols(load_csv_from_url(wines_url))
    pair_df = normalize_cols(load_csv_from_url(pairings_url))
    return menu_df, wines_df, pair_df


def standardize_menu(menu_df: pd.DataFrame) -> pd.DataFrame:
    df = menu_df.copy()

    def pick(opts: List[str]) -> str:
        for c in opts:
            if c in df.columns:
                return c
        return ""

    c_id = pick(["id_prato", "id", "prato_id"])
    c_nome = pick(["nome_prato", "prato", "nome", "title"])
    c_desc = pick(["descricao_prato", "descricao", "descrição", "desc"])
    c_ativo = pick(["ativo", "active", "status"])

    if "id" in df.columns and not c_id:
        c_id = "id"
    if "prato" in df.columns and not c_nome:
        c_nome = "prato"
    if "descrição" in df.columns and not c_desc:
        c_desc = "descrição"

    out = pd.DataFrame()
    out["id_prato"] = df[c_id] if c_id else ""
    out["nome_prato"] = df[c_nome] if c_nome else ""
    out["descricao_prato"] = df[c_desc] if c_desc else ""
    out["ativo"] = df[c_ativo] if c_ativo else "1"

    out["id_prato"] = out["id_prato"].apply(norm_text)
    out["nome_prato"] = out["nome_prato"].apply(norm_text)
    out["descricao_prato"] = out["descricao_prato"].apply(norm_text)
    out["ativo"] = out["ativo"].apply(lambda x: 1 if norm_text(x).lower() in ["1", "1.0", "true", "sim"] else 0)

    m = out["id_prato"].eq("")
    out.loc[m, "id_prato"] = out.loc[m, "nome_prato"]

    out = out[(out["nome_prato"] != "") & (out["ativo"] == 1)].copy()
    return out.drop_duplicates(subset=["id_prato", "nome_prato"])


def standardize_wines(wines_df: pd.DataFrame) -> pd.DataFrame:
    df = wines_df.copy()

    def pick(opts: List[str]) -> str:
        for c in opts:
            if c in df.columns:
                return c
        return ""

    c_id = pick(["wine_id", "id_vinho", "id", "vinho_id"])
    c_nome = pick(["wine_name", "nome_vinho", "vinho", "nome"])
    c_price = pick(["price", "preco", "preço", "valor"])
    c_stock = pick(["estoque", "stock", "qtd", "quantidade"])
    c_active = pick(["active", "ativo", "status"])

    out = pd.DataFrame()
    out["id_vinho"] = df[c_id] if c_id else ""
    out["nome_vinho"] = df[c_nome] if c_nome else ""
    out["preco_num"] = df[c_price].apply(to_float) if c_price else None
    out["estoque"] = df[c_stock].apply(lambda x: to_int(x, 0)) if c_stock else 0
    out["ativo"] = (
        df[c_active].apply(lambda x: 1 if norm_text(x).lower() in ["1", "1.0", "true", "sim"] else 0) if c_active else 0
    )

    out["id_vinho"] = out["id_vinho"].apply(norm_text)
    out["nome_vinho"] = out["nome_vinho"].apply(norm_text)
    m = out["id_vinho"].eq("")
    out.loc[m, "id_vinho"] = out.loc[m, "nome_vinho"]

    return out[out["nome_vinho"] != ""].drop_duplicates(subset=["id_vinho", "nome_vinho"])


def standardize_pairings(pair_df: pd.DataFrame) -> pd.DataFrame:
    p = pair_df.copy()
    for c in ["chave_pratos", "id_vinho", "nome_vinho", "rotulo_valor"]:
        if c not in p.columns:
            p[c] = ""
    if "ativo" in p.columns:
        p["ativo"] = p["ativo"].apply(lambda x: 1 if norm_text(x).lower() in ["1", "1.0", "true", "sim"] else 0)
    else:
        p["ativo"] = 1
    return p[p["ativo"] == 1].copy()


def render_recos_block(title: str, p_subset: pd.DataFrame):
    st.markdown("<div class='yvora-card'>", unsafe_allow_html=True)
    st.markdown(f"#### {title}")

    order = {"$$$": 0, "$$": 1, "$": 2}
    p_subset = p_subset.copy()
    p_subset["ord"] = p_subset["rotulo_valor"].apply(lambda x: order.get(norm_text(x), 9))
    p_subset = p_subset.sort_values(["ord", "nome_vinho"], ascending=True).head(3)

    for _, row in p_subset.iterrows():
        rot = norm_text(row.get("rotulo_valor", "$")) or "$"
        nome_vinho = norm_text(row.get("nome_vinho", ""))

        st.markdown(f"<span class='yvora-pill'>{rot}</span>", unsafe_allow_html=True)
        st.markdown(f"**{nome_vinho}**")

        frase = norm_text(row.get("frase_mesa", ""))
        if frase:
            st.write(frase)

        por_vale = norm_text(row.get("por_que_vale", ""))
        if por_vale:
            st.caption(por_vale)

        with st.expander("Ver detalhes técnicos"):
            pc = norm_text(row.get("por_que_carne", ""))
            pq = norm_text(row.get("por_que_queijo", ""))
            pcombo = norm_text(row.get("por_que_combo", ""))
            if pc:
                st.write(f"**Carne/ingrediente:** {pc}")
            if pq:
                st.write(f"**Queijo:** {pq}")
            if pcombo:
                st.write(f"**Conjunto:** {pcombo}")

        st.divider()

    st.markdown("</div>", unsafe_allow_html=True)


def render_client(menu: pd.DataFrame, wines: pd.DataFrame, pairings: pd.DataFrame):
    st.markdown("## Escolha seus pratos")
    st.markdown(
        "<div class='yvora-subtitle'>Selecione 1 ou 2 pratos. As sugestões são filtradas pelo estoque atualizado no momento da consulta.</div>",
        unsafe_allow_html=True,
    )
    st.write("")

    selected_names = st.multiselect(
        "Selecione 1 ou 2 pratos",
        options=menu["nome_prato"].tolist(),
        max_selections=2,
        placeholder="Digite para buscar no menu",
    )

    if not selected_names:
        st.info("Selecione ao menos 1 prato para ver as sugestões.")
        return

    selected = menu[menu["nome_prato"].isin(selected_names)].copy()
    selected_ids = selected["id_prato"].tolist()

    wines_dict = wines.to_dict(orient="records")
    available_ids = set([w["id_vinho"] for w in wines_dict if is_wine_available_now(w)])

    if len(selected_ids) == 2:
        key_pair = make_key_for_pratos(selected_ids)
        p_pair = pairings[pairings["chave_pratos"].astype(str).str.strip() == key_pair].copy()
        p_pair = p_pair[p_pair["id_vinho"].isin(available_ids)].copy()

        if p_pair.empty:
            st.markdown(
                "<div class='yvora-warn'><b>Sem recomendação para o conjunto agora.</b><br>Esta combinação ainda não foi gerada ou os vinhos sugeridos estão sem estoque.</div>",
                unsafe_allow_html=True,
            )
        else:
            render_recos_block("Para os 2 pratos (equilíbrio do conjunto)", p_pair)

        st.write("")

    st.markdown("### Melhor por prato")
    for pid in selected_ids:
        key_single = make_key_for_pratos([pid])
        p_one = pairings[pairings["chave_pratos"].astype(str).str.strip() == key_single].copy()
        p_one = p_one[p_one["id_vinho"].isin(available_ids)].copy()

        prato_nome = menu[menu["id_prato"] == pid]["nome_prato"].iloc[0]

        if p_one.empty:
            st.markdown(
                f"<div class='yvora-warn'><b>{prato_nome}:</b> sem sugestão disponível agora.</div>",
                unsafe_allow_html=True,
            )
            continue

        render_recos_block(prato_nome, p_one)


def render_dm(menu: pd.DataFrame, wines: pd.DataFrame, pairings: pd.DataFrame):
    st.markdown("## DM")
    st.markdown(
        "<div class='yvora-subtitle'>Diagnóstico rápido de dados e cobertura de recomendações.</div>",
        unsafe_allow_html=True,
    )

    st.write(f"Menu hash: `{sheet_hash(menu)}`")
    st.write(f"Vinhos hash: `{sheet_hash(wines)}`")
    st.write(f"Pairings hash: `{sheet_hash(pairings)}`")

    wines_dict = wines.to_dict(orient="records")
    available_ids = set([w["id_vinho"] for w in wines_dict if is_wine_available_now(w)])
    st.write(f"Vinhos disponíveis agora: **{len(available_ids)}**")
    st.write(f"Linhas de pairings ativas: **{len(pairings)}**")


def main():
    set_page_style()
    sidebar_brand()
    dm = dm_login_block()
    header_area()

    try:
        menu_df, wines_df, pair_df = load_all_data()
        menu = standardize_menu(menu_df)
        wines = standardize_wines(wines_df)
        pairings = standardize_pairings(pair_df)
    except Exception as e:
        st.markdown(
            f"<div class='yvora-warn'><b>Erro ao carregar dados:</b><br>{e}</div>",
            unsafe_allow_html=True,
        )
        st.stop()

    if dm:
        render_dm(menu, wines, pairings)
    else:
        render_client(menu, wines, pairings)


if __name__ == "__main__":
    main()
