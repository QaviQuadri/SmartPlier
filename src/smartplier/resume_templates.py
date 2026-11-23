"""Print-oriented HTML and XeLaTeX templates based on the source resume."""

from datetime import date
from html import escape


TEMPLATE_VERSION = "a4-serif-1"
CONTACT_FIELDS = ("email", "phone", "github", "linkedin", "portfolio")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
TEX = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$",
    "&": r"\&", "#": r"\#", "%": r"\%", "_": r"\_", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}", "<": r"\textless{}", ">": r"\textgreater{}",
}


def latex_text(value):
    return "".join(TEX.get(char, char) for char in " ".join(value.split()))


def format_date(value):
    if value:
        try:
            month = date.fromisoformat(value + "-01")
            return f"{MONTHS[month.month - 1]} {month.year}"
        except ValueError:
            return value
    return ""


def sections(profile, selection):
    """Share section order and approved content between both renderers."""
    selected = {skill.casefold(): skill for skill in selection["skills"]}
    groups = []
    remaining = set(selected)
    for label, skills in profile["skill_groups"].items():
        matches = [skill for skill in skills if skill.casefold() in remaining]
        if matches:
            groups.append((label, ", ".join(matches)))
            remaining.difference_update(skill.casefold() for skill in matches)
    if remaining:
        groups.append(("Other", ", ".join(skill for key, skill in selected.items() if key in remaining)))

    entries = {"experience": [], "projects": []}
    for section in entries:
        for entry in profile[section]:
            bullets = [selection["bullets"][bullet["id"]] for bullet in entry["bullets"]
                       if bullet["id"] in selection["bullets"]]
            if not bullets:
                continue
            if section == "experience":
                dates = " – ".join(filter(None, (format_date(entry["start"]), format_date(entry["end"]))))
                entries[section].append((f'{entry["title"]} | {entry["company"]}', dates, "", bullets))
            else:
                entries[section].append((entry["name"], "", ", ".join(entry["tech"]), bullets))
    basics = profile["basics"]
    contacts = [basics[field] for field in CONTACT_FIELDS if basics.get(field)]
    return basics["name"].upper(), contacts, groups, entries, profile["education"]


HTML_STYLE = """
@page { size: A4; margin: 11mm 12.7mm; }
* { box-sizing: border-box; }
body { margin: 0; color: #111; font: 10.5pt/1.22 "Times New Roman", "Liberation Serif", serif; }
main { overflow-wrap: anywhere; }
header { text-align: center; }
h1 { font-size: 20pt; line-height: 1.05; letter-spacing: .015em; margin: 0 0 4pt; color: #19365b; }
h2 { font-size: 12pt; color: #19365b; border-bottom: .4pt solid #666; margin: 6pt 0 2.5pt; }
h2, .line, .tech { break-after: avoid; }
p { margin: 0; }
.line { display: flex; align-items: baseline; justify-content: space-between; gap: 6pt; margin-top: 1pt; }
.right { flex-shrink: 0; text-align: right; }
.tech { font-style: italic; }
.contacts { font-size: 9.5pt; }
ul { margin: 1pt 0 2pt; padding-left: 1.25em; }
li { break-inside: avoid; }
@media screen {
  body { background: #edf0f4; }
  main { width: min(210mm, 100%); min-height: 297mm; margin: 12px auto; padding: 11mm 12.7mm;
         background: white; box-shadow: 0 2px 10px #19264225; }
}
@media print {
  h1, h2 { print-color-adjust: exact; }
}
"""


