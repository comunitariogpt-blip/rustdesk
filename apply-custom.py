#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply-custom.py — embute a configuracao de servidor do `custom.env` no codigo
antes da compilacao.

Uso:
    python3 apply-custom.py                    # le custom.env e aplica os patches
    python3 apply-custom.py --check            # so mostra o que seria feito
    python3 apply-custom.py --variant operador # forca a variante (ignora o .env)

O que ele altera (apenas se o valor correspondente estiver preenchido):
  1. libs/hbb_common/src/config.rs -> RENDEZVOUS_SERVERS  (RENDEZVOUS_SERVER)
  2. libs/hbb_common/src/config.rs -> RS_PUB_KEY          (RS_PUB_KEY)
  3. src/common.rs                 -> fallback da API      (API_SERVER)
  4. flutter/lib/consts.dart       -> login obrigatorio    (REQUIRE_LOGIN)
  5. libs/hbb_common/src/config.rs -> HARD/BUILTIN_SETTINGS (BUILD_VARIANT)
  6. libs/hbb_common/src/config.rs -> APP_NAME              (APP_NAME)
     flutter/windows/runner/Runner.rc -> nome no .exe do Windows
     res/*.desktop                -> nome no menu do Linux

Variaveis de ambiente com os mesmos nomes tem prioridade sobre o custom.env
(util para configurar via GitHub Secrets sem commitar valores).

O script e idempotente: pode rodar varias vezes sem efeito colateral, e
trocar de variante sobrescreve a anterior.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(ROOT, "custom.env")
CONFIG_RS = os.path.join(ROOT, "libs", "hbb_common", "src", "config.rs")
COMMON_RS = os.path.join(ROOT, "src", "common.rs")
CONSTS_DART = os.path.join(ROOT, "flutter", "lib", "consts.dart")
RUNNER_RC = os.path.join(ROOT, "flutter", "windows", "runner", "Runner.rc")
DESKTOP_FILES = (
    os.path.join(ROOT, "res", "rustdesk.desktop"),
    os.path.join(ROOT, "res", "rustdesk-link.desktop"),
)

# APP_NAME vira caminho de pasta de config, nome de servico, prefixo de URI
# (<app_name>://) e literal Rust. Restringir aos caracteres seguros evita tanto
# caminho invalido no Windows quanto injecao no codigo gerado.
APP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")

# ---------------------------------------------------------------------------
# Variantes de build. As chaves abaixo sao opcoes nativas do RustDesk:
#   conn-type=outgoing    so conecta em outros (esconde o proprio ID/senha)
#   conn-type=incoming    so recebe conexao (esconde o campo de ID remoto)
#   disable-installation  remove a opcao de instalar (build portable)
#   disable-account       remove login/conta da interface
#   disable-settings      remove o menu de configuracoes
# ---------------------------------------------------------------------------
VARIANTS = {
    "operador": {
        "hard": [
            ("conn-type", "outgoing"),
            ("disable-installation", "Y"),
        ],
        "builtin": [
            ("hide-powered-by-me", "Y"),
        ],
    },
    "cliente": {
        "hard": [
            ("conn-type", "incoming"),
            ("disable-account", "Y"),
            ("disable-settings", "Y"),
        ],
        "builtin": [
            ("hide-powered-by-me", "Y"),
            ("disable-settings", "Y"),
            ("hide-server-settings", "Y"),
            ("hide-security-settings", "Y"),
            ("hide-network-settings", "Y"),
            ("hide-proxy-settings", "Y"),
            ("hide-websocket-settings", "Y"),
        ],
    },
    "completo": {"hard": [], "builtin": [("hide-powered-by-me", "Y")]},
}

# Aceita sinonimos em pt/en para a mesma variante.
VARIANT_ALIASES = {
    "operador": "operador", "operator": "operador", "op": "operador",
    "cliente": "cliente", "client": "cliente", "qs": "cliente",
    "suporte": "cliente", "quicksupport": "cliente",
    "completo": "completo", "full": "completo", "": "completo",
}


def rust_map(pairs):
    """Gera a expressao Rust de um HashMap<String, String> para lazy_static."""
    if not pairs:
        return "Default::default()"
    items = ",".join(
        '("%s".to_owned(),"%s".to_owned())' % (k, v) for k, v in pairs
    )
    return "RwLock::new(vec![%s].into_iter().collect())" % items


def patch_static_map(path, name, pairs, label, check_only):
    """Reescreve um lazy_static RwLock<HashMap<..>> do config.rs.

    O padrao aceita tanto o valor original (Default::default()) quanto um
    ja injetado, para que trocar de variante sobrescreva a anterior.
    """
    patch_file(
        path,
        r'pub static ref %s: RwLock<HashMap<String, String>> = [^;]+;' % name,
        'pub static ref %s: RwLock<HashMap<String, String>> = %s;'
        % (name, rust_map(pairs)),
        label,
        check_only,
    )


def load_env():
    values = {
        "APP_NAME": "",
        "RENDEZVOUS_SERVER": "",
        "RS_PUB_KEY": "",
        "API_SERVER": "",
        "REQUIRE_LOGIN": "",
        "BUILD_VARIANT": "",
    }
    if os.path.isfile(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key in values:
                    values[key] = val
    # Variaveis de ambiente (ex.: GitHub Secrets) tem prioridade
    for key in values:
        env_val = os.environ.get(key, "").strip()
        if env_val:
            values[key] = env_val
    return values


def patch_file(path, pattern, replacement, label, check_only):
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if replacement in content:
        print(f"OK  : {label} ja estava aplicado.")
        return
    new_content, count = re.subn(pattern, replacement, content, count=1)
    if count == 0:
        print(f"ERRO: padrao de '{label}' nao encontrado em {path}.")
        print("      O codigo upstream pode ter mudado; ajuste apply-custom.py.")
        sys.exit(1)
    if check_only:
        print(f"FARIA: {label} em {os.path.relpath(path, ROOT)}")
        return
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(new_content)
    print(f"OK  : {label} aplicado em {os.path.relpath(path, ROOT)}")


def resolve_variant(values):
    """Le a variante de --variant, do ambiente ou do custom.env, nesta ordem."""
    raw = values["BUILD_VARIANT"]
    if "--variant" in sys.argv:
        i = sys.argv.index("--variant")
        if i + 1 >= len(sys.argv):
            print("ERRO: --variant exige um valor (operador | cliente | completo).")
            sys.exit(1)
        raw = sys.argv[i + 1]
    raw = raw.strip().lower()
    if raw not in VARIANT_ALIASES:
        print("ERRO: BUILD_VARIANT invalido: %r" % raw)
        print("      Use: operador, cliente ou completo.")
        sys.exit(1)
    return VARIANT_ALIASES[raw]


def patch_app_name(app_name, check_only):
    """Troca "RustDesk" pelo APP_NAME em tudo que o usuario final enxerga.

    O grosso vem de graca: `lang.rs` ja substitui "RustDesk" pelo APP_NAME em
    toda string traduzida quando `is_rustdesk()` e falso, e a barra de titulo /
    taskbar do Windows le o nome via `get_rustdesk_app_name` (src/flutter.rs),
    que devolve o mesmo APP_NAME. Aqui so precisamos trocar a origem do valor e
    os textos que ficam fora do Rust (recurso do .exe e atalhos do Linux).
    """
    if not APP_NAME_RE.match(app_name):
        print("ERRO: APP_NAME invalido: %r" % app_name)
        print("      Use de 1 a 32 caracteres, apenas letras, numeros e hifen")
        print("      (o valor vira pasta de config, nome de servico e prefixo")
        print("      de URI <app_name>://).")
        sys.exit(1)

    patch_file(
        CONFIG_RS,
        r'pub static ref APP_NAME: RwLock<String> = RwLock::new\("[^"]*"\.to_owned\(\)\);',
        'pub static ref APP_NAME: RwLock<String> = RwLock::new("%s".to_owned());'
        % app_name,
        "APP_NAME = %s" % app_name,
        check_only,
    )

    # Recurso de versao do .exe do Windows: e o que o Gerenciador de Tarefas
    # mostra na coluna "Nome" e o que aparece em Propriedades -> Detalhes.
    # InternalName/OriginalFilename ficam como estao: o binario continua
    # se chamando rustdesk.exe.
    if os.path.isfile(RUNNER_RC):
        patch_file(
            RUNNER_RC,
            r'VALUE "FileDescription", "[^"]*"',
            'VALUE "FileDescription", "%s Remote Desktop"' % app_name,
            "APP_NAME em FileDescription (Runner.rc)",
            check_only,
        )
        patch_file(
            RUNNER_RC,
            r'VALUE "ProductName", "[^"]*"',
            'VALUE "ProductName", "%s"' % app_name,
            "APP_NAME em ProductName (Runner.rc)",
            check_only,
        )

    # Atalhos do Linux. `Exec=`/`Icon=` continuam apontando para "rustdesk"
    # (nome do binario e do arquivo de icone instalado); so o nome exibido e o
    # esquema de URI acompanham o APP_NAME — get_uri_prefix() em src/common.rs
    # deriva o esquema de APP_NAME.to_lowercase().
    for path in DESKTOP_FILES:
        if not os.path.isfile(path):
            continue
        # O \n faz parte do padrao e da substituicao de proposito: sem ele a
        # checagem de idempotencia de patch_file ("replacement in content")
        # casaria com "GenericName=<APP_NAME> ..." e pularia o patch.
        patch_file(
            path,
            r'(?m)\nName=(?!Open a New Window$).*$',
            "\nName=%s" % app_name,
            "APP_NAME em %s" % os.path.basename(path),
            check_only,
        )
        with open(path, "r", encoding="utf-8") as f:
            if "x-scheme-handler/" in f.read():
                patch_file(
                    path,
                    r'x-scheme-handler/[^;]*;',
                    "x-scheme-handler/%s;" % app_name.lower(),
                    "esquema de URI em %s" % os.path.basename(path),
                    check_only,
                )


def main():
    check_only = "--check" in sys.argv
    values = load_env()
    variant = resolve_variant(values)

    if not any(values.values()) and variant == "completo":
        print("custom.env sem valores preenchidos — nada a fazer (build padrao).")
        return

    if not os.path.isfile(CONFIG_RS):
        print(f"ERRO: {CONFIG_RS} nao existe.")
        print("      Baixe os submodulos: git submodule update --init --recursive")
        sys.exit(1)

    if values["APP_NAME"]:
        patch_app_name(values["APP_NAME"], check_only)

    if values["RENDEZVOUS_SERVER"]:
        patch_file(
            CONFIG_RS,
            r'pub const RENDEZVOUS_SERVERS: &\[&str\] = &\[[^\]]*\];',
            'pub const RENDEZVOUS_SERVERS: &[&str] = &["%s"];'
            % values["RENDEZVOUS_SERVER"],
            "RENDEZVOUS_SERVER = %s" % values["RENDEZVOUS_SERVER"],
            check_only,
        )

    if values["RS_PUB_KEY"]:
        patch_file(
            CONFIG_RS,
            r'pub const RS_PUB_KEY: &str = "[^"]*";',
            'pub const RS_PUB_KEY: &str = "%s";' % values["RS_PUB_KEY"],
            "RS_PUB_KEY = %s..." % values["RS_PUB_KEY"][:12],
            check_only,
        )

    if values["API_SERVER"]:
        patch_file(
            COMMON_RS,
            r'"https://admin\.rustdesk\.com"\.to_owned\(\)',
            '"%s".to_owned()' % values["API_SERVER"],
            "API_SERVER = %s" % values["API_SERVER"],
            check_only,
        )

    # O build "cliente" nao tem login; forcar REQUIRE_LOGIN nele so criaria um
    # dialogo que a propria variante desabilita (disable-account).
    require_login = values["REQUIRE_LOGIN"].lower() in (
        "y", "yes", "s", "sim", "1", "true"
    )
    effective_login = require_login and variant != "cliente"
    label = "REQUIRE_LOGIN = %s" % ("Y" if effective_login else "N")
    if require_login and not effective_login:
        label += " (ignorado: a variante 'cliente' nao tem login)"
    patch_file(
        CONSTS_DART,
        r'const bool kRequireLoginDefault = (?:false|true);',
        'const bool kRequireLoginDefault = %s;'
        % ("true" if effective_login else "false"),
        label,
        check_only,
    )

    spec = VARIANTS[variant]
    patch_static_map(CONFIG_RS, "HARD_SETTINGS", spec["hard"],
                     "BUILD_VARIANT = %s (HARD_SETTINGS)" % variant, check_only)
    patch_static_map(CONFIG_RS, "BUILTIN_SETTINGS", spec["builtin"],
                     "BUILD_VARIANT = %s (BUILTIN_SETTINGS)" % variant, check_only)

    # api-server precisa entrar como OPCAO, nao so como fallback em common.rs.
    # get_api_server_() so usa aquele fallback quando NAO ha servidor de
    # rendezvous configurado; havendo, ele deriva a API de
    # http://<rendezvous>:21114 e ignora o valor embutido. Como OVERWRITE, o
    # valor vence a derivacao e tambem qualquer config local antiga da maquina.
    # As constantes RENDEZVOUS_SERVERS / RS_PUB_KEY sao apenas o PADRAO: a
    # config local da maquina (%APPDATA%/RustDesk) vence sobre elas. Numa
    # maquina que ja rodou outro build (ou o RustDesk oficial), o servidor
    # antigo continuaria valendo. Gravando tambem em OVERWRITE_SETTINGS, o
    # servidor do build vence qualquer config preexistente.
    overwrite = []
    if values["RENDEZVOUS_SERVER"]:
        overwrite.append(("custom-rendezvous-server", values["RENDEZVOUS_SERVER"]))
    if values["RS_PUB_KEY"]:
        overwrite.append(("key", values["RS_PUB_KEY"]))
    if values["API_SERVER"]:
        overwrite.append(("api-server", values["API_SERVER"]))
    # Sem aviso de "nova versao disponivel": este e um cliente de marca
    # propria, atualizado pelo painel, nao pelos releases do RustDesk.
    # enable-check-update desliga a checagem da tela inicial; allow-auto-update
    # desliga o updater periodico (updater.rs nao consulta a primeira chave).
    overwrite.append(("allow-auto-update", "N"))
    patch_static_map(CONFIG_RS, "OVERWRITE_SETTINGS", overwrite,
                     "servidor/chave/API embutidos + auto-update off "
                     "(OVERWRITE_SETTINGS)", check_only)
    patch_static_map(CONFIG_RS, "OVERWRITE_LOCAL_SETTINGS",
                     [("enable-check-update", "N")],
                     "Aviso de nova versao desativado (OVERWRITE_LOCAL_SETTINGS)",
                     check_only)

    print("Pronto. Agora compile normalmente (build.py / cargo / flutter).")


if __name__ == "__main__":
    main()
