"""控制台页面（单文件 HTML，无构建步骤、无 CDN 依赖）。

为什么是单文件内联
------------------
演示环境要能离线跑。引 CDN 的图表库/字体，断网就白屏；上构建链又要 node。
所以整页一个字符串：内联 CSS + 原生 JS，曲线用手写 canvas 画。

布局：**画布铺满 + 浮动面板**（layout-patterns 第 4 套）
------------------------------------------------------
3D 画面铺满视口、是唯一视觉重心；遥测/控制/诊断做成半透明浮层贴四角，
距边 20px。面板数量靠内容决定，不为了填满而加。

视觉：Apple 浅色系统组件
------------------------
背景 #F5F5F7（Apple 分组背景），面板白 78% + 30px backdrop-blur，发丝级
边框 rgba(0,0,0,.06)，圆角 20px。字体走 SF Pro / 苹方。

**彩色只用在"当前值 / 选中态 / 告警"三处**，大面积一律中性灰。
这是 Apple 系统设置与「家庭」App 的做法：颜色是信息，不是装饰。
强调色用系统蓝 #0A84FF，语义色 #30D158 绿 / #FF9F0A 橙 / #FF453A 红。

3D 场景也要跟着变亮：深色星空顶 + 深色棋盘地在浅灰页面上就是一块黑洞。
`rig.apply_light_stage()` 把贴图刷成浅色展示台，见那边的说明。
"""

CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Go2 仿真控制台</title>
<style>
:root{
  /* Apple 系统色 */
  --bg:#F5F5F7;
  --pan:rgba(255,255,255,.78);
  --pan-solid:#FFFFFF;
  --pan-fill:rgba(120,120,128,.08);
  --pan-fill-2:rgba(120,120,128,.14);
  --hair:rgba(0,0,0,.06);
  --hair-2:rgba(0,0,0,.12);
  --t1:#1D1D1F; --t2:#6E6E73; --t3:#8E8E93;
  --blue:#0A84FF; --green:#30D158; --amber:#FF9F0A; --red:#FF453A;
  --blue-soft:rgba(10,132,255,.12);
  --green-soft:rgba(48,209,88,.14);
  --amber-soft:rgba(255,159,10,.14);
  --red-soft:rgba(255,69,58,.12);
  --r:20px; --r-sm:12px;
  --shadow:0 1px 2px rgba(0,0,0,.04), 0 8px 28px rgba(0,0,0,.07);
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",
         "PingFang SC","Microsoft YaHei",system-ui,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{
  font-family:var(--sans);color:var(--t1);background:var(--bg);
  -webkit-font-smoothing:antialiased;
}

/* 画布层：3D 画面铺满。浅色场景与页面同族，用一个柔和的底部渐变
   把画面接到面板上，避免"贴图边界"那种生硬的切口。 */
/* 这里原来写成 display:grid;place-items:center，隐式行高是 auto，
   #frame 的 height:100% 解析成 auto，图片就按 1100×760 的固有比例撑到
   995px 高、把 cover 撑失效，画面底部被顶出视口。改成绝对定位铺满。 */
#stage{position:fixed;inset:0;z-index:0;overflow:hidden;
  background:linear-gradient(180deg,#FAFAFC 0%,#F5F5F7 55%,#EDEDF0 100%)}
#frame{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
  object-position:50% 46%;
  transition:opacity .3s ease}
#frame.hide{opacity:0}
/* 上下各压一层极淡的渐隐，画面像"印在纸上"，不是贴上去的 */
#stage::after{content:'';position:absolute;inset:0;pointer-events:none;
  background:
    linear-gradient(180deg, rgba(245,245,247,.55) 0%, transparent 16%,
                    transparent 84%, rgba(245,245,247,.65) 100%)}

/* 浮动面板：Apple 的分组卡片 */
/* 面板自己滚动，而不是让 flex 去挤压子项。子项被压缩时 max-height 形同虚设，
   .jt 会缩到 52px 只剩两行——"整块面板可滚"是矮屏唯一不丢内容的做法。 */
.pan{position:fixed;z-index:2;overflow:auto;overscroll-behavior:contain;
  background:var(--pan);
  backdrop-filter:blur(30px) saturate(180%);
  -webkit-backdrop-filter:blur(30px) saturate(180%);
  border:.5px solid var(--hair);border-radius:var(--r);
  box-shadow:var(--shadow);padding:16px 18px 14px}
