"""Pinned chart frames survive hover and bound synchronized wheel zoom."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_wheel_zoom_bounds_and_anchor_lifetime():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for Viewer behavior checks")
    template = (
        Path(__file__).resolve().parents[2]
        / "lerobot/data_platform/templates/visualize_dataset_template.html"
    ).read_text()
    methods = []
    for name in ("zoomChartAtAnchor", "selectChartTime", "setChartWindow", "resetChartZoom"):
        match = re.search(rf"^                {name}\([^\n]*\) \{{", template, re.MULTILINE)
        end = re.search(r"^                \},", template[match.end() :], re.MULTILINE)
        methods.append(template[match.start() : match.end() + end.end()])
    script = "const assert = require('node:assert/strict'); const app = {" + "\n".join(methods) + "};\n"
    script += r"""
    const graph = () => ({range: [0, 10], xAxisRange() {return this.range;},
        xAxisExtremes() {return [-0.3, 10.3];}, numRows() {return 2;},
        getValue(row) {return row ? 9.9 : 0;}, getArea() {return {h: 200};},
        updateOptions(options) {if (options.dateWindow) this.range = options.dateWindow;}});
    const a = graph(), b = graph();
    Object.assign(app, {dygraphArmJoints: a, dygraphGripperFlag: b, fps: 30, chartAnchorTime: null, playbackDuration: () => 10,
        timeToFrame: t => Math.max(0, Math.min(299, Math.round(t * 30))), frameToTime: f => f / 30,
        seekPlayback(t) {this.dygraphTime = t;}, _redrawTrimCharts() {}});
    let prevented = 0;
    const wheel = {deltaY: -100, deltaMode: 0, preventDefault() {prevented++;}};
    app.zoomChartAtAnchor(wheel, a);
    assert.equal(prevented, 0, 'Before selection, wheel must retain page scrolling');
    app.selectChartTime(4.01);
    assert.equal(app.chartAnchorTime, 4);
    assert.equal(app.dygraphTime, 4);
    app.zoomChartAtAnchor(wheel, a);
    assert.equal(prevented, 1);
    assert.deepEqual(a.range, b.range);
    assert.ok(a.range[1] - a.range[0] < 10);
    for (let i=0; i<50; i++) app.zoomChartAtAnchor(wheel, b);
    assert.ok(Math.abs(a.range[1] - a.range[0] - 2/30) < 1e-10);
    assert.ok(Math.abs((a.range[0] + a.range[1])/2 - 4) < 1e-10);
    app.dygraphTime = 8; // Playback must not move the pinned zoom anchor.
    app.resetChartZoom();
    assert.equal(app.chartAnchorTime, 4);
    for (const flags of [{ctrlKey:true}, {metaKey:true}, {deltaY:0}, {deltaY:NaN}]) {
        const before = prevented;
        app.zoomChartAtAnchor({...wheel, ...flags}, a);
        assert.equal(prevented, before);
    }
    for (const state of ['_chartSeekState', '_trimDragState', '_dragState']) {
        app[state] = {};
        const before = prevented;
        app.zoomChartAtAnchor(wheel, a);
        assert.equal(prevented, before);
        app[state] = null;
    }
    for (const time of [0, 9.99]) {
        app.selectChartTime(time);
        for (const deltaMode of [0, 1, 2]) {
            app.zoomChartAtAnchor({...wheel, deltaMode}, a);
            assert.ok(a.range[0] >= 0 && a.range[1] <= 10);
            assert.ok(a.range[0] <= app.chartAnchorTime && a.range[1] >= app.chartAnchorTime);
        }
        for (let i=0; i<50; i++) app.zoomChartAtAnchor({...wheel, deltaY:100}, a);
        assert.deepEqual(a.range, [0,10]);
        assert.deepEqual(a.range, b.range);
        assert.equal(app.chartZoomed, false);
    }
    app.selectChartTime(NaN);
    assert.ok(Number.isFinite(app.chartAnchorTime));
    a.range = [0,0];
    const before = prevented;
    app.zoomChartAtAnchor(wheel, a);
    assert.equal(prevented, before, 'Degenerate plots must not capture scrolling');
    """
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
