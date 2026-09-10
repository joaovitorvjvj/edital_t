"""
Job Worker unificado — Extração + Consolidação
Edital FAPESC/SEPLAN 69/2025

Uma única service task "extrair_e_consolidar":
  1. Recebe o array 'documentos' do formulário
  2. Faz o loop em Python (sem multi-instance do Zeebe)
  3. Para cada documento: extrai texto (pdfplumber/Tesseract) e chama o Groq
  4. Consolida tudo nas variáveis que a DMN espera
  5. Devolve score de confiança + variáveis consolidadas

Rodar: python worker.py
"""

import asyncio
import base64
import io
import json
import logging
from datetime import date, datetime

import os
import requests
import pdfplumber
from dotenv import load_dotenv
from pyzeebe import ZeebeWorker, create_camunda_cloud_channel

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Configuração (lida do arquivo .env) ───────────────────────
CLUSTER_ID    = os.getenv("CAMUNDA_CLUSTER_ID")
REGION        = os.getenv("CAMUNDA_REGION", "cle-1")
CLIENT_ID     = os.getenv("CAMUNDA_CLIENT_ID")
CLIENT_SECRET = os.getenv("CAMUNDA_CLIENT_SECRET")
GROQ_API_KEY  = os.getenv("GROQ_API_KEY")
GROQ_MODEL    = "openai/gpt-oss-20b"

HIERARQUIA_CERT = {"CBPP": 4, "CBPA": 3, "SixSigma": 3, "OCEB": 3, "Nenhuma": 0}

PROMPTS = {
    "diploma_graduacao": "Diploma de graduação. Extraia JSON: {curso, instituicao, dataConclusao}.",
    "diploma_especializacao": "Diploma de especialização lato sensu. Extraia JSON: {titulo, area, instituicao, dataConclusao, cargaHorariaHoras}.",
    "diploma_mestrado": "Diploma de mestrado. Extraia JSON: {titulo, area, instituicao, dataConclusao}.",
    "diploma_doutorado": "Diploma de doutorado. Extraia JSON: {titulo, area, instituicao, dataConclusao}.",
    "certificado_curso_bpm": "Certificado de curso em BPM ou Seis Sigma. Extraia JSON: {nomeCurso, instituicao, cargaHorariaHoras, dataConclusao}.",
    "certificado_curso_bpms": "Certificado de curso de automação de processos com sistema BPMS. Extraia JSON: {nomeCurso, sistemaBpms, instituicao, cargaHorariaHoras, dataConclusao}.",
    "certificacao_profissional": "Certificação profissional BPM/Seis Sigma (CBPP, CBPA, OCEB, Six Sigma). Extraia JSON: {tipoCertificacao, emissor, dataEmissao, dataValidade}.",
    "comprovante_vinculo": "Comprovante de vínculo empregatício. Extraia JSON: {cargo, empresa, cnpj, dataInicio, dataFim, setorPublico, areaBpm, areaCorrelata}.",
    "curriculo_lattes": "Currículo Lattes. Extraia JSON: {nomeCompleto, dataAtualizacao, identificadorLattes}.",
}


def extrair_texto(conteudo_bytes: bytes) -> tuple:
    texto = ""
    try:
        with pdfplumber.open(io.BytesIO(conteudo_bytes)) as pdf:
            for pagina in pdf.pages:
                t = pagina.extract_text()
                if t:
                    texto += t + "\n"
    except Exception as e:
        log.warning(f"pdfplumber falhou: {e}")

    if len(texto.strip()) > 100:
        return texto.strip(), "pdfplumber"

    try:
        import pytesseract
        with pdfplumber.open(io.BytesIO(conteudo_bytes)) as pdf:
            for pagina in pdf.pages:
                img = pagina.to_image(resolution=300).original
                texto += pytesseract.image_to_string(img, lang="por") + "\n"
        return texto.strip(), "tesseract"
    except Exception as e:
        log.warning(f"Tesseract falhou: {e}")
        return texto.strip() or "[texto não extraído]", "falhou"


def chamar_groq(texto: str, tipo: str) -> dict:
    prompt = PROMPTS.get(tipo, "Extraia as informações principais em JSON.")
    payload = {
        "model": GROQ_MODEL,
        "temperature": 0.1,
        "max_tokens": 600,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": prompt + " Responda SOMENTE com JSON válido."},
            {"role": "user", "content": f"Texto do documento:\n\n{texto[:4000]}"}
        ]
    }
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json=payload, timeout=30
    )
    resp.raise_for_status()
    return json.loads(resp.json()["choices"][0]["message"]["content"])


def score_documento(dados: dict) -> float:
    if not dados:
        return 0.0
    presentes = sum(1 for v in dados.values() if v is not None and v != "")
    return round(min(presentes / max(len(dados), 1), 1.0), 2)


def calcular_anos(data_inicio: str, data_fim: str) -> float:
    def parse(d: str) -> date:
        d = str(d or "").strip().replace("null", "").replace("None", "")
        if not d:
            return date.today()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y"):
            try:
                return datetime.strptime(d, fmt).date()
            except ValueError:
                continue
        return date.today()
    delta = (parse(data_fim) - parse(data_inicio)).days / 365.25
    return max(round(delta, 2), 0.0)


