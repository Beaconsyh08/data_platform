"""Playback controls share one timeline, including native controls and late media."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_native_controls_share_playback_and_seek_without_event_feedback():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for Viewer behavior checks")
    template = (
        Path(__file__).resolve().parents[2]
        / "lerobot/data_platform/templates/visualize_dataset_template.html"
    ).read_text()
    methods = []
    for name in (
        "playbackVideos",
        "playbackVideo",
        "playbackDuration",
        "updatePlaybackPosition",
        "seekVideo",
        "seekPlayback",
        "playVideo",
        "setPlaybackPaused",
        "videoPlaybackChanged",
        "videoSeeking",
        "videoTimeUpdate",
        "videoEnded",
        "videoCanPlay",
        "setPlaybackRate",
        "formatTime",
        "timeToFrame",
        "clampTrimFrame",
        "maxTrimFrame",
        "timeToGraphRow",
    ):
        match = re.search(rf"^                {name}\([^\n]*\) \{{", template, re.MULTILINE)
        assert match, f"Missing shared playback method: {name}"
        end = re.search(r"^                \},", template[match.end() :], re.MULTILINE)
        methods.append(template[match.start() : match.end() + end.end()])
    script = "const assert = require('node:assert/strict'); const app = {" + "\n".join(methods) + "};\n"
    script += r"""
(async () => {
    const events = [];
    const flush = () => {
        let count = 0;
        while (events.length) {
            assert.ok(++count < 100, 'Media events must settle without feedback');
            events.shift()();
        }
    };
    function media(duration = 10) {
        return {duration, readyState: 4, paused: true, ended: false, dataset: {}, _time: 0,
            get currentTime() {return this._time;},
            set currentTime(time) {
                this._time = time;
                events.push(() => app.videoSeeking(this));
                events.push(() => app.videoTimeUpdate(this));
                events.push(() => app._mediaSeekTargets.delete(this));
            },
            play() {
                if (this.paused) {this.paused = false; events.push(() => app.videoPlaybackChanged(this));}
                return Promise.resolve();
            },
            pause() {
                if (!this.paused) {this.paused = true; events.push(() => app.videoPlaybackChanged(this));}
            }};
    }
    function graph() {
        return {rawData_: [[0], [2], [4], [6], [8], [9.9]], numRows() {return this.rawData_.length;},
            setSelection(row) {this.selection = row;}};
    }
    const main = media(), other = media(), late = media();
    late.readyState = 0;
    global.sessionStorage = {setItem() {}};
    Object.assign(app, {videos: [main, other, late], video: main, isZoomOpen: false,
        playbackPaused: true, playbackRate: 1, _playbackRevision: 0, _mediaSeekTargets: new WeakMap(),
        dygraphTime: 0, fps: 30, episodeLength: 300, nVideoReadyToPlay: 0, _autoplayStarted: false,
        dygraphArmJoints: graph(), dygraphGripperFlag: graph(),
        $refs: {slider: {}, timer: {}, btnPlay: {classList: {toggle() {}}},
            btnPause: {classList: {toggle() {}}}},
        updateTableValues(time) {this.tableTime = time;}, updateImages(frame) {this.imageFrame = frame;},
        _updateCurrentStage() {this.stageTime = this.dygraphTime;}});
    app.videoCanPlay({target: main}); flush();
    assert.equal(app.playbackPaused, false);
    assert.equal(other.paused, false);
    other._time = 2;
    other.pause(); flush();
    assert.equal(app.playbackPaused, true);
    assert.ok(app.videos.every(video => video.paused));
    assert.equal(main.currentTime, 2);
    other.currentTime = 6; flush();
    assert.equal(main.currentTime, 6);
    assert.equal(app.dygraphTime, 6);
    assert.equal(app.tableTime, 6);
    assert.equal(app.imageFrame, 180);
    assert.equal(app.dygraphArmJoints.selection, 3);
    assert.equal(app.dygraphGripperFlag.selection, 3);
    assert.ok(app.videos.every(video => video.paused), 'Seeking while paused must stay paused');
    late.readyState = 4;
    app.videoCanPlay({target: late}); flush();
    assert.equal(late.currentTime, 6);
    assert.equal(late.paused, true, 'Late canplay must not restart paused playback');
    other.play(); flush();
    assert.ok(app.videos.every(video => !video.paused));
    main._time = 7;
    app.videoTimeUpdate(main); flush();
    assert.equal(other.currentTime, 7, 'Correct drift using the shared clock');
    other._time = 7.02;
    app.videoTimeUpdate(other);
    assert.equal(app.dygraphTime, 7, 'Followers must not compete for the chart clock');
    main._time = 7.02;
    app.videoCanPlay({target: main}); flush();
    assert.equal(main.currentTime, 7.02, 'Repeated canplay after a seek must not rewind the clock');
    other.currentTime = 6; flush();
    assert.equal(main.currentTime, 6, 'A native seek back to a previous sync target must propagate');
    app.seekPlayback(7); flush();
    app.seekPlayback(NaN);
    assert.equal(app.dygraphTime, 7);
    app.seekPlayback(100); flush();
    assert.equal(app.dygraphTime, 10);
    app.seekPlayback(-1); flush();
    assert.equal(app.dygraphTime, 0);
    app.setPlaybackPaused(true); flush();
    app.setPlaybackPaused(false);
    app.setPlaybackPaused(true); flush();
    assert.ok(app.videos.every(video => video.paused), 'Queued events must preserve the latest intent');
    const zoom = media();
    app.$refs.zoomVideo = zoom; app.isZoomOpen = true;
    app.seekPlayback(4); flush();
    assert.equal(zoom.currentTime, 4);
    zoom.play(); flush();
    assert.ok(app.playbackVideos().every(video => !video.paused));
    zoom.currentTime = 8; flush();
    assert.equal(main.currentTime, 8);
    app.isZoomOpen = false;
    zoom.pause(); flush();
    assert.equal(app.playbackPaused, false, 'Closing zoom must not pause the camera grid');
    let rejectZoom;
    zoom.play = () => new Promise((resolve, reject) => {rejectZoom = reject;});
    app.isZoomOpen = true; app.playVideo(zoom);
    app.isZoomOpen = false; rejectZoom(new Error('Closed while loading'));
    await Promise.resolve(); flush();
    assert.equal(app.playbackPaused, false, 'An aborted closed zoom must not pause other cameras');
    main.ended = true; main.paused = true;
    app.videoPlaybackChanged(main);
    app.videoEnded(main); main.ended = false; flush();
    assert.equal(app.dygraphTime, 0, 'Loop all cameras together');
    assert.ok(app.videos.every(video => !video.paused));
    main.play = () => Promise.reject(new Error('play blocked'));
    app.setPlaybackPaused(false);
    await Promise.resolve(); flush();
    assert.equal(app.playbackPaused, true, 'Rejected playback must leave controls and media paused');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
