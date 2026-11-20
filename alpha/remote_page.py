"""The single page served by `alpha.remote` (kept as a string so it ships
with the package without package-data configuration).

Static: it carries no session data. The token arrives in the URL fragment
and is sent as a Bearer header on every `/api/*` call. All conversation
text is inserted escaped — the small Markdown renderer escapes first and
only then adds its own tags.
"""

PAGE = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>Mythos · remoto</title>
<style>
:root{
  --bg:#0e0c15; --panel:#17141f; --panel2:#1f1b2b; --line:#2c2740;
  --text:#e9e6f2; --dim:#9a94ad; --faint:#6b6580;
  --violet:#4f7bff; --violet-soft:#87a7ff; --amber:#f5b556;
  --green:#4ade80; --red:#f87171;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
}
@media (prefers-color-scheme: light){
  :root{
    --bg:#f7f6fb; --panel:#ffffff; --panel2:#efecf8; --line:#ddd8ec;
    --text:#1c1929; --dim:#5f5975; --faint:#8a849e;
    --violet:#2f5fe0; --violet-soft:#3b6cf0; --amber:#a86a00;
    --green:#15803d; --red:#c0262d;
  }
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);
  font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  display:flex;flex-direction:column;-webkit-text-size-adjust:100%}
header{flex:none;display:flex;align-items:center;gap:10px;
  padding:calc(10px + env(safe-area-inset-top)) 16px 10px;
  border-bottom:1px solid var(--line);background:var(--panel)}
.brand{font-weight:700;letter-spacing:.2px;white-space:nowrap}
.brand b{color:var(--violet-soft)}
.meta{flex:1;min-width:0;color:var(--dim);font-size:12.5px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dot{flex:none;width:9px;height:9px;border-radius:50%;background:var(--faint)}
.dot.on{background:var(--green)}
.dot.busy{background:var(--amber);animation:pulse 1.2s ease-in-out infinite}
.dot.off{background:var(--red)}
@keyframes pulse{50%{opacity:.35}}
main{flex:1;overflow-y:auto;padding:14px 16px 8px;overscroll-behavior:contain}
.wrap{max-width:820px;margin:0 auto}
.row{margin:0 0 12px}
.user{background:var(--panel2);border-left:3px solid var(--violet);
  padding:8px 12px;border-radius:6px;white-space:pre-wrap;overflow-wrap:anywhere}
.user small{color:var(--faint);margin-left:6px}
.queued{opacity:.6;border-left-style:dashed}
.asst{overflow-wrap:anywhere}
.asst p{margin:0 0 9px}
.asst h1,.asst h2,.asst h3{margin:12px 0 6px;font-size:1.05em}
.asst ul,.asst ol{margin:0 0 9px;padding-left:22px}
.asst code,.tool,.out,pre{font-family:var(--mono);font-size:13.5px}
.asst code{background:var(--panel2);padding:1px 5px;border-radius:4px}
pre{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:10px 12px;margin:0 0 9px;overflow-x:auto;white-space:pre}
.asst pre code,pre code{background:none;padding:0}
.tablebox{overflow-x:auto;margin:0 0 9px}
table{border-collapse:collapse;font-size:14px}
th,td{border:1px solid var(--line);padding:4px 9px;text-align:left;vertical-align:top}
th{background:var(--panel2)}
.live{white-space:pre-wrap;color:var(--dim);overflow-wrap:anywhere}
.tool{color:var(--text);overflow-wrap:anywhere}
.tool::before{content:"● ";color:var(--violet-soft)}
.res{color:var(--dim);white-space:pre-wrap;overflow-wrap:anywhere;
  margin:2px 0 0 18px;padding-left:10px;border-left:2px solid var(--line);
  font-family:var(--mono);font-size:13px;max-height:15em;overflow-y:auto}
.res.bad{color:var(--red)}
.note{color:var(--faint);font-size:13px;text-align:center}
.err{color:var(--red);white-space:pre-wrap;overflow-wrap:anywhere}
.out{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:10px 12px;white-space:pre;overflow-x:auto;color:var(--dim)}
#ask{flex:none;display:none;border-top:1px solid var(--amber);
  background:var(--panel);padding:12px 16px;max-height:60vh;overflow-y:auto}
