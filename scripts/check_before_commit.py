#!/usr/bin/env python3
"""check_before_commit.py
=========================

Controlli eseguiti automaticamente dal pre-commit hook (vedi hooks/pre-commit)
prima di ogni commit, per evitare di scoprire solo su Render che qualcosa
non va (come successo con Pillow mancante da webapp/requirements.txt).

Controlli:
  1. Sintassi di tutti i file .py in staging (py_compile).
  2. Tutti i template usati in render_template(...) in webapp/app.py esistono
     davvero in webapp/templates/.
  3. Tutti i moduli importati in webapp/*.py hanno il pacchetto pip
     corrispondente elencato in webapp/requirements.txt.
  4. Simulazione del boot esatto che farebbe Render: venv pulito,
     `pip install -r webapp/requirements.txt`, poi `create_app()`.

Se un controllo fallisce, esce con codice diverso da 0: git blocca il commit.
"""

from __future__ import annotations

import ast
import py_compile
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBAPP_DIR = ROOT / "webapp"
VENV_DIR = ROOT / ".precommit-venv"

# Modulo importato in Python -> nome pacchetto pip atteso in requirements.txt.
# Se un modulo non è in questa mappa, si assume che il nome pip coincida.
MODULE_TO_PACKAGE = {
    "flask": "flask",
    "flask_sqlalchemy": "flask-sqlalchemy",
    "sqlalchemy": "flask-sqlalchemy",  # arriva insieme a Flask-SQLAlchemy
    "werkzeug": "flask",  # dipendenza transitiva di Flask
    "jinja2": "flask",
    "dotenv": "python-dotenv",
    "PIL": "pillow",
}

# Moduli di libreria standard usati nel progetto (evitiamo di dipendere da
# sys.stdlib_module_names per compatibilità con versioni Python più vecchie).
STDLIB_EXTRA = {
    "os", "re", "sys", "json", "pathlib", "functools", "uuid", "unicodedata",
    "typing", "datetime", "sqlite3", "logging", "itertools", "collections",
    "threading", "time", "__future__",
}


def _local_module_names() -> set[str]:
    """Nomi di moduli locali del progetto (root e webapp/), da non trattare
    come pacchetti pip da cercare in requirements.txt."""
    return {
        path.stem
        for path in list(ROOT.glob("*.py")) + list(WEBAPP_DIR.glob("*.py"))
    }


def fail(message: str) -> None:
    print(f"\n[PRE-COMMIT] ERRORE: {message}\n")
    sys.exit(1)


def get_staged_files() -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line.strip()]


def check_python_syntax(staged_files: list[Path]) -> None:
    py_files = [f for f in staged_files if f.suffix == ".py" and f.is_file()]
    for file_path in py_files:
        try:
            py_compile.compile(str(file_path), doraise=True)
        except py_compile.PyCompileError as error:
            fail(f"Errore di sintassi in {file_path.relative_to(ROOT)}:\n{error}")
    if py_files:
        print(f"[PRE-COMMIT] Sintassi OK per {len(py_files)} file Python modificati.")


def _render_template_names(source_path: Path) -> list[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if func_name == "render_template" and node.args:
                first_arg = node.args[0]
                if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                    names.append(first_arg.value)
    return names


def check_templates_exist() -> None:
    app_py = WEBAPP_DIR / "app.py"
    templates_dir = WEBAPP_DIR / "templates"
    if not app_py.is_file():
        return

    missing = sorted(
        {
            name
            for name in _render_template_names(app_py)
            if not (templates_dir / name).is_file()
        }
    )
    if missing:
        fail(
            "webapp/app.py chiama render_template(...) con questi file mancanti "
            "in webapp/templates/: " + ", ".join(missing)
        )
    print("[PRE-COMMIT] Tutti i template referenziati da app.py esistono.")


def _imported_modules() -> set[str]:
    modules: set[str] = set()
    for py_file in WEBAPP_DIR.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    modules.add(node.module.split(".")[0])
    return modules


def check_requirements_cover_imports() -> None:
    requirements_file = WEBAPP_DIR / "requirements.txt"
    if not requirements_file.is_file():
        return

    requirements_text = requirements_file.read_text(encoding="utf-8").lower()
    local_modules = _local_module_names()
    missing = []
    for module in sorted(_imported_modules()):
        if module in STDLIB_EXTRA or module in local_modules:
            continue
        package = MODULE_TO_PACKAGE.get(module, module).lower()
        if package not in requirements_text:
            missing.append(f"{module} (pacchetto atteso: {package})")

    if missing:
        fail(
            "Questi import usati in webapp/*.py non risultano in "
            "webapp/requirements.txt: " + "; ".join(missing) + "\n"
            "Aggiungi il pacchetto mancante a webapp/requirements.txt prima di "
            "committare, altrimenti il deploy su Render fallirà."
        )
    print("[PRE-COMMIT] webapp/requirements.txt copre tutti gli import usati.")


def _venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _ensure_venv() -> Path:
    python_exe = _venv_python()
    if not python_exe.is_file():
        print("[PRE-COMMIT] Creo un venv di verifica isolato (solo la prima volta)...")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    return python_exe


def check_webapp_boot() -> None:
    """Riproduce esattamente ciò che fa Render: venv pulito + requirements.txt
    + avvio dell'app tramite create_app()."""
    requirements_file = WEBAPP_DIR / "requirements.txt"
    if not requirements_file.is_file():
        return

    python_exe = _ensure_venv()

    install = subprocess.run(
        [str(python_exe), "-m", "pip", "install", "-q", "-r", str(requirements_file)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if install.returncode != 0:
        fail(
            "`pip install -r webapp/requirements.txt` è fallito in un venv pulito:\n"
            + install.stdout + install.stderr
        )

    boot = subprocess.run(
        [str(python_exe), "-c", "from app import create_app; create_app(); print('BOOT-OK')"],
        cwd=WEBAPP_DIR,
        capture_output=True,
        text=True,
    )
    if boot.returncode != 0 or "BOOT-OK" not in boot.stdout:
        fail(
            "L'app NON si avvia usando solo le dipendenze di "
            "webapp/requirements.txt (esattamente ciò che farebbe Render):\n"
            + boot.stdout + boot.stderr
        )
    print("[PRE-COMMIT] Avvio dell'app OK con le sole dipendenze di requirements.txt "
          "(simulazione del deploy Render).")


def main() -> None:
    staged_files = get_staged_files()
    if not staged_files:
        return

    check_python_syntax(staged_files)

    webapp_touched = any(
        str(f.relative_to(ROOT)).replace("\\", "/").startswith("webapp/")
        for f in staged_files
    )
    if webapp_touched:
        check_templates_exist()
        check_requirements_cover_imports()
        check_webapp_boot()

    print("\n[PRE-COMMIT] Tutti i controlli superati, procedo con il commit.\n")


if __name__ == "__main__":
    main()

