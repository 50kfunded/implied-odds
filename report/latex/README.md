I compile from this folder with:

```sh
latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
```

I keep the numeric values in the `\newcommand` block in `main.tex`. Each computed result has its `summary.json` path and print format immediately above it. The remaining values preserve the source paper's fixed wording and parameters.

I keep Figure 1 in `figure-1.pdf`. Updating the text macros doesn't redraw its labels or data.

I include the bibliography style in `main.tex`; the first LaTeX pass writes `references-exact.bst`, then latexmk runs BibTeX and the remaining LaTeX passes. I keep `main.tex`, `references.bib` and `figure-1.pdf` together.

I use a TeX distribution with latexmk, Latin Modern, amsmath, booktabs and natbib. The Python results rebuild doesn't compile or change this manuscript.

I render the README snapshot from the compiled first page with Poppler:

```sh
pdftoppm -f 1 -singlefile -r 120 -png main.pdf paper-preview
```
