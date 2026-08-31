"""Scrollable visuals: a chart whose content outgrows its pane grows the
canvas and scrolls, instead of being crushed into the fit.

The failure this covers, found by browser verification (Constitution IV): a
sankey with ~30 nodes per stage laid out in a builder-size pane collapsed
every node into a 1px sliver with its labels overprinting each other, and
past ~45 nodes the inter-node gaps alone outgrew the plot area — the scale
went negative and the layout landed outside the frame, clipped, with nothing
in the UI able to reach it. Dense bar charts shaved every bar to the 1.5px
floor the same way, and the table/stat forms of a visual overflowed their
pane with no scrollbar anywhere but a dashboard tile.

These are source assertions against the served assets, in the same spirit as
tests/test_static.py: the layout maths itself is verified in a browser, but
the pieces that make scrolling possible at all are pinned here so they can't
be quietly undone.
"""


def frame_js(client):
    return client.get("/static/js/charts/frame.js").text


def css(client):
    return client.get("/static/style.css").text


# ── the shared scaffolding ───────────────────────────────────

def test_plot_frame_takes_the_canvas_its_caller_needs(client):
    """plotFrame's second argument is the canvas the content actually wants.
    Without it every chart is sized by the pane alone and has no way to ask
    for more room, which is the whole bug."""
    src = frame_js(client)
    assert "export function plotFrame(box, want = {})" in src
    assert "want.width" in src and "want.height" in src
    # the pane's own size stays a floor — growing must never shrink a chart
    assert "Math.max(space.W," in src and "Math.max(space.H," in src


def test_a_grown_canvas_gets_a_scrolling_pane(client):
    """The growth is only useful if the overflow is reachable: a canvas
    bigger than its pane goes inside .viz-scroll, and the frame reports it so
    callers can tell a grown chart from a fitted one."""
    src = frame_js(client)
    assert 'class: "viz-scroll"' in src
    assert "scrolls" in src, "plotFrame must report whether the canvas grew"
    assert ".viz-scroll { width: 100%; height: 100%; overflow: auto; }" in css(client)


def test_the_stylesheet_cannot_shrink_a_grown_canvas_back_into_the_pane(client):
    """`.chart-box svg { width: 100%; height: 100% }` sizes a fitted chart —
    applied to a grown one it would scale the canvas straight back down to
    the pane and there would be nothing to scroll. The grown canvas carries
    its own pixel size, and the stylesheet has to stand down for it."""
    style = css(client)
    assert ".chart-box svg { display: block; width: 100%; height: 100%; }" in style
    assert ".chart-box .viz-scroll > svg { width: auto; height: auto; }" in style
    src = frame_js(client)
    assert 'svg.style.width = W + "px"' in src
    assert 'svg.style.height = H + "px"' in src


def test_plot_space_measures_the_pane_without_its_padding(client):
    """clientWidth/clientHeight include the pane's own padding. That was
    harmless while the canvas was always scaled to fit (an SVG viewBox
    absorbs the error), but a canvas sized in real pixels that is wider than
    the pane holding it scrolls sideways on its own overshoot. Measure the
    content box."""
    src = frame_js(client)
    assert "getComputedStyle(box)" in src
    assert "paddingLeft" in src and "paddingRight" in src
    assert "paddingTop" in src and "paddingBottom" in src


def test_the_scrollbar_is_charged_to_the_axis_that_did_not_grow(client):
    """A vertically grown canvas gives the pane a vertical scrollbar, which
    takes width the canvas was already using — leaving a sliver of horizontal
    scroll over nothing. The axis that grew keeps its full size; the other
    one pays for the scrollbar."""
    src = frame_js(client)
    assert "SCROLLBAR" in src
    assert "growsY && !growsX" in src and "growsX && !growsY" in src


def test_canvas_growth_is_capped(client):
    """A 10k-row result can carry 10k distinct dimension values. Growing a
    canvas to fit those unbounded would cost real memory to paint something
    nobody can read; past the cap a chart compresses again, as before."""
    src = frame_js(client)
    assert "export const MAX_CANVAS" in src
    assert "Math.min(MAX_CANVAS" in src


# ── sankey: the diagram that reported the bug ────────────────

