"""控制台页面（单文件 HTML，无构建步骤、无 CDN 依赖）。

为什么是单文件内联
------------------
演示环境要能离线跑。引 CDN 的图表库/字体，断网就白屏；上构建链又要 node。
所以整页一个字符串：内联 CSS + 原生 JS，曲线用手写 canvas 画。

布局：**画布铺满 + 浮动面板**（layout-patterns 第 4 套）
------------------------------------------------------
3D 画面铺满视口、是唯一视觉重心；遥测/控制/诊断做成半透明浮层贴四角，
距边 16px。面板数量靠内容决定，不为了填满而加。

配色往"更亮更饱和的紫"走（用户偏好），背景用径向渐变做层次，
不用纯黑——纯黑会把机器狗的深灰材质吞掉。
"""

CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Go2 仿真控制台</title>
<style>
:root{
  --bg:#0a0713;
  --pan:rgba(23,17,40,.80);
  --pan-2:rgba(32,24,54,.72);
  --line:rgba(168,132,255,.22);
  --line-hi:rgba(168,132,255,.46);
  --t1:#f4f0ff; --t2:#c6bbe4; --t3:#8d81ad;
  --violet:#a855f7; --violet-2:#7c3aed; --violet-glow:rgba(168,85,247,.45);
  --mint:#34d399; --amber:#fbbf24; --rose:#fb7185;
  --r:16px;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{
  font-family:var(--sans);color:var(--t1);
  background:
    radial-gradient(120% 90% at 76% 8%, rgba(139,92,246,.34), transparent 60%),
    radial-gradient(90% 80% at 10% 92%, rgba(45,212,191,.15), transparent 58%),
    radial-gradient(70% 60% at 50% 50%, rgba(124,58,237,.16), transparent 70%),
    var(--bg);
}
/* 画布层：3D 画面铺满 */
#stage{position:fixed;inset:0;display:grid;place-items:center;z-index:0}
#frame{max-width:100%;max-height:100%;object-fit:contain;
  filter:drop-shadow(0 30px 80px rgba(0,0,0,.6))}
#frame.hide{opacity:0}
/* 网格叠加，给背景质感 */
#stage::before{content:'';position:absolute;inset:0;pointer-events:none;
  background-image:
    linear-gradient(to right, rgba(168,132,255,.075) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(168,132,255,.075) 1px, transparent 1px);
  background-size:88px 88px;
  mask-image:radial-gradient(120% 100% at 50% 50%, #000 30%, transparent 78%)}

/* 浮动面板 */
.pan{position:fixed;z-index:2;
  background:var(--pan);backdrop-filter:blur(22px) saturate(150%);
  -webkit-backdrop-filter:blur(22px) saturate(150%);
  border:1px solid var(--line);border-radius:var(--r);
  box-shadow:0 22px 60px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.06);
  padding:18px 20px}
.pan .ttl{font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--t3);font-weight:700;margin-bottom:12px;display:flex;
  justify-content:space-between;align-items:center;gap:10px}
.pan .ttl .tag{font-size:9.5px;letter-spacing:.08em;padding:2px 7px;
  border-radius:99px;background:rgba(168,85,247,.16);color:#d8b4fe;
  border:1px solid rgba(168,85,247,.3)}
#tl{left:16px;top:16px;width:300px}
#tr{right:16px;top:16px;width:274px}
#bl{left:16px;bottom:16px;width:352px}
#br{right:16px;bottom:16px;width:390px;max-height:44vh;display:flex;
  flex-direction:column;min-height:0}

