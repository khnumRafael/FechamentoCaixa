from __future__ import annotations

import configparser
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import unicodedata
import tkinter as tk
from tkinter import messagebox, ttk


def _obter_diretorio_app() -> Path:
    """
    Em desenvolvimento: pasta do script.
    Em executavel PyInstaller: pasta do .exe (gravavel no computador do cliente).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _obter_diretorio_app()
DB_PATH = APP_DIR / "fechamento_caixa.db"
CONFIG_PATH = APP_DIR / "fechamento_caixa.ini"
CUPOM_PATH = APP_DIR / "ultimo_cupom.txt"
PDF_PATH = APP_DIR / "relatorio_fechamento.pdf"
# Bobina 80 mm em modo texto costuma ser 42-48 colunas (fonte A); mais que isso quebra linha na impressora.
COLUNAS_CUPOM_PADRAO = 48
COLUNAS_CUPOM_MIN = 32
COLUNAS_CUPOM_MAX = 80

IMPRESSORA_PADRAO_LABEL = "(Padrao do sistema)"

FORMAS_PAGAMENTO_PADRAO = [
    "dinheiro",
    "cartao debito",
    "cartao credito",
    "convenio",
    "crediario",
    "outros",
    "pix",
    "Pesos convertidos",
]

CONTEUDO_INI_PADRAO = """; Arquivo de configuracao do fechamento de caixa (UTF-8)
; Reinicie o aplicativo ou use o botao Recarregar INI apos editar.

[cupom]
razao_social = SUA EMPRESA LTDA
; Use o caractere | para separar linhas do endereco (cada parte vira uma linha no cupom)
endereco = Rua Exemplo, 123 | Centro | Cidade/UF - CEP 00000-000

[formas_pagamento]
; Lista separada por virgulas
lista = dinheiro, cartao debito, cartao credito, convenio, crediario, outros, pix, Pesos convertidos

[operadores]
; Lista separada por virgulas (aparece no combobox). Se vazio, o campo fica liberado para digitacao.
lista = Operador 1, Operador 2, Operador 3