#ask.show{display:block}
#ask h2{margin:0 0 6px;font-size:15px;color:var(--amber)}
#ask pre{max-height:26vh;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}
.opts{display:flex;flex-direction:column;gap:8px}
button{font:inherit;color:inherit;border:1px solid var(--line);background:var(--panel2);
  border-radius:10px;padding:11px 14px;min-height:44px;text-align:left;cursor:pointer}
button:active{filter:brightness(1.25)}
button:disabled{opacity:.5;cursor:default}
.opts button b{color:var(--violet-soft);margin-right:8px}
footer{flex:none;border-top:1px solid var(--line);background:var(--panel);
  padding:10px 16px calc(10px + env(safe-area-inset-bottom))}
.bar{max-width:820px;margin:0 auto;display:flex;gap:8px;align-items:flex-end}
textarea{flex:1;min-width:0;resize:none;font:inherit;color:var(--text);
  background:var(--bg);border:1px solid var(--line);border-radius:10px;
  padding:10px 12px;height:46px;min-height:46px;max-height:40vh}
textarea:focus{outline:2px solid var(--violet);outline-offset:-1px}
#send{background:var(--violet);border-color:var(--violet);color:#fff;
  text-align:center;font-weight:600;flex:none}
#stop{display:none;border-color:var(--red);color:var(--red);text-align:center;flex:none}
body.busy #stop{display:block}
#hint{max-width:820px;margin:6px auto 0;color:var(--faint);font-size:12.5px;min-height:1.2em}
#gate{margin:18vh auto 0;max-width:420px;text-align:center;color:var(--dim);padding:0 16px}
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <span class="brand"><b>μ</b> Mythos</span>
  <span class="meta" id="meta">conectando…</span>
</header>
<main id="main"><div class="wrap" id="log"></div></main>
<section id="ask"><div class="wrap">
  <h2 id="askTitle"></h2>
  <pre id="askDetail"></pre>
  <div class="opts" id="askOpts"></div>
</div></section>
<footer>
  <div class="bar">
    <textarea id="box" rows="1" placeholder="Mensagem…" autocomplete="off"></textarea>
    <button id="stop" title="Interromper o turno">Parar</button>
    <button id="send">Enviar</button>
  </div>
  <div id="hint"></div>