/* 大数字 */
.big{font-family:var(--mono);font-size:38px;line-height:1;font-weight:800;
  letter-spacing:-.035em;font-variant-numeric:tabular-nums;
  background:linear-gradient(135deg,#f4f0ff,#c084fc 60%,#a855f7);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.big small{font-size:14px;font-weight:600;letter-spacing:0;color:var(--t3);
  -webkit-text-fill-color:var(--t3);margin-left:5px}

/* 行 */
.row{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
  font-size:12.5px;line-height:1.5;color:var(--t2);padding:6px 0;
  border-top:1px solid rgba(168,132,255,.12)}
.row:first-of-type{border-top:0;padding-top:0}
.row b{color:var(--t1);font-weight:600;font-variant-numeric:tabular-nums;
  font-family:var(--mono);font-size:12.5px}
.row .warn{color:var(--amber)} .row .bad{color:var(--rose)}
.row .ok{color:var(--mint)}

/* 关节表 */
.jt{display:grid;grid-template-columns:1fr 54px 46px;gap:5px 10px;
  font-family:var(--mono);font-size:11.5px;max-height:190px;overflow:auto;
  padding-right:4px}
.jt .h{color:var(--t3);font-size:9.5px;letter-spacing:.1em;
  text-transform:uppercase;position:sticky;top:0;background:transparent}
.jt .n{color:var(--t2)}
.jt .v{text-align:right;font-variant-numeric:tabular-nums;color:var(--t1)}
.jt .v.hot{color:var(--amber)} .jt .v.crit{color:var(--rose)}
.jt .v.dead{color:var(--t3);text-decoration:line-through}

/* 按钮 */
.btns{display:flex;gap:7px;flex-wrap:wrap}
button{font-family:inherit;font-size:12px;font-weight:600;color:var(--t1);
  background:var(--pan-2);border:1px solid var(--line);border-radius:10px;
  padding:8px 13px;cursor:pointer;transition:all .16s}
button:hover{border-color:var(--line-hi);background:rgba(168,85,247,.18);
  transform:translateY(-1px)}
button:active{transform:translateY(0)}
button.on{background:linear-gradient(135deg,var(--violet-2),var(--violet));
  border-color:transparent;box-shadow:0 6px 20px var(--violet-glow)}
button.danger{border-color:rgba(251,113,133,.4);color:#fecdd3}
button.danger:hover{background:rgba(251,113,133,.18)}

/* 状态点 */
.dot{display:inline-block;width:7px;height:7px;border-radius:99px;
  margin-right:7px;background:var(--mint);box-shadow:0 0 10px var(--mint)}
.dot.w{background:var(--amber);box-shadow:0 0 10px var(--amber)}
.dot.b{background:var(--rose);box-shadow:0 0 10px var(--rose)}
.dot.idle{background:var(--t3);box-shadow:none}

/* 曲线 */
canvas.chart{width:100%;height:66px;display:block;margin-top:10px}

/* 候选列表 */
.cands{overflow:auto;min-height:0;flex:1;display:flex;flex-direction:column;
  gap:7px;margin:-2px -2px 0}
.cand{background:rgba(168,85,247,.09);border:1px solid rgba(168,85,247,.2);
  border-radius:10px;padding:9px 11px;font-size:11.5px;line-height:1.5}
.cand .top{display:flex;justify-content:space-between;gap:8px;
  align-items:baseline}
.cand .nm{font-weight:700;color:var(--t1);font-size:12px}
.cand .sev{font-size:9.5px;letter-spacing:.07em;text-transform:uppercase;
  padding:2px 7px;border-radius:99px;font-weight:700}
.sev.critical{background:rgba(251,113,133,.2);color:#fda4af}
.sev.warning{background:rgba(251,191,36,.18);color:#fcd34d}
.sev.info{background:rgba(52,211,153,.16);color:#6ee7b7}
.cand .msg{color:var(--t2);margin-top:3px}
.cand .tm{color:var(--t3);font-family:var(--mono);font-size:10px;margin-top:3px}
.empty{color:var(--t3);font-size:12px;padding:14px 0;text-align:center}

/* 故障选择 */
select{font-family:inherit;font-size:12px;color:var(--t1);
  background:rgba(23,17,40,.95);border:1px solid var(--line);
  border-radius:10px;padding:8px 10px;width:100%;cursor:pointer}
select:focus{outline:none;border-color:var(--line-hi)}
input[type=range]{width:100%;accent-color:var(--violet);cursor:pointer}

@media (max-width:1180px){
  #tl,#tr,#bl,#br{position:fixed;width:calc(50vw - 24px)}
  #tl{left:12px;top:12px} #tr{right:12px;top:12px}
  #bl{left:12px;bottom:12px} #br{right:12px;bottom:12px;max-height:40vh}
}
</style>
</head>
<body>
<div id="stage"><img id="frame" alt="Go2 3D 视图"></div>

<!-- 左上：运行状态 -->
<section class="pan" id="tl">
  <div class="ttl">运行状态 <span class="tag" id="tagSpeed">1x</span></div>
  <div style="display:flex;align-items:baseline;gap:14px">
    <div class="big" id="simT">0.0<small>s 仿真</small></div>
  </div>
  <div style="margin-top:14px">
    <div class="row"><span>姿态</span><b id="posture">—</b></div>
    <div class="row"><span>故障</span><b id="faultName">正常</b></div>
    <div class="row"><span>注入状态</span><b id="faultState">—</b></div>
    <div class="row"><span>实际倍率</span><b id="actualSpeed">—</b></div>
    <div class="row"><span>机身高度</span><b id="baseZ">—</b></div>
    <div class="row"><span>翻倒</span><b id="fell">—</b></div>
  </div>
</section>

<!-- 右上：温度与电流 -->
<section class="pan" id="tr">
  <div class="ttl">热与电流</div>
  <div style="display:flex;align-items:baseline;gap:10px">
    <div class="big" id="tempMax">25<small>°C</small></div>
  </div>
  <canvas class="chart" id="chartTemp"></canvas>
  <div class="row" style="margin-top:6px"><span>峰值电流</span>
    <b id="currMax">—</b></div>
  <div class="row"><span>温度保护线</span><b>90 °C</b></div>
  <div class="row"><span>告警线</span><b>75 °C</b></div>
</section>

<!-- 左下：控制 -->
<section class="pan" id="bl">
  <div class="ttl">控制</div>
  <div class="btns" style="margin-bottom:10px" id="speedBtns">
    <button data-speed="1">1x</button>
    <button data-speed="4">4x</button>
    <button data-speed="15">15x</button>
    <button id="pauseBtn">暂停</button>
  </div>
  <div class="ttl" style="margin-top:12px">运行中注入</div>
  <select id="faultSel"></select>
  <div style="display:flex;justify-content:space-between;font-size:11px;
       color:var(--t3);margin-top:9px">
    <span>严重度</span><b id="sevVal" style="color:var(--t2)">1.00</b>
  </div>
  <input type="range" id="sev" min="0.1" max="1.5" step="0.05" value="1">
  <div class="btns" style="margin-top:10px">
    <button class="danger" id="injectBtn">注入故障</button>
    <button id="clearBtn">恢复正常</button>
  </div>
  <div class="btns" style="margin-top:9px" id="postureBtns"></div>
</section>

<!-- 右下：12 关节 + 诊断 -->
<section class="pan" id="br">
  <div class="ttl">关节遥测
    <span class="tag" id="planeTag">数据面</span></div>
  <div class="jt" id="joints"></div>
  <div class="ttl" style="margin-top:14px">data_dog 诊断</div>
  <div class="cands" id="cands">
    <div class="empty">等待候选…</div>
  </div>
</section>

<script>
const $ = s => document.querySelector(s);
const fmt = (v, n=2) => (v==null||Number.isNaN(v)) ? '—' : Number(v).toFixed(n);

let state = {posture:'stand', speed:1, paused:false};

// ---------------------------------------------------------------- 故障库
let FAULTS = [];
async function loadFaults(){
  const r = await fetch('/api/faults');
  const d = await r.json();
  FAULTS = d.faults;
  const sel = $('#faultSel');
  // 按层分组：物理/热/观测/电气——现场的故障就是这样分科的
  const layers = {};
  for (const f of d.faults){
    if (f.key === 'normal') continue;
    (layers[f.layer] ||= []).push(f);
  }
  const label = {physics:'物理层', thermal:'热层',
                 observation:'观测层', electrical:'电气层'};
  sel.innerHTML = '';
  for (const [k, arr] of Object.entries(layers)){
    const g = document.createElement('optgroup');
    g.label = label[k] || k;
    for (const f of arr){
      const o = document.createElement('option');
      o.value = f.key;
      o.textContent = `${f.key} · ${f.label}`;
      o.title = f.story + '\n症状：' + f.symptom;
      g.appendChild(o);
    }
    sel.appendChild(g);
  }
  const pb = $('#postureBtns');
  pb.innerHTML = '';
  for (const p of d.postures){
    const b = document.createElement('button');
    b.textContent = p.label;
    b.dataset.posture = p.key;
    b.title = p.load_note;
    b.onclick = () => resetSim({posture:p.key});
    pb.appendChild(b);
  }
}

// ---------------------------------------------------------------- 控制
document.querySelectorAll('#speedBtns button[data-speed]').forEach(b => {
  b.onclick = async () => {
    await fetch('/api/control', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({speed: Number(b.dataset.speed)})});
  };
});
$('#pauseBtn').onclick = async () => {
  await fetch('/api/control', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({paused: !state.paused})});
};
$('#sev').oninput = e => $('#sevVal').textContent = fmt(e.target.value,2);
$('#injectBtn').onclick = async () => {
  const key = $('#faultSel').value;
  const sev = Number($('#sev').value);
  await fetch('/api/inject', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({key, severity: sev, start_s: 0})});
};
$('#clearBtn').onclick = () => injectRaw('normal');
async function injectRaw(key){
  await fetch('/api/inject', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({key})});
}
async function resetSim(body){
  await fetch('/api/reset', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)});
}