[impressao]
; Colunas de texto no cupom/PDF (na bobina 80 mm use 42-48 se o layout estiver estourando ou sumindo linhas).
colunas_cupom = 48
; Ultima impressora escolhida no app (nome exato da impressora ou "(Padrao do sistema)").
impressora_selecionada = (Padrao do sistema)
; RAW com comandos ESC/POS (Epson e compatíveis): negrito so nas linhas marcadas pelo layout.
; Defina esc_pos = nao se a impressora nao entender ESC/POS (evita caracteres estranhos).
esc_pos = sim
; (Legado) Se o envio RAW nao receber lista de linhas em negrito, usa este limite desde o inicio.
linhas_negrito = 5
; Corte automatico ao final (somente se a impressora tiver guilhotina).
corte_papel = nao
"""


@dataclass
class ConfigCaixa:
    razao_social: str
    endereco_linhas: list[str]
    formas_pagamento: list[str]
    operadores: list[str]
    impressao_esc_pos: bool
    impressao_linhas_negrito: int
    impressao_corte_papel: bool
    colunas_cupom: int
    impressora_selecionada: str


def _split_lista_csv(texto: str) -> list[str]:
    return [p.strip() for p in texto.split(",") if p.strip()]


def _ordenar_lista_alfabetica(valores: list[str]) -> list[str]:
    return sorted(valores, key=lambda s: s.casefold())


def _split_endereco_ini(texto: str) -> list[str]:
    t = texto.strip()
    if not t:
        return []
    if "|" in t:
        return [p.strip() for p in t.split("|") if p.strip()]
    return [t]


def quebrar_texto_largura(texto: str, largura: int) -> list[str]:
    texto = " ".join(texto.split())
    if not texto:
        return []
    linhas: list[str] = []
    restante = texto
    while restante:
        if len(restante) <= largura:
            linhas.append(restante)
            break
        bloco = restante[:largura]
        if " " in bloco:
            bloco = bloco[: bloco.rfind(" ")].rstrip()
        else:
            bloco = restante[:largura]
        linhas.append(bloco.strip())
        restante = restante[len(bloco) :].lstrip()
    return linhas


def garantir_arquivo_ini(caminho: Path) -> None:
    if not caminho.exists():
        caminho.write_text(CONTEUDO_INI_PADRAO, encoding="utf-8")


def config_caixa_padrao() -> ConfigCaixa:
    return ConfigCaixa(
        razao_social="",
        endereco_linhas=[],
        formas_pagamento=list(FORMAS_PAGAMENTO_PADRAO),
        operadores=[],
        impressao_esc_pos=True,
        impressao_linhas_negrito=5,
        impressao_corte_papel=False,
        colunas_cupom=COLUNAS_CUPOM_PADRAO,
        impressora_selecionada=IMPRESSORA_PADRAO_LABEL,
    )


def _ini_booleano_sim(valor: str, padrao: bool) -> bool:
    v = valor.strip().lower()
    if not v:
        return padrao
    return v in ("sim", "s", "1", "true", "yes", "ligado", "on")


def carregar_config_caixa(caminho: Path) -> ConfigCaixa:
    garantir_arquivo_ini(caminho)
    parser = configparser.ConfigParser()
    try:
        lido = parser.read(caminho, encoding="utf-8")
        if not lido:
            return config_caixa_padrao()
    except configparser.Error:
        return config_caixa_padrao()

    def secao(nome: str) -> dict[str, str]:
        if parser.has_section(nome):
            return {k: v for k, v in parser.items(nome)}
        return {}

    cup = secao("cupom")
    fp = secao("formas_pagamento")
    op = secao("operadores")
    imp = secao("impressao")

    razao = cup.get("razao_social", "").strip()
    endereco = _split_endereco_ini(cup.get("endereco", ""))

    formas = _split_lista_csv(fp.get("lista", ""))
    if not formas:
        formas = list(FORMAS_PAGAMENTO_PADRAO)
    formas = _ordenar_lista_alfabetica(formas)

    operadores = _split_lista_csv(op.get("lista", ""))
    operadores = _ordenar_lista_alfabetica(operadores)

    esc_pos = _ini_booleano_sim(imp.get("esc_pos", "sim"), True)
    try:
        linhas_negrito = int(imp.get("linhas_negrito", "12").strip() or "12")
    except ValueError:
        linhas_negrito = 12
    linhas_negrito = max(0, min(linhas_negrito, 80))
    corte = _ini_booleano_sim(imp.get("corte_papel", "nao"), False)
    try:
        colunas_cupom = int(imp.get("colunas_cupom", str(COLUNAS_CUPOM_PADRAO)).strip() or str(COLUNAS_CUPOM_PADRAO))
    except ValueError:
        colunas_cupom = COLUNAS_CUPOM_PADRAO
    colunas_cupom = max(COLUNAS_CUPOM_MIN, min(COLUNAS_CUPOM_MAX, colunas_cupom))
    impressora_selecionada = imp.get("impressora_selecionada", "").strip() or IMPRESSORA_PADRAO_LABEL

    return ConfigCaixa(
        razao_social=razao,
        endereco_linhas=endereco,
        formas_pagamento=formas,
        operadores=operadores,
        impressao_esc_pos=esc_pos,
        impressao_linhas_negrito=linhas_negrito,
        impressao_corte_papel=corte,
        colunas_cupom=colunas_cupom,
        impressora_selecionada=impressora_selecionada,
    )


def parse_valor(valor_digitado: str) -> Decimal:
    texto = valor_digitado.strip().replace("R$", "").replace(" ", "")
    if not texto:
        raise ValueError("Informe um valor.")

    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "," in texto:
        texto = texto.replace(".", "").replace(",", ".")

    try:
        valor = Decimal(texto).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("Valor invalido.") from exc

    if valor <= 0:
        raise ValueError("O valor deve ser maior que zero.")
    return valor


def validar_data(data_digitada: str) -> str:
    texto = data_digitada.strip()
    if not texto:
        raise ValueError("Informe a data.")
    try:
        data = datetime.strptime(texto, "%d/%m/%Y")
    except ValueError as exc:
        raise ValueError("Data invalida. Use o formato DD/MM/AAAA.") from exc
    return data.strftime("%d/%m/%Y")


def valor_para_centavos(valor: Decimal) -> int:
    return int((valor * 100).to_integral_value(rounding=ROUND_HALF_UP))


def centavos_para_valor(centavos: int) -> Decimal:
    return (Decimal(centavos) / Decimal(100)).quantize(Decimal("0.01"))


def formatar_valor_br(valor: Decimal) -> str:
    numero = f"{valor:,.2f}"
    return numero.replace(",", "X").replace(".", ",").replace("X", ".")


def centralizar(texto: str, largura: int) -> str:
    t = texto.replace("\n", " ").replace("\r", "")[:largura]
    return t.center(largura)


def linha_cupom_fixa(linha: str, largura: int) -> str:
    """Linha com exatamente `largura` caracteres (cupom termica / PDF)."""
    texto = linha.replace("\n", " ").replace("\r", "")
    if len(texto) > largura:
        return texto[:largura]
    return texto.ljust(largura)


def largura_coluna_valor_direita(largura_linha: int) -> int:
    """Largura da coluna monetaria em funcao do papel."""
    return max(11, min(22, largura_linha // 3))


def linha_texto_coluna_valor(
    texto_esquerda: str,
    texto_direita: str,
    *,
    largura: int,
    monetario: bool = False,
) -> str:
    """Esquerda = rotulo ou forma; direita = texto ou valor (monetario=True compacta espacos)."""
    wv = largura_coluna_valor_direita(largura)
    wt = largura - 1 - wv
    esq = texto_esquerda.strip()[:wt].ljust(wt)
    vf = texto_direita.strip()
    if monetario:
        vf = vf.replace(" ", "")
        if len(vf) > wv:
            vf = vf[-wv:]
    else:
        if len(vf) > wv:
            vf = vf[:wv]
    vf = vf.rjust(wv)
    return linha_cupom_fixa(esq + " " + vf, largura)


def linha_vazia_cupom(largura: int) -> str:
    return linha_cupom_fixa("", largura)


def linhas_texto_centro_cupom(texto: str, largura: int) -> list[str]:
    return [
        centralizar(parte, largura) for parte in quebrar_texto_largura(texto, largura)
    ]


def linhas_texto_esquerda_cupom(texto: str, largura: int) -> list[str]:
    return [linha_cupom_fixa(parte, largura) for parte in quebrar_texto_largura(texto, largura)]


def sanear_texto_impressora_termica(texto: str) -> str:
    """Acentos -> ASCII basico para CP850 na impressora termica (evita falha silenciosa)."""
    norm = unicodedata.normalize("NFD", texto)
    return "".join(ch for ch in norm if unicodedata.category(ch) != "Mn")


# ESC/POS (Epson TM, Bematech, Daruma, etc.): enviado junto com texto em modo RAW.
_ESC_POS_INIT = b"\x1b@"
_ESC_POS_NEGRITO_ON = b"\x1bE\x01"
_ESC_POS_NEGRITO_OFF = b"\x1bE\x00"
_ESC_POS_CORTE_PARCIAL = b"\x1dV\x00"


def texto_cupom_para_bytes_raw(
    texto: str,
    *,
    esc_pos: bool,
    linhas_negrito: int,
    corte_final: bool,
    indices_negrito: frozenset[int] | None = None,
    colunas_max: int | None = None,
) -> bytes:
    """
    Cupom em texto -> bytes para WritePrinter RAW.
    esc_pos: envia ESC @ e negrito (ESC E 1/0).
    Se indices_negrito for informado, apenas essas linhas ficam em negrito; senao,
    nas primeiras linhas_negrito linhas (compatibilidade).
    colunas_max: recorta cada linha (evita quebra espuria na bobina).
    """
    texto_imp = sanear_texto_impressora_termica(texto)
    linhas_br = texto_imp.replace("\r\n", "\n").split("\n")
    if not esc_pos:
        linhas_join = []
        for line in linhas_br:
            if colunas_max is not None:
                line = line[:colunas_max]
            linhas_join.append(line)
        return "\r\n".join(linhas_join).encode("cp850", errors="replace")

    out = bytearray(_ESC_POS_INIT)
    bold_on = False
    lim = max(0, linhas_negrito)
    for i, line in enumerate(linhas_br):
        if colunas_max is not None:
            line = line[:colunas_max]
        if indices_negrito is not None:
            quero_negrito = i in indices_negrito
        else:
            quero_negrito = i < lim
        if quero_negrito and not bold_on:
            out.extend(_ESC_POS_NEGRITO_ON)
            bold_on = True
        elif not quero_negrito and bold_on:
            out.extend(_ESC_POS_NEGRITO_OFF)
            bold_on = False
        out.extend(line.encode("cp850", errors="replace"))
        out.extend(b"\r\n")
    if bold_on:
        out.extend(_ESC_POS_NEGRITO_OFF)
    if corte_final:
        out.extend(b"\r\n\r\n")
        out.extend(_ESC_POS_CORTE_PARCIAL)
    return bytes(out)


def escapar_pdf(texto: str) -> str:
    return texto.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def listar_impressoras_windows() -> list[str]:
    try:
        import win32print  # type: ignore
    except ImportError:
        return []

    nomes: list[str] = []
    flags_opcoes = (
        win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS,
        win32print.PRINTER_ENUM_LOCAL,
    )
    for flags in flags_opcoes:
        try:
            for info in win32print.EnumPrinters(flags, None, 2):
                nome = info.get("pPrinterName")
                if nome and nome not in nomes:
                    nomes.append(nome)
            if nomes:
                break
        except Exception:
            continue
    return sorted(nomes, key=str.casefold)


@dataclass
class Lancamento:
    data_hora: str
    operador_caixa: str
    data_movimento: str
    forma_pagamento: str
    valor_centavos: int
    id: int | None = None


@dataclass
class FechamentoResumo:
    fechamento_id: str
    operador_caixa: str
    data_movimento: str
    total_centavos: int
    itens: int


class CaixaDB:
    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._criar_tabelas()

    def _criar_tabelas(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lancamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fechamento_id TEXT NOT NULL,
                data_hora TEXT NOT NULL,
                forma_pagamento TEXT NOT NULL,
                valor_centavos INTEGER NOT NULL
            )
            """
        )
        self._garantir_colunas_extras()
        self.conn.commit()

    def _garantir_colunas_extras(self) -> None:
        rows = self.conn.execute("PRAGMA table_info(lancamentos)").fetchall()
        colunas = {row["name"] for row in rows}

        if "operador_caixa" not in colunas:
            self.conn.execute(
                """
                ALTER TABLE lancamentos
                ADD COLUMN operador_caixa TEXT NOT NULL DEFAULT ''
                """
            )
        if "data_movimento" not in colunas:
            self.conn.execute(
                """
                ALTER TABLE lancamentos
                ADD COLUMN data_movimento TEXT NOT NULL DEFAULT ''
                """
            )

    def inserir_lancamento(self, fechamento_id: str, lancamento: Lancamento) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO lancamentos (
                fechamento_id,
                data_hora,
                operador_caixa,
                data_movimento,
                forma_pagamento,
                valor_centavos
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                fechamento_id,
                lancamento.data_hora,
                lancamento.operador_caixa,
                lancamento.data_movimento,
                lancamento.forma_pagamento,
                lancamento.valor_centavos,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def listar_lancamentos(self, fechamento_id: str) -> list[Lancamento]:
        rows = self.conn.execute(
            """
            SELECT id, data_hora, operador_caixa, data_movimento, forma_pagamento, valor_centavos
            FROM lancamentos
            WHERE fechamento_id = ?
            ORDER BY id
            """,
            (fechamento_id,),
        ).fetchall()
        return [
            Lancamento(
                data_hora=row["data_hora"],
                operador_caixa=row["operador_caixa"],
                data_movimento=row["data_movimento"],
                forma_pagamento=row["forma_pagamento"],
                valor_centavos=row["valor_centavos"],
                id=int(row["id"]),
            )
            for row in rows
        ]

    def total_geral_centavos(self, fechamento_id: str) -> int:
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(valor_centavos), 0) AS total
            FROM lancamentos
            WHERE fechamento_id = ?
            """,
            (fechamento_id,),
        ).fetchone()
        return int(row["total"])

    def totais_por_forma(self, fechamento_id: str) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            """
            SELECT forma_pagamento, COALESCE(SUM(valor_centavos), 0) AS total
            FROM lancamentos
            WHERE fechamento_id = ?
            GROUP BY forma_pagamento
            ORDER BY forma_pagamento
            """,
            (fechamento_id,),
        ).fetchall()
        return [(row["forma_pagamento"], int(row["total"])) for row in rows]

    def buscar_fechamentos(
        self, *, operador: str = "", data_movimento: str = ""
    ) -> list[FechamentoResumo]:
        filtros: list[str] = []
        params: list[str] = []

        if operador.strip():
            filtros.append("UPPER(COALESCE(operador_caixa, '')) LIKE UPPER(?)")
            params.append(f"%{operador.strip()}%")
        if data_movimento.strip():
            filtros.append("data_movimento = ?")
            params.append(data_movimento.strip())

        where = " AND ".join(filtros) if filtros else "1=1"
        rows = self.conn.execute(
            f"""
            SELECT
                fechamento_id,
                COALESCE(MAX(operador_caixa), '') AS operador_caixa,
                COALESCE(MAX(data_movimento), '') AS data_movimento,
                COALESCE(SUM(valor_centavos), 0) AS total_centavos,
                COUNT(*) AS itens
            FROM lancamentos
            WHERE {where}
            GROUP BY fechamento_id
            ORDER BY fechamento_id DESC
            """,
            params,
        ).fetchall()

        return [
            FechamentoResumo(
                fechamento_id=row["fechamento_id"],
                operador_caixa=row["operador_caixa"] or "-",
                data_movimento=row["data_movimento"] or "-",
                total_centavos=int(row["total_centavos"]),
                itens=int(row["itens"]),
            )
            for row in rows
        ]

    def excluir_lancamento(self, fechamento_id: str, lancamento_id: int) -> bool:
        cursor = self.conn.execute(
            """
            DELETE FROM lancamentos
            WHERE id = ? AND fechamento_id = ?
            """,
            (lancamento_id, fechamento_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self.conn.close()


class FechamentoCaixaApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Fechamento de Caixa - PDV")
        self.geometry("920x580")
        self.minsize(800, 480)

        self.cfg = carregar_config_caixa(CONFIG_PATH)

        self.db = CaixaDB(DB_PATH)
        self.fechamento_id = datetime.now().strftime("%Y%m%d-%H%M%S")

        formas = self.cfg.formas_pagamento or list(FORMAS_PAGAMENTO_PADRAO)
        self.forma_var = tk.StringVar(value=formas[0])
        self.operador_var = tk.StringVar()
        self.data_var = tk.StringVar(value=datetime.now().strftime("%d/%m/%Y"))
        self.valor_var = tk.StringVar()
        self.total_var = tk.StringVar(value="Total geral: 0,00")
        self.status_var = tk.StringVar(value="Preencha os campos e clique em Incluir no grid.")
        self.impressora_var = tk.StringVar(value=IMPRESSORA_PADRAO_LABEL)
        self._cab_razao_var = tk.StringVar()
        self._cab_endereco_var = tk.StringVar()
        self._aplicar_textos_cabecalho_gui()

        self._configurar_estilo()
        self._montar_tela()
        self._configurar_combo_operadores_apos_ini()
        self._popular_combo_impressoras(atualizar_status=False)
        self._carregar_grid()
        self._atualizar_total()
        self.combo_operador.focus_set()
        self.bind("<F9>", lambda _event: self.imprimir_cupom())
        self.bind("<Control-i>", lambda _event: self.incluir_lancamento())
        self.protocol("WM_DELETE_WINDOW", self._ao_fechar)

    def _configurar_estilo(self) -> None:
        style = ttk.Style(self)
        temas = style.theme_names()
        if "vista" in temas:
            style.theme_use("vista")
        elif "clam" in temas:
            style.theme_use("clam")

        style.configure("Titulo.TLabel", font=("Segoe UI", 14, "bold"))
        style.configure("Subtitulo.TLabel", font=("Segoe UI", 9), foreground="#444444")
        style.configure("EmpresaCab.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("EnderecoCab.TLabel", font=("Segoe UI", 9))
        style.configure("Campo.TLabel", font=("Segoe UI", 9))
        style.configure("Total.TLabel", font=("Segoe UI", 12, "bold"), foreground="#0b6e4f")
        style.configure("Acao.TButton", padding=(10, 5))

    def _aplicar_textos_cabecalho_gui(self) -> None:
        self._cab_razao_var.set(self.cfg.razao_social.strip())
        self._cab_endereco_var.set(
            " | ".join(self.cfg.endereco_linhas) if self.cfg.endereco_linhas else ""
        )

    def _configurar_combo_operadores_apos_ini(self) -> None:
        self.combo_operador["values"] = self.cfg.operadores
        if self.cfg.operadores:
            self.combo_operador.configure(state="readonly")
            self.operador_var.set(self.cfg.operadores[0])
        else:
            self.combo_operador.configure(state="normal")

    def _recarregar_ini(self) -> None:
        self.cfg = carregar_config_caixa(CONFIG_PATH)
        self._aplicar_textos_cabecalho_gui()

        formas = self.cfg.formas_pagamento or list(FORMAS_PAGAMENTO_PADRAO)
        self.combo_forma["values"] = formas
        if self.forma_var.get() not in formas:
            self.forma_var.set(formas[0])

        self._configurar_combo_operadores_apos_ini()
        self._popular_combo_impressoras(atualizar_status=False)
        self._set_status("Arquivo INI recarregado.")

    def _montar_tela(self) -> None:
        px = 10
        py = 8

        principal = ttk.Frame(self, padding=(px, py))
        principal.pack(fill="both", expand=True)
        principal.columnconfigure(0, weight=1)
        principal.rowconfigure(0, weight=1)

        abas = ttk.Notebook(principal)
        abas.grid(row=0, column=0, sticky="nsew")

        aba_lancamento = ttk.Frame(abas, padding=(6, 6))
        aba_lancamento.columnconfigure(0, weight=1)
        aba_lancamento.rowconfigure(2, weight=1)
        abas.add(aba_lancamento, text="Lançamento")

        # --- Zona 1: cabecalho (titulo + empresa + atalhos) ---
        frm_topo = ttk.LabelFrame(aba_lancamento, text=" Identificação ", padding=(px, py))
        frm_topo.grid(row=0, column=0, sticky="ew", pady=(0, py))
        frm_topo.columnconfigure(0, weight=1)

        ttk.Label(frm_topo, text="Fechamento de Caixa", style="Titulo.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            frm_topo,
            textvariable=self._cab_razao_var,
            style="EmpresaCab.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(
            frm_topo,
            textvariable=self._cab_endereco_var,
            style="EnderecoCab.TLabel",
        ).grid(row=2, column=0, sticky="w")

        barra_topo = ttk.Frame(frm_topo)
        barra_topo.grid(row=3, column=0, sticky="ew", pady=(py, 0))
        barra_topo.columnconfigure(0, weight=1)

        ttk.Label(
            barra_topo,
            text="Atalhos: Ctrl+I incluir · F9 imprimir cupom · Delete excluir linha",
            style="Subtitulo.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(barra_topo, text="Recarregar INI", command=self._recarregar_ini).grid(
            row=0, column=1, sticky="e", padx=(px, 0)
        )

        # --- Zona 2: formulario de lancamento ---
        frm_entrada = ttk.LabelFrame(aba_lancamento, text=" Registrar lançamento ", padding=(px, py))
        frm_entrada.grid(row=1, column=0, sticky="ew", pady=(0, py))
        frm_entrada.columnconfigure(1, weight=2)
        frm_entrada.columnconfigure(3, weight=1)

        ttk.Label(frm_entrada, text="Operador", style="Campo.TLabel").grid(
            row=0, column=0, sticky="nw", padx=(0, 8), pady=(0, 4)
        )
        self.combo_operador = ttk.Combobox(
            frm_entrada,
            textvariable=self.operador_var,
            values=self.cfg.operadores,
            width=24,
            state="readonly" if self.cfg.operadores else "normal",
        )
        self.combo_operador.grid(row=0, column=1, sticky="ew", pady=(0, 4))

        ttk.Label(frm_entrada, text="Data (DD/MM/AAAA)", style="Campo.TLabel").grid(
            row=0, column=2, sticky="nw", padx=(16, 8), pady=(0, 4)
        )
        self.entrada_data = ttk.Entry(frm_entrada, textvariable=self.data_var, width=14)
        self.entrada_data.grid(row=0, column=3, sticky="w", pady=(0, 4))

        ttk.Label(frm_entrada, text="Forma de pagamento", style="Campo.TLabel").grid(
            row=1, column=0, sticky="nw", padx=(0, 8), pady=(4, 0)
        )
        self.combo_forma = ttk.Combobox(
            frm_entrada,
            values=self.cfg.formas_pagamento,
            textvariable=self.forma_var,
            state="readonly",
            width=24,
        )
        self.combo_forma.grid(row=1, column=1, sticky="ew", pady=(4, 0))

        ttk.Label(frm_entrada, text="Valor (R$)", style="Campo.TLabel").grid(
            row=1, column=2, sticky="nw", padx=(16, 8), pady=(4, 0)
        )
        self.entrada_valor = ttk.Entry(frm_entrada, textvariable=self.valor_var, width=14)
        self.entrada_valor.grid(row=1, column=3, sticky="ew", pady=(4, 0))
        self.entrada_valor.bind("<Return>", lambda _event: self.incluir_lancamento())
        self.entrada_valor.bind("<FocusOut>", lambda _event: self._formatar_valor_digitado())

        barra_acoes = ttk.Frame(frm_entrada)
        barra_acoes.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(py + 2, 0))
        barra_acoes.columnconfigure(0, weight=1)

        botoes = ttk.Frame(barra_acoes)
        botoes.grid(row=0, column=1, sticky="e")

        ttk.Button(
            botoes,
            text="Incluir no grid",
            style="Acao.TButton",
            command=self.incluir_lancamento,
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            botoes,
            text="Limpar valor",
            style="Acao.TButton",
            command=self.limpar_valor,
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            botoes,
            text="Excluir selecionado",
            style="Acao.TButton",
            command=self.excluir_lancamento_selecionado,
        ).pack(side="left")

        # --- Zona 3: lista (expande com a janela) ---
        frm_lista = ttk.LabelFrame(aba_lancamento, text=" Movimentos do fechamento ", padding=(px, py))
        frm_lista.grid(row=2, column=0, sticky="nsew", pady=(0, py))
        frm_lista.columnconfigure(0, weight=1)
        frm_lista.rowconfigure(0, weight=1)

        holder_grid = ttk.Frame(frm_lista)
        holder_grid.grid(row=0, column=0, sticky="nsew")
        holder_grid.columnconfigure(0, weight=1)
        holder_grid.rowconfigure(0, weight=1)

        self.grid_lancamentos = ttk.Treeview(
            holder_grid,
            columns=("data_hora", "operador", "forma", "valor"),
            show="headings",
            height=12,
        )
        self.grid_lancamentos.heading("data_hora", text="Data/Hora")
        self.grid_lancamentos.heading("operador", text="Operador")
        self.grid_lancamentos.heading("forma", text="Forma de pagamento")
        self.grid_lancamentos.heading("valor", text="Valor")

        self.grid_lancamentos.column("data_hora", width=130, anchor="center", stretch=False)
        self.grid_lancamentos.column("operador", width=120, anchor="w", stretch=False)
        self.grid_lancamentos.column("forma", width=260, anchor="w", stretch=True)
        self.grid_lancamentos.column("valor", width=100, anchor="e", stretch=False)

        barra = ttk.Scrollbar(holder_grid, orient="vertical", command=self.grid_lancamentos.yview)
        self.grid_lancamentos.configure(yscrollcommand=barra.set)

        self.grid_lancamentos.grid(row=0, column=0, sticky="nsew")
        barra.grid(row=0, column=1, sticky="ns")

        self.grid_lancamentos.tag_configure("par", background="#f0f4f8")
        self.grid_lancamentos.tag_configure("impar", background="#ffffff")
        self.grid_lancamentos.bind("<Delete>", lambda _event: self.excluir_lancamento_selecionado())

        # --- Zona 4: rodape (resumo + impressao) ---
        frm_rodape = ttk.LabelFrame(aba_lancamento, text=" Resumo e impressão ", padding=(px, py))
        frm_rodape.grid(row=3, column=0, sticky="ew")
        frm_rodape.columnconfigure(1, weight=1)

        linha_resumo = ttk.Frame(frm_rodape)
        linha_resumo.grid(row=0, column=0, columnspan=2, sticky="ew")
        linha_resumo.columnconfigure(1, weight=1)

        ttk.Label(linha_resumo, textvariable=self.total_var, style="Total.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(linha_resumo, textvariable=self.status_var, style="Subtitulo.TLabel").grid(
            row=0, column=1, sticky="ew", padx=(16, 0)
        )

        linha_imp = ttk.Frame(frm_rodape)
        linha_imp.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(py, 0))
        linha_imp.columnconfigure(1, weight=1)

        ttk.Label(linha_imp, text="Impressora", style="Campo.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.combo_impressora = ttk.Combobox(
            linha_imp,
            textvariable=self.impressora_var,
            width=32,
            state="readonly",
        )
        self.combo_impressora.grid(row=0, column=1, sticky="ew")
        self.combo_impressora.bind(
            "<<ComboboxSelected>>", lambda _event: self._salvar_impressora_selecionada()
        )
        ttk.Button(linha_imp, text="Atualizar lista", command=self._popular_combo_impressoras).grid(
            row=0, column=2, padx=(8, 16)
        )

        grp_imp = ttk.Frame(linha_imp)
        grp_imp.grid(row=0, column=3, sticky="e")

        ttk.Button(
            grp_imp,
            text="PDF cupom",
            style="Acao.TButton",
            command=self.gerar_relatorio_pdf,
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            grp_imp,
            text="Imprimir cupom",
            style="Acao.TButton",
            command=self.imprimir_cupom,
        ).pack(side="left")

        # --- Aba 2: segunda via ---
        aba_segunda_via = ttk.Frame(abas, padding=(6, 6))
        aba_segunda_via.columnconfigure(0, weight=1)
        aba_segunda_via.rowconfigure(1, weight=1)
        abas.add(aba_segunda_via, text="2ª Via")

        self.busca_operador_var = tk.StringVar()
        self.busca_data_var = tk.StringVar(value=datetime.now().strftime("%d/%m/%Y"))

        frm_filtro = ttk.LabelFrame(aba_segunda_via, text=" Buscar fechamento ", padding=(px, py))
        frm_filtro.grid(row=0, column=0, sticky="ew", pady=(0, py))
        frm_filtro.columnconfigure(1, weight=1)
        frm_filtro.columnconfigure(3, weight=1)

        ttk.Label(frm_filtro, text="Operador contém", style="Campo.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.entrada_busca_operador = ttk.Entry(
            frm_filtro, textvariable=self.busca_operador_var, width=24
        )
        self.entrada_busca_operador.grid(row=0, column=1, sticky="ew")

        ttk.Label(frm_filtro, text="Data (DD/MM/AAAA)", style="Campo.TLabel").grid(
            row=0, column=2, sticky="w", padx=(16, 8)
        )
        self.entrada_busca_data = ttk.Entry(frm_filtro, textvariable=self.busca_data_var, width=14)
        self.entrada_busca_data.grid(row=0, column=3, sticky="w")

        ttk.Button(frm_filtro, text="Pesquisar", command=self.pesquisar_fechamentos).grid(
            row=0, column=4, padx=(10, 0)
        )

        frm_result = ttk.LabelFrame(
            aba_segunda_via, text=" Fechamentos encontrados ", padding=(px, py)
        )
        frm_result.grid(row=1, column=0, sticky="nsew")
        frm_result.columnconfigure(0, weight=1)
        frm_result.rowconfigure(0, weight=1)

        holder_busca = ttk.Frame(frm_result)
        holder_busca.grid(row=0, column=0, sticky="nsew")
        holder_busca.columnconfigure(0, weight=1)
        holder_busca.rowconfigure(0, weight=1)

        self.grid_fechamentos = ttk.Treeview(
            holder_busca,
            columns=("fechamento", "operador", "data", "itens", "total"),
            show="headings",
            height=14,
        )
        self.grid_fechamentos.heading("fechamento", text="Fechamento")
        self.grid_fechamentos.heading("operador", text="Operador")
        self.grid_fechamentos.heading("data", text="Data")
        self.grid_fechamentos.heading("itens", text="Itens")
        self.grid_fechamentos.heading("total", text="Total")

        self.grid_fechamentos.column("fechamento", width=170, anchor="center", stretch=False)
        self.grid_fechamentos.column("operador", width=180, anchor="w", stretch=True)
        self.grid_fechamentos.column("data", width=120, anchor="center", stretch=False)
        self.grid_fechamentos.column("itens", width=70, anchor="center", stretch=False)
        self.grid_fechamentos.column("total", width=120, anchor="e", stretch=False)

        barra_busca = ttk.Scrollbar(holder_busca, orient="vertical", command=self.grid_fechamentos.yview)
        self.grid_fechamentos.configure(yscrollcommand=barra_busca.set)
        self.grid_fechamentos.grid(row=0, column=0, sticky="nsew")
        barra_busca.grid(row=0, column=1, sticky="ns")
        self.grid_fechamentos.bind("<Double-1>", lambda _event: self.imprimir_segunda_via())

        barra_segvia = ttk.Frame(frm_result)
        barra_segvia.grid(row=1, column=0, sticky="ew", pady=(py, 0))
        barra_segvia.columnconfigure(0, weight=1)

        ttk.Label(
            barra_segvia,
            text="Selecione um fechamento e clique em Imprimir 2ª via.",
            style="Subtitulo.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            barra_segvia,
            text="Imprimir 2ª via",
            style="Acao.TButton",
            command=self.imprimir_segunda_via,
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))

    def _set_status(self, mensagem: str) -> None:
        self.status_var.set(mensagem)

    def _popular_combo_impressoras(self, atualizar_status: bool = True) -> None:
        impressoras = listar_impressoras_windows()
        valores = [IMPRESSORA_PADRAO_LABEL, *impressoras]
        self.combo_impressora["values"] = valores

        padrao = ""
        try:
            import win32print  # type: ignore

            padrao = win32print.GetDefaultPrinter() or ""
        except Exception:
            padrao = ""

        salva_ini = self.cfg.impressora_selecionada.strip()
        atual = self.impressora_var.get().strip()
        if salva_ini in valores:
            escolha = salva_ini
        elif atual in valores:
            escolha = atual
        elif padrao and padrao in valores:
            escolha = padrao
        elif impressoras:
            escolha = impressoras[0]
        else:
            escolha = IMPRESSORA_PADRAO_LABEL

        self.impressora_var.set(escolha)
        self._salvar_impressora_selecionada()
        if atualizar_status:
            self._set_status("Lista de impressoras atualizada.")

    def _salvar_impressora_selecionada(self) -> None:
        selecao = self.impressora_var.get().strip() or IMPRESSORA_PADRAO_LABEL
        if self.cfg.impressora_selecionada == selecao:
            return

        self.cfg.impressora_selecionada = selecao
        self._salvar_opcao_impressao_ini("impressora_selecionada", selecao)

    def _salvar_opcao_impressao_ini(self, chave: str, valor: str) -> None:
        parser = configparser.ConfigParser()
        try:
            parser.read(CONFIG_PATH, encoding="utf-8")
            if not parser.has_section("impressao"):
                parser.add_section("impressao")
            parser.set("impressao", chave, valor)
            with CONFIG_PATH.open("w", encoding="utf-8") as f:
                parser.write(f)
        except Exception:
            # Mantem funcionamento mesmo se o INI nao puder ser gravado.
            return

    def _resolver_nome_impressora_efetiva(self) -> str | None:
        try:
            import win32print  # type: ignore
        except ImportError:
            return None

        selecao = self.impressora_var.get().strip()
        if not selecao or selecao == IMPRESSORA_PADRAO_LABEL:
            try:
                return win32print.GetDefaultPrinter()
            except Exception:
                return None
        return selecao

    def _formatar_valor_digitado(self) -> None:
        texto = self.valor_var.get().strip()
        if not texto:
            return
        try:
            valor = parse_valor(texto)
        except ValueError:
            return
        self.valor_var.set(formatar_valor_br(valor))

    def limpar_valor(self) -> None:
        self.valor_var.set("")
        self.entrada_valor.focus_set()
        self._set_status("Campo de valor limpo.")

    def _inserir_item_grid(self, lancamento: Lancamento) -> None:
        valor = formatar_valor_br(centavos_para_valor(lancamento.valor_centavos))
        indice = len(self.grid_lancamentos.get_children())
        tag = "par" if indice % 2 == 0 else "impar"
        valores = (
            lancamento.data_hora,
            lancamento.operador_caixa,
            lancamento.forma_pagamento,
            valor,
        )
        item_id = str(lancamento.id) if lancamento.id is not None else ""

        if item_id and not self.grid_lancamentos.exists(item_id):
            self.grid_lancamentos.insert("", tk.END, iid=item_id, values=valores, tags=(tag,))
            return

        self.grid_lancamentos.insert("", tk.END, values=valores, tags=(tag,))

    def _reaplicar_listras_grid(self) -> None:
        for indice, item in enumerate(self.grid_lancamentos.get_children()):
            tag = "par" if indice % 2 == 0 else "impar"
            self.grid_lancamentos.item(item, tags=(tag,))

    def _carregar_grid(self) -> None:
        for item in self.grid_lancamentos.get_children():
            self.grid_lancamentos.delete(item)

        for lanc in self.db.listar_lancamentos(self.fechamento_id):
            self._inserir_item_grid(lanc)

    def _atualizar_total(self) -> None:
        total_centavos = self.db.total_geral_centavos(self.fechamento_id)
        total = formatar_valor_br(centavos_para_valor(total_centavos))
        self.total_var.set(f"Total geral: {total}")

    def pesquisar_fechamentos(self) -> None:
        operador = self.busca_operador_var.get().strip()
        data_txt = self.busca_data_var.get().strip()
        data_filtro = ""
        if data_txt:
            try:
                data_filtro = validar_data(data_txt)
            except ValueError as exc:
                messagebox.showwarning("Pesquisa", str(exc))
                self.entrada_busca_data.focus_set()
                return

        for item in self.grid_fechamentos.get_children():
            self.grid_fechamentos.delete(item)

        resultados = self.db.buscar_fechamentos(operador=operador, data_movimento=data_filtro)
        for res in resultados:
            total = formatar_valor_br(centavos_para_valor(res.total_centavos))
            self.grid_fechamentos.insert(
                "",
                tk.END,
                iid=res.fechamento_id,
                values=(
                    res.fechamento_id,
                    res.operador_caixa,
                    res.data_movimento,
                    res.itens,
                    total,
                ),
            )

        if resultados:
            self._set_status(f"Pesquisa retornou {len(resultados)} fechamento(s).")
        else:
            self._set_status("Nenhum fechamento encontrado para os filtros informados.")

    def _gerar_texto_cupom_de(
        self, fechamento_id: str, *, operador_cupom: str, data_cupom: str
    ) -> tuple[str, frozenset[int]]:
        w = self.cfg.colunas_cupom
        lancamentos = self.db.listar_lancamentos(fechamento_id)
        total_geral = self.db.total_geral_centavos(fechamento_id)
        total_geral_str = formatar_valor_br(centavos_para_valor(total_geral))

        linhas: list[str] = []
        bold_indices: set[int] = set()

        def push(texto_linha: str, *, negrito: bool = False) -> None:
            idx = len(linhas)
            linhas.append(texto_linha)
            if negrito:
                bold_indices.add(idx)

        linha_pontilhada = self._linha_pontilhada_cupom(w)
        push(linha_pontilhada)
        self._adicionar_cabecalho_cupom(linhas, bold_indices, largura=w)
        self._adicionar_bloco_resumo_cupom(
            linhas,
            largura=w,
            operador_cupom=operador_cupom,
            data_cupom=data_cupom,
            total_geral_str=total_geral_str,
        )
        self._adicionar_bloco_pagamentos_cupom(
            linhas, bold_indices, lancamentos, largura=w
        )
        self._adicionar_espaco_final_cupom(linhas, largura=w, qtd_linhas=5)
        push(linha_pontilhada)

        texto = "\n".join(linhas) + "\n"
        return texto, frozenset(bold_indices)

    def _linha_pontilhada_cupom(self, largura: int) -> str:
        return linha_cupom_fixa("." * largura, largura)

    def _adicionar_cabecalho_cupom(
        self, linhas: list[str], bold_indices: set[int], *, largura: int
    ) -> None:
        def push_cab(texto_linha: str, *, negrito: bool = False) -> None:
            idx = len(linhas)
            linhas.append(texto_linha)
            if negrito:
                bold_indices.add(idx)

        if self.cfg.razao_social.strip():
            for parte in quebrar_texto_largura(self.cfg.razao_social.strip(), largura):
                push_cab(centralizar(parte, largura), negrito=True)
        for linha_end in self.cfg.endereco_linhas:
            for parte in quebrar_texto_largura(linha_end.strip(), largura):
                push_cab(centralizar(parte, largura), negrito=True)

        linhas.append(linha_vazia_cupom(largura))
        linhas.append(linha_vazia_cupom(largura))

    def _adicionar_bloco_resumo_cupom(
        self,
        linhas: list[str],
        *,
        largura: int,
        operador_cupom: str,
        data_cupom: str,
        total_geral_str: str,
    ) -> None:
        linhas.append(linha_cupom_fixa(f"Operador:{operador_cupom}", largura))
        linhas.append(linha_cupom_fixa(f"data:{data_cupom}", largura))
        linhas.append(linha_cupom_fixa(f"Total:R$ {total_geral_str}", largura))

    def _adicionar_bloco_pagamentos_cupom(
        self,
        linhas: list[str],
        bold_indices: set[int],
        lancamentos: list[Lancamento],
        *,
        largura: int,
    ) -> None:
        idx_pagamento = len(linhas)
        linhas.append(centralizar("Pagamento", largura))
        bold_indices.add(idx_pagamento)
        if not lancamentos:
            linhas.append(linha_cupom_fixa("(sem itens)", largura))
            return

        for lanc in lancamentos:
            valor_str = formatar_valor_br(centavos_para_valor(lanc.valor_centavos))
            desc = (lanc.forma_pagamento or "-").strip()
            linhas.append(
                linha_texto_coluna_valor(desc, f"R$ {valor_str}", largura=largura, monetario=False)
            )

    def _adicionar_espaco_final_cupom(
        self, linhas: list[str], *, largura: int, qtd_linhas: int
    ) -> None:
        for _ in range(max(0, qtd_linhas)):
            linhas.append(linha_vazia_cupom(largura))

    def incluir_lancamento(self) -> None:
        operador = self.operador_var.get().strip()
        if not operador:
            messagebox.showwarning("Validacao", "Selecione ou informe o operador de caixa.")
            self.combo_operador.focus_set()
            return

        try:
            data_movimento = validar_data(self.data_var.get())
        except ValueError as exc:
            messagebox.showwarning("Validacao", str(exc))
            self.entrada_data.focus_set()
            return

        forma = self.forma_var.get().strip()
        if not forma:
            messagebox.showwarning("Validacao", "Selecione uma forma de pagamento.")
            return

        try:
            valor = parse_valor(self.valor_var.get())
        except ValueError as exc:
            messagebox.showwarning("Validacao", str(exc))
            self.entrada_valor.focus_set()
            return

        lancamento = Lancamento(
            data_hora=f"{data_movimento} {datetime.now().strftime('%H:%M:%S')}",
            operador_caixa=operador,
            data_movimento=data_movimento,
            forma_pagamento=forma,
            valor_centavos=valor_para_centavos(valor),
        )
        lancamento.id = self.db.inserir_lancamento(self.fechamento_id, lancamento)

        self._inserir_item_grid(lancamento)

        self.valor_var.set("")
        self.entrada_valor.focus_set()
        self._atualizar_total()
        self._set_status("Lancamento incluido com sucesso.")

    def excluir_lancamento_selecionado(self) -> None:
        selecionados = self.grid_lancamentos.selection()
        if not selecionados:
            messagebox.showwarning("Exclusao", "Selecione um registro no grid para excluir.")
            return

        if not messagebox.askyesno(
            "Confirmar exclusao", "Deseja realmente excluir o(s) registro(s) selecionado(s)?"
        ):
            return

        excluidos = 0
        for item in selecionados:
            try:
                lancamento_id = int(item)
            except ValueError:
                continue

            if self.db.excluir_lancamento(self.fechamento_id, lancamento_id):
                self.grid_lancamentos.delete(item)
                excluidos += 1

        self._reaplicar_listras_grid()
        self._atualizar_total()

        if excluidos == 0:
            messagebox.showwarning(
                "Exclusao", "Nao foi possivel excluir os registros selecionados."
            )
            return

        self._set_status(f"{excluidos} registro(s) excluido(s) com sucesso.")

    def _gerar_pdf_80_colunas(self, texto: str, caminho_pdf: Path, colunas: int) -> None:
        linhas = texto.replace("\r\n", "\n").split("\n")
        if linhas and linhas[-1] == "":
            linhas = linhas[:-1]
        linhas = [linha[:colunas] for linha in linhas] or [""]

        largura_pagina = 226.77 * (colunas / 80.0)
        altura_pagina = 650.0
        margem_h = 5.0
        margem_v = 8.0
        largura_util = largura_pagina - (margem_h * 2)
        largura_char = largura_util / colunas
        tamanho_fonte = max(4.2, min(6.0, largura_char / 0.6))
        # Espaco vertical entre baselines (Courier precisa folga para nao sobrepor linhas).
        altura_linha = max(tamanho_fonte * 1.52, tamanho_fonte + 1.8)
        altura_util_texto = altura_pagina - (margem_v * 2) - tamanho_fonte
        linhas_por_pagina = max(1, int(altura_util_texto // altura_linha))

        paginas = [
            linhas[i : i + linhas_por_pagina] for i in range(0, len(linhas), linhas_por_pagina)
        ]

        objetos: list[bytes] = []

        def add_obj(dados: bytes) -> int:
            objetos.append(dados)
            return len(objetos)

        catalog_id = add_obj(b"<< /Type /Catalog >>")
        pages_id = add_obj(b"<< /Type /Pages /Kids [] /Count 0 >>")
        font_id = add_obj(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"
        )

        pagina_ids: list[int] = []
        for pagina in paginas:
            comandos = ["BT", f"/F1 {tamanho_fonte:.2f} Tf"]
            # Posicao absoluta por linha (Tm): evita deslocamento horizontal acumulado de Td apos Tj.
            for indice, linha in enumerate(pagina):
                y_linha = altura_pagina - margem_v - tamanho_fonte - (indice * altura_linha)
                comandos.append(f"1 0 0 1 {margem_h:.2f} {y_linha:.2f} Tm")
                comandos.append(f"({escapar_pdf(linha)}) Tj")
            comandos.append("ET")

            conteudo = "\n".join(comandos).encode("latin-1", "replace")
            stream = (
                f"<< /Length {len(conteudo)} >>\nstream\n".encode("ascii")
                + conteudo
                + b"\nendstream"
            )
            content_id = add_obj(stream)

            pagina_obj = (
                "<< /Type /Page "
                f"/Parent {pages_id} 0 R "
                f"/MediaBox [0 0 {largura_pagina:.2f} {altura_pagina:.2f}] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
            pagina_ids.append(add_obj(pagina_obj))

        kids = " ".join(f"{pid} 0 R" for pid in pagina_ids)
        objetos[pages_id - 1] = (
            f"<< /Type /Pages /Kids [{kids}] /Count {len(pagina_ids)} >>"
        ).encode("ascii")
        objetos[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii")

        pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for indice, obj in enumerate(objetos, start=1):
            offsets.append(len(pdf))
            pdf.extend(f"{indice} 0 obj\n".encode("ascii"))
            pdf.extend(obj)
            pdf.extend(b"\nendobj\n")

        xref_inicio = len(pdf)
        pdf.extend(f"xref\n0 {len(objetos) + 1}\n".encode("ascii"))
        pdf.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))

        trailer = (
            f"trailer\n<< /Size {len(objetos) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_inicio}\n%%EOF\n"
        )
        pdf.extend(trailer.encode("ascii"))
        caminho_pdf.write_bytes(pdf)

    def _imprimir_pdf_windows(self, caminho_pdf: Path) -> bool:
        caminho_abs = str(caminho_pdf.resolve())
        selecao = self.impressora_var.get().strip()

        if not selecao or selecao == IMPRESSORA_PADRAO_LABEL:
            try:
                os.startfile(caminho_abs, "print")
                return True
            except Exception:
                return False

        try:
            import win32api  # type: ignore
        except ImportError:
            try:
                os.startfile(caminho_abs, "print")
                return True
            except Exception:
                return False

        try:
            resultado = int(win32api.ShellExecute(0, "printto", caminho_abs, selecao, ".", 0))
            return resultado > 32
        except Exception:
            try:
                os.startfile(caminho_abs, "print")
                return True
            except Exception:
                return False

    def gerar_relatorio_pdf(self) -> None:
        texto, _ = self.gerar_texto_cupom()
        colunas = self.cfg.colunas_cupom
        try:
            self._gerar_pdf_80_colunas(texto, PDF_PATH, colunas)
        except Exception as exc:
            self._set_status("Falha ao gerar PDF do relatorio.")
            messagebox.showerror("PDF", f"Nao foi possivel gerar o PDF.\nErro: {exc}")
            return

        if self._imprimir_pdf_windows(PDF_PATH):
            self._set_status("Relatorio PDF enviado para impressao.")
            messagebox.showinfo(
                "PDF",
                f"Relatorio PDF ({colunas} colunas) gerado e enviado para impressao.",
            )
            return

        self._set_status("PDF gerado com sucesso.")
        messagebox.showinfo(
            "PDF",
            f"Relatorio PDF ({colunas} colunas) gerado com sucesso.\n"
            f"Arquivo: {PDF_PATH}",
        )

    def gerar_texto_cupom(self) -> tuple[str, frozenset[int]]:
        operador_cupom = self.operador_var.get().strip() or "-"
        data_cupom = self.data_var.get().strip() or datetime.now().strftime("%d/%m/%Y")
        return self._gerar_texto_cupom_de(
            self.fechamento_id,
            operador_cupom=operador_cupom,
            data_cupom=data_cupom,
        )

    def imprimir_segunda_via(self) -> None:
        selecionados = self.grid_fechamentos.selection()
        if not selecionados:
            messagebox.showwarning(
                "2ª Via", "Selecione um fechamento na aba 2ª Via para imprimir."
            )
            return

        fechamento_id = selecionados[0]
        valores = self.grid_fechamentos.item(fechamento_id, "values")
        operador = str(valores[1]) if len(valores) > 1 else "-"
        data_mov = str(valores[2]) if len(valores) > 2 else "-"

        texto, indices_negrito = self._gerar_texto_cupom_de(
            fechamento_id,
            operador_cupom=operador or "-",
            data_cupom=data_mov or "-",
        )
        CUPOM_PATH.write_text(texto, encoding="utf-8")

        if self._enviar_para_impressora_windows(
            texto,
            indices_negrito=indices_negrito,
            colunas_max=self.cfg.colunas_cupom,
        ):
            nome = self._resolver_nome_impressora_efetiva() or "padrao"
            self._set_status(f"2ª via do fechamento {fechamento_id} enviada para {nome}.")
            messagebox.showinfo(
                "2ª Via",
                f"2ª via do fechamento {fechamento_id} enviada para impressora ({nome}).",
            )
            return

        if self._imprimir_com_notepad():
            self._set_status(f"2ª via do fechamento {fechamento_id} enviada via Notepad.")
            messagebox.showinfo("2ª Via", "2ª via enviada para impressao usando o Notepad.")
            return

        messagebox.showwarning(
            "2ª Via",
            "Nao foi possivel imprimir automaticamente a 2ª via.\n"
            f"O cupom foi salvo em:\n{CUPOM_PATH}",
        )

    def imprimir_cupom(self) -> None:
        texto, indices_negrito = self.gerar_texto_cupom()
        CUPOM_PATH.write_text(texto, encoding="utf-8")

        if self._enviar_para_impressora_windows(
            texto,
            indices_negrito=indices_negrito,
            colunas_max=self.cfg.colunas_cupom,
        ):
            nome = self._resolver_nome_impressora_efetiva() or "padrao"
            self._set_status(f"Cupom enviado para impressora: {nome}")
            messagebox.showinfo(
                "Impressao",
                f"Cupom enviado para impressora ({nome}) em formato de {self.cfg.colunas_cupom} colunas.",
            )
            return

        if self._imprimir_com_notepad():
            self._set_status("Cupom enviado para impressao via Notepad.")
            messagebox.showinfo(
                "Impressao",
                "Cupom enviado para impressao usando o Notepad.",
            )
            return

        self._set_status("Falha na impressao automatica. Cupom salvo em arquivo.")
        messagebox.showwarning(
            "Impressao",
            "Nao foi possivel imprimir automaticamente.\n"
            f"O cupom foi salvo em:\n{CUPOM_PATH}",
        )

    def _enviar_para_impressora_windows(
        self,
        texto: str,
        *,
        indices_negrito: frozenset[int] | None = None,
        colunas_max: int | None = None,
    ) -> bool:
        try:
            import win32print  # type: ignore
        except ImportError:
            return False

        try:
            nome_impressora = self._resolver_nome_impressora_efetiva()
            if not nome_impressora:
                return False
            handle = win32print.OpenPrinter(nome_impressora)
        except Exception:
            return False

        try:
            job = win32print.StartDocPrinter(handle, 1, ("Fechamento de Caixa", None, "RAW"))
            win32print.StartPagePrinter(handle)
            payload = texto_cupom_para_bytes_raw(
                texto,
                esc_pos=self.cfg.impressao_esc_pos,
                linhas_negrito=self.cfg.impressao_linhas_negrito,
                corte_final=self.cfg.impressao_corte_papel,
                indices_negrito=indices_negrito,
                colunas_max=colunas_max,
            )
            offset = 0
            chunk_sz = 4096
            while offset < len(payload):
                chunk = payload[offset : offset + chunk_sz]
                win32print.WritePrinter(handle, chunk)
                offset += len(chunk)
            win32print.EndPagePrinter(handle)
            win32print.EndDocPrinter(handle)
            return bool(job)
        except Exception:
            return False
        finally:
            win32print.ClosePrinter(handle)

    def _imprimir_com_notepad(self) -> bool:
        try:
            subprocess.run(
                ["notepad.exe", "/p", str(CUPOM_PATH)],
                check=False,
                timeout=20,
            )
            return True
        except Exception:
            return False

    def _ao_fechar(self) -> None:
        self.db.close()
        self.destroy()


if __name__ == "__main__":
    app = FechamentoCaixaApp()
    app.mainloop()
