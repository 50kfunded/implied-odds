The manuscript uses an article layout, numbered booktabs tables, amsmath equations and natbib/BibTeX references. From this folder, I compile it with:

```sh
latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
```

Reported values live in the `\newcommand` block in `main.tex`. Each computed result has its `summary.json` path and print format above it; the remaining macros hold fixed parameters and source values. The numerical results rebuild is separate from manuscript compilation.

Figure 1 is a static PDF in `figure-1.pdf`. Editing the text macros leaves its labels and data unchanged. The first LaTeX pass writes the bibliography style, then latexmk runs BibTeX and the remaining passes. The required inputs are `main.tex`, `references.bib` and `figure-1.pdf`.

The TeX distribution needs latexmk, Latin Modern, amsmath, booktabs and natbib. Poppler renders the README snapshot from the compiled first page:

```sh
pdftoppm -f 1 -singlefile -r 120 -png main.pdf paper-preview
```
