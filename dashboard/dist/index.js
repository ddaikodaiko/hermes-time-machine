(function () {
  "use strict";
  // hermes-time-machine dashboard tab: the point of no return, visible.
  // Read-only. Plain ES5-ish JS on the host's React (window.__HERMES_PLUGIN_SDK__), no build step.
  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useCallback = SDK.hooks.useCallback;
  var C = SDK.components || {};
  var API = "/api/plugins/hermes-time-machine";
  var NAME = "hermes-time-machine";

  function get(path) {
    return SDK.fetchJSON(API + path);
  }

  function parseTs(value) {
    var t = Date.parse(value || "");
    return isNaN(t) ? null : t;
  }

  function clock(value) {
    var t = parseTs(value);
    if (t === null) return "";
    var d = new Date(t);
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function day(value) {
    var t = parseTs(value);
    return t === null ? "" : new Date(t).toLocaleDateString();
  }

  function kindLabel(kind) {
    var map = {
      git_push: "git push", github_pr: "Pull request", github_issue: "Issue", github_merge: "Merge",
      github_release: "Release", github_commit: "Direct commit", github_branch_delete: "Branch delete",
      npm_publish: "npm publish", cargo_publish: "cargo publish", pypi_publish: "PyPI upload",
      gem_push: "gem push", docker_push: "docker push", kubectl_write: "kubectl", terraform_apply: "terraform apply",
      message_send: "Message sent", http_write: "HTTP write", database_write: "Database write", email: "Email",
      remote_shell: "Remote shell", outside_workspace_write: "Write outside workspace", tool_call: "Tool call",
      workspace_delete: "rm -rf", git_reset: "git reset --hard", git_clean: "git clean"
    };
    return map[kind] || String(kind || "effect").replace(/_/g, " ");
  }

  function shortWs(path) {
    var parts = String(path || "").split(/[\\/]/).filter(Boolean);
    return parts.slice(-2).join("/") || String(path || "");
  }

  function Badge(props) {
    return h("span", { className: "atm-badge atm-badge-" + (props.tone || "muted"), title: props.title }, props.children);
  }

  function CopyButton(props) {
    var s = useState(false);
    var done = s[0], setDone = s[1];
    return h("button", {
      type: "button",
      className: "atm-copy",
      onClick: function () {
        try {
          navigator.clipboard.writeText(props.text).then(function () {
            setDone(true);
            setTimeout(function () { setDone(false); }, 1400);
          });
        } catch (e) { /* clipboard blocked: the command stays visible */ }
      }
    }, done ? "Copied" : "Copy undo");
  }

  // ------------------------------------------------------------------ timeline

  function Timeline(props) {
    var cps = props.checkpoints || [];
    var effects = (props.effects || []).filter(function (e) { return e.permanent || e.status === "failed"; });
    var times = [];
    cps.forEach(function (c) { var t = parseTs(c.created_at); if (t !== null) times.push(t); });
    effects.forEach(function (e) { var t = parseTs(e.ts); if (t !== null) times.push(t); });
    if (!times.length) {
      return h("div", { className: "atm-empty" },
        "No checkpoints or effects yet. They appear here as soon as the agent edits files or crosses a point of no return.");
    }
    var W = 1000, L = 150, R = 40;
    // Ordinal scale: events are evenly spaced in time order, so a burst of activity after an
    // hours-long gap stays readable. Equal timestamps share one slot.
    var uniq = times.slice().sort(function (a, b) { return a - b; }).filter(function (t, i, a) { return i === 0 || t !== a[i - 1]; });
    var x = function (t) {
      if (uniq.length === 1) return (L + W - R) / 2;
      var i = uniq.indexOf(t);
      return L + (i / (uniq.length - 1)) * (W - L - R);
    };
    var yWs = 52, yOut = 140;
    var selected = props.selected;
    var selCp = cps.filter(function (c) { return c.id === selected; })[0];
    var selX = selCp ? x(parseTs(selCp.created_at)) : null;
    var selT = selCp ? parseTs(selCp.created_at) : null;

    var children = [
      h("text", { key: "l1", x: 0, y: yWs - 4, className: "atm-lane-title" }, "Workspace"),
      h("text", { key: "l1b", x: 0, y: yWs + 13, className: "atm-lane-sub" }, "a rewind undoes this"),
      h("text", { key: "l2", x: 0, y: yOut - 4, className: "atm-lane-title" }, "Outside world"),
      h("text", { key: "l2b", x: 0, y: yOut + 13, className: "atm-lane-sub" }, "nothing undoes this"),
      h("line", { key: "a1", x1: L - 10, y1: yWs, x2: W - 10, y2: yWs, className: "atm-axis" }),
      h("line", { key: "a2", x1: L - 10, y1: yOut, x2: W - 10, y2: yOut, className: "atm-axis atm-axis-out" })
    ];
    if (selX !== null) {
      children.push(h("rect", { key: "shade", x: selX, y: yWs - 24, width: W - 10 - selX, height: 48, className: "atm-shade" }));
      children.push(h("text", { key: "shade-t", x: Math.min(selX + 8, W - 260), y: yWs - 30, className: "atm-lane-sub" },
        "reverted if you restore " + clock(selCp.created_at)));
    }
    effects.forEach(function (e, i) {
      var t = parseTs(e.ts);
      if (t === null) return;
      var ex = x(t);
      var stays = selT !== null && t >= selT && e.permanent;
      var failed = e.status === "failed";
      var labelY = yOut + 30 + (i % 2) * 14;
      children.push(h("g", {
        key: "e" + i,
        className: "atm-effect" + (failed ? " atm-failed" : "") + (props.focus === i ? " atm-focus" : ""),
        onClick: function () { props.onFocusEffect(i); },
        style: { cursor: "pointer" }
      },
        h("title", null, kindLabel(e.kind) + (e.target ? " → " + e.target : "") + " · " + clock(e.ts) + (failed ? " (failed)" : "")),
        e.permanent ? h("line", { x1: ex, y1: yWs + 8, x2: ex, y2: yOut - 10, className: "atm-cross" }) : null,
        h("rect", { x: ex - 8, y: yOut - 8, width: 16, height: 16, rx: 2, transform: "rotate(45 " + ex + " " + yOut + ")" }),
        h("text", { x: ex, y: labelY, textAnchor: "middle", className: "atm-effect-label" }, kindLabel(e.kind)),
        stays ? h("text", { x: ex, y: labelY + 28 - (i % 2) * 14, textAnchor: "middle", className: "atm-stays" }, "stays true") : null
      ));
    });
    cps.forEach(function (c) {
      var t = parseTs(c.created_at);
      if (t === null) return;
      var cx = x(t);
      var isSel = c.id === selected;
      var after = selT !== null && t > selT;
      children.push(h("g", {
        key: c.id,
        className: "atm-cp" + (isSel ? " atm-cp-sel" : "") + (after ? " atm-cp-after" : "") + (c.auto ? "" : " atm-cp-manual"),
        onClick: function () { props.onSelect(c.id); },
        style: { cursor: "pointer" }
      },
        h("title", null, (c.label || c.id) + " · " + clock(c.created_at) + (c.files != null ? " · " + c.files + " files" : "")),
        h("circle", { cx: cx, cy: yWs, r: isSel ? 10 : 7 }),
        isSel || cps.length <= 12 ? h("text", { x: cx, y: yWs + 28, textAnchor: "middle", className: isSel ? "atm-cp-label" : "atm-cp-time" }, clock(c.created_at).slice(0, 5)) : null
      ));
    });
    return h("svg", { viewBox: "0 0 " + W + " 200", className: "atm-timeline", role: "img", "aria-label": "Checkpoints and permanent effects over time" }, children);
  }

  // ------------------------------------------------------------------ panels

  function DiffView(props) {
    var s = useState(null);
    var data = s[0], setData = s[1];
    useEffect(function () {
      var live = true;
      setData(null);
      get("/diff/" + encodeURIComponent(props.checkpoint) + "?path=" + encodeURIComponent(props.path))
        .then(function (d) { if (live) setData(d); })
        .catch(function (e) { if (live) setData({ error: String(e && e.message || e) }); });
      return function () { live = false; };
    }, [props.checkpoint, props.path]);
    if (!data) return h("div", { className: "atm-muted" }, "Loading diff…");
    if (data.error) return h("div", { className: "atm-error" }, data.error);
    if (data.note) return h("div", { className: "atm-muted" }, data.note);
    if (!data.diff) return h("div", { className: "atm-muted" }, "No text differences.");
    return h("pre", { className: "atm-diff" }, data.diff.split("\n").map(function (line, i) {
      var cls = line.indexOf("+++") === 0 || line.indexOf("---") === 0 ? "atm-d-file"
        : line[0] === "+" ? "atm-d-add" : line[0] === "-" ? "atm-d-del" : line.indexOf("@@") === 0 ? "atm-d-hunk" : "";
      return h("div", { key: i, className: cls }, line || " ");
    }), data.truncated ? h("div", { className: "atm-muted" }, "… truncated") : null);
  }

  function CheckpointPanel(props) {
    var s = useState(null);
    var data = s[0], setData = s[1];
    var f = useState(null);
    var file = f[0], setFile = f[1];
    useEffect(function () {
      setData(null);
      setFile(null);
    }, [props.id]);
    useEffect(function () {
      // Refresh in place: keep what is on screen (and the open diff) until new data arrives.
      var live = true;
      get("/checkpoint/" + encodeURIComponent(props.id))
        .then(function (d) { if (live) setData(d); })
        .catch(function (e) { if (live) setData({ error: String(e && e.message || e) }); });
      return function () { live = false; };
    }, [props.id, props.tick]);
    if (!data) return h("div", { className: "atm-card atm-muted" }, "Loading checkpoint…");
    if (data.error) return h("div", { className: "atm-card atm-error" }, data.error);
    var cp = data.checkpoint || {};
    var counts = data.counts || {};
    var files = data.files || {};
    var rows = [];
    [["modified", "reverted"], ["deleted", "brought back"], ["added", "moved to trash"]].forEach(function (pair) {
      (files[pair[0]] || []).forEach(function (p) { rows.push({ path: p, what: pair[1], kind: pair[0] }); });
    });
    var stays = data.stays_true || [];
    return h("div", { className: "atm-grid2" },
      h("div", { className: "atm-card" },
        h("div", { className: "atm-card-head" },
          h("div", null, h("strong", null, cp.label || cp.id), " ", h("span", { className: "atm-muted" }, day(cp.created_at) + " " + clock(cp.created_at))),
          h(Badge, { tone: cp.auto ? "muted" : "accent" }, cp.auto ? "auto" : "manual")),
        h("div", { className: "atm-muted atm-small" }, cp.id + (cp.trigger_type ? " · before " + (cp.tool || cp.trigger_type) : "")),
        data.files_error ? h("div", { className: "atm-error" }, data.files_error) :
          h("div", null,
            h("div", { className: "atm-counts" },
              h(Badge, { tone: "warn" }, (counts.modified || 0) + " reverted"),
              h(Badge, { tone: "ok" }, (counts.deleted || 0) + " brought back"),
              h(Badge, { tone: "muted" }, (counts.added || 0) + " to trash"),
              h(Badge, { tone: "muted" }, (counts.unchanged || 0) + " unchanged")),
            rows.length ? h("ul", { className: "atm-files" }, rows.slice(0, 60).map(function (r) {
              return h("li", { key: r.kind + r.path, className: file === r.path ? "atm-file-sel" : "", onClick: function () { setFile(r.path); } },
                h("span", { className: "atm-mono" }, r.path), h("span", { className: "atm-muted atm-small" }, r.what));
            })) : h("div", { className: "atm-muted" }, "The workspace matches this checkpoint."),
            file ? h(DiffView, { checkpoint: cp.id, path: file }) : (rows.length ? h("div", { className: "atm-muted atm-small" }, "Click a file to see its diff.") : null)),
        h("div", { className: "atm-hint" }, "Restore from chat with ", h("code", null, "/timemachine rewind " + cp.id), ". This page never changes anything.")),
      h("div", { className: "atm-card atm-card-danger" },
        h("div", { className: "atm-card-head" }, h("strong", null, "What this restore cannot undo"), h(Badge, { tone: stays.length ? "danger" : "ok" }, stays.length + " effect" + (stays.length === 1 ? "" : "s"))),
        stays.length ? h("ul", { className: "atm-stays-list" }, stays.map(function (e, i) {
          return h("li", { key: i },
            h("div", null, h("strong", null, kindLabel(e.kind)), e.target ? h("span", { className: "atm-mono" }, " → " + e.target) : null),
            e.compensation ? h("div", { className: "atm-cmd-row" }, h("code", { className: "atm-cmd" }, e.compensation), h(CopyButton, { text: e.compensation }))
              : h("div", { className: "atm-muted atm-small" }, e.compensation_note || "No automatic undo."));
        })) : h("div", { className: "atm-muted" }, "Nothing irreversible happened after this checkpoint. A restore brings everything back."))
    );
  }

  function EffectCard(props) {
    var e = props.effect;
    var tone = e.status === "failed" ? "muted" : e.permanent ? "danger" : "ok";
    return h("div", { className: "atm-card atm-effect-card" + (props.focus ? " atm-effect-focus" : ""), id: "atm-effect-" + props.index },
      h("div", { className: "atm-card-head" },
        h("strong", null, kindLabel(e.kind)),
        h("div", { className: "atm-badges" },
          e.via === "tool" ? h(Badge, { tone: "accent", title: e.tool }, "via MCP / tool") : null,
          e.reminded ? h(Badge, { tone: "warn", title: "The model lost sight of this effect (rollback, undo or compaction) and the plugin reminded it." }, "model reminded") : null,
          h(Badge, { tone: tone }, e.status === "failed" ? "failed" : e.permanent ? "permanent" : "local"))),
      h("div", { className: "atm-muted atm-small" }, (e.target ? e.target + " · " : "") + day(e.ts) + " " + clock(e.ts)),
      e.reason ? h("div", { className: "atm-small" }, e.reason) : null,
      e.compensation ? h("div", { className: "atm-cmd-row" }, h("code", { className: "atm-cmd" }, e.compensation), h(CopyButton, { text: e.compensation }))
        : (e.compensation_note ? h("div", { className: "atm-note" }, e.compensation_note) : null));
  }

  // ------------------------------------------------------------------ page

  function useOverview(workspace) {
    var s = useState(null);
    var data = s[0], setData = s[1];
    var t = useState(0);
    var tick = t[0], setTick = t[1];
    var load = useCallback(function () {
      get("/overview" + (workspace ? "?workspace=" + encodeURIComponent(workspace) : ""))
        .then(function (d) { setData(d); setTick(function (n) { return n + 1; }); })
        .catch(function (e) { setData({ error: String(e && e.message || e) }); });
    }, [workspace]);
    useEffect(function () {
      load();
      var id = setInterval(load, 10000);
      return function () { clearInterval(id); };
    }, [load]);
    return [data, tick];
  }

  function TimeMachinePage() {
    var w = useState(null);
    var workspace = w[0], setWorkspace = w[1];
    var ov = useOverview(workspace);
    var data = ov[0], tick = ov[1];
    var s = useState(function () {
      try { return new URLSearchParams(window.location.search).get("checkpoint"); } catch (e) { return null; }
    });
    var selected = s[0], setSelected = s[1];
    var f = useState(null);
    var focus = f[0], setFocus = f[1];
    var filt = useState("permanent");
    var filter = filt[0], setFilter = filt[1];

    useEffect(function () {
      if (!data || !data.checkpoints || !data.checkpoints.length) return;
      var ids = data.checkpoints.map(function (c) { return c.id; });
      if (!selected || ids.indexOf(selected) < 0) {
        // Default: the newest checkpoint that has a permanent effect after it, else the newest one.
        var pick = null;
        for (var i = data.checkpoints.length - 1; i >= 0 && !pick; i--) {
          var t = parseTs(data.checkpoints[i].created_at);
          var after = (data.effects || []).some(function (e) { return e.permanent && parseTs(e.ts) >= t; });
          if (after) pick = data.checkpoints[i].id;
        }
        setSelected(pick || ids[ids.length - 1]);
      }
    }, [data]);

    if (!data) return h("div", { className: "atm-page atm-muted" }, "Loading the time machine…");
    if (data.error) return h("div", { className: "atm-page atm-error" }, data.error);
    var sum = data.summary || {};
    var effects = data.effects || [];
    var shown = effects.map(function (e, i) { return { e: e, i: i }; }).filter(function (x) {
      return filter === "all" || (filter === "permanent" ? x.e.permanent : x.e.status === "failed");
    }).reverse();

    return h("div", { className: "atm-page" },
      h("div", { className: "atm-hero" },
        h("div", null,
          h("div", { className: "atm-eyebrow" }, "hermes-time-machine"),
          h("h2", { className: "atm-title" }, "The rollback that knows what it can't undo"),
          h("div", { className: "atm-muted" }, "Checkpoints a rewind can restore, next to the effects nothing can restore.")),
        h("div", { className: "atm-hero-side" },
          (data.workspaces || []).length > 1 ? h("select", {
            className: "atm-select", value: data.workspace || "",
            onChange: function (ev) { setWorkspace(ev.target.value); setSelected(null); }
          }, data.workspaces.map(function (ws) { return h("option", { key: ws, value: ws }, shortWs(ws)); }))
            : h("span", { className: "atm-mono atm-small", title: data.workspace }, shortWs(data.workspace) || "no workspace yet"),
          h("div", { className: "atm-badges" },
            h(Badge, { tone: "muted" }, (sum.checkpoints || 0) + " checkpoints"),
            h(Badge, { tone: sum.permanent ? "danger" : "ok" }, (sum.permanent || 0) + " permanent"),
            sum.reminded ? h(Badge, { tone: "warn" }, sum.reminded + " reminded to the model") : null))),
      h("div", { className: "atm-card" },
        h(Timeline, {
          checkpoints: data.checkpoints, effects: effects, selected: selected, focus: focus,
          onSelect: setSelected,
          onFocusEffect: function (i) {
            setFocus(i);
            if (filter === "permanent" && !effects[i].permanent) setFilter("all");
            setTimeout(function () {
              var el = document.getElementById("atm-effect-" + i);
              if (el && el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "center" });
            }, 50);
          }
        }),
        h("div", { className: "atm-legend" },
          h("span", null, h("i", { className: "atm-dot" }), " checkpoint (click to preview a restore)"),
          h("span", null, h("i", { className: "atm-diamond" }), " permanent effect"),
          h("span", null, h("i", { className: "atm-diamond atm-diamond-failed" }), " failed attempt"))),
      selected ? h(CheckpointPanel, { id: selected, tick: tick }) : null,
      h("div", { className: "atm-section-head" },
        h("h3", null, "Effect ledger"),
        h("div", { className: "atm-filters" }, ["permanent", "failed", "all"].map(function (k) {
          return h("button", { key: k, type: "button", className: "atm-filter" + (filter === k ? " atm-filter-on" : ""), onClick: function () { setFilter(k); } }, k);
        }))),
      shown.length ? h("div", { className: "atm-effects" }, shown.map(function (x) {
        return h(EffectCard, { key: x.i, effect: x.e, index: x.i, focus: focus === x.i });
      })) : h("div", { className: "atm-empty" }, filter === "permanent"
        ? "No permanent effects recorded. When the agent pushes, publishes, merges or sends, it shows up here with its undo."
        : "Nothing to show.")
    );
  }

  function SessionsStrip() {
    var ov = useOverview(null);
    var data = ov[0];
    if (!data || data.error || !data.summary || !data.summary.permanent) return null;
    var sum = data.summary;
    return h("a", { className: "atm-strip", href: "/time-machine" },
      h("span", { className: "atm-strip-dot" }),
      h("span", null, sum.permanent + " permanent effect" + (sum.permanent === 1 ? "" : "s") + " in " + shortWs(data.workspace)
        + (sum.reminded ? " · " + sum.reminded + " reminded to the model" : "") + " — a rollback won't undo them"),
      h("span", { className: "atm-strip-link" }, "Open time machine →"));
  }

  window.__HERMES_PLUGINS__.register(NAME, TimeMachinePage);
  if (window.__HERMES_PLUGINS__.registerSlot) {
    window.__HERMES_PLUGINS__.registerSlot(NAME, "sessions:top", SessionsStrip);
  }
})();
