"""Baixador local de XMLs da NFS-e Nacional (ADN).

O código usa a API de distribuição do contribuinte. O portal entrega os
documentos por NSU; portanto o filtro de datas ocorre localmente, após o XML
ser recebido. A senha do A1 nunca é persistida.
"""
from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import json
import queue
import re
import subprocess
import threading
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

PRODUCAO = "https://adn.nfse.gov.br"
RESTRITA = "https://adn.producaorestrita.nfse.gov.br"
MAX_REQUISICOES = 50_000
PORTAL_LOGIN = "https://www.nfse.gov.br/EmissorNacional/Login?ReturnUrl=%2fEmissorNacional"
PORTAL_GOVBR = "https://www.nfse.gov.br/EmissorNacional/AcessoGovBR/AcessarGovBR"


@dataclass(frozen=True)
class CertificadoInstalado:
    armazenamento: str
    caminho: str
    impressao_digital: str
    assunto: str
    validade: str

    @property
    def rotulo(self) -> str:
        return f"{self.assunto} — vence {self.validade} ({self.armazenamento})"


def _powershell(script: str, *argumentos: str) -> str:
    """Executa PowerShell somente no Windows e retorna o texto produzido."""
    if __import__("sys").platform != "win32":
        raise RuntimeError("A busca de certificado instalado está disponível somente no Windows.")
    resultado = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script, *argumentos],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    saida = (resultado.stdout or "").strip()
    erro = (resultado.stderr or "").strip()
    if resultado.returncode != 0:
        raise RuntimeError(erro or "Não foi possível acessar o repositório de certificados do Windows.")
    return saida


def certificados_instalados() -> tuple[list[CertificadoInstalado], int, int]:
    """Lista certificados do repositório Pessoal usando a API criptográfica do Windows.

    Retorna os certificados utilizáveis, a quantidade total encontrada e quantos
    deles informam possuir chave privada. Evita depender da enumeração recursiva
    do provedor virtual ``Cert:`` do PowerShell, que varia entre instalações.
    """
    script = r'''
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $resultados = @()
    $total = 0
    $comChave = 0
    $fontes = @(
      @{ caminho = 'Cert:\CurrentUser\My'; rotulo = 'Usuário atual / Pessoal' },
      @{ caminho = 'Cert:\LocalMachine\My'; rotulo = 'Computador local / Pessoal' }
    )
    foreach ($fonte in $fontes) {
      # O provedor Cert: é a mesma visão exibida pelo certmgr.msc. Esta forma
      # funciona também em versões antigas do Windows PowerShell/.NET.
      $itens = @(Get-ChildItem -LiteralPath $fonte.caminho -ErrorAction SilentlyContinue)
      foreach ($cert in $itens) {
        $total++
        if ($cert.HasPrivateKey) {
          $comChave++
          $resultados += [PSCustomObject]@{
            loja = $fonte.rotulo
            caminho = "$($fonte.caminho)\$($cert.Thumbprint)"
            thumbprint = [string]$cert.Thumbprint
            subject = [string]$cert.Subject
            validade = $cert.NotAfter.ToString('dd/MM/yyyy')
          }
        }
      }
    }
    [PSCustomObject]@{
      total = $total
      comChave = $comChave
      certificados = @($resultados)
    } | ConvertTo-Json -Compress -Depth 4
    '''
    saida = _powershell(script)
    if not saida:
        return [], 0, 0
    dados = json.loads(saida)
    itens = dados.get("certificados") or [] if isinstance(dados, dict) else []
    if isinstance(itens, dict):
        itens = [itens]
    certificados: list[CertificadoInstalado] = []
    for item in itens:
        if not isinstance(item, dict):
            continue
        loja = str(item.get("loja") or "").strip()
        caminho = str(item.get("caminho") or "").strip()
        thumbprint = str(item.get("thumbprint") or "").strip()
        assunto = str(item.get("subject") or "Certificado sem identificação").strip()
        validade = str(item.get("validade") or "data desconhecida").strip()
        if loja and caminho and thumbprint:
            certificados.append(CertificadoInstalado(loja, caminho, thumbprint, assunto, validade))
    # O mesmo certificado pode aparecer mais de uma vez por causa de links do provedor.
    unicos = {item.caminho: item for item in certificados}
    ordenados = sorted(unicos.values(), key=lambda item: (item.assunto.lower(), item.validade))
    return ordenados, int(dados.get("total") or 0), int(dados.get("comChave") or 0)


