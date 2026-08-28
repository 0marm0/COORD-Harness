# Brand font

The wordmark may use a licensed typeface. Font licences do not permit
redistribution, so the files are **not** in this repository and the public
stylesheet does not request them over HTTP. It asks the browser for a locally
installed `Lausanne` face and falls back to the system sans stack when absent.

To use it on a build you have licensed, install the face locally. Any face also
works if you change the `local(...)` names in `app.css`: the stylesheet names
the family `Coord Brand`, and nothing else depends on which typeface answers to
it. The letterspacing in `h1.wordmark` is what makes the mark read as a mark;
the face is the part you can swap.
