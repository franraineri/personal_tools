#!/usr/bin/env python3
"""Auto-genera tablas LaTeX de la tesis desde las fuentes de verdad del proyecto.

Reutiliza la infraestructura existente (NO re-parsea datos crudos):
  - ``scripts/analysis/generate_best_results_scoreboard.py``
        scan_all_reports / parse_eval_report / compute_best_per_topology_n
        + generate_scoreboard (regenera best_results_scoreboard.json)
  - ``qmbp_simulation.framework.result_index.ResultIndex``  (conteos de campaña)
  - ``qmbp_simulation.analysis.metrics.compute_deploy_summary``  (agregados por-N)

Produce tablas centradas en información general y de perspectiva, en el mismo
espíritu que ``results/best_results_scoreboard.md``:

  auto_scoreboard         Mejor resultado por (topología × N)  [todas las topologías]
  auto_coverage           Matriz de cobertura: calificación por topología × N
  auto_campaign           Veredictos/conteos de campaña (incl. taxonomía 46 = 26 + 20)
  auto_heavy_hex_intra_n  Tabla por-h intra-N de heavy_hex (interpolación)
  auto_heavy_hex_large_n  Tabla por-h large-N de heavy_hex (extrapolación zero-shot)

Reglas (steering thesis-style-and-process):
  - Fidelidad al dato: nunca inventa números; donde falta un dato deja un
    marcador ``%TODO-<TOPICO>`` en el lugar exacto del .tex.
  - Todo TODO se vuelca a ``tesis_todos.txt`` agrupado por tópico, con el número
    de línea donde aparece. Las inconsistencias detectadas se anexan al final.

Uso:
    .venv/bin/python scripts/general_project_maintenance/generate_thesis_tables.py
    .venv/bin/python scripts/general_project_maintenance/generate_thesis_tables.py --out-dir internal/tables
    .venv/bin/python scripts/general_project_maintenance/generate_thesis_tables.py --no-refresh
    .venv/bin/python scripts/general_project_maintenance/generate_thesis_tables.py --only auto_scoreboard,auto_coverage
    .venv/bin/python scripts/general_project_maintenance/generate_thesis_tables.py --check-tex internal/tesis-v4.0.tex

Salida:
    <out-dir>/auto_*.tex        una tabla LaTeX por archivo (booktabs)
    <out-dir>/tesis_todos.txt   TODOs por tópico + inconsistencias detectadas
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "analysis"))

DEFAULT_OUT_DIR = ROOT / "internal" / "tables"
SCOREBOARD_JSON = ROOT / "results" / "best_results_scoreboard_p1.json"

# Topología focal del capítulo de resultados (steering thesis-writing)
FOCUS_TOPOLOGY = "heavy_hex"

# Nombres canónicos en español para topologías (steering: unificar grafías)
TOPO_ES = {
    "chain_1d": "cadena 1D",
    "heavy_hex": "heavy-hex",
    "ladder": "escalera",
    "square": "red cuadrada",
    "triangular": "triangular",
    "kagome": "kagome",
}

# ═══════════════════════════════════════════════════════════════════════════════
# Recolector de TODOs e inconsistencias
# ═══════════════════════════════════════════════════════════════════════════════


TODO_MARKER_RE = re.compile(r"^\s*%TODO-([A-Z]+):\s*(.*)$")

# Tono comercial a moderar (steering §6). Palabra completa, sin distinguir mayúsc.
TONE_WORDS_RE = re.compile(
    r"\b(demuestra\w*|garantiza\w*|exhaustiv\w+|notable\w*|excelente\w*|"
    r"potente\w*|radicalment\w*|robust\w+|óptim\w+|optim[oa]s?|"
    r"ampliamente validad\w+|madurez|coste cuántico cero|días a minutos)\b",
    re.IGNORECASE,
)
# Métricas: speedup (eliminado) y PassRate como sujeto principal (steering §5).
SPEEDUP_RE = re.compile(r"\b(speedup|aceleraci[oó]n de\s*\d|factor de mejora)\b", re.IGNORECASE)
# Error por sitio |ΔE|/N: PROHIBIDO (steering §5, se retiró por no aportar sobre
# |ΔE|). Detecta las formas LaTeX: |\Delta E|/N, \Delta E/N, \frac{\Delta E}{N},
# y la prosa "error por sitio" / "por sitio".
PER_SITE_RE = re.compile(
    r"\\Delta\s*E\s*(?:\\rvert|\|)?\s*/\s*N\b"
    r"|\\frac\{[^{}]*\\Delta\s*E[^{}]*\}\{\s*N\s*\}"
    r"|error\s+por\s+sitio|\bpor\s+sitio\b|per[-\s]site"
)
PASSRATE_RE = re.compile(r"\b(passrate|tasa de aprobaci[oó]n)\b", re.IGNORECASE)
ABS_ERR_RE = re.compile(r"\\Delta E|\|\\Delta E\||error(?:\s+energético)?\s+absoluto")
# Anglicismos de métrica prohibidos en el cuerpo (usar equivalentes en español).
ENGLISH_METRIC_RE = re.compile(r"\b(Grade|Pass|Rate|PassRate)\b")
# Énfasis en profundidad requerida (a de-enfatizar, steering §5): p ∝ N, p ≈ N/2, p = N-1.
DEPTH_EMPHASIS_RE = re.compile(
    r"p\s*\\propto\s*N|p\s*\\approx\s*N|p\s*=\s*N\s*-\s*1|p\s*=\s*N/2|N/2\s*capas"
)

# (Editorial 1) Siglas que deben definirse en el primer uso (sigla -> expansión esperada).
ACRONYMS: dict[str, str] = {
    "VQE": "Variational Quantum Eigensolver",
    "HVA": "Hamiltonian Variational Ansatz",
    "GNN": "Graph Neural Network",
    "MPNN": "Message-Passing Neural Network",
    "GIN": "Graph Isomorphism Network",
    "TFIM": "Transverse Field Ising Model",
    "MPS": "Matrix Product State",
    "DMRG": "Density Matrix Renormalization Group",
    "NISQ": "Noisy Intermediate-Scale Quantum",
    "PCA": "Principal Component Analysis",
}

# (Editorial 2) Grupos de variantes que deben unificarse (steering §6/§11).
TERM_VARIANTS: list[tuple[str, list[str]]] = [
    ("heavy-hex", [r"heavy-hex", r"Heavy-Hex", r"heavy hex", r"Heavy Hex"]),
    ("coste", [r"\bcoste\b", r"\bcosto\b"]),
    ("gap espectral", [r"gap espectral", r"brecha espectral"]),
    ("cadena 1D", [r"cadena 1D", r"chain_1d", r"cadena unidimensional"]),
    ("warm-start", [r"warm-start", r"warm start"]),
]

# (Editorial 4) Rango numérico con guion simple (debería usar -- en LaTeX).
BAD_RANGE_RE = re.compile(r"(?<![-\d])\d+(?:[.,]\d+)?-\d+(?:[.,]\d+)?(?![-\d])")
# (Editorial 3) Porcentaje sin caso absoluto (n/m) cercano.
PCT_RE = re.compile(r"\d{1,3}\\%")
ABS_CASE_RE = re.compile(r"\(\s*\d+\s*/\s*\d+\s*\)")
# Reproducibilidad: "exacto/a" junto a DMRG/MPS, y N grande + statevector.
EXACT_TN_RE = re.compile(
    r"\b(exact[oa]s?)\b[^.]*\b(DMRG|MPS)\b|\b(DMRG|MPS)\b[^.]*\b(exact[oa]s?)\b", re.IGNORECASE
)
SV_BIGN_RE = re.compile(r"statevector", re.IGNORECASE)
BIGN_RE = re.compile(r"N\s*[=>]\s*(\d+)")

# Sustituciones de tono DETERMINISTAS (frase fija -> reemplazo seguro), steering §6.
# Solo entran aquí las que no dependen del contexto (no rompen el sentido).
TONE_FIXES: list[tuple[str, str]] = [
    ("el pipeline funciona", "el pipeline satisface los criterios establecidos"),
    ("coste cuántico cero", "coste de optimización nulo en inferencia"),
    ("ampliamente validado", "validado en el régimen operativo evaluado"),
    ("ampliamente validada", "validada en el régimen operativo evaluado"),
    ("límite fundamental", "límite observado en las configuraciones evaluadas"),
    ("resultado exhaustivo", "resultado sistemático en las configuraciones evaluadas"),
    ("madurez del procedimiento", "consistencia del procedimiento"),
]

# Anglicismos (steering §3). Un ÚNICO término español por concepto.
# ANGLICISM_FIXES: reemplazo unívoco en prosa (auto-corregible con --fix-anglicisms).
# La clave es un patrón regex (con \b para límites de palabra); el valor es el
# reemplazo español canónico. Se aplican SOLO en prosa: se excluyen \texttt{},
# math $...$, comandos protegidos, comentarios, el entorno abstract y la
# bibliografía (títulos en inglés legítimos). Orden: los más específicos primero
# (p.ej. "ground truth" antes que "ground").
# SEGUROS: reemplazo que no cambia la concordancia con un artículo/adjetivo
# precedente (van sin artículo pegado, o el reemplazo mantiene género/número).
# Opción A del usuario: el auto-fix solo toca lo determinista y sin riesgo gramatical.
ANGLICISM_FIXES: list[tuple[str, str]] = [
    (r"\bnoiseless\b", "simulación ideal"),
    (r"zero-shot", "sin reentrenamiento"),
    (r"cross-topology", "entre topologías"),
    (r"cross-N", "entre tamaños"),
    (r"cross-seed", "entre semillas"),
    (r"multi-seed", "con múltiples semillas"),
    (r"multi-semilla", "con múltiples semillas"),
    (r"random initialization", "inicialización aleatoria"),
    (r"end-to-end", "de extremo a extremo"),
    (r"machine learning", "aprendizaje automático"),
    (r"\bMachine Learning\b", "Aprendizaje automático"),
    (r"message passing", "paso de mensajes"),
    (r"forward pass", "inferencia"),
    (r"pasada forward", "inferencia"),
    (r"early stopping", "parada temprana"),
    (r"early-stopping", "parada temprana"),
    (r"post-hoc", "a posteriori"),
    (r"\bepochs\b", "épocas"),
    (r"\brestarts\b", "reinicios"),
    (r"\bnegligible\b", "despreciable"),
    (r"sweet spot", "mejor compromiso"),
    (r"machine epsilon", "precisión de máquina"),
    (r"smoke test", "prueba de humo"),
]

# ANGLICISM_DETECT: términos que se DETECTAN pero NO se auto-corrigen porque:
#  - el reemplazo cambia género/número y rompería la concordancia con un artículo
#    o adjetivo vecino (ground truth, dataset, framework, area law, ...), o
#  - el reemplazo depende del contexto (deploy, benchmark, run, input/output), o
#  - el término se conserva por convención (bond-resolved), definido 1 vez en cursiva.
# Se marcan como %TODO-ANGLICISMO para corrección manual con criterio.
# (patrón, nota).
ANGLICISM_DETECT: list[tuple[str, str]] = [
    (
        r"ground truth",
        "ground truth -> datos de referencia (ajustar artículo: 'los datos', no 'el')",
    ),
    (r"ground state", "ground state -> estado fundamental"),
    (r"\bdatasets\b", "datasets -> conjuntos de datos"),
    (r"\bdataset\b", "dataset -> conjunto de datos (ajustar artículo: 'el conjunto')"),
    (r"\bframework\b", "framework -> marco (ajustar artículo/adjetivo al género masculino)"),
    (r"generalization gap", "generalization gap -> brecha de generalización (fem.: 'la brecha')"),
    (r"area law", "area law -> ley de área (fem.: 'la ley'); definir en cursiva 1 vez"),
    (r"hidden dimension", "hidden dimension -> dimensión oculta"),
    (r"learning rate", "learning rate -> tasa de aprendizaje"),
    (r"edge feature", "edge feature -> atributo de arista"),
    (r"node feature", "node feature -> atributo de nodo"),
    (r"global mean pool", "global mean pool -> agregación global por media"),
    (r"\bpooling\b", "pooling -> agregación"),
    (r"\boverfit(ting)?\b", "overfit/overfitting -> sobreajuste"),
    (
        r"conocimiento accionable",
        "conocimiento accionable -> información útil para delimitar el dominio",
    ),
    (r"\bactionable\b", "actionable -> útil (parafrasear)"),
    (r"\bdeploy\b", "deploy -> despliegue / evaluación en inferencia (según contexto)"),
    (r"\bbenchmark\b", "benchmark -> prueba comparativa / banco de pruebas (según contexto)"),
    (r"\brun(s)?\b", "run/runs -> ejecución/ejecuciones (si es prosa, no nombre de comando)"),
    (r"\blandscape\b", "landscape -> paisaje de optimización"),
    (r"\binput\b", "input -> entrada"),
    (r"\boutput\b", "output -> salida"),
    (r"\bgrid\b", "grid -> malla / rejilla"),
    (
        r"bond-resolved",
        "conservable como 'parametrización por enlace (bond-resolved)'; definir 1 vez",
    ),
]

# Términos que se conservan (asentados) — no marcar. Definir en cursiva 1ª vez.
ANGLICISM_KEEP = ("pipeline", "warm-start", "ansatz", "qubit", "gap")

# Guiones tipográficos: el usuario pide no usar incisos con guiones ('-- --').
# Detecta em-dash de LaTeX '---' usado como inciso (separador visual). Es criterio
# humano decidir reemplazar por comas/paréntesis, así que solo se DETECTA.
LATEX_EMDASH_RE = re.compile(r"---")

# Errores de gramática/registro (steering §4). (patrón, nota, auto_fix|None).
# Cuando auto_fix no es None, el reemplazo es determinista y seguro.
GRAMMAR_FIXES: list[tuple[str, str]] = [
    (r"\ben base a\b", "a partir de"),
    (r"\ben función a\b", "en función de"),
]
# Detección de "a N=..." donde debería ser "para N=..." (steering §4). Se marca
# (no auto-fix: "a $N$" puede ser legítimo en otros contextos matemáticos).
A_N_RE = re.compile(r"\ba\s+\$?N\$?\s*=")
# Muletillas encadenadas a moderar (steering §4): "Se observa que", "Esto confirma".
FILLER_RE = re.compile(r"Se observa que|Esto confirma que|El resultado confirma que")
# "Se puede observar como" sin tilde (debería ser "cómo") — steering §4.
COMO_RE = re.compile(r"observar\s+como\b|ver\s+como\b")


@dataclass
class TodoCollector:
    """Acumula marcadores TODO por tópico e inconsistencias detectadas.

    Los generadores insertan comentarios ``%TODO-<TOPICO>: ...`` como líneas
    normales de la tabla mediante :meth:`marker`. Tras escribir cada archivo,
    :meth:`scan_file` re-lee el ``.tex`` y registra cada TODO con su número de
    línea REAL en el archivo final (no una posición relativa estimada).
    """

    # topico -> lista de (archivo, linea_real, descripcion)
    todos: dict[str, list[tuple[str, int, str]]] = field(default_factory=lambda: defaultdict(list))
    inconsistencies: list[str] = field(default_factory=list)

    @staticmethod
    def marker(topic: str, description: str) -> str:
        """Devuelve el comentario LaTeX a insertar en la tabla."""
        return f"%TODO-{topic}: {description}"

    def scan_file(self, path: Path, rel_name: str) -> None:
        """Escanea un .tex ya escrito y registra TODOs con su línea real."""
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            m = TODO_MARKER_RE.match(line)
            if m:
                self.todos[m.group(1)].append((rel_name, i, m.group(2).strip()))

    def add_inconsistency(self, description: str) -> None:
        self.inconsistencies.append(description)

    def n_todos(self) -> int:
        return sum(len(v) for v in self.todos.values())

    def render_txt(self) -> str:
        """Genera el contenido de tesis_todos.txt."""
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "TODOs de la tesis — generado automáticamente",
            f"Generado: {now}",
            "Fuente: scripts/general_project_maintenance/generate_thesis_tables.py",
            f"Total TODOs: {self.n_todos()} | Inconsistencias: {len(self.inconsistencies)}",
            "=" * 72,
            "",
        ]

        if not self.todos:
            lines.append("(sin TODOs pendientes)")
        else:
            for topic in sorted(self.todos):
                entries = self.todos[topic]
                lines.append(f"## TODO-{topic}  ({len(entries)})")
                for filename, line_no, desc in entries:
                    lines.append(f"  [{filename}:{line_no}] {desc}")
                lines.append("")

        lines.append("=" * 72)
        lines.append(f"## INCONSISTENCIAS / ERRORES DETECTADOS  ({len(self.inconsistencies)})")
        lines.append("")
        if not self.inconsistencies:
            lines.append("(ninguna detectada en esta ejecución)")
        else:
            for i, desc in enumerate(self.inconsistencies, 1):
                lines.append(f"  {i}. {desc}")
        lines.append("")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Utilidades de formato LaTeX
# ═══════════════════════════════════════════════════════════════════════════════


_PROTECT_CMD_RE = re.compile(
    r"\\(?:texttt|url|href|ref|eqref|autoref|cite[tp]?|label|input|includegraphics)"
    r"\{[^}]*\}"
)
_MATH_INLINE_RE = re.compile(r"(?<!\\)\$[^$]*\$")


def _strip_protected(code: str) -> str:
    """Reemplaza por espacios las zonas que no son prosa (código, math, comandos).

    Se usa para buscar anglicismos y guiones solo en prosa, evitando falsos
    positivos dentro de \\texttt{...}, $...$, \\ref{...}, etc. Conserva longitud
    aproximada reemplazando por espacios (no altera índices de forma crítica).
    """
    out = _PROTECT_CMD_RE.sub(lambda m: " " * len(m.group(0)), code)
    out = _MATH_INLINE_RE.sub(lambda m: " " * len(m.group(0)), out)
    return out


def _esc(text: str) -> str:
    """Escapa caracteres especiales de LaTeX en texto plano."""
    if text is None:
        return ""
    out = str(text)
    for a, b in (
        ("\\", r"\textbackslash{}"),
        ("&", r"\&"),
        ("%", r"\%"),
        ("$", r"\$"),
        ("#", r"\#"),
        ("_", r"\_"),
        ("{", r"\{"),
        ("}", r"\}"),
        ("~", r"\textasciitilde{}"),
        ("^", r"\textasciicircum{}"),
    ):
        out = out.replace(a, b)
    return out


def _num(value: float, decimals: int = 4) -> str:
    """Formatea un número con coma decimal (convención española, steering §6)."""
    if value is None:
        return "---"
    return f"{value:.{decimals}f}".replace(".", ",")


def _topo_es(topo: str) -> str:
    return TOPO_ES.get(topo, topo.replace("_", " "))


def _wrap_table(
    caption: str,
    label: str,
    col_spec: str,
    header: list[str],
    rows: list[list[str]],
    notes: str = "",
    pre_lines: list[str] | None = None,
    short_caption: str = "",
) -> list[str]:
    """Envuelve una tabla en el entorno LaTeX estándar con booktabs.

    ``short_caption`` (opcional) produce \\caption[corto]{largo} para que el
    índice de tablas muestre solo el título breve (steering §7 / pedido usuario).
    """
    lines: list[str] = []
    lines.append(
        "% ==== AUTO-GENERADA — no editar a mano (regenerar con generate_thesis_tables.py) ===="
    )
    if pre_lines:
        lines.extend(pre_lines)
    lines.append("\\begin{table}[htbp]")
    lines.append("\\centering")
    if short_caption:
        lines.append(f"\\caption[{short_caption}]{{{caption}}}")
    else:
        lines.append(f"\\caption{{{caption}}}")
    lines.append(f"\\label{{{label}}}")
    lines.append(f"\\begin{{tabular}}{{{col_spec}}}")
    lines.append("\\toprule")
    lines.append(" & ".join(header) + " \\\\")
    lines.append("\\midrule")
    for row in rows:
        lines.append(" & ".join(row) + " \\\\")
    lines.append("\\bottomrule")
    lines.append("\\end{tabular}")
    if notes:
        lines.append(f"\\\\[2pt]\n\\footnotesize {notes}")
    lines.append("\\end{table}")
    return lines


# ═══════════════════════════════════════════════════════════════════════════════
# Carga de datos (reutiliza generadores existentes)
# ═══════════════════════════════════════════════════════════════════════════════


def load_scoreboard(refresh: bool, collector: TodoCollector) -> dict:
    """Carga best_results_scoreboard.json, regenerándolo si se solicita."""
    if refresh:
        try:
            from generate_best_results_scoreboard import generate_scoreboard

            generate_scoreboard(output_json=True)
        except Exception as e:  # noqa: BLE001
            collector.add_inconsistency(
                f"No se pudo regenerar el scoreboard ({e}); se usa el JSON existente si lo hay."
            )
    if not SCOREBOARD_JSON.exists():
        collector.add_inconsistency(
            f"No existe {SCOREBOARD_JSON.relative_to(ROOT)}: correr "
            "generate_best_results_scoreboard.py --json primero."
        )
        return {}
    return json.loads(SCOREBOARD_JSON.read_text(encoding="utf-8"))


def load_heavy_hex_per_h(collector: TodoCollector) -> dict[int, list]:
    """Devuelve {N: [PerHResult...]} para heavy_hex agrupando por N (mejor por h).

    Reutiliza ``parse_eval_report`` del generador del scoreboard (misma lógica de
    parsing, dedup y validación física) pero con una tolerancia de h muy amplia
    para aceptar TODOS los puntos h (las tablas por-h necesitan el barrido
    completo, no solo h≈2.5).
    """
    try:
        import generate_best_results_scoreboard as sb
    except Exception as e:  # noqa: BLE001
        collector.add_inconsistency(f"No se pudo importar el generador del scoreboard: {e}")
        return {}

    eval_dir = ROOT / "results" / "extrapolation_evals"
    # Aceptar todo el rango de h reutilizando el parser existente.
    saved_tol = sb.H_TOLERANCE
    sb.H_TOLERANCE = 1e9
    per_n: dict[int, dict[float, sb.PerHResult]] = defaultdict(dict)
    try:
        for topo_dir in sorted(eval_dir.glob(f"{FOCUS_TOPOLOGY}_p*")):
            for report in sorted(topo_dir.glob("eval_*.md")):
                try:
                    entries = sb.parse_eval_report(report, target_h=0.0)
                except Exception as exc:  # noqa: BLE001
                    collector.add_inconsistency(f"Error parseando {report.name}: {exc}")
                    continue
                for entry in entries:
                    r = entry.result
                    h_key = round(r.h, 2)
                    prev = per_n[entry.n_qubits].get(h_key)
                    # Quedarse con el mejor |ΔE| por (N, h)
                    if prev is None or r.abs_error < prev.abs_error:
                        per_n[entry.n_qubits][h_key] = r
    finally:
        sb.H_TOLERANCE = saved_tol

    # Colapsar {h: PerHResult} -> lista ordenada por h
    return {n: [hmap[h] for h in sorted(hmap)] for n, hmap in per_n.items()}


def load_campaign_index(collector: TodoCollector) -> list[dict]:
    """Carga las entradas del ResultIndex (conteos de campaña)."""
    try:
        from qmbp_simulation.framework.result_index import ResultIndex

        idx = ResultIndex()
        return list(idx.entries)
    except Exception as e:  # noqa: BLE001
        collector.add_inconsistency(f"No se pudo cargar ResultIndex: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Generadores de tablas
# ═══════════════════════════════════════════════════════════════════════════════


def gen_scoreboard(scoreboard: dict, collector: TodoCollector) -> list[str]:
    """Tabla resumen: mejor grade por topología (perspectiva general)."""
    by_topo = scoreboard.get("best_by_topology", {})
    if not by_topo:
        return [collector.marker("DATOS", "best_by_topology vacío en el scoreboard JSON.")]

    header = ["Topología", "$N$ máx.", "Mejor calificación", "$|\\Delta E|$ medio"]
    rows: list[list[str]] = []
    for topo in sorted(by_topo):
        n_results = by_topo[topo]
        if not n_results:
            continue
        ns = sorted(int(k) for k in n_results)
        best = min(n_results.values(), key=lambda r: r["best_abs_error"])
        rows.append(
            [
                _esc(_topo_es(topo)),
                str(max(ns)),
                _esc(best["grade"]),
                _num(best.get("mean_abs_error", best["best_abs_error"])),
            ]
        )

    caption = (
        "Resultado por topología en el punto de operación más exigente "
        f"($h \\approx {_num(scoreboard.get('target_h', 2.5), 2)}$). "
        "El $|\\Delta E|$ medio promedia sobre los puntos evaluados de la "
        "configuración. Calificación por $|\\Delta E|$: A ($<0{,}05$), "
        "B ($<0{,}10$), C ($<0{,}30$), D ($<1{,}00$), F ($\\geq 1{,}00$)."
    )
    return _wrap_table(
        caption,
        "tab:auto_scoreboard",
        "lccc",
        header,
        rows,
        short_caption="Resultado por topología",
    )


def gen_coverage(
    scoreboard: dict, collector: TodoCollector, coverage_ns: set[int] | None = None
) -> list[str]:
    """Matriz de cobertura: grade alcanzado por topología × N.

    Si ``coverage_ns`` se provee, restringe las columnas a esos tamaños (evita
    una tabla demasiado ancha para la página).
    """
    by_topo = scoreboard.get("best_by_topology", {})
    if not by_topo:
        return [collector.marker("DATOS", "best_by_topology vacío en el scoreboard JSON.")]

    # Conjunto de N presentes (columnas). Si se pasó coverage_ns, filtrar a esos.
    all_ns = sorted({int(n) for tr in by_topo.values() for n in tr})
    if coverage_ns:
        all_ns = [n for n in all_ns if n in coverage_ns]
    header = ["Topología"] + [f"$N{{=}}{n}$" for n in all_ns]
    rows: list[list[str]] = []
    for topo in sorted(by_topo):
        n_results = by_topo[topo]
        row = [_esc(_topo_es(topo))]
        for n in all_ns:
            entry = n_results.get(str(n))
            row.append(_esc(entry["grade"]) if entry else "---")
        rows.append(row)

    col_spec = "l" + "c" * len(all_ns)
    caption = (
        "Matriz de cobertura: calificación alcanzada por topología y tamaño del sistema "
        f"$N$ en $h \\approx {_num(scoreboard.get('target_h', 2.5), 2)}$."
    )
    notes = "Un guion (---) indica que no hay resultado evaluado para esa combinación."
    return _wrap_table(
        caption,
        "tab:auto_coverage",
        col_spec,
        header,
        rows,
        notes=notes,
        short_caption="Matriz de cobertura por topología y tamaño",
    )


def gen_campaign(index: list[dict], collector: TodoCollector) -> list[str]:
    """Conteos de campaña por modelo, con la taxonomía de conteo del steering."""
    if not index:
        return [
            collector.marker("CAMPANA", "ResultIndex vacío: no se pudieron contar ejecuciones.")
        ]

    # Filtrar entradas con modelo válido
    valid = [e for e in index if e.get("model")]
    by_model: dict[str, list[dict]] = defaultdict(list)
    for e in valid:
        by_model[e["model"]].append(e)

    header = ["Modelo", "Ejecuciones", "Tasa aprob."]
    rows: list[list[str]] = []
    for model in sorted(by_model):
        runs = by_model[model]
        n = len(runs)
        n_pass = sum(1 for r in runs if r.get("passed"))
        rate = n_pass / n if n else 0.0
        rows.append(
            [
                _esc(model),
                str(n),
                f"{n_pass}/{n} ({_num(rate * 100, 1)}\\%)",
            ]
        )

    # Taxonomía Heisenberg 46 = 26 + 20 (verificar contra el índice)
    n_heis = len(by_model.get("heisenberg", []))
    n_heis_t = len(by_model.get("heisenberg_transverse", []))
    pre_lines: list[str] = []
    total_heis = n_heis + n_heis_t
    if total_heis:
        expected = " (46 = 26 XXZ + 20 transversal)" if total_heis == 46 else ""
        note = (
            f"Heisenberg: {n_heis} XXZ + {n_heis_t} transversal = {total_heis} ejecuciones"
            f"{expected}."
        )
        if total_heis != 46:
            collector.add_inconsistency(
                f"Conteo Heisenberg en ResultIndex = {total_heis} "
                f"({n_heis} XXZ + {n_heis_t} transversal), no 46 como en la tesis. "
                "Verificar campaña o texto."
            )
            # marcador visible en el .tex
            pre_lines.append(
                collector.marker(
                    "CAMPANA",
                    f"ResultIndex reporta {total_heis} runs Heisenberg "
                    f"({n_heis} XXZ + {n_heis_t} transv.), la tesis dice 46. Revisar.",
                )
            )

    caption = (
        "Conteo de ejecuciones del pipeline por modelo en la campaña experimental "
        "(fuente: ResultIndex). Una \\emph{ejecución} es una corrida completa de "
        "las Fases 1--3 para una (configuración, semilla)."
    )
    notes = note if total_heis else ""
    return _wrap_table(
        caption,
        "tab:auto_campaign",
        "lcc",
        header,
        rows,
        notes=notes,
        pre_lines=pre_lines or None,
        short_caption="Conteo de ejecuciones por modelo",
    )


def _per_h_table(
    per_n: dict[int, list],
    n_values: list[int],
    label: str,
    caption: str,
    collector: TodoCollector,
    topic_kind: str,
    short_caption: str = "",
) -> list[str]:
    """Construye una tabla por-N (una fila por N) con métricas agregadas por-h.

    Columnas: N, nº puntos, |ΔE| (media), gap (media), ΔE/gap (media).
    El rango de h se unifica al más abarcativo y se declara en el pie (no como
    columna, para evitar rangos heterogéneos entre filas).
    """
    from qmbp_simulation.analysis.metrics import compute_deploy_summary

    # Métrica primaria |ΔE| primero (steering §5); ΔE/gap relegado a apoyo.
    header = [
        "$N$",
        "$|\\Delta E|$",
        "gap",
        "$\\Delta E/\\mathrm{gap}$",
    ]
    rows: list[list[str]] = []
    pre_lines: list[str] = []
    h_min_global = float("inf")
    h_max_global = float("-inf")

    missing_ns = [n for n in n_values if n not in per_n or not per_n[n]]
    if missing_ns:
        pre_lines.append(
            collector.marker(
                topic_kind,
                f"Faltan datos por-h de heavy_hex para N={missing_ns}; "
                "no hay eval report per-h para esos tamaños.",
            )
        )

    for n in n_values:
        results = per_n.get(n)
        if not results:
            continue
        hs = [r.h for r in results]
        h_min_global = min(h_min_global, min(hs))
        h_max_global = max(h_max_global, max(hs))
        per_h_dicts = [{"de_gap": r.de_gap, "abs_error": r.abs_error} for r in results]
        summary = compute_deploy_summary(per_h_dicts)
        mean_gap = sum(r.gap for r in results) / len(results)
        mean_abs = summary.get("mean_abs_error", 0.0)
        rows.append(
            [
                str(n),
                _num(mean_abs),
                _num(mean_gap),
                _num(summary["mean_de_gap"]),
            ]
        )

    if not rows:
        return [collector.marker(topic_kind, f"Sin datos por-h de heavy_hex para {label}.")]

    # Rango de h unificado (el más abarcativo cubierto por el conjunto de filas).
    # El pie describe QUÉ contiene la tabla (no explica las métricas — eso va en
    # el texto / la sección de métricas del capítulo).
    rango = f"$h \\in [{_num(h_min_global, 2)}, {_num(h_max_global, 2)}]$"
    notes = (
        f"Valores promediados sobre los puntos $h$ ({rango}) del reporte de "
        "evaluación. Unidades de $J = 1$."
    )
    return _wrap_table(
        caption,
        label,
        "rrrr",
        header,
        rows,
        notes=notes,
        pre_lines=pre_lines,
        short_caption=short_caption,
    )


def gen_heavy_hex_intra_n(per_n: dict[int, list], collector: TodoCollector) -> list[str]:
    """Tabla de interpolación a un mismo tamaño de heavy_hex."""
    intra_ns = [n for n in sorted(per_n) if n <= 20]
    caption = (
        "Interpolación a un mismo tamaño en heavy-hex: la GNN reproduce "
        "$\\theta^*(h)$ en valores de $h$ no vistos, para cada $N$ de entrenamiento."
    )
    return _per_h_table(
        per_n,
        intra_ns,
        "tab:auto_heavy_hex_intra_n",
        caption,
        collector,
        "DATOS",
        short_caption="Interpolación a un mismo tamaño (heavy-hex)",
    )


def gen_heavy_hex_large_n(per_n: dict[int, list], collector: TodoCollector) -> list[str]:
    """Tabla de predicción a N grande de heavy_hex (entre tamaños)."""
    large_ns = [n for n in sorted(per_n) if n > 20]
    caption = (
        "Predicción entre tamaños (sobre casos no observados) en heavy-hex: la "
        "UnifiedMPNN entrenada con $N$ pequeños predice ángulos para $N$ grandes."
    )
    return _per_h_table(
        per_n,
        large_ns,
        "tab:auto_heavy_hex_large_n",
        caption,
        collector,
        "DATOS",
        short_caption="Predicción a $N$ grande (heavy-hex)",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Detección de inconsistencias (a partir del scoreboard JSON)
# ═══════════════════════════════════════════════════════════════════════════════


def detect_inconsistencies(scoreboard: dict, collector: TodoCollector) -> None:
    """Propaga inconsistencias del scoreboard (grades vs gaps, campañas mezcladas)."""
    by_topo = scoreboard.get("best_by_topology", {})
    for topo, n_results in by_topo.items():
        # Campañas mezcladas: fechas muy dispersas dentro de una misma topología
        dates = sorted({e.get("date", "")[:10] for e in n_results.values() if e.get("date")})
        if len(dates) > 1:
            span = f"{dates[0]}..{dates[-1]}"
            # Solo informativo; no bloquea
            if dates[0][:7] != dates[-1][:7]:
                collector.add_inconsistency(
                    f"{topo}: resultados de campañas de distintos meses ({span}); "
                    "verificar que no se mezclen criterios/rangos de h en una misma tabla."
                )
        # Grade A/B con gap muy pequeño (posible gap_masked cerca de frontera)
        for n, e in n_results.items():
            if e.get("grade") in ("A", "B") and e.get("gap_at_best", 1.0) < 0.5:
                collector.add_inconsistency(
                    f"{topo} N={n}: grade {e['grade']} con gap={_num(e['gap_at_best'], 3)} "
                    "pequeño; posible enmascaramiento por gap (revisar |ΔE| absoluto)."
                )


# ═══════════════════════════════════════════════════════════════════════════════
# Chequeos de consistencia sobre el documento LaTeX
# ═══════════════════════════════════════════════════════════════════════════════


def check_tex(tex_path: Path, out_dir: Path, collector: TodoCollector) -> None:
    """Chequeos ligeros de consistencia sobre el .tex, alimentando el TODO log.

    Registra hallazgos como inconsistencias (con [archivo:línea]) para:
      - refs/labels: \\ref sin \\label (rompe -> ??), y label de tabla sin \\ref.
      - decimales con punto en lugar de coma (convención española del steering).
      - tablas auto_*.tex no conectadas al documento (sin \\input o label sin \\ref).
    """
    if not tex_path.exists():
        collector.add_inconsistency(f"No existe el .tex a chequear: {tex_path}")
        return

    lines = tex_path.read_text(encoding="utf-8").splitlines()
    rel = tex_path.name

    labels: dict[str, int] = {}  # label -> primera linea
    refs: dict[str, int] = {}  # ref -> primera linea
    inputs: set[str] = set()  # basenames incluidos via \input
    float_appear: dict[str, int] = {}  # label fig/tab -> linea del \begin{float} que lo contiene
    in_verbatim = False
    cur_float_line = 0  # linea del \begin{figure/table} abierto
    in_english = False  # dentro del Abstract (inglés) o bibliografía: no marcar anglicismos

    for i, line in enumerate(lines, start=1):
        # Ignorar comentarios completos y bloques verbatim/lstlisting
        if re.search(r"\\begin\{(verbatim|lstlisting)\}", line):
            in_verbatim = True
        if re.search(r"\\end\{(verbatim|lstlisting)\}", line):
            in_verbatim = False
            continue
        if in_verbatim:
            continue
        # Zonas en inglés legítimo: capítulo Abstract y bibliografía.
        if re.search(r"\\chapter\{Abstract\}|\\begin\{thebibliography\}", line):
            in_english = True
        if re.search(r"\\mainmatter|\\end\{thebibliography\}", line):
            in_english = False
        # Quitar comentario de línea respetando el porcentaje escapado (\%)
        code = re.sub(r"(?<!\\)%.*$", "", line)
        if not code.strip():
            continue
        # Prosa: quita \texttt{...}, \url{...}, math $...$ y comandos con label/ref
        # para no marcar anglicismos dentro de código o identificadores.
        prose = _strip_protected(code)

        if re.search(r"\\begin\{(figure|table)\*?\}", code):
            cur_float_line = i
        # (11) Caption sin título corto para el índice (pedido del usuario): las
        # figuras/tablas deben usar \caption[corto]{largo} para que el índice no
        # muestre la descripción completa. Detecta \caption{ (sin corchete).
        if re.search(r"\\caption\{", code) and not re.search(r"\\caption\[", code):
            collector.add_inconsistency(
                f"[{rel}:{i}] %TODO-INDICE \\caption sin título corto "
                "(usar \\caption[título breve]{descripción} para el índice de "
                "figuras/tablas; requiere criterio para el título breve)."
            )
        for m in re.finditer(r"\\label\{([^}]+)\}", code):
            labels.setdefault(m.group(1), i)
            # Aparición visual del float = línea de su \begin (o la del label si va suelto)
            if m.group(1).startswith(("fig:", "tab:")):
                float_appear.setdefault(m.group(1), cur_float_line or i)
        if re.search(r"\\end\{(figure|table)\*?\}", code):
            cur_float_line = 0
        for m in re.finditer(r"\\(?:ref|eqref|autoref)\{([^}]+)\}", code):
            refs.setdefault(m.group(1), i)
        for m in re.finditer(r"\\input\{([^}]+)\}", code):
            inputs.add(Path(m.group(1)).name.replace(".tex", ""))

        # (2) Decimales con punto: número.número (no versiones tipo v2.x ni URLs,
        # ni longitudes LaTeX tipo 0.90\textwidth / 0.5cm que no son decimales de prosa,
        # ni versiones de software tipo "Python 3.11").
        for m in re.finditer(r"(?<![\w.])\d+\.\d+(?![\w.])", code):
            after = code[m.end() : m.end() + 14]
            before = code[max(0, m.start() - 16) : m.start()]
            if re.match(r"\s*(\\(?:text|line|column)width|\\height|cm|mm|pt|em|ex|in|\+)\b", after):
                continue
            if _VERSION_RE.search(before):
                continue
            collector.add_inconsistency(
                f"[{rel}:{i}] decimal con punto '{m.group(0)}' "
                "(usar coma decimal en tablas/texto, steering §7)."
            )

        # (2c) Anglicismos de métrica (Grade/Pass/Rate): usar equivalentes en español.
        for m in ENGLISH_METRIC_RE.finditer(code):
            collector.add_inconsistency(
                f"[{rel}:{i}] palabra inglesa '{m.group(0)}' "
                "(usar español: Grade->calificación/nota, Pass->aprobado, "
                "Rate->tasa/proporción)."
            )

        # (2d) Énfasis en profundidad requerida (de-enfatizar, steering §5).
        for m in DEPTH_EMPHASIS_RE.finditer(code):
            collector.add_inconsistency(
                f"[{rel}:{i}] %TODO-PROFUNDIDAD fórmula de capas requeridas "
                f"'{m.group(0)}' (quitar énfasis; la profundidad es un parámetro "
                "fijo, no el eje del trabajo)."
            )

        # (Editorial 3) Tasa de aprobación sin caso absoluto (n/m), steering §5.
        # Solo aplica a porcentajes de aprobación (no a tasas de error, fidelidades,
        # umbrales o mejoras porcentuales, que son magnitudes físicas sin (n/m)).
        _is_passrate = re.search(r"(aprobaci|aprobad|tasa de aprob)", code)
        _pct_threshold = re.search(r"[<>]\s*\d{1,3}\\%|\\leq|\\geq|umbral|criterio", code)
        # Falsos positivos: el % es un ΔE/gap, una proporción de fallos, o un
        # ahorro de iteraciones; o la línea ya declara un conteo (N ejecuciones).
        _pct_other = re.search(
            r"gap|de los fallos|de fallos|prevenible|ahorro|iteraciones|"
            r"\d+\s+ejecuciones|de acuerdo",
            code,
        )
        # El % candidato debe estar cerca de la palabra 'aprobaci' (misma cláusula).
        _pr_pct = re.search(
            r"aprobaci[oó]n[^.]{0,20}?(\d{1,3}\\%)|(\d{1,3}\\%)[^.]{0,20}?aprob", code
        )
        if (
            _is_passrate
            and _pr_pct
            and not ABS_CASE_RE.search(code)
            and not _pct_threshold
            and not _pct_other
        ):
            pct = _pr_pct.group(1) or _pr_pct.group(2)
            collector.add_inconsistency(
                f"[{rel}:{i}] %TODO-CIFRA tasa de aprobación '{pct}' sin caso "
                "absoluto entre paréntesis (usar formato '95\\% (37/39)', steering §5)."
            )

        # (Editorial 4) Rango numérico con guion simple (usar -- en LaTeX).
        for m in BAD_RANGE_RE.finditer(code):
            # Excluir dentro de comandos protegidos ya filtrados por 'code' sin comentario.
            collector.add_inconsistency(
                f"[{rel}:{i}] rango '{m.group(0)}' con guion simple "
                "(usar '--' en LaTeX para rangos numéricos)."
            )

        # (2b) Guiones Unicode em/en-dash: prohibidos; usar '---' (em) o '--' (rango) de LaTeX.
        for ch, name, repl_hint in (("\u2014", "em-dash", "---"), ("\u2013", "en-dash", "--")):
            col = line.find(ch)
            if col != -1:
                n_occ = line.count(ch)
                collector.add_inconsistency(
                    f"[{rel}:{i}] símbolo Unicode '{ch}' ({name}) x{n_occ} en col {col + 1} "
                    f"(prohibido; reemplazar por '{repl_hint}' de LaTeX)."
                )

        # (8) Anglicismos (steering §3). Solo en prosa (no Abstract/biblio/código).
        if not in_english:
            for pat, repl in ANGLICISM_FIXES:
                for m in re.finditer(pat, prose):
                    collector.add_inconsistency(
                        f"[{rel}:{i}] %TODO-ANGLICISMO '{m.group(0)}' -> '{repl}' "
                        "(auto-corregible con --fix-anglicisms, steering §3)."
                    )
            for pat, note in ANGLICISM_DETECT:
                for m in re.finditer(pat, prose):
                    collector.add_inconsistency(
                        f"[{rel}:{i}] %TODO-ANGLICISMO '{m.group(0)}': {note} "
                        "(requiere criterio; no auto-corregible)."
                    )
            # (9) Inciso con em-dash '---' (el usuario pide no usar guiones como
            # separador). Criterio humano para reemplazar por comas/paréntesis.
            if LATEX_EMDASH_RE.search(prose):
                n_dash = len(LATEX_EMDASH_RE.findall(prose))
                collector.add_inconsistency(
                    f"[{rel}:{i}] inciso con '---' (em-dash LaTeX) x{n_dash} "
                    "(el usuario pide no usar guiones como separador; usar comas o "
                    "paréntesis — requiere criterio, no auto-corregible)."
                )

            # (10) Gramática/registro (steering §4).
            for pat, repl in GRAMMAR_FIXES:
                for m in re.finditer(pat, prose):
                    collector.add_inconsistency(
                        f"[{rel}:{i}] %TODO-GRAMATICA '{m.group(0)}' -> '{repl}' "
                        "(auto-corregible con --fix-grammar, steering §4)."
                    )
            if A_N_RE.search(prose):
                collector.add_inconsistency(
                    f"[{rel}:{i}] %TODO-GRAMATICA 'a N=...' -> 'para N=...' "
                    "(steering §4; verificar que no sea un uso matemático legítimo)."
                )
            for m in COMO_RE.finditer(prose):
                collector.add_inconsistency(
                    f"[{rel}:{i}] %TODO-GRAMATICA '{m.group(0)}' -> usar 'cómo' con "
                    "tilde cuando explica el modo (steering §4)."
                )
            if FILLER_RE.search(prose):
                collector.add_inconsistency(
                    f"[{rel}:{i}] %TODO-ESTILO muletilla '{FILLER_RE.search(prose).group(0)}' "
                    "(variar la redacción; reservar 'confirma' para evidencia concluyente, §4)."
                )

        # (5) Tono comercial (steering §6): moderar, añadir matiz cuantitativo.
        for m in TONE_WORDS_RE.finditer(code):
            collector.add_inconsistency(
                f"[{rel}:{i}] %TODO-TONO tono a moderar '{m.group(0)}' "
                "(usar muestra/sugiere; añadir comparación cuantitativa o "
                "'dentro de las configuraciones evaluadas')."
            )

        # (6) Métricas (steering §5): speedup eliminado; PassRate sin |ΔE|.
        for m in SPEEDUP_RE.finditer(code):
            collector.add_inconsistency(
                f"[{rel}:{i}] %TODO-METRICA speedup/aceleración '{m.group(0)}' "
                "(eliminar hasta tener definición reproducible de coste)."
            )
        if PASSRATE_RE.search(code) and not ABS_ERR_RE.search(code):
            collector.add_inconsistency(
                f"[{rel}:{i}] %TODO-METRICA PassRate sin |ΔE| acompañante "
                "(reportar la métrica primaria |ΔE|, steering §5)."
            )
        if PER_SITE_RE.search(code):
            m = PER_SITE_RE.search(code)
            collector.add_inconsistency(
                f"[{rel}:{i}] %TODO-METRICA error por sitio '{m.group(0)}' PROHIBIDO "
                "(|ΔE|/N se retiró; no aporta sobre |ΔE|, steering §5). Eliminar."
            )

        # (7) Reproducibilidad (steering §8): 'exacto' + DMRG/MPS; statevector a N grande.
        if EXACT_TN_RE.search(code):
            collector.add_inconsistency(
                f"[{rel}:{i}] %TODO-REPRO 'exacto' aplicado a DMRG/MPS "
                "(usar 'convergido dentro de la tolerancia'; mostrar convergencia en χ)."
            )
        if SV_BIGN_RE.search(code):
            for bm in BIGN_RE.finditer(code):
                if int(bm.group(1)) > 22:
                    collector.add_inconsistency(
                        f"[{rel}:{i}] %TODO-REPRO statevector con N={bm.group(1)}>22 "
                        "(inviable en memoria; ¿era backend MPS? indicar χ y tolerancia)."
                    )

    # (1) refs -> label inexistente (renderiza como ??)
    for ref, ln in sorted(refs.items(), key=lambda kv: kv[1]):
        if ref not in labels:
            collector.add_inconsistency(
                f"[{rel}:{ln}] \\ref{{{ref}}} sin \\label correspondiente (saldrá como ??)."
            )
    # (1b) label de tabla nunca referenciado
    for lab, ln in sorted(labels.items(), key=lambda kv: kv[1]):
        if lab.startswith("tab:") and lab not in refs:
            collector.add_inconsistency(
                f"[{rel}:{ln}] tabla \\label{{{lab}}} nunca referenciada con \\ref."
            )

    # (4) tablas auto_*.tex no conectadas al documento
    for auto_tex in sorted(out_dir.glob("auto_*.tex")):
        stem = auto_tex.stem
        auto_label = f"tab:{stem}"
        if stem not in inputs:
            collector.add_inconsistency(
                f"tabla generada '{auto_tex.name}' no está incluida en {rel} "
                f"(falta \\input{{tables/{stem}}})."
            )
        elif auto_label not in refs:
            collector.add_inconsistency(
                f"tabla generada '{auto_tex.name}' incluida pero su \\label{{{auto_label}}} "
                "no se referencia con \\ref."
            )

    # (21) Figuras/tablas que aparecen antes de mencionarse.
    for lab, appear_ln in sorted(float_appear.items(), key=lambda kv: kv[1]):
        ref_ln = refs.get(lab)
        if ref_ln is None:
            continue  # ya cubierto por 'nunca referenciada'
        if ref_ln > appear_ln:
            kind = "figura" if lab.startswith("fig:") else "tabla"
            collector.add_inconsistency(
                f"[{rel}:{appear_ln}] {kind} \\label{{{lab}}} aparece (L{appear_ln}) "
                f"antes de su primera mención \\ref (L{ref_ln})."
            )

    # (20), (22) y (15): cruces semánticos y bibliografía sobre el texto completo.
    full_text = tex_path.read_text(encoding="utf-8")
    _check_cross_section_figures(full_text, rel, collector)
    _check_hypotheses_coverage(full_text, rel, collector)
    _check_bibliography(full_text, rel, collector)
    _check_editorial(full_text, rel, collector)
    _check_tabular_columns(lines, rel, collector)


def _count_tabular_cols(spec: str) -> int:
    """Cuenta columnas de un preámbulo de tabular (l/c/r/p{..}), ignorando | y @{}."""
    # Quitar @{...} y p{...}/m{...}/b{...} (cuentan como 1 columna cada uno).
    spec = re.sub(r"@\{[^}]*\}", "", spec)
    n_p = len(re.findall(r"[pmb]\{[^}]*\}", spec))
    spec_wo_p = re.sub(r"[pmb]\{[^}]*\}", "", spec)
    n_lcr = len(re.findall(r"[lcr]", spec_wo_p))
    return n_lcr + n_p


def _check_tabular_columns(lines: list[str], rel: str, collector: TodoCollector) -> None:
    """Detecta filas de un tabular cuyo nº de celdas no coincide con el preámbulo.

    Un desajuste ('lccc' con filas de 3 celdas) descuadra la tabla o rompe la
    compilación. Cuenta '&' no escapados (+1) por fila de datos, ignorando líneas
    de \\multicolumn (que agregan celdas) y reglas (\\toprule, \\midrule, etc.).
    """
    in_tab = False
    ncols = 0
    start_line = 0
    for i, line in enumerate(lines, start=1):
        code = re.sub(r"(?<!\\)%.*$", "", line)
        # Captura el preámbulo permitiendo un nivel de llaves anidadas (p{2cm}).
        m = re.search(r"\\begin\{tabular\}\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{[^}]*\})*)\}", code)
        if m:
            in_tab = True
            ncols = _count_tabular_cols(m.group(1))
            start_line = i
            code = code[m.end() :]  # analizar el resto de la línea por si hay fila
        if not in_tab:
            continue
        if re.search(r"\\end\{tabular\}", code):
            in_tab = False
            continue
        # Solo filas de datos: deben terminar en '\\' y no ser reglas/comandos.
        if "\\\\" not in code:
            continue
        if re.search(r"\\(top|mid|bottom|cmid)rule|\\hline", code):
            continue
        if "\\multicolumn" in code or "\\multirow" in code:
            continue  # estos alteran el conteo simple de '&'; se omiten
        row = code.split("\\\\")[0]
        n_amp = len(re.findall(r"(?<!\\)&", row))
        n_cells = n_amp + 1
        if row.strip() and n_cells != ncols:
            collector.add_inconsistency(
                f"[{rel}:{i}] tabla con desajuste de columnas: preámbulo declara "
                f"{ncols} (\\begin{{tabular}} en L{start_line}) pero la fila tiene "
                f"{n_cells} celdas. Corregir el preámbulo o la fila."
            )


def _split_chapters(text: str) -> dict[str, str]:
    """Devuelve {titulo_capitulo: cuerpo} partiendo por \\chapter{...}."""
    parts = re.split(r"\\chapter\{([^}]+)\}", text)
    out: dict[str, str] = {}
    # parts = [pre, title1, body1, title2, body2, ...]
    for k in range(1, len(parts) - 1, 2):
        out[parts[k].strip()] = parts[k + 1]
    return out


def _check_cross_section_figures(text: str, rel: str, collector: TodoCollector) -> None:
    """(20) Cruce de cifras del mismo concepto entre secciones clave.

    Heurística ligera: para conceptos con una etiqueta reconocible (topología +
    métrica de PassRate, y el rango de speedup), recoge todos los porcentajes y
    señala si un mismo concepto aparece con cifras distintas en capítulos
    distintos (posible contradicción). Es una señal, no una verdad: requiere
    revisión humana.
    """
    chapters = _split_chapters(text)
    # 1) Rango de speedup: solo rangos explícitos "N--M$\times$" o "N--M veces"
    #    (evita capturar números sueltos con \times que no son aceleraciones).
    speedup_by_chap: dict[str, set[str]] = {}
    range_re = re.compile(r"(\d+)\s*--\s*(\d+)\s*\$?\\times")
    for title, body in chapters.items():
        ranges = {f"{a}-{b}" for a, b in range_re.findall(body)}
        if ranges:
            speedup_by_chap[title] = ranges
    all_ranges = {s for v in speedup_by_chap.values() for s in v}
    if len(all_ranges) > 1:
        detail = "; ".join(f"{c}: {sorted(v)}" for c, v in speedup_by_chap.items())
        collector.add_inconsistency(
            f"[{rel}] %TODO-CIFRA rangos de aceleración distintos entre capítulos ({detail}); "
            "unificar la cifra (o eliminarla, steering §5)."
        )

    # 1b) Rango de "puntos de entrenamiento / puntos VQE": debe ser único en todo
    #     el documento (canónico 16--39). Detecta N--M inmediatamente antes de
    #     "puntos" (VQE / de entrenamiento) y marca si conviven varios rangos.
    pts_by_chap: dict[str, set[str]] = {}
    pts_re = re.compile(r"(\d+)\s*--\s*(\d+)\s*puntos(?:\s+VQE|\s+de\s+entrenamiento)?")
    for title, body in chapters.items():
        ranges = {f"{a}-{b}" for a, b in pts_re.findall(body)}
        if ranges:
            pts_by_chap[title] = ranges
    all_pts = {s for v in pts_by_chap.values() for s in v}
    if len(all_pts) > 1:
        detail = "; ".join(f"{c}: {sorted(v)}" for c, v in pts_by_chap.items())
        collector.add_inconsistency(
            f"[{rel}] %TODO-CIFRA rangos de 'puntos de entrenamiento' distintos entre "
            f"capítulos ({detail}); unificar (canónico 16--39, steering §1)."
        )

    # 2) PassRate por topología: "cadena 1D ... 95\%" en resumen vs resultados vs conclusiones.
    key_chaps = {
        t: b
        for t, b in chapters.items()
        if any(k in t.lower() for k in ("resumen", "resultado", "discus", "conclus"))
    }
    topo_pat = re.compile(
        r"(cadena 1D|heavy-hex|escalera|cuadrada|triangular)[^.]{0,60}?(\d{2,3})\\%", re.IGNORECASE
    )
    concept: dict[str, dict[str, set[str]]] = {}
    for title, body in key_chaps.items():
        for m in topo_pat.finditer(body):
            topo = m.group(1).lower()
            concept.setdefault(topo, {}).setdefault(m.group(2), set()).add(title)
    for topo, vals in concept.items():
        if len(vals) > 1:
            detail = "; ".join(f"{pct}\\% en {sorted(ch)}" for pct, ch in vals.items())
            collector.add_inconsistency(
                f"[{rel}] %TODO-CIFRA '{topo}' aparece con porcentajes distintos entre "
                f"secciones ({detail}); verificar coherencia resumen/resultados/conclusiones."
            )


def _check_hypotheses_coverage(text: str, rel: str, collector: TodoCollector) -> None:
    """(22, señal) Cobertura estructural hipótesis -> conclusiones.

    Detecta las hipótesis Hn definidas en el capítulo de objetivos/hipótesis y
    verifica que cada una se mencione en Conclusiones. NO evalúa si la conclusión
    responde de fondo (eso requiere lectura semántica); solo marca huérfanas.
    """
    chapters = _split_chapters(text)
    obj_body = next(
        (b for t, b in chapters.items() if "hipótesis" in t.lower() or "objetivo" in t.lower()), ""
    )
    concl_body = next((b for t, b in chapters.items() if "conclus" in t.lower()), "")
    if not obj_body or not concl_body:
        return
    hyps = sorted(set(re.findall(r"\bH(\d+)\b", obj_body)), key=int)
    if not hyps:
        return
    missing = [f"H{h}" for h in hyps if not re.search(rf"\bH{h}\b", concl_body)]
    if missing:
        collector.add_inconsistency(
            f"[{rel}] %TODO-HIPOTESIS hipótesis sin mención explícita en Conclusiones: "
            f"{', '.join(missing)} (verificar que las conclusiones respondan a cada una)."
        )
    else:
        # Señal informativa: todas cubiertas estructuralmente (falta juicio de fondo).
        collector.add_inconsistency(
            f"[{rel}] INFO: las {len(hyps)} hipótesis (H1--H{hyps[-1]}) se mencionan en "
            "Conclusiones; revisar manualmente que cada respuesta sea concluyente (§22)."
        )


def _check_editorial(text: str, rel: str, collector: TodoCollector) -> None:
    """(Editorial 1 y 2) Siglas sin definir en primer uso y términos no unificados.

    - Siglas: la primera aparición de cada sigla del glosario debe ir acompañada
      de su expansión (entre paréntesis, en cualquier orden). Se ignora el bloque
      de bibliografía (títulos en inglés) y las líneas de comando.
    - Términos: reporta variantes de grafía conviviendo (steering §6/§11).
    """
    # Recortar la bibliografía para no analizar títulos en inglés.
    body = re.split(r"\\begin\{thebibliography\}", text)[0]
    lines = body.split("\n")

    # (1) Siglas sin definir en primer uso.
    for acr, expansion in ACRONYMS.items():
        first_ln = None
        for idx, line in enumerate(lines, start=1):
            code = re.sub(r"(?<!\\)%.*$", "", line)
            if re.search(rf"\b{acr}\b", code):
                first_ln = idx
                first_code = code
                break
        if first_ln is None:
            continue
        # ¿La expansión aparece cerca (misma línea) de la primera aparición?
        key_words = expansion.split()[0]  # p. ej. "Variational"
        if (
            key_words.lower() not in first_code.lower()
            and expansion.lower() not in first_code.lower()
        ):
            collector.add_inconsistency(
                f"[{rel}:{first_ln}] sigla '{acr}' usada sin definir en su primer uso "
                f"(añadir expansión: '{expansion} ({acr})')."
            )

    # (2) Términos no unificados (variantes conviviendo).
    for canonical, variants in TERM_VARIANTS:
        found: dict[str, int] = {}
        for v in variants:
            n = len(re.findall(v, body))
            if n:
                found[v] = n
        if len(found) > 1:
            detail = ", ".join(f"{v.strip(chr(92) + 'b')}={n}" for v, n in found.items())
            collector.add_inconsistency(
                f"[{rel}] término no unificado (canónico '{canonical}'): variantes "
                f"conviviendo [{detail}]; unificar (steering §6/§11)."
            )


def _check_bibliography(text: str, rel: str, collector: TodoCollector) -> None:
    """(15) Chequeos automatizables de bibliografía, steering §9.

    Detecta (sin acceder a arXiv): números arXiv duplicados entre entradas,
    mismo primer-autor+año sin sufijo a/b, y uso inconsistente de versiones vN.
    La verificación de que autor/título/año coincidan con el registro real de
    arXiv NO es automatizable aquí -> queda como tarea humana (steering §9).
    """
    bibitems = re.findall(r"\\bibitem\[([^\]]*)\]\{([^}]+)\}([^\n]*)", text)
    if not bibitems:
        return
    arxiv_seen: dict[str, list[str]] = {}
    authoryear: dict[str, list[str]] = {}
    has_version = 0
    for label_disp, key, body in bibitems:
        # arXiv duplicado
        for ax in re.findall(r"arXiv:(\d{4}\.\d{4,5})", body):
            arxiv_seen.setdefault(ax, []).append(key)
        # versión vN presente
        if re.search(r"arXiv:\d{4}\.\d{4,5}v\d", body):
            has_version += 1
        # primer autor + año a partir del display [Autor(año)]
        m = re.match(r"([A-Za-zÀ-ÿ]+).*?\((\d{4})[a-z]?\)", label_disp)
        if m:
            ay = f"{m.group(1).lower()}{m.group(2)}"
            has_suffix = bool(re.search(r"\(\d{4}[a-z]\)", label_disp))
            authoryear.setdefault(ay, []).append(key + ("*" if has_suffix else ""))
    for ax, keys in arxiv_seen.items():
        if len(keys) > 1:
            collector.add_inconsistency(
                f"[{rel}] BIBLIO arXiv:{ax} aparece en múltiples entradas ({', '.join(keys)}); "
                "verificar duplicado o número incorrecto (§9)."
            )
    for ay, keys in authoryear.items():
        if len(keys) > 1 and not all(k.endswith("*") for k in keys):
            clean = [k.rstrip("*") for k in keys]
            collector.add_inconsistency(
                f"[{rel}] BIBLIO mismo primer-autor+año sin sufijo a/b: {ay} "
                f"({', '.join(clean)}); usar 2025a, 2025b en texto y bibliografía (§9)."
            )
    n_total = len(bibitems)
    if 0 < has_version < n_total:
        collector.add_inconsistency(
            f"[{rel}] BIBLIO versiones vN inconsistentes: {has_version}/{n_total} entradas "
            "incluyen 'vN'; unificar (todas o ninguna, §9)."
        )

    # (16) Orden alfabético de la bibliografía: el apellido del primer autor del
    # display [Apellido...] debe ir en orden ascendente. Marca la primera entrada
    # que rompe el orden (no reordena; requiere criterio para casos límite).
    surnames = []
    for label_disp, key, _ in bibitems:
        ms = re.match(r"([A-Za-zÀ-ÿ\\\"'{}]+)", label_disp)
        # Normaliza acentos LaTeX comunes para comparar (\"o -> o, etc.).
        raw = ms.group(1) if ms else label_disp
        norm = re.sub(r'\\[\'"`^~]?\{?([A-Za-z])\}?', r"\1", raw).lower()
        surnames.append((norm, key))
    for idx in range(1, len(surnames)):
        prev_name, prev_key = surnames[idx - 1]
        cur_name, cur_key = surnames[idx]
        if cur_name < prev_name:
            collector.add_inconsistency(
                f"[{rel}] BIBLIO orden alfabético roto: '{cur_key}' ({cur_name}) "
                f"aparece después de '{prev_key}' ({prev_name}); reordenar (§9)."
            )

    # (17) Sufijo a/b huérfano: un display '(YYYYb)' exige que exista '(YYYYa)'
    # del mismo primer autor; y un '(YYYYa)' exige un '(YYYYb)'. Detecta sufijos
    # sueltos (p.ej. '2026b' sin '2026a').
    suffixed: dict[str, set[str]] = {}
    for label_disp, key, _ in bibitems:
        m = re.match(r"([A-Za-zÀ-ÿ]+).*?\((\d{4})([a-z])\)", label_disp)
        if m:
            base = f"{m.group(1).lower()}{m.group(2)}"
            suffixed.setdefault(base, set()).add(m.group(3))
    for base, letters in suffixed.items():
        if "b" in letters and "a" not in letters:
            collector.add_inconsistency(
                f"[{rel}] BIBLIO sufijo huérfano: existe '{base}b' pero no '{base}a'; "
                "quitar el sufijo si hay una sola entrada de ese autor-año (§9)."
            )
        if "a" in letters and "b" not in letters:
            collector.add_inconsistency(
                f"[{rel}] BIBLIO sufijo huérfano: existe '{base}a' pero no '{base}b'; "
                "quitar el sufijo si hay una sola entrada de ese autor-año (§9)."
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-fix: decimales con punto -> coma (con exclusiones seguras)
# ═══════════════════════════════════════════════════════════════════════════════

# Contextos donde un punto decimal NO debe tocarse (valores literales/técnicos).
_VERSION_RE = re.compile(r"[vV]?\d$|Qiskit|Python|arXiv|v\d")


def fix_decimals(tex_path: Path, collector: TodoCollector) -> int:
    """Convierte 'N.M' -> 'N,M' en el .tex, excluyendo contextos técnicos.

    Reglas de exclusión (no se tocan):
      - Dentro de \\texttt{...}, \\url{...}, \\ref/\\cite/\\label, o comentarios.
      - Exponentes (10^{-14}, 1e-3), versiones (Qiskit 2.x), arXiv, URLs.
      - h_c = 1.0 y valores de campo crítico (contexto físico canónico).
      - Números con 3+ partes (1.2.3, IPs, versiones).

    Escribe un backup .bak y registra cada cambio en el TODO log. Devuelve el
    número de sustituciones aplicadas.
    """
    original = tex_path.read_text(encoding="utf-8")
    lines = original.split("\n")
    rel = tex_path.name
    n_fixed = 0
    out_lines: list[str] = []

    # Comandos cuyo argumento no debe tocarse
    protect_cmd = re.compile(
        r"\\(?:texttt|url|href|ref|eqref|autoref|cite[tp]?|label|input|includegraphics)\{[^}]*\}"
    )
    # Número decimal candidato: entero.decimales, no seguido/precedido de otro punto o dígito extra
    dec_re = re.compile(r"(?<![\w.])(\d+)\.(\d+)(?![\w.])")

    def make_repl(
        code: str,
        idx: int,
        spans: list[tuple[int, int]],
        math_spans: list[tuple[int, int]],
    ):
        """Crea el reemplazador para una línea concreta (bind explícito).

        En prosa: 'N.M' -> 'N,M'. Dentro de math $...$: 'N.M' -> 'N{,}M' (las
        llaves evitan el espacio espurio que LaTeX inserta tras una coma en modo
        matemático, tratándola como separador de lista).
        """
        line_has_url = "arXiv" in code or "http" in code or "github" in code.lower()

        def repl(mm: re.Match) -> str:
            nonlocal n_fixed
            start = mm.start()
            before = code[max(0, start - 12) : start]
            after = code[mm.end() : mm.end() + 14]
            whole = mm.group(0)
            if any(a <= start < b for a, b in spans):  # dentro de comando protegido
                return whole
            if "^" in before[-3:] or "10^" in before or "times 10" in before:
                return whole
            if re.search(r"[eE]$", before) and re.search(r"^\d", after):  # 1e-3
                return whole
            if _VERSION_RE.search(before) or line_has_url:
                return whole
            # Longitudes LaTeX (width=0.90\textwidth, 0.5cm, 2.5pt): no tocar.
            if re.match(r"\s*(\\(?:text|line|column)width|\\height|cm|mm|pt|em|ex|in)\b", after):
                return whole
            in_math = any(a <= start < b for a, b in math_spans)
            sep = "{,}" if in_math else ","
            fixed = mm.group(1) + sep + mm.group(2)
            n_fixed += 1
            collector.add_inconsistency(
                f"[{rel}:{idx}] FIX aplicado: '{whole}' -> '{fixed}' (decimal a coma)."
            )
            return fixed

        return repl

    # Math inline $...$: los decimales SÍ se convierten, pero con '{,}' para
    # evitar el espaciado espurio de la coma en modo matemático.
    math_re = re.compile(r"(?<!\\)\$[^$]*\$")

    for idx, line in enumerate(lines, start=1):
        m = re.search(r"(?<!\\)%", line)
        comment_pos = m.start() if m else None
        code = line if comment_pos is None else line[:comment_pos]
        tail = "" if comment_pos is None else line[comment_pos:]
        spans = [mm.span() for mm in protect_cmd.finditer(code)]
        math_spans = [mm.span() for mm in math_re.finditer(code)]
        new_code = dec_re.sub(make_repl(code, idx, spans, math_spans), code)
        out_lines.append(new_code + tail)

    if n_fixed == 0:
        return 0

    # Backup no destructivo: no pisar un .bak previo (preserva el original).
    backup = tex_path.with_suffix(tex_path.suffix + ".bak")
    if backup.exists():
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = tex_path.with_suffix(tex_path.suffix + f".{stamp}.bak")
    backup.write_text(original, encoding="utf-8")
    tex_path.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"  🔧 fix-decimals: {n_fixed} sustituciones (backup en {backup.name})")
    return n_fixed


def fix_tone(tex_path: Path, collector: TodoCollector) -> int:
    """Aplica las sustituciones DETERMINISTAS de tono (TONE_FIXES), steering §6.

    Solo toca frases fijas cuyo reemplazo no depende del contexto (no altera el
    sentido). Casos que requieren criterio (demuestra/garantiza, adjetivos, "dentro
    de las configuraciones evaluadas") NO se auto-corrigen: se dejan como señal.
    Crea backup .bak (no destructivo) y registra cada cambio en el TODO log.
    """
    original = tex_path.read_text(encoding="utf-8")
    text = original
    rel = tex_path.name
    n_fixed = 0
    for needle, repl in TONE_FIXES:
        # Case-insensitive pero preservando: solo aplicamos en minúscula/tal cual.
        for m in list(re.finditer(re.escape(needle), text, re.IGNORECASE)):
            frag = m.group(0)
            n_fixed += text.count(frag)
            text = text.replace(frag, repl)
            collector.add_inconsistency(
                f"[{rel}] FIX-TONO aplicado: '{frag}' -> '{repl}' (steering §6)."
            )
    if n_fixed == 0:
        return 0
    backup = tex_path.with_suffix(tex_path.suffix + ".bak")
    if backup.exists():
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = tex_path.with_suffix(tex_path.suffix + f".{stamp}.bak")
    backup.write_text(original, encoding="utf-8")
    tex_path.write_text(text, encoding="utf-8")
    print(f"  🔧 fix-tone: {n_fixed} sustituciones (backup en {backup.name})")
    return n_fixed


def fix_anglicisms(tex_path: Path, collector: TodoCollector) -> int:
    """Traduce anglicismos de reemplazo unívoco (ANGLICISM_FIXES), steering §3.

    Opera línea a línea y SOLO en prosa: excluye \\texttt{...}, math $...$,
    comandos con argumento (ref/cite/label/url/includegraphics), comentarios, el
    capítulo Abstract y la bibliografía (inglés legítimo). Para cada línea, calcula
    las zonas protegidas y aplica el reemplazo únicamente en los tramos de prosa,
    preservando mayúscula inicial cuando el original la tenía. Los términos que
    requieren criterio (ANGLICISM_DETECT) o se conservan (ANGLICISM_KEEP) NO se
    tocan. Crea backup .bak (no destructivo) y registra cada cambio.
    """
    original = tex_path.read_text(encoding="utf-8")
    lines = original.split("\n")
    rel = tex_path.name
    n_fixed = 0
    out_lines: list[str] = []
    in_verbatim = False
    in_english = False

    def _apply_line(code: str, idx: int) -> str:
        nonlocal n_fixed
        # Zonas protegidas de ESTA línea (spans a no tocar).
        spans = [m.span() for m in _PROTECT_CMD_RE.finditer(code)]
        spans += [m.span() for m in _MATH_INLINE_RE.finditer(code)]

        def _protected(pos: int) -> bool:
            return any(a <= pos < b for a, b in spans)

        new = code
        # Reaplicar sobre 'new' desplaza índices; por eso se procesa patrón a
        # patrón reconstruyendo la cadena y recomputando spans tras cada cambio.
        for pat, repl in ANGLICISM_FIXES:
            rebuilt = []
            last = 0
            changed = False
            for m in re.finditer(pat, new):
                if _protected(m.start()):
                    continue
                # Preservar mayúscula inicial del original.
                frag = m.group(0)
                replacement = repl
                if frag[:1].isupper():
                    replacement = repl[:1].upper() + repl[1:]
                rebuilt.append(new[last : m.start()])
                rebuilt.append(replacement)
                last = m.end()
                changed = True
                n_fixed += 1
                collector.add_inconsistency(
                    f"[{rel}:{idx}] FIX-ANGLICISMO aplicado: '{frag}' -> "
                    f"'{replacement}' (steering §3)."
                )
            if changed:
                rebuilt.append(new[last:])
                new = "".join(rebuilt)
                # Recomputar spans protegidos tras la reescritura de la línea.
                spans = [m.span() for m in _PROTECT_CMD_RE.finditer(new)]
                spans += [m.span() for m in _MATH_INLINE_RE.finditer(new)]
        return new

    for idx, line in enumerate(lines, start=1):
        if re.search(r"\\begin\{(verbatim|lstlisting)\}", line):
            in_verbatim = True
        if re.search(r"\\end\{(verbatim|lstlisting)\}", line):
            in_verbatim = False
            out_lines.append(line)
            continue
        if re.search(r"\\chapter\{Abstract\}|\\begin\{thebibliography\}", line):
            in_english = True
        if re.search(r"\\mainmatter|\\end\{thebibliography\}", line):
            in_english = False
        if in_verbatim or in_english:
            out_lines.append(line)
            continue
        # Separar comentario para no tocarlo.
        m = re.search(r"(?<!\\)%", line)
        comment_pos = m.start() if m else None
        code = line if comment_pos is None else line[:comment_pos]
        tail = "" if comment_pos is None else line[comment_pos:]
        out_lines.append(_apply_line(code, idx) + tail)

    if n_fixed == 0:
        return 0
    backup = tex_path.with_suffix(tex_path.suffix + ".bak")
    if backup.exists():
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = tex_path.with_suffix(tex_path.suffix + f".{stamp}.bak")
    backup.write_text(original, encoding="utf-8")
    tex_path.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"  🔧 fix-anglicisms: {n_fixed} sustituciones (backup en {backup.name})")
    return n_fixed


def fix_emdash(tex_path: Path, collector: TodoCollector) -> int:
    """Convierte incisos pareados '---texto---' en incisos con comas, steering §4.

    El usuario pide no usar guiones como separador. Regla SEGURA y determinista:
    solo se tocan PARES '---X---' dentro de una misma línea (un inciso completo),
    reemplazándolos por ', X,' (o ', X' si ya sigue coma/punto). Los '---' sueltos
    (impares en la línea, p.ej. la etiqueta 'Fase 1 --- datos' o un inciso que
    cierra con el punto final) NO se tocan: requieren criterio y se dejan como
    detección. Se excluyen comentarios, \\texttt{...} y math $...$. Crea backup .bak.
    """
    original = tex_path.read_text(encoding="utf-8")
    lines = original.split("\n")
    rel = tex_path.name
    n_fixed = 0
    out_lines: list[str] = []

    # Par de em-dash con contenido en el medio, sin '---' anidado ni fin de línea.
    pair_re = re.compile(r"---(?P<inner>(?:(?!---).)+?)---")

    for idx, line in enumerate(lines, start=1):
        # No tocar líneas de comentario (incluye los separadores %-----).
        stripped = line.lstrip()
        if stripped.startswith("%"):
            out_lines.append(line)
            continue
        m = re.search(r"(?<!\\)%", line)
        comment_pos = m.start() if m else None
        code = line if comment_pos is None else line[:comment_pos]
        tail = "" if comment_pos is None else line[comment_pos:]

        # Zonas protegidas (código/math): no reemplazar pares que las crucen.
        spans = [mm.span() for mm in _PROTECT_CMD_RE.finditer(code)]
        spans += [mm.span() for mm in _MATH_INLINE_RE.finditer(code)]

        def _in_protected(a: int, b: int) -> bool:
            return any(pa <= a and b <= pb for pa, pb in spans)

        def _repl(mm: re.Match) -> str:
            nonlocal n_fixed
            if _in_protected(mm.start(), mm.end()):
                return mm.group(0)
            inner = mm.group("inner")
            # El inciso equivale a comas: ', inner,'. La coma de apertura solo si
            # el carácter previo no es ya un espacio+coma o apertura.
            after_pos = mm.end()
            after = code[after_pos : after_pos + 1]
            close = "" if after in (",", ".", ";", ":", ")") else ","
            n_fixed += 1
            collector.add_inconsistency(
                f"[{rel}:{idx}] FIX-GUION aplicado: inciso '---...---' -> comas."
            )
            return f", {inner}{close}"

        new_code = pair_re.sub(_repl, code)
        # Limpiar dobles espacios o ' ,' introducidos por el reemplazo.
        new_code = re.sub(r"\s+,", ",", new_code)
        new_code = re.sub(r",\s*,", ",", new_code)
        out_lines.append(new_code + tail)

    if n_fixed == 0:
        return 0
    backup = tex_path.with_suffix(tex_path.suffix + ".bak")
    if backup.exists():
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = tex_path.with_suffix(tex_path.suffix + f".{stamp}.bak")
    backup.write_text(original, encoding="utf-8")
    tex_path.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"  🔧 fix-emdash: {n_fixed} incisos pareados convertidos (backup en {backup.name})")
    return n_fixed


def fix_grammar(tex_path: Path, collector: TodoCollector) -> int:
    """Aplica correcciones gramaticales deterministas (GRAMMAR_FIXES), steering §4.

    Solo frases fijas con reemplazo unívoco ("en base a" -> "a partir de";
    "en función a" -> "en función de"). No toca contextos protegidos ni cambia
    concordancia. Crea backup .bak y registra cada cambio.
    """
    original = tex_path.read_text(encoding="utf-8")
    text = original
    rel = tex_path.name
    n_fixed = 0
    for pat, repl in GRAMMAR_FIXES:
        for m in list(re.finditer(pat, text)):
            frag = m.group(0)
            n_fixed += 1
            collector.add_inconsistency(
                f"[{rel}] FIX-GRAMATICA aplicado: '{frag}' -> '{repl}' (steering §4)."
            )
        text = re.sub(pat, repl, text)
    if n_fixed == 0:
        return 0
    backup = tex_path.with_suffix(tex_path.suffix + ".bak")
    if backup.exists():
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = tex_path.with_suffix(tex_path.suffix + f".{stamp}.bak")
    backup.write_text(original, encoding="utf-8")
    tex_path.write_text(text, encoding="utf-8")
    print(f"  🔧 fix-grammar: {n_fixed} sustituciones (backup en {backup.name})")
    return n_fixed


# ═══════════════════════════════════════════════════════════════════════════════
# Compilación LaTeX + señales visuales (18/19)
# ═══════════════════════════════════════════════════════════════════════════════

_STUB_STY = r"""\NeedsTeXFormat{LaTeX2e}
\ProvidesPackage{estilo_unir-1}
\usepackage[utf8]{inputenc}\usepackage[T1]{fontenc}\usepackage[spanish]{babel}
\usepackage[draft]{graphicx}\usepackage{amsmath,amssymb}\usepackage{natbib}
\usepackage{hyperref}\usepackage{geometry}\usepackage{booktabs}\usepackage{multirow}\usepackage{siunitx}
\newcommand{\subject}[1]{\gdef\@subject{#1}}\newcommand{\profesor}[1]{\gdef\@profesor{#1}}
\providecommand{\@subject}{}\providecommand{\@profesor}{}
\renewcommand{\maketitle}{\begin{titlepage}\centering{\huge\@title\par}\end{titlepage}}
"""


def compile_tex(
    tex_path: Path, out_dir: Path, collector: TodoCollector, overfull_pt: float = 20.0
) -> None:
    """(18) Compila el .tex con pdflatex y (19) reporta señales visuales.

    Usa un stub de ``estilo_unir-1.sty`` (el .sty real vive fuera del repo), copia
    las tablas ``auto_*`` y compila 2 pasadas en un tempdir. Vuelca al TODO log:
      - errores fatales y referencias/citas indefinidas (18),
      - overfull hboxes por encima del umbral y figuras no encontradas (19),
    como señales para revisión visual. Requiere pdflatex en el PATH.
    """
    import shutil
    import subprocess
    import tempfile

    if shutil.which("pdflatex") is None:
        collector.add_inconsistency(
            "[compile] pdflatex no está en el PATH; no se pudo compilar (18/19)."
        )
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "tables").mkdir(exist_ok=True)
        shutil.copy(tex_path, tmp_path / tex_path.name)
        for auto in out_dir.glob("auto_*.tex"):
            shutil.copy(auto, tmp_path / "tables" / auto.name)
        (tmp_path / "estilo_unir-1.sty").write_text(_STUB_STY, encoding="utf-8")
        # Copiar carpetas de figuras si existen junto al .tex (para señales reales).
        for figdir in ("tesis-figures", "thesis_plots"):
            src = tex_path.parent / figdir
            if src.is_dir():
                shutil.copytree(src, tmp_path / figdir, dirs_exist_ok=True)

        log = ""
        try:
            for _ in range(2):
                proc = subprocess.run(
                    ["pdflatex", "-interaction=nonstopmode", tex_path.name],
                    cwd=tmp_path,
                    capture_output=True,
                    timeout=180,
                )
                log = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode(
                    "utf-8", "replace"
                )
        except subprocess.TimeoutExpired:
            collector.add_inconsistency(
                "[compile] pdflatex superó el timeout (180s); posible espera de entrada "
                "por un error no recuperable. Revisar el .tex manualmente (18)."
            )
            print("  📄 compile: TIMEOUT (pdflatex colgado)")
            return
        except (OSError, ValueError) as e:
            collector.add_inconsistency(f"[compile] fallo al ejecutar pdflatex: {e}")
            return

        rel = tex_path.name
        # Errores fatales
        fatals = re.findall(r"^! (.+)$", log, re.MULTILINE)
        for f in fatals[:10]:
            collector.add_inconsistency(f"[compile] error LaTeX: {f.strip()[:120]}")
        # Referencias/citas indefinidas
        for m in re.findall(r"(?:Reference|Citation) `([^']+)' (?:on page \S+ )?undefined", log):
            collector.add_inconsistency(f"[compile] referencia/cita indefinida: {m} (18).")
        undef_generic = len(re.findall(r"There were undefined references", log))
        if undef_generic and not fatals:
            collector.add_inconsistency(
                "[compile] el log reporta referencias indefinidas; correr otra pasada (18)."
            )
        # (19) Señales visuales: overfull hboxes grandes
        overs = re.findall(
            r"Overfull \\hbox \((\d+(?:\.\d+)?)pt too wide\)[^\n]*at lines (\d+)", log
        )
        big = [(float(pt), ln) for pt, ln in overs if float(pt) >= overfull_pt]
        for pt, ln in big[:15]:
            collector.add_inconsistency(
                f"[{rel}:{ln}] SEÑAL-VISUAL overfull hbox {pt:.0f}pt (texto se sale del "
                "margen; revisar visualmente esa página, §19)."
            )
        # (19) Figuras no encontradas (afectan el render)
        missing_figs = sorted(set(re.findall(r"File `([^']+)' not found", log)))
        for fig in missing_figs[:15]:
            collector.add_inconsistency(
                f"[compile] SEÑAL-VISUAL figura no encontrada: {fig} "
                "(no renderiza; verificar ruta, §19)."
            )
        pdf_ok = (tmp_path / tex_path.with_suffix(".pdf").name).exists()
        n_over = len(big)
        print(
            f"  📄 compile: {'PDF OK' if pdf_ok else 'SIN PDF'} | "
            f"{len(fatals)} errores | {n_over} overfull>{overfull_pt:.0f}pt | "
            f"{len(missing_figs)} figuras faltantes"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Orquestación
# ═══════════════════════════════════════════════════════════════════════════════

GENERATORS = {
    "auto_scoreboard": ("scoreboard", gen_scoreboard),
    # auto_coverage (coverage matrix, tab:auto_coverage) removed from the thesis
    # — the grade-per-(topology×N) matrix is not used. gen_coverage() is kept for
    # ad-hoc use but no longer emitted.
    "auto_campaign": ("index", gen_campaign),
    "auto_heavy_hex_intra_n": ("per_n", gen_heavy_hex_intra_n),
    "auto_heavy_hex_large_n": ("per_n", gen_heavy_hex_large_n),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Directorio de salida (default: {DEFAULT_OUT_DIR.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--only",
        type=str,
        default="",
        help="Lista separada por comas de tablas a generar (default: todas)",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="No regenerar el scoreboard JSON; usar el existente",
    )
    parser.add_argument(
        "--coverage-ns",
        type=str,
        default="",
        help="Lista de N (coma) para las columnas de auto_coverage "
        "(default: todos los N presentes)",
    )
    parser.add_argument(
        "--check-tex",
        type=Path,
        default=None,
        help="Chequear consistencia de un .tex (refs/labels, decimales, "
        "tablas auto no conectadas) y volcar hallazgos a tesis_todos.txt",
    )
    parser.add_argument(
        "--fix-decimals",
        type=Path,
        default=None,
        help="Convertir decimales con punto a coma en el .tex indicado "
        "(excluye h_c, exponentes, versiones, URLs, arXiv, comandos). "
        "Crea backup .bak y registra cada cambio en tesis_todos.txt.",
    )
    parser.add_argument(
        "--fix-tone",
        type=Path,
        default=None,
        help="Aplicar sustituciones deterministas de tono comercial (frases fijas: "
        "'el pipeline funciona', 'coste cuántico cero', etc.) en el .tex indicado. "
        "Crea backup .bak y registra cada cambio en tesis_todos.txt (steering §6).",
    )
    parser.add_argument(
        "--fix-anglicisms",
        type=Path,
        default=None,
        help="Traducir anglicismos SEGUROS de reemplazo unívoco (noiseless->"
        "simulación ideal, cross-N->entre tamaños, epochs->épocas, etc.) en el .tex "
        "indicado, solo en prosa (excluye código, math, Abstract y bibliografía). "
        "Los términos que cambian concordancia (ground truth, dataset, framework) "
        "solo se detectan. Crea backup .bak y registra cada cambio (steering §3).",
    )
    parser.add_argument(
        "--fix-grammar",
        type=Path,
        default=None,
        help="Corregir gramática determinista ('en base a'->'a partir de', "
        "'en función a'->'en función de') en el .tex indicado. Crea backup .bak "
        "y registra cada cambio (steering §4).",
    )
    parser.add_argument(
        "--fix-emdash",
        type=Path,
        default=None,
        help="Convertir incisos pareados '---texto---' en incisos con comas "
        "(el usuario pide no usar guiones como separador). Solo toca pares en una "
        "misma línea; los '---' sueltos (etiquetas, cierre con punto) se dejan como "
        "detección. Excluye comentarios/código/math. Crea backup .bak (steering §4).",
    )
    parser.add_argument(
        "--compile",
        dest="compile_tex",
        type=Path,
        default=None,
        help="Compilar el .tex con pdflatex (stub .sty) y volcar errores, "
        "citas/refs indefinidas y señales visuales (overfull, figuras faltantes) "
        "a tesis_todos.txt (18/19).",
    )
    args = parser.parse_args()

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    collector = TodoCollector()
    only = {s.strip() for s in args.only.split(",") if s.strip()} or set(GENERATORS)
    coverage_ns: set[int] | None = None
    if args.coverage_ns:
        try:
            coverage_ns = {int(x) for x in args.coverage_ns.split(",") if x.strip()}
        except ValueError:
            print("  ⚠️ --coverage-ns inválido; se ignora", file=sys.stderr)

    # Cargar fuentes (perezoso según lo que se pida)
    scoreboard = load_scoreboard(refresh=not args.no_refresh, collector=collector)
    index = load_campaign_index(collector) if "auto_campaign" in only else []
    per_n = (
        load_heavy_hex_per_h(collector)
        if only & {"auto_heavy_hex_intra_n", "auto_heavy_hex_large_n"}
        else {}
    )

    if scoreboard:
        detect_inconsistencies(scoreboard, collector)

    written: list[str] = []
    for table_id, (src, gen) in GENERATORS.items():
        if table_id not in only:
            continue
        if table_id == "auto_coverage":
            lines = gen(scoreboard, collector, coverage_ns)
        elif src == "scoreboard":
            lines = gen(scoreboard, collector)
        elif src == "index":
            lines = gen(index, collector)
        else:
            lines = gen(per_n, collector)
        out_path = out_dir / f"{table_id}.tex"
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # Registrar TODOs con su número de línea REAL en el archivo final
        collector.scan_file(out_path, f"{table_id}.tex")
        written.append(str(out_path.relative_to(ROOT)))
        print(f"  ✅ {out_path.relative_to(ROOT)}")

    # Auto-fix de decimales (opcional; se aplica antes del chequeo)
    if args.fix_decimals is not None:
        fix_decimals(args.fix_decimals, collector)

    # Auto-fix de tono determinista (opcional; antes del chequeo)
    if args.fix_tone is not None:
        fix_tone(args.fix_tone, collector)

    # Auto-fix de anglicismos univocos (opcional; antes del chequeo)
    if args.fix_anglicisms is not None:
        fix_anglicisms(args.fix_anglicisms, collector)

    # Auto-fix de gramatica determinista (opcional; antes del chequeo)
    if args.fix_grammar is not None:
        fix_grammar(args.fix_grammar, collector)

    # Auto-fix de incisos con em-dash (opcional; antes del chequeo)
    if args.fix_emdash is not None:
        fix_emdash(args.fix_emdash, collector)

    # Chequeo del documento LaTeX (opcional)
    if args.check_tex is not None:
        check_tex(args.check_tex, out_dir, collector)
        print(f"  🔍 Chequeo LaTeX: {args.check_tex}")

    # Compilación + señales visuales (opcional, 18/19)
    if args.compile_tex is not None:
        compile_tex(args.compile_tex, out_dir, collector)

    # Volcar tesis_todos.txt
    todos_path = out_dir / "tesis_todos.txt"
    todos_path.write_text(collector.render_txt(), encoding="utf-8")
    print(
        f"  📝 {todos_path.relative_to(ROOT)} "
        f"({collector.n_todos()} TODOs, {len(collector.inconsistencies)} inconsistencias)"
    )

    print(f"\n  {len(written)} tablas generadas en {out_dir.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