@dataclass(frozen=True)
class Filtros:
    documento: str
    inicio: date
    fim: date
    papel: str
    pasta: Path
    ambiente: str
    certificado_instalado: CertificadoInstalado | None


def somente_digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto)


def data_iso(texto: str) -> date:
    return datetime.strptime(texto.strip(), "%d/%m/%Y").date()


def tag_local(elemento: ET.Element) -> str:
    return elemento.tag.rsplit("}", 1)[-1]


def texto_por_tag(raiz: ET.Element, *nomes: str) -> str | None:
    procurados = {nome.lower() for nome in nomes}
    for elemento in raiz.iter():
        if tag_local(elemento).lower() in procurados and elemento.text:
            return elemento.text.strip()
    return None


def chave_nfse(raiz: ET.Element) -> str | None:
    """Obtém a chave de acesso da NFS-e, inclusive do atributo Id de infNFSe."""
    chave = texto_por_tag(raiz, "chNFSe", "chaveAcesso")
    if chave:
        limpa = re.sub(r"[^A-Za-z0-9_-]", "", chave)
        return limpa or None
    for elemento in raiz.iter():
        if tag_local(elemento).lower() != "infnfse":
            continue
        identificador = next(
            (valor for nome, valor in elemento.attrib.items() if tag_local_nome(nome).lower() == "id"),
            None,
        )
        if identificador:
            limpa = re.sub(r"[^A-Za-z0-9_-]", "", identificador)
            # No leiaute nacional, o Id normalmente é "NFS" seguido da chave.
            if limpa.upper().startswith("NFS") and len(limpa) > 3:
                limpa = limpa[3:]
            return limpa or None
    return None


def tag_local_nome(nome: str) -> str:
    return nome.rsplit("}", 1)[-1]


def valor_documento(raiz: ET.Element, papel: str) -> str | None:
    """Localiza CPF ou CNPJ no ator da nota, sem depender do prefixo XML."""
    palavras = {"emitente": ("prest", "emit"), "tomador": ("tom", "adq", "dest")}[papel]
    for bloco in raiz.iter():
        if any(p in tag_local(bloco).lower() for p in palavras):
            for item in bloco.iter():
                if tag_local(item).lower() in {"cpf", "cnpj", "nif"} and item.text:
                    return somente_digitos(item.text)
    return None