// ---------------------------------------------------------------- 渲染
function renderState(s){
  state = s;
  $('#simT').innerHTML = fmt(s.t,1) + '<small>s 仿真</small>';
  $('#tagSpeed').textContent = (s.paused ? '已暂停' : s.speed + 'x');
  $('#posture').textContent = s.posture_label || s.posture;
  const f = s.fault || {};
  $('#faultName').textContent = (f.key === 'normal') ? '正常' : `${f.label}`;
  const active = s.fault_active;
  const st = $('#faultState');
  if (f.key === 'normal'){ st.textContent = '—'; st.className = ''; }
  else if (active){ st.textContent = '注入中'; st.className = 'bad'; }
  else { st.textContent = '等待生效'; st.className = 'warn'; }
  $('#actualSpeed').textContent = s.actual_speed == null ? '—'
      : fmt(s.actual_speed,2) + 'x';
  $('#baseZ').textContent = fmt((s.base_pos||[])[2], 3) + ' m';
  const fell = $('#fell');
  fell.textContent = s.fell ? '是' : '否';
  fell.className = s.fell ? 'bad' : 'ok';
  $('#pauseBtn').textContent = s.paused ? '继续' : '暂停';
  $('#pauseBtn').classList.toggle('on', !!s.paused);
  document.querySelectorAll('#speedBtns button[data-speed]').forEach(b => {
    b.classList.toggle('on', !s.paused && Number(b.dataset.speed) === s.speed);
  });
  const dp = s.data_plane || {};
  $('#planeTag').textContent = dp.available
      ? `已接入 ${dp.normalized||0}` : '未接入';
  $('#planeTag').style.opacity = dp.available ? 1 : .45;
}