def melhor_certificacao(certs: list) -> str:
    melhor, peso = "Nenhuma", 0
    for c in certs:
        p = HIERARQUIA_CERT.get(c, 0)
        if p > peso:
            peso, melhor = p, c
    return melhor


def processar_documentos(documentos: list) -> dict:
    """Loop em Python sobre todos os documentos — extrai e consolida."""
    tempo_pub_bpm = tempo_priv_bpm = tempo_pub_outras = tempo_outras = 0.0
    qtd_cursos = 0
    possui_bpms = possui_dout = possui_mest = False
    n_esp_bpm = n_esp_outras = 0
    certs = []
    scores = []

    for doc in documentos:
        if not doc:
            continue
        tipo   = doc.get("tipoDocumento")
        nome   = doc.get("nomeArquivo")
        b64    = doc.get("conteudoBase64", "") or ""

        log.info(f"  Documento: {nome} (tipo: {tipo})")

        if b64:
            texto, metodo = extrair_texto(base64.b64decode(b64))
        else:
            texto = (f"Documento tipo {tipo}. Instituição: UFSC. Data: 2020-06-15. "
                     f"Cargo: Analista de Processos. Início: 2018-01-01 Fim: 2024-12-31. "
                     f"Setor público. Área BPM. Carga horária: 40h.")
            metodo = "mock"

        dados = chamar_groq(texto, tipo)
        s = score_documento(dados)
        scores.append(s)
        log.info(f"    Score: {s} | Método: {metodo}")

        # Consolidação por tipo
        if tipo == "diploma_doutorado":
            possui_dout = True
        elif tipo == "diploma_mestrado":
            possui_mest = True
        elif tipo == "diploma_especializacao":
            area = str(dados.get("area", "")).lower()
            if any(p in area for p in ["bpm", "processo", "gpn"]):
                n_esp_bpm += 1
            else:
                n_esp_outras += 1
        elif tipo == "certificacao_profissional":
            tc = str(dados.get("tipoCertificacao", "")).upper()
            if "CBPP" in tc: certs.append("CBPP")
            elif "CBPA" in tc: certs.append("CBPA")
            elif "OCEB" in tc: certs.append("OCEB")
            elif "SIGMA" in tc or "SIX" in tc: certs.append("SixSigma")
        elif tipo == "certificado_curso_bpm":
            try:
                if float(dados.get("cargaHorariaHoras", 0)) >= 16:
                    qtd_cursos += 1
            except (TypeError, ValueError): pass
        elif tipo == "certificado_curso_bpms":
            try:
                if float(dados.get("cargaHorariaHoras", 0)) >= 16:
                    possui_bpms = True
            except (TypeError, ValueError): pass
        elif tipo == "comprovante_vinculo":
            anos = calcular_anos(dados.get("dataInicio"), dados.get("dataFim"))
            pub = dados.get("setorPublico", False)
            bpm = dados.get("areaBpm", False)
            corr = dados.get("areaCorrelata", False)
            if pub and bpm: tempo_pub_bpm += anos
            elif not pub and bpm: tempo_priv_bpm += anos
            elif pub and not bpm: tempo_pub_outras += anos
            elif corr: tempo_outras += anos

    score_final = round(sum(scores) / max(len(scores), 1), 2) if scores else 0.5

    return {
        "scoreConfianca":           score_final,
        "observacaoLLM":            f"Consolidado de {len(documentos)} documento(s). Score médio: {score_final}",
        "possuiDoutorado":          possui_dout,
        "possuiMestrado":           possui_mest,
        "numEspecializacoesBpm":    n_esp_bpm,
        "numEspecializacoesOutras": n_esp_outras,
        "tempoPublicoBpmAnos":      round(tempo_pub_bpm, 2),
        "tempoPrivadoBpmAnos":      round(tempo_priv_bpm, 2),
        "tempoPublicoOutrasAnos":   round(tempo_pub_outras, 2),
        "tempoOutrasAreasAnos":     round(tempo_outras, 2),
        "tempoExperienciaAnos":     round(tempo_pub_bpm + tempo_priv_bpm + tempo_pub_outras + tempo_outras, 2),
        "quantidadeCursosBpm16h":   qtd_cursos,
        "possuiCursoAutomacaoBpms": possui_bpms,
        "melhorCertificacao":       melhor_certificacao(certs) if certs else "Nenhuma",
    }


async def main():
    channel = create_camunda_cloud_channel(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
        cluster_id=CLUSTER_ID, region=REGION
    )
    worker = ZeebeWorker(channel)

    @worker.task(task_type="extrair_e_consolidar", timeout_ms=120000, max_jobs_to_activate=3)
    async def extrair_e_consolidar(documentos: list = None):
        if not documentos:
            documentos = []
        log.info(f"Processando {len(documentos)} documento(s)...")
        resultado = processar_documentos(documentos)
        log.info(f"Resultado: score={resultado['scoreConfianca']} "
                 f"dout={resultado['possuiDoutorado']} mest={resultado['possuiMestrado']} "
                 f"cert={resultado['melhorCertificacao']} cursos={resultado['quantidadeCursosBpm16h']}")
        return resultado

    log.info("Worker iniciado — aguardando tarefas...")
    await worker.work()


if __name__ == "__main__":
    asyncio.run(main())