def classificar_xml(xml: bytes, filtros: Filtros) -> tuple[str, date, str] | None:
    try:
        raiz = ET.fromstring(xml)
    except ET.ParseError:
        return None
    emissao = texto_por_tag(raiz, "dhEmi", "dEmi", "dataEmissao", "dhProc")
    if not emissao:
        return None
    try:
        competencia = datetime.fromisoformat(emissao.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            competencia = datetime.strptime(emissao[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    if not filtros.inicio <= competencia <= filtros.fim:
        return None
    documento_emitente = valor_documento(raiz, "emitente")
    documento_tomador = valor_documento(raiz, "tomador")
    papel = "emitidas" if documento_emitente == filtros.documento else "tomadas" if documento_tomador == filtros.documento else None
    if not papel or (filtros.papel != "ambos" and filtros.papel != papel):
        return None
    chave = chave_nfse(raiz)
    if not chave:
        # Evita sobrescrita caso um documento inesperado não traga chave reconhecível.
        chave = "sem_chave_" + hashlib.sha256(xml).hexdigest()[:24]
    return papel, competencia, chave[:100]


def possiveis_xmls(valor: Any) -> Iterable[bytes]:
    """Extrai XMLs de respostas JSON/XML, inclusive Base64 e GZip."""
    if isinstance(valor, dict):
        for item in valor.values():
            yield from possiveis_xmls(item)
    elif isinstance(valor, list):
        for item in valor:
            yield from possiveis_xmls(item)
    elif isinstance(valor, str):
        bruto = valor.encode()
        if b"<" in bruto and b">" in bruto:
            yield bruto
            return
        try:
            decodificado = base64.b64decode(valor, validate=True)
            if decodificado.startswith(b"\x1f\x8b"):
                decodificado = gzip.decompress(decodificado)
            if b"<" in decodificado and b">" in decodificado:
                yield decodificado
        except Exception:
            return


def valor_metadado_nsu(resposta: Any, nome_procurado: str) -> int | None:
    """Lê um metadado de paginação sem confundi-lo com o NSU de cada documento."""
    if isinstance(resposta, dict):
        for chave, valor in resposta.items():
            if chave.lower() == nome_procurado.lower():
                try:
                    return int(str(valor))
                except (TypeError, ValueError):
                    pass
        for valor in resposta.values():
            encontrado = valor_metadado_nsu(valor, nome_procurado)
            if encontrado is not None:
                return encontrado
    elif isinstance(resposta, list):
        for valor in resposta:
            encontrado = valor_metadado_nsu(valor, nome_procurado)
            if encontrado is not None:
                return encontrado
    return None


class ClienteADN:
    def __init__(self, filtros: Filtros):
        self.filtros = filtros
        base = RESTRITA if filtros.ambiente == "restrita" else PRODUCAO
        self.url_base = f"{base}/contribuintes/DFe"

    def consultar_nsu(self, nsu: int) -> Any | None:
        if self.filtros.certificado_instalado:
            return self._consultar_nsu_certificado_windows(nsu)
        raise RuntimeError("Selecione um certificado instalado no Windows.")

    def _consultar_nsu_certificado_windows(self, nsu: int) -> Any | None:
        """Faz mTLS pelo repositório do Windows sem exportar a chave nem pedir senha."""
        certificado = self.filtros.certificado_instalado
        assert certificado is not None
        parametro = "cpfConsulta" if len(self.filtros.documento) == 11 else "cnpjConsulta"
        url = f"{self.url_base}/{nsu}?{parametro}={self.filtros.documento}"
        # Os valores são incorporados em Base64 para não depender da passagem de
        # argumentos do Windows PowerShell, que varia entre versões e pode perder
        # caminhos do provedor Cert:. A chave privada continua dentro do Windows.
        thumb_b64 = base64.b64encode(certificado.impressao_digital.encode("utf-8")).decode("ascii")
        url_b64 = base64.b64encode(url.encode("utf-8")).decode("ascii")
        script = rf'''
        $Thumbprint = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{thumb_b64}'))
        $Uri = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{url_b64}'))
        $Thumbprint = ($Thumbprint -replace '[^0-9A-Fa-f]', '').ToUpperInvariant()
        $cert = @(
          Get-ChildItem -LiteralPath 'Cert:\CurrentUser\My' -ErrorAction SilentlyContinue
          Get-ChildItem -LiteralPath 'Cert:\LocalMachine\My' -ErrorAction SilentlyContinue
        ) | Where-Object {{
          (($_.Thumbprint -replace '[^0-9A-Fa-f]', '').ToUpperInvariant() -eq $Thumbprint) -and $_.HasPrivateKey
        }} | Select-Object -First 1
        if ($null -eq $cert) {{ throw 'O certificado selecionado não foi encontrado no Windows ou não possui chave privada disponível.' }}
        try {{
          $resposta = Invoke-WebRequest -Uri $Uri -Method Get -Certificate $cert -UseBasicParsing -Headers @{{ Accept = 'application/json, application/xml' }} -ErrorAction Stop
          $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$resposta.Content)
          [PSCustomObject]@{{ status = [int]$resposta.StatusCode; tipo = [string]$resposta.Headers['Content-Type']; corpo = [Convert]::ToBase64String($bytes) }} | ConvertTo-Json -Compress
        }} catch [System.Net.WebException] {{
          if ($_.Exception.Response) {{
            $status = [int]$_.Exception.Response.StatusCode
            [PSCustomObject]@{{ status = $status; tipo = ''; corpo = '' }} | ConvertTo-Json -Compress
          }} else {{ throw }}
        }}
        '''
        retorno = json.loads(_powershell(script))
        if int(retorno["status"]) in (204, 404):
            return None
        if not 200 <= int(retorno["status"]) < 300:
            raise RuntimeError(f"O ADN retornou HTTP {retorno['status']}.")
        conteudo = base64.b64decode(retorno.get("corpo") or "")
        if "json" in (retorno.get("tipo") or "").lower():
            return json.loads(conteudo.decode("utf-8-sig"))
        return {"xml": conteudo.decode("utf-8-sig")}


def baixar_lote(filtros: Filtros, log: callable) -> dict[str, int]:
    cliente = ClienteADN(filtros)
    destinos = {"emitidas": filtros.pasta / "XML" / "Emitidas", "tomadas": filtros.pasta / "XML" / "Tomadas"}
    for destino in destinos.values():
        destino.mkdir(parents=True, exist_ok=True)
    linhas: list[dict[str, str]] = []
    nsu, consultas, salvos = 0, 0, 0
    vistos: set[str] = set()
    while consultas < MAX_REQUISICOES:
        consultas += 1
        log(f"Consultando NSU {nsu}...")
        payload = cliente.consultar_nsu(nsu)
        if payload is None:
            break
        for xml in possiveis_xmls(payload):
            resultado = classificar_xml(xml, filtros)
            if not resultado:
                continue
            papel, competencia, chave = resultado
            assinatura = hashlib.sha256(xml).hexdigest()
            if assinatura in vistos:
                continue
            vistos.add(assinatura)
            nome = f"{chave}.xml"
            caminho = destinos[papel] / nome
            if caminho.exists() and caminho.read_bytes() == xml:
                continue
            caminho.write_bytes(xml)
            linhas.append({"arquivo": str(caminho.relative_to(filtros.pasta)), "papel": papel, "competencia": competencia.isoformat(), "chave": chave})
            salvos += 1
        # O endpoint recebe o último NSU conhecido e devolve até 50 documentos.
        # É obrigatório avançar pelo ultNSU. maxNSU é apenas o limite da fila;
        # usá-lo como próximo valor pula todos os lotes intermediários.
        novo_nsu = valor_metadado_nsu(payload, "ultNSU")
        max_nsu = valor_metadado_nsu(payload, "maxNSU")
        if novo_nsu is None or novo_nsu <= nsu:
            break
        nsu = novo_nsu
        if max_nsu is not None and nsu >= max_nsu:
            break
    with (filtros.pasta / "resultado.csv").open("w", newline="", encoding="utf-8-sig") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=["arquivo", "papel", "competencia", "chave"])
        escritor.writeheader()
        escritor.writerows(linhas)
    return {"consultas": consultas, "xmls": salvos}


class Janela(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Baixador NFS-e Nacional — XML")
        self.resizable(False, False)
        self.eventos: queue.Queue[tuple[str, str]] = queue.Queue()
        self.vars = {nome: tk.StringVar() for nome in ("documento", "inicio", "fim", "pasta")}
        self.certificados: dict[str, CertificadoInstalado] = {}
        self.certificado_instalado = tk.StringVar()
        self.forma_acesso = tk.StringVar(value="certificado")
        self.papel = tk.StringVar(value="ambos")
        self.ambiente = tk.StringVar(value="producao")
        self._montar()
        self.after(150, self._consumir_eventos)

    def _montar(self) -> None:
        quadro = ttk.Frame(self, padding=16)
        quadro.grid()
        def campo(linha: int, rotulo: str, var: tk.StringVar, senha: bool = False) -> None:
            ttk.Label(quadro, text=rotulo).grid(row=linha, column=0, sticky="w", pady=4)
            ttk.Entry(quadro, textvariable=var, width=46, show="•" if senha else "").grid(row=linha, column=1, sticky="we", pady=4)
        ttk.Label(quadro, text="Forma de acesso").grid(row=0, column=0, sticky="nw", pady=4)
        acessos = ttk.Frame(quadro)
        acessos.grid(row=0, column=1, columnspan=2, sticky="w", pady=4)
        ttk.Radiobutton(acessos, text="Certificado digital", variable=self.forma_acesso, value="certificado", command=self._atualizar_acesso).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(acessos, text="Conta gov.br", variable=self.forma_acesso, value="govbr", command=self._atualizar_acesso).grid(row=1, column=0, sticky="w")
        ttk.Radiobutton(acessos, text="Usuário e senha do Portal", variable=self.forma_acesso, value="portal", command=self._atualizar_acesso).grid(row=2, column=0, sticky="w")
        ttk.Label(quadro, text="Certificado instalado no Windows").grid(row=1, column=0, sticky="w", pady=4)
        self.combo_certificados = ttk.Combobox(quadro, textvariable=self.certificado_instalado, width=43, state="readonly")
        self.combo_certificados.grid(row=1, column=1, sticky="we", pady=4)
        self.botao_buscar = ttk.Button(quadro, text="Buscar", command=self._buscar_certificados)
        self.botao_buscar.grid(row=1, column=2, padx=(8, 0))
        campo(2, "CPF ou CNPJ para consulta", self.vars["documento"])
        campo(3, "Data inicial (dd/mm/aaaa)", self.vars["inicio"])
        campo(4, "Data final (dd/mm/aaaa)", self.vars["fim"])
        campo(5, "Pasta de saída", self.vars["pasta"])
        ttk.Button(quadro, text="Selecionar", command=self._selecionar_pasta).grid(row=5, column=2, padx=(8, 0))
        ttk.Label(quadro, text="Notas").grid(row=6, column=0, sticky="w", pady=(10, 4))
        ttk.Radiobutton(quadro, text="Emitidas e tomadas", variable=self.papel, value="ambos").grid(row=6, column=1, sticky="w", pady=(10, 4))
        ttk.Radiobutton(quadro, text="Somente emitidas", variable=self.papel, value="emitidas").grid(row=7, column=1, sticky="w")
        ttk.Radiobutton(quadro, text="Somente tomadas", variable=self.papel, value="tomadas").grid(row=8, column=1, sticky="w")
        ttk.Label(quadro, text="Ambiente").grid(row=9, column=0, sticky="w", pady=(10, 4))
        ttk.Radiobutton(quadro, text="Produção", variable=self.ambiente, value="producao").grid(row=9, column=1, sticky="w", pady=(10, 4))
        ttk.Radiobutton(quadro, text="Produção restrita (teste)", variable=self.ambiente, value="restrita").grid(row=10, column=1, sticky="w")
        self.botao = ttk.Button(quadro, text="Baixar XMLs", command=self._iniciar)
        self.botao.grid(row=11, column=1, sticky="e", pady=(16, 4))
        self.status = tk.StringVar(value="Clique em Buscar e selecione um e-CPF/e-CNPJ A1 ou A3 instalado no Windows.")
        ttk.Label(quadro, textvariable=self.status, wraplength=520).grid(row=12, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _atualizar_acesso(self) -> None:
        certificado = self.forma_acesso.get() == "certificado"
        self.combo_certificados.configure(state="readonly" if certificado else "disabled")
        self.botao_buscar.configure(state="normal" if certificado else "disabled")
        if certificado:
            self.botao.configure(text="Baixar XMLs")
            self.status.set("Clique em Buscar e selecione um e-CPF/e-CNPJ A1 ou A3 instalado no Windows.")
        elif self.forma_acesso.get() == "govbr":
            self.botao.configure(text="Abrir acesso gov.br")
            self.status.set("O login será feito na página oficial do gov.br. O aplicativo não lê nem salva a senha.")
        else:
            self.botao.configure(text="Abrir Portal NFS-e")
            self.status.set("O usuário e a senha serão informados somente na página oficial do Portal NFS-e.")

    def _buscar_certificados(self) -> None:
        try:
            encontrados, total, com_chave = certificados_instalados()
        except Exception as erro:
            messagebox.showerror("Busca de certificados", str(erro))
            return
        self.certificados = {item.rotulo: item for item in encontrados}
        self.combo_certificados["values"] = list(self.certificados)
        if encontrados:
            self.certificado_instalado.set(encontrados[0].rotulo)
            self.status.set(f"{len(encontrados)} certificado(s) encontrado(s). Aceita e-CPF/e-CNPJ A1 e A3; o A3 pode solicitar PIN.")
        else:
            self.certificado_instalado.set("")
            messagebox.showinfo(
                "Busca de certificados",
                f"O Windows informou {total} certificado(s) no repositório Pessoal, mas {com_chave} com chave privada disponível.\n\n"
                "Abra certmgr.msc → Pessoal → Certificados e confirme, na aba Geral do certificado da empresa, "
                "se aparece: ‘Você tem uma chave privada correspondente a este certificado’. "
                "Execute o programa no mesmo usuário do Windows. Para A3, conecte o token/cartão e instale o driver do fabricante.",
            )

    def _selecionar_pasta(self) -> None:
        caminho = filedialog.askdirectory()
        if caminho:
            self.vars["pasta"].set(caminho)

    def _filtros(self) -> Filtros:
        documento = somente_digitos(self.vars["documento"].get())
        if len(documento) not in (11, 14):
            raise ValueError("Informe um CPF com 11 dígitos ou um CNPJ com 14 dígitos.")
        escolhido = self.certificados.get(self.certificado_instalado.get())
        if not escolhido:
            raise ValueError("Clique em Buscar e selecione o certificado da empresa instalado no Windows.")
        pasta = Path(self.vars["pasta"].get())
        if not str(pasta):
            raise ValueError("Selecione a pasta de saída.")
        inicio, fim = data_iso(self.vars["inicio"].get()), data_iso(self.vars["fim"].get())
        if inicio > fim:
            raise ValueError("A data inicial deve ser anterior à final.")
        return Filtros(documento, inicio, fim, self.papel.get(), pasta, self.ambiente.get(), escolhido)

    def _iniciar(self) -> None:
        if self.forma_acesso.get() != "certificado":
            endereco = PORTAL_GOVBR if self.forma_acesso.get() == "govbr" else PORTAL_LOGIN
            if not webbrowser.open(endereco, new=2):
                messagebox.showerror("Portal NFS-e", "Não foi possível abrir o navegador padrão.")
                return
            self.status.set("Portal oficial aberto no navegador. Faça o login diretamente na página; nenhuma senha passa pelo aplicativo.")
            return
        try:
            filtros = self._filtros()
        except ValueError as erro:
            messagebox.showerror("Dados incompletos", str(erro))
            return
        self.botao.configure(state="disabled")
        self.status.set("Conectando ao Ambiente de Dados Nacional...")
        threading.Thread(target=self._executar, args=(filtros,), daemon=True).start()

    def _executar(self, filtros: Filtros) -> None:
        try:
            resultado = baixar_lote(filtros, lambda mensagem: self.eventos.put(("log", mensagem)))
            self.eventos.put(("ok", f"Concluído: {resultado['xmls']} XML(s) salvos após {resultado['consultas']} consulta(s)."))
        except Exception as erro:
            self.eventos.put(("erro", str(erro)))

    def _consumir_eventos(self) -> None:
        try:
            while True:
                tipo, mensagem = self.eventos.get_nowait()
                self.status.set(mensagem)
                if tipo in {"ok", "erro"}:
                    self.botao.configure(state="normal")
                    if tipo == "erro":
                        messagebox.showerror("Falha no download", mensagem)
        except queue.Empty:
            pass
        self.after(150, self._consumir_eventos)


if __name__ == "__main__":
    Janela().mainloop()