function renderTelemetry(d){
  const box = $('#joints');
  const rows = [];
  rows.push('<div class="h">关节</div><div class="h">温度</div><div class="h">电流</div>');
  for (const j of d.joints){
    const cls = !j.valid ? 'dead' : j.temp >= 90 ? 'crit'
              : j.temp >= 75 ? 'hot' : '';
    rows.push(`<div class="n">${j.name}</div>` +
              `<div class="v ${cls}">${fmt(j.temp,1)}°</div>` +
              `<div class="v">${fmt(j.current,1)}A</div>`);
  }
  box.innerHTML = rows.join('');
  const s = d.series || {};
  if (s.temp && s.temp.length){
    const mx = Math.max(...s.temp);
    $('#tempMax').innerHTML = fmt(mx,0) + '<small>°C</small>';
    drawChart($('#chartTemp'), s.temp, 25, 95);
    $('#currMax').textContent = fmt(Math.max(...(s.current||[0])),1) + ' A';
  }
}

// 手写曲线：不引图表库（离线可用）。区间固定 25~95°C，
// 这样"涨了多少"是**绝对**可比的，不会因为自动缩放而看不出变化。
function drawChart(cv, vals, lo, hi){
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (!w || !h) return;
  cv.width = w*dpr; cv.height = h*dpr;
  const c = cv.getContext('2d');
  c.setTransform(dpr,0,0,dpr,0,0);
  c.clearRect(0,0,w,h);
  // 90°C 保护线
  const yOf = v => h - ((v-lo)/(hi-lo))*h;
  c.strokeStyle = 'rgba(251,113,133,.34)'; c.lineWidth = 1;
  c.setLineDash([4,4]); c.beginPath();
  c.moveTo(0, yOf(90)); c.lineTo(w, yOf(90)); c.stroke();
  c.setLineDash([]);
  // 75°C 告警线
  c.strokeStyle = 'rgba(251,191,36,.28)'; c.setLineDash([3,5]);
  c.beginPath(); c.moveTo(0, yOf(75)); c.lineTo(w, yOf(75)); c.stroke();
  c.setLineDash([]);
  // 曲线
  const n = vals.length;
  const grad = c.createLinearGradient(0,0,0,h);
  grad.addColorStop(0,'rgba(168,85,247,.42)');
  grad.addColorStop(1,'rgba(168,85,247,0)');
  c.beginPath();
  vals.forEach((v,i) => {
    const x = n>1 ? (i/(n-1))*w : 0;
    const y = Math.max(0, Math.min(h, yOf(v)));
    i ? c.lineTo(x,y) : c.moveTo(x,y);
  });
  c.strokeStyle = '#c084fc'; c.lineWidth = 2; c.lineJoin='round';
  c.stroke();
  c.lineTo(w,h); c.lineTo(0,h); c.closePath();
  c.fillStyle = grad; c.fill();
}