def test_sankey_asks_for_the_room_its_flows_need(client):
    """Room per node (its label has to fit somewhere) and room per stage (the
    labels sit between the stages), rather than whatever the pane happens to
    offer."""
    src = client.get("/static/js/charts/sankey.js").text
    assert "plotSpace" in src, "sankey must measure the pane before sizing its canvas"
    assert "crowd * SLOT_H" in src, "the node count has to drive the canvas height"
    assert "(stages.length - 1) * STAGE_W" in src, "the stage count has to drive the canvas width"


def test_sankey_scale_can_never_go_negative(client):
    """The original scale was `(plotH - GAP * (nodes - 1)) / total`. Enough
    nodes and the gaps alone exceed plotH, the numerator goes negative, and
    every node lands above the frame with a negative height — the clipped,
    unreachable layout this feature exists to fix. The numerator is floored
    now, and the canvas has usually grown to make that floor moot."""
    src = client.get("/static/js/charts/sankey.js").text
    assert "Math.max(1, f.plotH - gap * (s.nodes.length - 1)) / s.total" in src


def test_sankey_layout_stays_inside_its_canvas(client):
    """Flooring the tiny nodes to a visible height adds height the scale
    didn't account for, and the cap above can hand back less room than was
    asked for. Either way the tallest stage can outgrow the canvas — so the
    whole layout (heights, floor and gaps together) is squeezed by one
    factor, which keeps every proportion and puts nothing outside the
    frame."""
    src = client.get("/static/js/charts/sankey.js").text
    assert "const squeeze = f.plotH / tallest;" in src
    assert "scale *= squeeze; floorH *= squeeze; gap *= squeeze;" in src


def test_sankey_links_fill_both_ends_of_their_nodes(client):
    """A node floored up off its true height is taller than the flows through
    it, so a link sized once (`v * scale`) would leave a gap at whichever end
    was floored. Each end takes its share of the node it lands on instead —
    the same trick real sankeys use for ribbons of differing end thickness —
    so both ends still fill their node exactly."""
    src = client.get("/static/js/charts/sankey.js").text
    assert "const h0 = (v / na.total) * na.h, h1 = (v / nb.total) * nb.h;" in src
    assert "na.outOff += h0; nb.inOff += h1;" in src
    # both thicknesses reach the path, or one end silently keeps the other's
    assert "${y1 + h1}" in src and "${y0 + h0}" in src


def test_sankey_still_separates_flow_keys_with_nul(client):
    """Regression guard, carried over from the fix the constitution cites: a
    space would collide with real dimension values. The layout rewrite must
    not have quietly changed the separator (or written a raw NUL byte into
    the source in place of the escape)."""
    src = client.get("/static/js/charts/sankey.js").text
    assert 'const SEP = "\\u0000";' in src
    assert "\x00" not in src, "the escape, not a literal NUL byte in the source"


# ── the other forms a visual can take ────────────────────────

def test_bar_grows_rather_than_shaving_every_bar_to_a_hairline(client):
    """Past a few hundred categories in a builder-size pane, `barW` sat on
    its 1.5px floor: bars too thin to read and nearly too thin to hover. The
    canvas takes the width the categories need and the pane scrolls."""
    src = client.get("/static/js/charts/bar.js").text
    assert "MIN_BAND" in src and "MIN_BAR_SLOT" in src
    assert "xs.length * Math.max(MIN_BAND, MIN_BAR_SLOT * series.length)" in src


def test_table_form_of_a_visual_scrolls_wherever_it_renders(client):
    """.table-scroll had a height (and so a scrollbar) only inside a
    dashboard tile — in the focus modal, a notebook or a chat answer the same
    element just overflowed its pane. charts/index.js builds one element for
    all of them, so the base rule carries the scrolling."""
    style = css(client)
    assert ".table-scroll { height: 100%; overflow: auto; }" in style
    assert 'el("div", { class: "table-scroll" })' in client.get("/static/js/charts/index.js").text


def test_stat_tiles_scroll_when_they_outgrow_their_pane(client):
    """A dimensionless query with many measures wraps past a short pane; the
    tiles below the fold were unreachable."""
    style = css(client)
    grid = style.split(".stat-grid {", 1)[1].split("}", 1)[0]
    assert "overflow: auto" in grid
    assert "align-content: center" in grid, "packed lines, so the scroll starts at the tiles"
