# Fechamento de Caixa (Python + Tkinter)

Aplicativo desktop para registrar lançamentos de caixa, imprimir cupom térmico, gerar PDF e emitir 2ª via por operador/data.

## Funcionalidades

- Cadastro de lançamentos com:
  - Operador
  - Data de movimento
  - Forma de pagamento
  - Valor
- Grid de lançamentos com exclusão de itens.
- Total geral automático.
- Impressão de cupom em impressora térmica (RAW/ESC-POS quando disponível).
- Geração de relatório em PDF.
- Aba **2ª Via** para pesquisar fechamentos e reimprimir cupom.
- Persistência da última impressora selecionada.

## Requisitos (modo Python)

- Windows
- Python 3.13+
- Dependências:
  - `pywin32` (para listar/imprimir via spooler RAW no Windows)

Instalação de dependência:

```bash
pip install pywin32
```

## Como executar (modo fonte)

Na pasta do projeto:

```powershell
python main.py
```

## Configuração (`fechamento_caixa.ini`)

Seções principais:

- `[cupom]`
  - `razao_social`
  - `endereco` (use `|` para quebrar em linhas)
- `[formas_pagamento]`
  - `lista` (separada por vírgula)
- `[operadores]`
  - `lista` (separada por vírgula)
- `[impressao]`
  - `colunas_cupom` (recomendado 42–48 para bobina 80 mm)
  - `esc_pos` (`sim`/`nao`)
  - `linhas_negrito`
  - `corte_papel`
  - `impressora_selecionada`

## Estrutura de arquivos gerados

- `fechamento_caixa.db` (SQLite)
- `ultimo_cupom.txt`
- `relatorio_fechamento.pdf`

## Distribuição do executável

A pasta `distribuicao_exe` contém os arquivos para rodar em outro computador:

- `FechamentoCaixa.exe`
- `fechamento_caixa.ini`
- `LEIA-ME.txt`

Também existe o pacote zip:

- `distribuicao_exe.zip`

Se quiser levar os dados já cadastrados, copie também o arquivo `fechamento_caixa.db` para a mesma pasta do executável.