.pan .ttl{display:flex;justify-content:space-between;align-items:center;
  gap:10px;font-size:11px;font-weight:600;letter-spacing:.02em;
  color:var(--t3);text-transform:none;margin-bottom:10px}

/* 状态胶囊 */
.tag{display:inline-flex;align-items:center;gap:5px;font-size:11px;
  font-weight:600;padding:3px 9px;border-radius:99px;
  background:var(--pan-fill);color:var(--t2);white-space:nowrap}
.tag.ok{background:var(--green-soft);color:#1E7B37}
.tag.warn{background:var(--amber-soft);color:#95560A}
.tag.bad{background:var(--red-soft);color:#B3261E}
.tag.live{background:var(--blue-soft);color:#0060C0}
.tag .dot{width:6px;height:6px;border-radius:99px;background:currentColor;
  flex:none}

#tl{left:20px;top:20px;width:296px}
#tr{right:20px;top:20px;width:268px}
/* 左列上下两块都 fixed，矮屏上会互相顶：900×600 时 #tl 底边 317、
   #bl 顶边 291，重叠 26px，#bl 在后面把 #tl 最后一行盖掉。
   给 #bl 留出"别再往上涨"的地板，超出的部分由 .pan 的 overflow:auto 滚。

   算式要留意 max-height 与"顶边"差一个下边距：设 max-height:100vh-C，
   则顶边 = 视口高 - 下边距 - 高 = C - 下边距。要求
   C - 20 >= 20 + #tl 高 + 间距 12  →  C >= 52 + #tl 高。
   #tl 实测高 309（宽屏，20→329），故取 365 留 4px 余量。改 #tl 内容或
   .big 字号要同步这里。 */
#bl{left:20px;bottom:20px;width:348px;
  max-height:calc(100vh - 365px)}
/* 46vh 在 900px 高的屏上只给 414px，装不下"12 行关节 + 链路 + 诊断"，
   后腿整段被截。放到 62vh 并给关节区弹性，矮屏仍由 .jt 的 38vh 兜底。

   **但 62vh 在 720p 上会顶穿上面的 #tr**：720×62%=446px，底部贴边 20px，
   顶边就落到 254px，而 #tr（top:20 + 内容 263px）底边在 283px，右下直接
   压掉右上最后一行"保护线 90°C"。四角面板都是 position:fixed、z-index 同为
   2，DOM 里 #br 在后，于是它盖住 #tr——这不是设计意图，是纯算错了。

   同 #bl 那套算法：顶边 = C - 20，要求 C - 20 >= 20 + #tr 高 + 间距 12。
   #tr 实测 263（20→283），C >= 315，取 320 留 5px 余量。
   CSS 取不到兄弟元素高度，只能按实测值留死，改 #tr 内容时要同步。 */
#br{right:20px;bottom:20px;width:392px;display:flex;
  max-height:min(62vh, calc(100vh - 320px));
  flex-direction:column;min-height:0}

/* 大数字：Apple 的"数值先行"。用 -apple-system 的圆润字重，
   不用渐变文字（那是炫技，不是系统）。 */
.big{font-size:42px;line-height:1.05;font-weight:600;letter-spacing:-.03em;
  font-variant-numeric:tabular-nums}
.big small{font-size:15px;font-weight:500;color:var(--t3);
  letter-spacing:0;margin-left:4px}
.big .unit{font-size:20px;font-weight:500;color:var(--t2)}

/* 行：Apple 设置里的"标签 —— 值" */
.row{display:flex;justify-content:space-between;align-items:baseline;
  gap:12px;font-size:13px;color:var(--t2);padding:7px 0}
.row+.row{border-top:.5px solid var(--hair)}
.row b{font-weight:600;color:var(--t1);font-variant-numeric:tabular-nums;
  font-family:var(--mono);font-size:12.5px}
.row b.warn{color:#B36B00} .row b.bad{color:var(--red)}
.row b.ok{color:#1E7B37} .row b.mut{color:var(--t3);font-weight:500}

/* 关节表：12 个关节 × 3 列 + 表头 = 13 行 ≈ 264px。原来卡 168px 只露 6 行，
   后腿整段要滚才看得见——这一屏的全部意义就是"一眼看完 12 个关节"。
   矮屏（<820px 高）自动退回滚动，不跟画面抢地方。 */
/* flex:none——面板是 column flex，默认 flex-shrink:1 会把 .jt 从 255 压到
   198（1024×768 下只剩 9 行）。关节表是这块的固定主体，不该参与收缩；
   需要让位的是下面那个候选列表（它本来就 flex:1 且自带滚动）。 */
.jt{display:grid;grid-template-columns:1fr 52px 52px;gap:4px 10px;flex:none;
  font-family:var(--mono);font-size:11.5px;max-height:264px;overflow:auto;
  padding-right:4px;scrollbar-width:thin}
@media (max-height:820px){
  .jt{max-height:min(264px, 38vh)}
}
.jt .h{position:sticky;top:0;z-index:1;background:transparent;
  color:var(--t3);font-size:10px;font-weight:600;letter-spacing:.04em;
  padding-bottom:3px}
.jt .n{color:var(--t2);font-family:var(--sans);font-size:12px}
.jt .v{text-align:right;font-variant-numeric:tabular-nums;color:var(--t1)}
.jt .v.hot{color:#B36B00;font-weight:600}
.jt .v.crit{color:var(--red);font-weight:600}
.jt .v.dead{color:var(--t3);text-decoration:line-through}

/* 按钮：Apple 的圆角填充按钮 + 分段控件 */
.btns{display:flex;gap:6px;flex-wrap:wrap}
button{font-family:inherit;font-size:13px;font-weight:500;color:var(--t1);
  background:var(--pan-fill);border:none;border-radius:9px;
  padding:7px 14px;cursor:pointer;
  transition:background .18s ease, transform .12s ease}
button:hover{background:var(--pan-fill-2)}
button:active{transform:scale(.97)}
button:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
button.on{background:var(--blue);color:#fff;font-weight:600}
button.danger{background:var(--red-soft);color:var(--red);font-weight:600}
button.danger:hover{background:rgba(255,69,58,.2)}

/* 分段控件（倍率）——Apple 的 UISegmentedControl */
.seg{display:flex;background:var(--pan-fill);border-radius:9px;padding:2px;
  gap:2px}
.seg button{flex:1;background:transparent;border-radius:7px;padding:6px 0;
  font-size:12.5px;font-weight:500;color:var(--t2)}
.seg button.on{background:var(--pan-solid);color:var(--t1);font-weight:600;
  box-shadow:0 1px 3px rgba(0,0,0,.12)}

/* 曲线 */
canvas.chart{width:100%;height:60px;display:block;margin:8px 0 2px}

/* 候选 */
.cands{overflow:auto;min-height:0;flex:1;display:flex;flex-direction:column;
  gap:8px;margin-top:2px}
.cand{background:var(--pan-solid);border:.5px solid var(--hair);
  border-radius:var(--r-sm);padding:10px 12px}
.cand.critical{border-color:rgba(255,69,58,.28);
  background:linear-gradient(180deg,rgba(255,69,58,.045),var(--pan-solid))}
.cand.warning{border-color:rgba(255,159,10,.3);
  background:linear-gradient(180deg,rgba(255,159,10,.05),var(--pan-solid))}
.cand .top{display:flex;justify-content:space-between;gap:8px;
  align-items:baseline}
.cand .nm{font-weight:600;font-size:12.5px;color:var(--t1)}
.cand .sev{font-size:10px;font-weight:600;padding:2px 8px;border-radius:99px}
.sev.critical{background:var(--red-soft);color:#B3261E}
.sev.warning{background:var(--amber-soft);color:#95560A}
.sev.info{background:var(--blue-soft);color:#0060C0}
.cand .msg{color:var(--t2);font-size:11.5px;line-height:1.5;margin-top:4px}
.cand .tm{color:var(--t3);font-family:var(--mono);font-size:10.5px;
  margin-top:4px}
.empty{color:var(--t3);font-size:12.5px;padding:18px 0;text-align:center}

/* 表单控件 */
select{font-family:inherit;font-size:13px;color:var(--t1);
  background:var(--pan-solid);border:.5px solid var(--hair-2);
  border-radius:9px;padding:8px 10px;width:100%;cursor:pointer;
  appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='7' viewBox='0 0 10 7'%3E%3Cpath d='M1 1.5L5 5.5L9 1.5' stroke='%238E8E93' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 11px center}
select:focus{outline:none;border-color:var(--blue)}
input[type=range]{width:100%;accent-color:var(--blue);cursor:pointer;
  margin:6px 0 2px}
.lbl{display:flex;justify-content:space-between;align-items:baseline;
  font-size:12px;color:var(--t3);margin-top:10px}
.lbl b{color:var(--t1);font-family:var(--mono);font-size:12px;font-weight:600}

/* 健康条：连接/遥测用一条紧凑的状态列表，不占大数字的地方 */
.hl{display:flex;flex-direction:column;gap:0}
.hl .row{padding:6px 0;font-size:12.5px}

@media (max-width:1180px){
  #tl,#tr,#bl,#br{width:calc(50vw - 30px)}
  #tl{left:16px;top:16px;width:calc(50vw - 24px)}
  #tr{right:16px;top:16px;width:calc(50vw - 24px)}
  /* 与宽屏同一条：矮屏左列上下两块会顶，让 #bl 滚动而不是盖住 #tl。
     同式：顶边 = C - 16，要求 C - 16 >= 16 + #tl 高 + 间距 13。
     窄屏 #tl 实测 301（16→317），故 C = 16+301+13+16 = 346，取 350。 */
  #bl{left:16px;bottom:16px;width:calc(50vw - 24px);
    max-height:calc(100vh - 350px)}
  /* 这里原来写的是 max-height:38vh。1024×768 下 38vh=291px，而这块内容要
     446px（12 行关节就占 255），flex 挤压把关节表压到 52px、候选列表压到
     11px——两处都只剩一两行。窄屏时该按"上方 #tr 之下的剩余空间"取高，
     不按视口比例。320 与宽屏那条同义：上边距 16 + #tr 高约 268 + 间距 12
     + 下边距 16（窄屏字号更小，#tr 也矮一点，这里取宽屏实测值偏保守）。 */
  #br{right:16px;bottom:16px;width:calc(50vw - 24px);
    max-height:calc(100vh - 320px);
    /* 窄屏空间不足，面板整体滚动（.pan 已设 overflow:auto）。
       子项一律不许收缩，否则 flex 会先压扁关节表、再压没候选列表。 */
    overflow:auto}
  #br > *{flex:none}
  .big{font-size:34px}
}
</style>
</head>
<body>
<div id="stage"><img id="frame" alt="Go2 3D 视图"></div>

<!-- 左上：运行状态 -->
<section class="pan" id="tl">
  <div class="ttl">运行状态 <span class="tag" id="tagSpeed">1x</span></div>
  <div class="big"><span id="simT">0.0</span><small>s 仿真</small></div>
  <div style="margin-top:12px">
    <div class="row"><span>姿态</span><b id="posture">—</b></div>
    <div class="row"><span>故障</span><b id="faultName" class="mut">正常</b></div>
    <div class="row"><span>注入状态</span><b id="faultState" class="mut">—</b></div>
    <div class="row"><span>实际倍率</span><b id="actualSpeed">—</b></div>
    <div class="row"><span>机身高度</span><b id="baseZ">—</b></div>
    <div class="row"><span>翻倒</span><b id="fell">—</b></div>
  </div>
</section>

<!-- 右上：热与电流 -->
<section class="pan" id="tr">
  <div class="ttl">热与电流 <span class="tag" id="tempTag">正常</span></div>
  <div class="big"><span id="tempMax">25</span><span class="unit">°C</span></div>
  <canvas class="chart" id="chartTemp"></canvas>
  <div class="hl">
    <div class="row"><span>峰值电流</span><b id="currMax">—</b></div>
    <div class="row"><span>告警线</span><b class="mut">75 °C</b></div>
    <div class="row"><span>保护线</span><b class="mut">90 °C</b></div>
  </div>
</section>

<!-- 左下：控制 -->
<section class="pan" id="bl">
  <div class="ttl">控制</div>
  <div class="seg" id="speedSeg">
    <button data-speed="1">1x</button>
    <button data-speed="4">4x</button>
    <button data-speed="15">15x</button>
    <button id="pauseBtn">暂停</button>
  </div>
  <div class="ttl" style="margin-top:14px">运行中注入</div>
  <select id="faultSel"></select>
  <div class="lbl"><span>严重度</span><b id="sevVal">1.00</b></div>
  <input type="range" id="sev" min="0.1" max="1.5" step="0.05" value="1">
  <div class="btns" style="margin-top:8px">
    <button class="danger" id="injectBtn">注入故障</button>
    <button id="clearBtn">恢复正常</button>
  </div>
  <div class="btns" style="margin-top:8px" id="postureBtns"></div>
</section>

<!-- 右下：12 关节 + 链路 + 诊断 -->
<section class="pan" id="br">
  <div class="ttl">关节遥测 <span class="tag" id="planeTag">数据面</span></div>
  <div class="jt" id="joints"></div>

  <div class="ttl" style="margin-top:14px">链路</div>
  <div class="hl">
    <div class="row"><span>MQTT</span><b id="mqttState">—</b></div>
    <div class="row"><span>已发布 / 失败</span><b id="mqttCount">—</b></div>
    <div class="row"><span>投递失败 / 积压丢弃</span><b id="dropCount">—</b></div>
  </div>

  <div class="ttl" style="margin-top:14px">data_dog 诊断</div>
  <div class="cands" id="cands">
    <div class="empty">等待候选…</div>
  </div>
</section>

<script>
const $ = s => document.querySelector(s);
const fmt = (v, n=2) => (v==null||Number.isNaN(v)) ? '—' : Number(v).toFixed(n);
const num = v => (v==null) ? '—' : Number(v).toLocaleString('en-US');

let state = {posture:'stand', speed:1, paused:false};

// ---------------------------------------------------------------- 故障库
let FAULTS = [];
async function loadFaults(){
  const r = await fetch('/api/faults');
  const d = await r.json();
  FAULTS = d.faults;
  const sel = $('#faultSel');
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
      // 只显示人话名。`F1_stall` 这类键是给协议用的，摆在选单里
      // 等于逼值班员先做一次人机翻译——它降到 title，要的时候能看见。
      o.textContent = f.label;
      o.title = `${f.key}\n${f.story}\n症状：${f.symptom}`;
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
document.querySelectorAll('#speedSeg button[data-speed]').forEach(b => {
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
  await fetch('/api/inject', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({key: $('#faultSel').value,
                          severity: Number($('#sev').value), start_s: 0})});
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
  $('#simT').textContent = fmt(s.t, 1);
  $('#tagSpeed').textContent = s.paused ? '已暂停' : s.speed + 'x';
  $('#tagSpeed').className = 'tag' + (s.paused ? ' warn' : ' live');
  $('#posture').textContent = s.posture_label || s.posture;

  const f = s.fault || {};
  const fn = $('#faultName');
  fn.textContent = (f.key === 'normal') ? '正常' : f.label;
  fn.className = (f.key === 'normal') ? 'mut' : 'bad';

  const st = $('#faultState');
  if (f.key === 'normal'){ st.textContent = '—'; st.className = 'mut'; }
  else if (s.fault_active){ st.textContent = '注入中'; st.className = 'bad'; }
  else { st.textContent = '等待生效'; st.className = 'warn'; }

  // 实际倍率：如实显示。请求 15x 只跑到 6x 就写 6x——
  // 这是本页最不该美化的一个数（见 session.snapshot 的说明）。
  const as = $('#actualSpeed');
  as.textContent = s.actual_speed == null ? '—' : fmt(s.actual_speed, 2) + 'x';
  as.className = (s.actual_speed==null) ? 'mut'
      : (s.actual_speed < s.speed * 0.6 ? 'warn' : 'ok');

  $('#baseZ').textContent = fmt((s.base_pos||[])[2], 3) + ' m';
  const fell = $('#fell');
  fell.textContent = s.fell ? '是' : '否';
  fell.className = s.fell ? 'bad' : 'ok';

  $('#pauseBtn').textContent = s.paused ? '继续' : '暂停';
  $('#pauseBtn').classList.toggle('on', !!s.paused);
  document.querySelectorAll('#speedSeg button[data-speed]').forEach(b => {
    b.classList.toggle('on', !s.paused && Number(b.dataset.speed) === s.speed);
  });

  // 数据面：被拒的条数必须显示。限流/解析失败会让"候选变少"
  // 看起来像"没有故障"——那是演示里最容易讲错的一句。
  const dp = s.data_plane || {};
  const rejected = dp.rejected || 0;
  const pt = $('#planeTag');
  pt.textContent = dp.available
      ? `${num(dp.normalized)} 条` + (rejected ? ` · 拒 ${num(rejected)}` : '')
      : '未接入';
  pt.className = 'tag' + (dp.available ? (rejected ? ' warn' : ' ok') : ' bad');
  pt.title = dp.available
      ? `已归一化 ${num(dp.normalized)}，解析拒绝 ${num(rejected)}`
      : (dp.error || '数据面未接入');

  // 链路：MQTT 与丢弃计数
  const mq = s.mqtt || {};
  const ms = $('#mqttState');
  if (!mq.enabled){
    ms.textContent = '未启用';
    ms.className = 'mut';
    ms.title = mq.last_error || '未配置 --mqtt-host';
  } else if (mq.connected){
    ms.textContent = `已连接 · ${mq.topic || ''}`;
    ms.className = 'ok';
    ms.title = `${mq.host}:${mq.port} QoS ${mq.qos}`;
  } else {
    ms.textContent = '连接中…';
    ms.className = 'warn';
    ms.title = mq.last_error || `${mq.host}:${mq.port}`;
  }
  $('#mqttCount').textContent = mq.enabled
      ? `${num(mq.published)} / ${num(mq.failed)}` : '—';
  $('#mqttCount').className = (mq.failed > 0) ? 'bad' : '';

  // 两个丢弃来源分开显示：合成一个数时，1 亿的积压和 3 次投递异常
  // 长得一模一样，没人解释得清"那些帧去哪了"。
  const stats = s.stats || {};
  const pf = stats.publish_failed || 0, bd = stats.backlog_dropped || 0;
  const dc = $('#dropCount');
  dc.textContent = `${num(pf)} / ${num(bd)}`;
  dc.className = pf > 0 ? 'bad' : (bd > 0 ? 'warn' : 'ok');
  dc.title = stats.drop_reason || '投递失败：下游抛异常；积压丢弃：追不上墙钟被放弃的仿真步';
}

function renderTelemetry(d){
  const box = $('#joints');
  const rows = ['<div class="h">关节</div><div class="h">温度</div>'
              + '<div class="h">电流</div>'];
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
    $('#tempMax').textContent = fmt(mx, 0);
    const tt = $('#tempTag');
    tt.textContent = mx >= 90 ? '超保护线' : mx >= 75 ? '接近告警'
                   : '正常';
    tt.className = 'tag' + (mx >= 90 ? ' bad' : mx >= 75 ? ' warn' : ' ok');
    drawChart($('#chartTemp'), s.temp, 25, 95);
    $('#currMax').textContent = fmt(Math.max(...(s.current||[0])), 1) + ' A';
  }
}

// 手写曲线：不引图表库（离线可用）。
// 区间固定 25~95°C，这样"涨了多少"是**绝对**可比的，不因自动缩放失真。
function drawChart(cv, vals, lo, hi){
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (!w || !h) return;
  cv.width = w*dpr; cv.height = h*dpr;
  const c = cv.getContext('2d');
  c.setTransform(dpr,0,0,dpr,0,0);
  c.clearRect(0,0,w,h);
  const yOf = v => h - ((v-lo)/(hi-lo))*h;

  // 75 / 90°C 参考线
  [[75,'rgba(255,159,10,.35)'],[90,'rgba(255,69,58,.38)']].forEach(([v,col])=>{
    c.strokeStyle = col; c.lineWidth = 1; c.setLineDash([4,4]);
    c.beginPath(); c.moveTo(0, yOf(v)); c.lineTo(w, yOf(v)); c.stroke();
  });
  c.setLineDash([]);

  const n = vals.length;
  const last = vals[n-1];
  const color = last >= 90 ? '#FF453A' : last >= 75 ? '#FF9F0A' : '#0A84FF';

  // 面积
  const grad = c.createLinearGradient(0,0,0,h);
  grad.addColorStop(0, color + '38');
  grad.addColorStop(1, color + '00');
  c.beginPath();
  vals.forEach((v,i) => {
    const x = n>1 ? (i/(n-1))*w : 0;
    const y = Math.max(0, Math.min(h, yOf(v)));
    i ? c.lineTo(x,y) : c.moveTo(x,y);
  });
  c.lineTo(w,h); c.lineTo(0,h); c.closePath();
  c.fillStyle = grad; c.fill();

  // 线
  c.beginPath();
  vals.forEach((v,i) => {
    const x = n>1 ? (i/(n-1))*w : 0;
    const y = Math.max(0, Math.min(h, yOf(v)));
    i ? c.lineTo(x,y) : c.moveTo(x,y);
  });
  c.strokeStyle = color; c.lineWidth = 1.8;
  c.lineJoin = 'round'; c.lineCap = 'round'; c.stroke();

  // 末端圆点：强调"当前值"
  if (n){
    c.beginPath();
    c.arc(w-1.5, Math.max(2, Math.min(h-2, yOf(last))), 2.6, 0, 7);
    c.fillStyle = color; c.fill();
  }
}

function renderCandidates(d){
  const box = $('#cands');
  const list = d.candidates || [];
  if (!list.length){
    const dp = d.plane || {};
    box.innerHTML = `<div class="empty">${dp.available
      ? '暂无候选（阈值未触发）' : '数据面未接入'}</div>`;
    return;
  }
  box.innerHTML = list.map(c => {
    const sev = (c.severity_hint||c.severity||'info').toLowerCase();
    // 人话名优先。`names` 由服务端按 semantic.py 填好；
    // 老服务端没有这个字段时退回原始 ref，**不能显示成空白**。
    const n = c.names || {};
    const name = n.component || c.component_ref || c.component || '—';
    const rules = (n.rules && n.rules.length) ? n.rules.join('、') : '';
    const msgs = (c.evidence||[]).map(e => e.message||e.metric).filter(Boolean);
    const msg = msgs.slice(0,2).join('；') || (c.reason||'');
    const tm = (c.window_start || c.occurred_at || '').toString().slice(11,19);
    // title 里留原始标识符 —— 工单和 grep 还要用，只是不该占着头条位置。
    const raw = [c.component_ref, (c.rule_matches||[]).join(',')].filter(Boolean).join(' · ');
    return `<div class="cand ${sev}" title="${raw}">
      <div class="top"><span class="nm">${name}</span>
        <span class="sev ${sev}">${sev}</span></div>
      ${rules ? `<div class="msg">${rules}</div>` : ''}
      ${msg ? `<div class="msg">${msg}</div>` : ''}
      <div class="tm">${tm} · hits=${c.sample_count||c.hit_count||'—'}</div>
    </div>`;
  }).join('');
}

// ---------------------------------------------------------------- 画面
// 按视口要尺寸：固定 1100×760 在 1920 宽的屏上被放大 1.75 倍，狗会发虚。
// 上限 1280×800 是 rig.py 的离屏 framebuffer（OFFSCREEN_W/H），再要也没用。
let frameBusy = false;
const MAX_W = 1280, MAX_H = 800;   // = rig.py 的 OFFSCREEN_W/H
function frameSize(){
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  // 等比缩到上限以内：两个方向独立截断会把 16:9 拧成 16:10，
  // cover 再多裁一刀，狗的取景就变了。
  let w = innerWidth * dpr, h = innerHeight * dpr;
  if (w > MAX_W) { h = h * MAX_W / w; w = MAX_W; }
  if (h > MAX_H) { w = w * MAX_H / h; h = MAX_H; }
  w = Math.max(64, Math.round(w / 10) * 10);
  h = Math.max(48, Math.round(h / 10) * 10);
  return `width=${w}&height=${h}`;
}
async function tickFrame(){
  if (frameBusy) return;
  frameBusy = true;
  try{
    const r = await fetch('/api/frame.jpg?' + frameSize(), {cache:'no-store'});
    if (r.ok){
      const img = $('#frame');
      const url = URL.createObjectURL(await r.blob());
      const old = img.dataset.url;
      img.src = url;
      img.dataset.url = url;
      if (old) URL.revokeObjectURL(old);
      img.classList.remove('hide');
    } else if (r.status === 503){
      // 渲染不可用（无 GL）：只隐藏画面，遥控/遥测照旧——
      // 不能让"看不到 3D"连带把整个演示搞停。
      $('#frame').classList.add('hide');
    }
  }catch(e){}
  finally{ frameBusy = false; }
}

// ---------------------------------------------------------------- 轮询
async function tickState(){
  try{ renderState(await (await fetch('/api/state')).json()); }catch(e){}
}
async function tickTelemetry(){
  try{ renderTelemetry(await (await fetch('/api/telemetry?n=200')).json()); }
  catch(e){}
}
async function tickCandidates(){
  try{ renderCandidates(await (await fetch('/api/candidates')).json()); }
  catch(e){}
}

(async () => {
  await loadFaults();
  tickState(); tickTelemetry(); tickCandidates(); tickFrame();
  setInterval(tickState, 500);
  setInterval(tickTelemetry, 300);
  setInterval(tickCandidates, 1000);
  // 画面 5fps：足够看出姿态与故障，又不至于把 CPU 吃满影响仿真本身。
  setInterval(tickFrame, 200);
})();
</script>
</body>
</html>
"""