function renderCandidates(d){
  const box = $('#cands');
  const list = d.candidates || [];
  if (!list.length){
    const dp = d.plane || {};
    box.innerHTML = `<div class="empty">${dp.available
      ? '暂无候选（阈值未触发）' : '数据面未接入：' + (dp.error||'')}</div>`;
    return;
  }
  box.innerHTML = list.map(c => {
    const sev = (c.severity_hint||c.severity||'info').toLowerCase();
    const name = c.component_ref || c.component || '—';
    const msgs = (c.evidence||[]).map(e => e.message||e.metric).filter(Boolean);
    const msg = msgs.slice(0,2).join('；') || (c.reason||'');
    const tm = (c.window_start || c.occurred_at || '').toString().slice(11,19);
    return `<div class="cand">
      <div class="top"><span class="nm">${name}</span>
        <span class="sev ${sev}">${sev}</span></div>
      ${msg ? `<div class="msg">${msg}</div>` : ''}
      <div class="tm">${tm} · hits=${c.sample_count||c.hit_count||'—'}</div>
    </div>`;
  }).join('');
}

// ---------------------------------------------------------------- 画面
let frameOn = true, frameBusy = false;
async function tickFrame(){
  if (frameBusy) return;
  frameBusy = true;
  try{
    const r = await fetch('/api/frame.jpg?width=880&height=550', {cache:'no-store'});
    if (r.ok){
      const b = await r.blob();
      const img = $('#frame');
      img.src = URL.createObjectURL(b);
      img.classList.remove('hide');
      frameOn = true;
    } else if (r.status === 503){
      // 渲染不可用（无 GL）：隐藏画面，遥控/遥测照旧——
      // 不能让"看不到 3D"连带把整个演示搞停。
      $('#frame').classList.add('hide');
      frameOn = false;
    }
  }catch(e){ frameOn = false; }
  finally{ frameBusy = false; }
}

// ---------------------------------------------------------------- 轮询
async function tickState(){
  try{ renderState(await (await fetch('/api/state')).json()); }
  catch(e){}
}
async function tickTelemetry(){
  try{ renderTelemetry(await (await fetch('/api/telemetry?n=200')).json()); }
  catch(e){}
}
async function tickCandidates(){
  try{ renderCandidates(await (await fetch('/api/candidates')).json()); }
  catch(e){}
}

// 顶层的 await 只在这里用一次（普通 <script> 不支持它，改成 async IIFE）。
(async () => {
  await loadFaults();
  tickState(); tickTelemetry(); tickCandidates(); tickFrame();
  setInterval(tickState, 400);
  setInterval(tickTelemetry, 250);
  setInterval(tickCandidates, 900);
  // 画面 5fps：足够看出姿态与故障，又不至于把 CPU 吃满影响仿真本身。
  setInterval(tickFrame, 200);
})();
</script>
</body>
</html>
"""