</footer>
<script>
(function(){
"use strict";
var $ = function(id){ return document.getElementById(id); };
var log = $("log"), main = $("main"), box = $("box"), hint = $("hint");
var token = (location.hash || "").slice(1);
try {
  if (token) sessionStorage.setItem("alpha_remote_token", token);
  else token = sessionStorage.getItem("alpha_remote_token") || "";
} catch (e) {}
var lastSeq = 0, live = null, question = null, stopped = false;

function el(tag, cls, text){
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}
function nearBottom(){ return main.scrollHeight - main.scrollTop - main.clientHeight < 140; }
function add(node){
  var stick = nearBottom();
  node.classList.add("row");
  log.appendChild(node);
  if (stick) main.scrollTop = main.scrollHeight;
  return node;
}
function esc(s){
  return s.replace(/[&<>"']/g, function(ch){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch];
  });
}
function inline(s){
  s = esc(s);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  return s;
}
function cells(line){
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|");
}
function md(src){
  var out = [], parts = src.split(/```/);
  for (var p = 0; p < parts.length; p++){
    if (p % 2){
      var code = parts[p].replace(/^[^\n]*\n/, "");
      out.push("<pre><code>" + esc(code.replace(/\n$/, "")) + "</code></pre>");
      continue;
    }
    var lines = parts[p].split("\n"), para = [], list = null, i = 0;
    var flushP = function(){ if (para.length){ out.push("<p>" + para.join("<br>") + "</p>"); para = []; } };
    var flushL = function(){ if (list){ out.push("</" + list + ">"); list = null; } };
    while (i < lines.length){
      var ln = lines[i], m;
      if (/^\s*\|.*\|\s*$/.test(ln)){
        flushP(); flushL();
        var rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])){ rows.push(lines[i]); i++; }
        var html = "<div class='tablebox'><table>", head = true;
        for (var r = 0; r < rows.length; r++){
          if (/^[\s|:-]+$/.test(rows[r])) { head = false; continue; }
          var tag = (head && r === 0 && rows.length > 1 && /^[\s|:-]+$/.test(rows[1])) ? "th" : "td";
          html += "<tr>" + cells(rows[r]).map(function(c){
            return "<" + tag + ">" + inline(c.trim()) + "</" + tag + ">"; }).join("") + "</tr>";
        }
        out.push(html + "</table></div>");
        continue;
      }
      if ((m = /^(#{1,4})\s+(.*)$/.exec(ln))){
        flushP(); flushL();
        var lvl = Math.min(3, m[1].length);
        out.push("<h" + lvl + ">" + inline(m[2]) + "</h" + lvl + ">");
      } else if ((m = /^\s*[-*•]\s+(.*)$/.exec(ln))){
        flushP(); if (list !== "ul"){ flushL(); out.push("<ul>"); list = "ul"; }
        out.push("<li>" + inline(m[1]) + "</li>");
      } else if ((m = /^\s*\d+[.)]\s+(.*)$/.exec(ln))){
        flushP(); if (list !== "ol"){ flushL(); out.push("<ol>"); list = "ol"; }
        out.push("<li>" + inline(m[1]) + "</li>");
      } else if (!ln.trim()){
        flushP(); flushL();
      } else {
        flushL(); para.push(inline(ln));
      }
      i++;
    }
    flushP(); flushL();
  }
  return out.join("");
}

function setDot(state){ $("dot").className = "dot " + state; }
function setBusy(b){
  document.body.classList.toggle("busy", !!b);
  setDot(b ? "busy" : "on");
}
function dropLive(){ if (live){ live.remove(); live = null; } }
function dropQueued(text){
  var q = log.querySelectorAll(".queued");
  for (var i = 0; i < q.length; i++){
    if (text == null || q[i].dataset.text === text){ q[i].remove(); if (text != null) return; }
  }
}
function showQuestion(q){
  question = q;
  var askBox = $("ask");
  if (!q){ askBox.classList.remove("show"); return; }
  $("askTitle").textContent = (q.kind === "approval" ? "Permissão: " : "") + q.title;
  $("askDetail").textContent = q.detail || "";
  $("askDetail").style.display = q.detail ? "" : "none";
  var opts = $("askOpts");
  opts.textContent = "";
  q.options.forEach(function(label, i){
    var b = el("button");
    b.appendChild(el("b", null, (i + 1) + "."));
    b.appendChild(document.createTextNode(label));
    b.onclick = function(){
      Array.prototype.forEach.call(opts.children, function(x){ x.disabled = true; });
      api("answer", {id: q.id, index: i}).then(function(r){
        if (!r || !r.ok) say("Essa pergunta já foi respondida.");
      });
    };
    opts.appendChild(b);
  });
  askBox.classList.add("show");
  main.scrollTop = main.scrollHeight;
}
function say(text){ hint.textContent = text || ""; }

function handle(ev){
  if (ev.seq) lastSeq = ev.seq;
  switch (ev.type){
    case "hello":
      if (ev.reset){ log.textContent = ""; lastSeq = 0; live = null; }
      var info = ev.info || {};
      $("meta").textContent = [info.model, info.cwd].filter(Boolean).join(" · ") || "conectado";
      setBusy(ev.busy);
      showQuestion(ev.question);
      dropQueued(null);
      (ev.queued || []).forEach(function(t){ queuedRow(t); });
      say("");
      break;
    case "user":
      dropQueued(ev.text);
      var u = el("div", "user", ev.text);
      var tag = {remote: "remoto", command: "comando", history: ""}[ev.source];
      if (tag) u.appendChild(el("small", null, tag));
      add(u);
      break;
    case "queued": queuedRow(ev.text); break;
    case "queue_cleared": dropQueued(null); break;
    case "delta":
      if (!live) live = add(el("div", "live", ""));
      var stick = nearBottom();
      live.textContent += ev.text;
      if (stick) main.scrollTop = main.scrollHeight;
      break;
    case "assistant":
      dropLive();
      var a = el("div", "asst");
      a.innerHTML = md(ev.text);
      add(a);
      break;
    case "tool_call":
      dropLive();
      var t = el("div");
      t.dataset.call = ev.id;
      t.appendChild(el("div", "tool", ev.header));
      add(t);
      break;
    case "tool_result":
      var rows = log.querySelectorAll("[data-call]"), target = null;
      for (var i = rows.length - 1; i >= 0; i--){
        if (rows[i].dataset.call === ev.id && !rows[i].dataset.done){ target = rows[i]; break; }
      }
      if (!target){ target = add(el("div")); }
      target.dataset.done = "1";
      if (ev.text){
        var stick2 = nearBottom();
        target.appendChild(el("div", "res" + (ev.ok ? "" : " bad"), ev.text));
        if (stick2) main.scrollTop = main.scrollHeight;
      }
      break;
    case "question": showQuestion(ev); break;
    case "question_closed":
      if (question && question.id === ev.id) showQuestion(null);
      if (ev.outcome) add(el("div", "note", ev.outcome + (ev.source === "remote" ? " · pelo remoto" : " · no terminal")));
      break;
    case "status": setBusy(ev.busy); break;
    case "turn_end":
      dropLive();
      add(el("div", "note", (ev.aborted ? "interrompido · " : "") + ev.tools + (ev.tools === 1 ? " tool · " : " tools · ") + ev.duration + "s"));
      break;
    case "output": add(el("div", "out", ev.text)); break;
    case "error": add(el("div", "err", ev.text)); break;
    case "note": add(el("div", "note", ev.text)); break;
    case "clear": log.textContent = ""; live = null; break;
    case "bye":
      stopped = true; setDot("off"); showQuestion(null);
      $("meta").textContent = "remoto desligado no terminal";
      break;
  }
}
function queuedRow(text){
  var qd = el("div", "user queued", text);
  qd.dataset.text = text;
  qd.appendChild(el("small", null, "na fila"));
  add(qd);
}

function api(path, body){
  return fetch("/api/" + path, {
    method: "POST",
    headers: {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    body: JSON.stringify(body || {})
  }).then(function(r){
    return r.json().then(function(j){
      if (!r.ok) say(j.error || ("erro " + r.status));
      return r.ok ? j : null;
    });
  }).catch(function(){ say("Sem conexão com o terminal."); return null; });
}

function connect(){
  if (stopped) return;
  fetch("/api/events?since=" + lastSeq, {headers: {"Authorization": "Bearer " + token}})
  .then(function(r){
    if (r.status === 401){
      stopped = true; setDot("off");
      $("meta").textContent = "link inválido ou expirado — abra o link novo do terminal";
      return;
    }
    if (!r.ok || !r.body) throw new Error("http " + r.status);
    var reader = r.body.getReader(), dec = new TextDecoder(), buf = "";
    var pump = function(){
      return reader.read().then(function(chunk){
        if (chunk.done) throw new Error("closed");
        buf += dec.decode(chunk.value, {stream: true});
        var idx;
        while ((idx = buf.indexOf("\n\n")) >= 0){
          var block = buf.slice(0, idx); buf = buf.slice(idx + 2);
          if (block.indexOf("data: ") === 0){
            try { handle(JSON.parse(block.slice(6))); } catch (e) {}
          }
        }
        return pump();
      });
    };
    return pump();
  })
  .catch(function(){
    if (stopped) return;
    setDot("off");
    $("meta").textContent = "reconectando…";
    setTimeout(connect, 2000);
  });
}

function send(){
  var text = box.value.trim();
  if (!text) return;
  $("send").disabled = true;
  api("message", {text: text}).then(function(r){
    $("send").disabled = false;
    if (r){ box.value = ""; grow(); say(""); }
  });
}
function grow(){ box.style.height = "auto"; box.style.height = Math.min(box.scrollHeight + 2, innerHeight * 0.4) + "px"; }
box.addEventListener("input", grow);
box.addEventListener("keydown", function(e){
  // Enter sends on a hardware keyboard; on touch keyboards it adds a line.
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(pointer: fine)").matches){
    e.preventDefault(); send();
  }
});
$("send").onclick = send;
$("stop").onclick = function(){ api("interrupt"); };

if (!token){
  log.appendChild(el("div", null, "Este link veio sem o token. Abra o link completo mostrado no terminal (/remote)."));
  log.firstChild.id = "gate";
  setDot("off"); $("meta").textContent = "sem token";
} else {
  connect();
}
})();
</script>
</body>
</html>
"""
