# Baixador NFS-e Nacional

Versão v5: percorre corretamente todos os lotes usando o `ultNSU` retornado
pela API e salva cada XML com a chave de acesso como nome (`CHAVE.xml`).

Aplicação local para consultar, em lote, os XMLs distribuídos pelo Ambiente de Dados Nacional (ADN) da NFS-e usando e-CPF ou e-CNPJ A1/A3 instalado no Windows.

## O que esta versão faz

- apresenta as três formas de acesso: certificado, conta gov.br e usuário/senha do Portal;
- abre os acessos gov.br e usuário/senha diretamente no site oficial, sem capturar ou armazenar credenciais;
- usa mTLS com e-CPF ou e-CNPJ A1/A3 instalado no Windows;
- encontra certificados com chave privada em **Pessoal** (usuário atual e computador local) diretamente pela API criptográfica do Windows e permite selecioná-los pela tela;
- ao selecionar um certificado instalado, usa diretamente a chave protegida pelo Windows: não exporta o certificado e não pede senha;
- aceita CPF (11 dígitos) ou CNPJ (14 dígitos) e escolhe automaticamente `cpfConsulta` ou `cnpjConsulta`;
- percorre os NSUs recebidos, extrai XMLs (inclusive quando vierem em Base64/GZip), e filtra o período e o papel do CPF/CNPJ (emitente, tomador ou ambos);
- continua consultando os lotes de até 50 documentos até alcançar o `maxNSU`, sem saltar os NSUs intermediários;
- usa a chave de acesso da NFS-e como nome do arquivo (`CHAVE.xml`), inclusive quando ela estiver no atributo `Id` de `infNFSe`;
- grava os XMLs em `XML/Emitidas` e `XML/Tomadas` e cria um `resultado.csv`.

## Limite atual: PDF/DANFSe

Em 03/08/2026 a API antiga de geração de DANFSe foi suspensa. Por isso, este projeto **não declara os PDFs gerados como DANFSe válido**. A próxima etapa é implementar o renderizador integral conforme a Nota Técnica 008/2026 e validá-lo com XMLs reais; isso exige uma amostra de NFS-e, de preferência anonimizada, e conferência do layout final.

## Como executar no Windows

1. Instale Python 3.11 ou mais recente.
2. Abra o Prompt na pasta do projeto.
3. Execute `py app.py`.

Para gerar um `.exe` depois da validação: `py -m pip install pyinstaller` e `py -m PyInstaller --noconsole --onefile --name BaixadorNFSe app.py`.

### Aplicativo com ícone, sem CMD e sem Python no computador de destino

No computador Windows onde o Python já está instalado, dê dois cliques em
`GERAR_EXE.bat`. Ao final, será criado `Baixador NFSe.exe` na mesma pasta.
Esse arquivo pode ser copiado isoladamente para outros computadores Windows e
aberto com dois cliques. O certificado precisa estar disponível no Windows do
computador em que o aplicativo será utilizado. Para A3, o token/cartão deve
estar conectado e o driver do fabricante instalado.

## Segurança

- Não inclua o `.pfx/.p12` no projeto e não o envie por mensagem.
- Esta versão seleciona exclusivamente certificados já instalados no Windows; não há campo de senha.
- Certificados A1 normalmente não pedem senha depois de instalados. Certificados A3 podem solicitar o PIN a cada uso; o aplicativo não armazena nem contorna essa proteção.
- Use somente certificado e CPF/CNPJ para os quais você tenha representação/autorização.
- O download automático pela API continua disponível no modo certificado. Nos modos gov.br e usuário/senha, esta versão abre o Portal Web para login manual; não declara uma automação interna ainda não validada como se estivesse pronta.

## Fontes oficiais

- Manual de APIs do ADN (12/02/2026): https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/documentacao-atual/manual-contribuintes-apis-adn-sistema-nacional-nfse.pdf
- Endereços de produção e produção restrita: https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/apis-prod-restrita-e-producao
- Especificação atual do DANFSe (NT 008/2026): https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/rtc/nt-008-se-cgnfse-danfse-20260714-v1-02.pdf
