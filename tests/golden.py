"""The golden set: real pages with a known correct reading order.

Assertions check the *order of key phrases* rather than exact strings, because
recognition on real rendered text is imperfect in ways that are not the
ordering algorithm's fault. Getting "Serchive" instead of "Settings Archive" is
an OCR artefact; reading the right-hand column before the left is a Slicer bug,
and only the second kind should fail a test.
"""

from __future__ import annotations

BASE = """
<html><head><meta charset="utf-8"><style>
  body { font: 17px Georgia, serif; margin: 0; padding: 24px; width: %dpx;
         background: #fff; color: #111; }
  h1 { font-size: 30px; margin: 0 0 18px; }
  p  { margin: 0 0 14px; line-height: 1.45; }
  nav div { font-size: 13px; padding: 5px 0; }
  code, pre { font-family: Menlo, monospace; font-size: 14px; }
  table { border-collapse: collapse; } td, th { padding: 6px 22px; text-align: left; }
</style></head><body>%s</body></html>
"""

PAGES: dict[str, tuple[str, list[str]]] = {}


def page(name: str, body: str, order: list[str], width: int = 940) -> None:
    PAGES[name] = (BASE % (width, body), order)


page("single_column", """
<h1>A Single Column Article</h1>
<p>The first paragraph establishes the subject and runs for long enough to wrap
onto a second line, which is what real body text does.</p>
<p>The second paragraph follows it and should be read immediately afterwards,
without anything appearing in between.</p>
""", ["A Single Column Article", "The first paragraph", "The second paragraph"])

page("two_column", """
<div style="column-count:2;column-gap:48px">
<p>ALPHA the left column begins here and continues with enough words that it
fills its side of the page properly before the break.</p>
<p>BRAVO still the left column, a second paragraph that keeps the first column
occupied for a while longer than one line.</p>
<p>CHARLIE now the right hand column starts, and it should be read only after
everything on the left has finished.</p>
<p>DELTA the final paragraph of the right column ends the page.</p>
</div>
""", ["ALPHA", "BRAVO", "CHARLIE", "DELTA"])

page("header_two_column", """
<h1>Headline Spanning The Page</h1>
<div style="column-count:2;column-gap:48px">
<p>ALPHA the left column under a full width headline, running for a couple of
lines so the column is properly established.</p>
<p>BRAVO the right column, which must not be read until the left is done.</p>
</div>
""", ["Headline Spanning The Page", "ALPHA", "BRAVO"])

page("sidebar", """
<div style="display:flex;gap:44px">
  <nav style="width:120px"><div>Home</div><div>Reports</div><div>Settings</div>
       <div>Archive</div><div>Billing</div></nav>
  <div style="width:700px">
    <h1>Content Beside Navigation</h1>
    <p>ALPHA the body text of the page, which is what a reader actually wants
    to hear, running across several lines of ordinary prose.</p>
    <p>BRAVO a second paragraph of the same body content.</p>
  </div>
</div>
""", ["Content Beside Navigation", "ALPHA", "BRAVO"])

page("table", """
<h1>Regional Results</h1>
<table>
<tr><th>Region</th><th>Revenue</th><th>Growth</th></tr>
<tr><td>North</td><td>4,318</td><td>12%</td></tr>
<tr><td>South</td><td>2,901</td><td>8%</td></tr>
<tr><td>West</td><td>5,144</td><td>21%</td></tr>
</table>
""", ["Region", "North", "4,318", "South", "2,901", "West", "5,144"])

page("three_column", """
<div style="column-count:3;column-gap:36px">
<p>ALPHA the first of three columns with enough text to fill it.</p>
<p>BRAVO the second column follows the first one completely.</p>
<p>CHARLIE the third column is last and must not be dropped.</p>
</div>
""", ["ALPHA", "BRAVO", "CHARLIE"])

page("code_and_prose", """
<h1>Rotating Certificates</h1>
<p>ALPHA the prose explains what the function below is for, in ordinary words.</p>
<pre>def rotate(cert):
    if cert.expires_in() &lt; 30:
        renew(cert)</pre>
<p>BRAVO the prose resumes after the code block has been announced.</p>
""", ["Rotating Certificates", "ALPHA", "rotate", "BRAVO"])
