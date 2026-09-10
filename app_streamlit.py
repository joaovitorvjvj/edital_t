"""
Portal de inscrição — Streamlit
Edital FAPESC/SEPLAN 69/2025

Interface pública de inscrição que:
  1. Coleta dados do candidato
  2. Aceita upload de múltiplos PDFs, cada um com seu tipo
  3. Converte cada PDF para base64
  4. Dispara o processo no Camunda via API REST

Rodar: streamlit run app_streamlit.py
"""

import os
import base64
import time
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ── Configuração do cluster (lida do arquivo .env) ────────────
CLUSTER_ID    = os.getenv("CAMUNDA_CLUSTER_ID")
REGION        = os.getenv("CAMUNDA_REGION", "cle-1")
CLIENT_ID     = os.getenv("CAMUNDA_CLIENT_ID")
CLIENT_SECRET = os.getenv("CAMUNDA_CLIENT_SECRET")
PROCESS_ID    = "Process_InscricaoEdital69"

OAUTH_URL  = "https://login.cloud.camunda.io/oauth/token"
ZEEBE_URL  = f"https://{REGION}.api.camunda.io/{CLUSTER_ID}/v2/process-instances"

TIPOS_DOCUMENTO = {
    "Diploma de graduação": "diploma_graduacao",
    "Diploma de especialização": "diploma_especializacao",
    "Diploma de mestrado": "diploma_mestrado",
    "Diploma de doutorado": "diploma_doutorado",
    "Certificado curso BPM/Six Sigma (>=16h)": "certificado_curso_bpm",
    "Certificado curso automação BPMS (>=16h)": "certificado_curso_bpms",
    "Certificação profissional (CBPP, CBPA, OCEB, Six Sigma)": "certificacao_profissional",
    "Comprovante de vínculo (CTPS, contrato, portaria, atestado)": "comprovante_vinculo",
    "Currículo Lattes": "curriculo_lattes",
}

VAGAS = {
    "Vaga 01 — Especialista em Gestão de Processos": "Vaga01",
    "Vaga 02 — Especialista em Direito": "Vaga02",
    "Vaga 03 — Especialista em Tecnologia da Inovação": "Vaga03",
}

TITULACOES = {
    "Doutorado": "Doutorado",
    "Mestrado": "Mestrado",
    "Especialização lato sensu": "Especialista",
    "Graduação sem certificação": "GraduacaoSemCertificacao",
    "Graduação com certificação": "GraduacaoComCertificacao",
}


@st.cache_data(ttl=3000)
def get_token():
    resp = requests.post(OAUTH_URL, data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "audience": "zeebe.camunda.io"
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def disparar_processo(variaveis: dict):
    token = get_token()
    payload = {"processDefinitionId": PROCESS_ID, "variables": variaveis}
    resp = requests.post(
        ZEEBE_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload
    )
    return resp


# ── Interface ─────────────────────────────────────────────────
st.set_page_config(page_title="Inscrição NUPROC — FAPESC", page_icon="📋", layout="centered")

st.title("Portal de inscrição — NUPROC")
st.caption("Edital FAPESC/SEPLAN 69/2025 — Programa Catarinense de Inovação e Desenvolvimento Científico")

st.divider()

st.subheader("Dados pessoais")
nome = st.text_input("Nome completo")
col1, col2 = st.columns(2)
with col1:
    vaga_label = st.selectbox("Vaga pretendida", list(VAGAS.keys()))
with col2:
    idade = st.number_input("Idade", min_value=18, max_value=100, value=30)

st.subheader("Titulação")
tit_label = st.selectbox("Maior titulação obtida", list(TITULACOES.keys()))
tempo_exp = st.number_input("Tempo de experiência total na área da vaga (anos)", min_value=0, value=0)

st.subheader("Documentos comprobatórios")
st.caption("Anexe cada documento em PDF e informe o tipo. O sistema extrai e pontua automaticamente.")

if "docs" not in st.session_state:
    st.session_state.docs = [0]

def add_doc():
    st.session_state.docs.append(max(st.session_state.docs) + 1 if st.session_state.docs else 0)

def rm_doc(i):
    st.session_state.docs.remove(i)

documentos_upload = []
for i in st.session_state.docs:
    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        with c1:
            tipo_label = st.selectbox("Tipo do documento", list(TIPOS_DOCUMENTO.keys()), key=f"tipo_{i}")
        with c2:
            st.write("")
            st.write("")
            if len(st.session_state.docs) > 1:
                st.button("Remover", key=f"rm_{i}", on_click=rm_doc, args=(i,))
        arquivo = st.file_uploader("Arquivo PDF", type=["pdf"], key=f"file_{i}")
        documentos_upload.append({"tipo_label": tipo_label, "arquivo": arquivo})

st.button("➕ Adicionar documento", on_click=add_doc)

st.divider()

if st.button("Enviar inscrição", type="primary", use_container_width=True):
    if not nome:
        st.error("Preencha o nome completo.")
    elif not any(d["arquivo"] for d in documentos_upload):
        st.error("Anexe pelo menos um documento em PDF.")
    else:
        documentos = []
        for d in documentos_upload:
            if d["arquivo"] is not None:
                conteudo = base64.b64encode(d["arquivo"].read()).decode()
                documentos.append({
                    "tipoDocumento": TIPOS_DOCUMENTO[d["tipo_label"]],
                    "nomeArquivo": d["arquivo"].name,
                    "observacaoDocumento": "",
                    "conteudoBase64": conteudo
                })

        variaveis = {
            "nomeCandidato": nome,
            "vagaPretendida": VAGAS[vaga_label],
            "idade": int(idade),
            "titulacaoObtida": TITULACOES[tit_label],
            "tempoExperienciaAnos": int(tempo_exp),
            "documentos": documentos
        }

        with st.spinner("Enviando inscrição e processando documentos..."):
            try:
                resp = disparar_processo(variaveis)
                if resp.status_code in (200, 201):
                    data = resp.json()
                    key = data.get("processInstanceKey", data.get("key", "?"))
                    st.success(f"Inscrição enviada com sucesso! Protocolo: {key}")
                    st.info(f"{len(documentos)} documento(s) enviado(s) para análise. "
                            "O sistema está extraindo os dados e calculando a pontuação.")
                    st.balloons()
                else:
                    st.error(f"Erro ao enviar (HTTP {resp.status_code}): {resp.text[:300]}")
            except Exception as e:
                st.error(f"Falha na conexão com o Camunda: {e}")