def render_html(profile, selection):
    name, contacts, groups, entries, education = sections(profile, selection)
    parts = [
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(name)} · Resume</title><style>{HTML_STYLE}</style></head><body><main>",
        f'<header><h1>{escape(name)}</h1><p class="contacts">{escape("  |  ".join(contacts))}</p></header>',
    ]
    if selection["summary"]:
        parts.extend(("<h2>SUMMARY</h2>", f'<p>{escape(selection["summary"])}</p>'))
    if groups:
        parts.append("<h2>SKILLS</h2>")
        parts.extend(f"<p><strong>{escape(label)}:</strong> {escape(skills)}</p>" for label, skills in groups)
    for section, records in entries.items():
        if not records:
            continue
        parts.append(f"<h2>{section.upper()}</h2>")
        for title, dates, tech, bullets in records:
            parts.append(f'<div class="line"><strong>{escape(title)}</strong>'
                         f'<span class="right">{escape(dates)}</span></div>')
            if tech:
                parts.append(f'<p class="tech">{escape(tech)}</p>')
            parts.append("<ul>" + "".join(f"<li>{escape(bullet)}</li>" for bullet in bullets) + "</ul>")
    if education:
        parts.append("<h2>EDUCATION</h2>")
        for item in education:
            parts.append(f'<div class="line"><strong>{escape(item["degree"])}</strong>'
                         f'<span class="right">{escape(item["year"])}</span></div>')
            gpa = f'GPA: {escape(item["gpa"])}' if item.get("gpa") else ""
            parts.append(f'<div class="line"><em>{escape(item["school"])}</em><span class="right">{gpa}</span></div>')
    parts.append("</main></body></html>\n")
    return "\n".join(parts)


LATEX_START = r"""\documentclass[a4paper,10pt]{article}
\usepackage[a4paper,top=11mm,bottom=11mm,left=12.7mm,right=12.7mm]{geometry}
\usepackage{fontspec}
\usepackage{xcolor}
\setmainfont{lmroman10-regular.otf}[
  BoldFont=lmroman10-bold.otf,
  ItalicFont=lmroman10-italic.otf,
  BoldItalicFont=lmroman10-bolditalic.otf]
\definecolor{heading}{HTML}{19365B}
\definecolor{rulegray}{HTML}{666666}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0pt}
\setlength{\emergencystretch}{2em}
\newcommand{\resumeheading}[1]{%
  \par\vspace{5pt}{\color{heading}\bfseries\fontsize{12}{13}\selectfont #1}\par
  \nobreak\vspace{1pt}{\color{rulegray}\hrule height .4pt}\nobreak\vspace{2pt}}
\newcommand{\resumeline}[2]{\par\textbf{#1}\hfill #2\par\nobreak}
\newenvironment{resumebullets}{%
  \begin{list}{\textbullet}{\setlength{\leftmargin}{1.3em}\setlength{\labelsep}{.35em}
    \setlength{\topsep}{1pt}\setlength{\itemsep}{0pt}\setlength{\parsep}{0pt}
    \setlength{\partopsep}{0pt}}}{\end{list}}
\begin{document}
\fontsize{10.5}{12.8}\selectfont
"""


def render_latex(profile, selection):
    name, contacts, groups, entries, education = sections(profile, selection)
    tx = latex_text
    parts = [
        LATEX_START,
        r"{\centering {\color{heading}\bfseries\fontsize{20}{22}\selectfont " + tx(name) + r"}\par",
        r"\vspace{3pt}{\fontsize{9.5}{12}\selectfont "
        + r" \quad\textbar\quad ".join(map(tx, contacts)) + r"}\par}",
    ]
    if selection["summary"]:
        parts.extend((r"\resumeheading{SUMMARY}", tx(selection["summary"]) + r"\par"))
    if groups:
        parts.append(r"\resumeheading{SKILLS}")
        parts.extend(rf"\textbf{{{tx(label)}:}} {tx(skills)}\par" for label, skills in groups)
    for section, records in entries.items():
        if not records:
            continue
        parts.append(rf"\resumeheading{{{section.upper()}}}")
        for title, dates, tech, bullets in records:
            parts.append(rf"\resumeline{{{tx(title)}}}{{{tx(dates)}}}")
            if tech:
                parts.append(rf"\textit{{{tx(tech)}}}\par\nobreak")
            parts.append(r"\begin{resumebullets}")
            parts.extend(rf"\item {{{tx(bullet)}}}" for bullet in bullets)
            parts.append(r"\end{resumebullets}")
    if education:
        parts.append(r"\resumeheading{EDUCATION}")
        for item in education:
            parts.append(rf'\resumeline{{{tx(item["degree"])}}}{{{tx(item["year"])}}}')
            gpa = f'GPA: {tx(item["gpa"])}' if item.get("gpa") else ""
            parts.append(rf'\textit{{{tx(item["school"])}}}\hfill {gpa}\par')
    parts.append("\\end{document}\n")
    return "\n".join(parts)
