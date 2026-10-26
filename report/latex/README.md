The manuscript uses an article layout, numbered booktabs tables, amsmath equations and natbib/BibTeX references. From this folder, I compile it with:

```sh
latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
```

Reported values live in the `\newcommand` block in `main.tex`. Each computed result has its JSON path and print format above it; `v2.` selects the separate v2 summary. The remaining macros hold fixed parameters and source values. From the repo root, I regenerate or check the computed values with:

```sh
python scripts/build_paper_numbers.py
python scripts/build_paper_numbers.py --check
```

The defaults read the checked summaries in `report/`. CI uses fresh offline rebuilds for both summaries and fails on numeric drift. The numerical rebuild remains separate from manuscript compilation.

Figure 1 is a static PDF in `figure-1.pdf`. Editing the text macros leaves its labels and data unchanged. The first LaTeX pass writes the bibliography style, then latexmk runs BibTeX and the remaining passes. The required inputs are `main.tex`, `references.bib` and `figure-1.pdf`.

The TeX distribution needs latexmk, Latin Modern, amsmath, booktabs and natbib. Poppler renders the README snapshot from the compiled first page:

```sh
pdftoppm -f 1 -singlefile -r 120 -png main.pdf paper-preview
```
