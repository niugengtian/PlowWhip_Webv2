HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Plow Whip · 无人值守控制台</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter,"PingFang SC","Microsoft YaHei",ui-sans-serif,system-ui,-apple-system,sans-serif;
      color:#dde4f8;background:#07090f;
      --bg:#07090f;--panel:#0f1219;--panel2:#141824;--line:#1c2236;--line2:#2b3450;
      --text:#dde4f8;--muted:#68799e;--ok:#2ecc8a;--warn:#f5c842;--danger:#ff5270;
      --accent:#6a9eff;--violet:#a07aff;
    }
    *{box-sizing:border-box} body{margin:0;min-width:320px;min-height:100vh;background:var(--bg)}
    button,input,textarea,select{font:inherit} button,select{cursor:pointer} button:disabled{cursor:not-allowed;opacity:.45}
    button{display:inline-flex;align-items:center;justify-content:center;gap:7px;border:1px solid var(--line2);border-radius:5px;padding:8px 11px;background:#141a29;color:#aebbe0;font-size:.76rem}
    button:hover:not(:disabled){border-color:#40517d;background:#192135;color:#eef2ff} button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
    button.primary{border-color:#527ed7;background:#315faa;color:white;font-weight:650} button.ghost{background:transparent} button.danger{border-color:rgba(255,82,112,.4);background:rgba(255,82,112,.06);color:#ff8298}
    input,textarea,select{width:100%;outline:0;border:1px solid var(--line2);border-radius:5px;padding:9px 10px;background:#0b0e15;color:#d9e1f5;font-size:.76rem}
    textarea{min-height:86px;resize:vertical;line-height:1.5} code,.mono,pre{font-family:"SFMono-Regular",Consolas,"Liberation Mono",monospace}
    .topbar{height:58px;display:grid;grid-template-columns:290px minmax(280px,1fr) auto;align-items:center;gap:24px;padding:0 clamp(18px,3vw,42px);border-bottom:1px solid var(--line);background:#0a0d14}
    .brand{display:flex;align-items:center;gap:11px}.brand-mark,.metric-icon,.list-icon{display:grid;place-items:center;width:34px;height:34px;border:1px solid #2d3e66;border-radius:7px;background:#111827;color:var(--accent);font-weight:800}
    .brand strong,.brand span{display:block}.brand strong{font-size:.92rem;letter-spacing:.02em}.brand span{margin-top:2px;color:var(--muted);font-size:.69rem}
    .principle{overflow:hidden;color:#8494b9;text-align:center;text-overflow:ellipsis;white-space:nowrap;font-size:.76rem}.top-status{display:flex;justify-content:flex-end;gap:14px}
    .status-dot{display:inline-flex;align-items:center;gap:6px;color:#8a9abc;font-size:.7rem;white-space:nowrap}.status-dot::before{width:7px;height:7px;border-radius:50%;background:#59647f;content:""}.status-dot.ok::before{background:var(--ok);box-shadow:0 0 0 3px rgba(46,204,138,.08)}
    .tabs{height:43px;display:flex;align-items:stretch;gap:3px;overflow-x:auto;padding:0 clamp(18px,3vw,42px);border-bottom:1px solid var(--line);background:#0b0e15}
    .tabs button{position:relative;border:0;padding:0 13px;background:transparent;color:#69799c;white-space:nowrap}.tabs button::after{position:absolute;right:10px;bottom:-1px;left:10px;height:2px;background:transparent;content:""}.tabs button:hover{background:transparent;color:#aebbe0}.tabs button.active{color:#e8edfc}.tabs button.active::after{background:var(--accent)}
    main{width:min(1400px,calc(100% - 36px));margin:0 auto;padding:18px 0 52px}.context-bar{min-height:42px;display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:14px}
    .scope-control,.context-actions{display:flex;align-items:center;gap:9px}.scope-control span,.context-copy{color:var(--muted);font-size:.7rem;white-space:nowrap}.scope-control select{width:auto;min-width:180px}.context-actions{flex-wrap:wrap;justify-content:flex-end}
    section[hidden]{display:none}.metrics-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px;margin-bottom:12px}.metric-card{min-height:74px;display:grid;grid-template-columns:38px minmax(0,1fr);align-items:center;gap:10px;border:1px solid var(--line);border-radius:6px;padding:12px;background:var(--panel)}
    .metric-icon{width:36px;height:36px}.metric-card span{display:block;color:#7b8caf;font-size:.68rem}.metric-card strong{display:block;margin-top:2px;color:#eff3ff;font-size:1.32rem;font-variant-numeric:tabular-nums}.metric-card small{color:#5f7095;font-size:.62rem}
    .layout{display:grid;grid-template-columns:minmax(280px,.75fr) minmax(480px,1.55fr);gap:12px}.stack{display:grid;align-content:start;gap:12px}.panel{min-width:0;border:1px solid var(--line);border-radius:7px;background:var(--panel)}
    .panel-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;padding:16px 18px;border-bottom:1px solid var(--line)}.panel-heading h1,.panel-heading h2{margin:4px 0 0;color:#edf1ff;font-size:1.02rem;letter-spacing:-.01em}.kicker{color:#62739a;font-size:.62rem;font-weight:700;letter-spacing:.13em;text-transform:uppercase}.muted{color:var(--muted);font-size:.7rem}
    .panel-body{padding:16px 18px}.field{display:grid;gap:6px;margin-bottom:12px}.field>span{color:#7182a7;font-size:.67rem;font-weight:650}.form-actions,.detail-actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px}.notice{min-height:19px;margin:10px 0 0;color:#f6d87d;font-size:.7rem}
    .table{overflow-x:auto}.table-head,.table-row{min-width:650px;display:grid;grid-template-columns:minmax(170px,1.4fr) minmax(110px,.75fr) minmax(110px,.75fr) minmax(150px,1fr);align-items:center;gap:12px;padding:10px 14px;border-bottom:1px solid var(--line)}
    .table-head{background:#0c1018;color:#58698f;font-size:.62rem;font-weight:700}.table-row{width:100%;border-width:0 0 1px;border-radius:0;background:transparent;color:#8d9dc0;text-align:left}.table-row:hover{background:#151c2c}.table-row strong,.table-row small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.table-row strong{color:#c5cfe7;font-size:.74rem}.table-row small{margin-top:3px;color:#596b91;font-size:.61rem}
    .status-pill{display:inline-flex;max-width:100%;overflow:hidden;border:1px solid #2d3853;border-radius:3px;padding:3px 6px;background:#161c2a;color:#8ea0c5;font-size:.62rem;text-overflow:ellipsis;white-space:nowrap}.status-done{border-color:rgba(46,204,138,.32);background:rgba(46,204,138,.08);color:#69dca9}.status-in_progress{border-color:rgba(106,158,255,.35);background:rgba(106,158,255,.08);color:#8bb1ff}.status-needs_decision{border-color:rgba(255,82,112,.34);background:rgba(255,82,112,.08);color:#ff8ba0}.status-pending{border-color:rgba(245,200,66,.32);background:rgba(245,200,66,.06);color:#ebcf76}
    .empty{padding:34px 18px;color:#4d5a78;text-align:center;font-size:.72rem}.search-result{display:grid;grid-template-columns:90px minmax(0,1fr) auto;gap:10px;padding:10px 0;border-bottom:1px solid var(--line);color:#8192b6;font-size:.69rem}.search-result strong{overflow-wrap:anywhere;color:#c8d2e9}.search-result code{color:#617398;font-size:.62rem}
    .page-heading{margin-bottom:12px}.page-heading h1{margin:0;color:#eef2ff;font-size:1.25rem}.page-heading p{margin:5px 0 0;color:#6d7da0;font-size:.72rem}
    .facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border-bottom:1px solid var(--line)}.facts>div{min-width:0;padding:13px 17px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}.facts>div:nth-child(3n){border-right:0}.facts dt{color:#5f7095;font-size:.63rem}.facts dd{overflow:hidden;margin:5px 0 0;color:#cdd6ee;text-overflow:ellipsis;white-space:nowrap;font-size:.73rem}
    .objective{margin:0;padding:17px 18px;color:#93a2c4;font-size:.8rem;line-height:1.65;overflow-wrap:anywhere}.timeline{padding:15px 18px}.timeline-row{display:grid;grid-template-columns:110px minmax(0,1fr) auto;gap:9px;padding:9px 0;border-bottom:1px solid var(--line);color:#7788ab;font-size:.67rem}.timeline-row strong{color:#b9c4df}.timeline-row code{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#65769a}
    .task-workspace{display:grid;grid-template-columns:minmax(260px,.78fr) minmax(420px,1.45fr) minmax(300px,1fr);align-items:start;gap:12px}.task-workspace>.panel{max-height:calc(100vh - 175px);overflow:auto}.section{padding:15px 18px;border-bottom:1px solid var(--line)}.section:last-child{border-bottom:0}.section h2{margin:0 0 10px;color:#d9e2f7;font-size:.82rem}.codebox{max-height:250px;overflow:auto;margin:0;border:1px solid #1d263b;border-radius:5px;padding:11px;background:#090d15;color:#94a6ca;font-size:.65rem;line-height:1.55;white-space:pre-wrap;overflow-wrap:anywhere}
    .session-card,.artifact-card{margin-top:8px;border:1px solid #26324d;border-radius:5px;padding:10px;background:#111725}.session-card:first-child,.artifact-card:first-child{margin-top:0}.session-card header,.artifact-card header{display:flex;align-items:center;justify-content:space-between;gap:10px}.session-card strong,.artifact-card strong{color:#dce5f8;font-size:.73rem}.session-card small,.artifact-card small{display:block;margin-top:5px;color:#617398;font-size:.61rem;overflow-wrap:anywhere}.artifact-card code{display:block;margin-top:6px;color:#788bae;font-size:.61rem;overflow-wrap:anywhere}
    .settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.setting-row{display:grid;grid-template-columns:minmax(150px,.8fr) minmax(0,1fr) 110px;gap:12px;padding:11px 18px;border-bottom:1px solid var(--line);color:#8d9dc0;font-size:.68rem}.setting-row strong{color:#c5cfe7}.setting-row code{overflow-wrap:anywhere;color:#8194bd}.library-card{margin:12px;border:1px solid #20283d;border-radius:6px;padding:13px;background:var(--panel2)}.library-card header{display:flex;align-items:center;justify-content:space-between;gap:10px}.library-card h2{margin:0;color:#e2e8fa;font-size:.82rem}.library-card code{display:block;margin-top:8px;color:#7789af;font-size:.61rem;overflow-wrap:anywhere}.library-card small{color:#617398}.ok-text{color:#69dca9!important}.warning-text{color:#f6d87d!important}
    @media(max-width:1050px){.topbar{grid-template-columns:250px 1fr}.principle{display:none}.metrics-strip{grid-template-columns:repeat(2,1fr)}.task-workspace{grid-template-columns:1fr 1fr}.task-workspace>.panel:last-child{grid-column:1/-1;max-height:none}.settings-grid{grid-template-columns:1fr}}
    @media(max-width:760px){.topbar{height:auto;grid-template-columns:1fr;padding-top:12px;padding-bottom:12px}.top-status{justify-content:flex-start}.context-bar{align-items:flex-start;flex-direction:column}.context-actions{justify-content:flex-start}.layout,.task-workspace{grid-template-columns:1fr}.task-workspace>.panel{max-height:none}.task-workspace>.panel:last-child{grid-column:auto}.facts{grid-template-columns:repeat(2,1fr)}.facts>div:nth-child(3n){border-right:1px solid var(--line)}.facts>div:nth-child(2n){border-right:0}.timeline-row{grid-template-columns:90px 1fr}.timeline-row time{grid-column:2}.settings-grid{grid-template-columns:1fr}}
    @media(max-width:520px){main{width:min(100% - 22px,1400px)}.metrics-strip{grid-template-columns:1fr}.scope-control{width:100%}.scope-control select{min-width:0}.context-actions{width:100%}.facts{grid-template-columns:1fr}.facts>div{border-right:0!important}.setting-row{grid-template-columns:1fr}.top-status{gap:9px;flex-wrap:wrap}}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand"><div class="brand-mark">PW</div><div><strong>Plow Whip</strong><span>无人值守控制台</span></div></div>
    <div class="principle">一条主线，一个推进器，Evidence 决定终态。</div>
    <div class="top-status"><span class="status-dot" id="health-status">控制面连接中</span><span class="status-dot ok">SQLite WAL</span><span class="status-dot ok">Cronner 应用内</span></div>
  </header>
  <nav class="tabs" aria-label="主导航">
    <button data-view="home" class="active" aria-current="page">全局</button>
    <button data-view="project" aria-current="false">项目</button>
    <button data-view="task" aria-current="false">Task</button>
    <button data-view="settings" aria-current="false">设置与资源库</button>
  </nav>
  <main>
    <div class="context-bar">
      <label class="scope-control"><span>项目范围</span><select id="project-scope"><option value="">全部项目</option></select></label>
      <div class="context-actions"><span class="context-copy" id="context-copy">读取权威状态</span><button class="ghost" id="refresh">刷新</button></div>
    </div>

    <section id="home">
      <div class="metrics-strip">
        <div class="metric-card"><div class="metric-icon">P</div><div><span>项目</span><strong id="metric-projects">0</strong><small>SQLite 权威记录</small></div></div>
        <div class="metric-card"><div class="metric-icon">→</div><div><span>进行中</span><strong id="metric-active">0</strong><small>pending / in_progress</small></div></div>
        <div class="metric-card"><div class="metric-icon">✓</div><div><span>已完成</span><strong id="metric-done">0</strong><small>Evidence 已通过</small></div></div>
        <div class="metric-card"><div class="metric-icon">!</div><div><span>待决定</span><strong id="metric-decision">0</strong><small>自动推进已停手</small></div></div>
      </div>
      <div class="layout">
        <div class="stack">
          <article class="panel">
            <header class="panel-heading"><div><span class="kicker">Messages Intake</span><h1>提交指令</h1></div><span class="status-pill status-pending">进入 SQLite</span></header>
            <form class="panel-body" id="message-form">
              <label class="field"><span>项目 ID</span><input id="project-id" required pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,63}" placeholder="project-alpha"></label>
              <label class="field"><span>确定性指令</span><textarea id="message" required placeholder="写入 result.txt: 闭环完成"></textarea></label>
              <div class="form-actions"><button class="primary" type="submit">进入队列</button></div>
              <p class="notice" id="home-notice" role="status"></p>
            </form>
          </article>
          <article class="panel">
            <header class="panel-heading"><div><span class="kicker">Exact Search</span><h2>跨项目搜索</h2></div><span class="muted">不调用模型</span></header>
            <form class="panel-body" id="search-form"><label class="field"><span>Task / Message / Artifact</span><input id="search" required maxlength="128" placeholder="result.txt"></label><button type="submit">精确搜索</button></form>
            <div class="section" id="search-results"><div class="empty">输入索引内容开始搜索。</div></div>
          </article>
        </div>
        <article class="panel">
          <header class="panel-heading"><div><span class="kicker">Canonical Projects</span><h1>项目与当前 Task</h1></div><span class="muted" id="project-count">0 项</span></header>
          <div class="table"><div class="table-head"><span>项目</span><span>公开状态</span><span>内部阶段</span><span>最近更新</span></div><div id="project-list"><div class="empty">正在读取…</div></div></div>
        </article>
      </div>
    </section>

    <section id="project" hidden>
      <div class="page-heading"><h1 id="project-title">项目</h1><p>当前 Task、四态、内部 phase 与最近 20 条事件。</p></div>
      <div id="project-detail"><article class="panel"><div class="empty">请从全局首页选择项目。</div></article></div>
    </section>

    <section id="task" hidden>
      <div class="page-heading"><h1 id="task-title">Task</h1><p>执行角色与 Checker 独立；Monitor 只读展示权威事实和有界输出。</p></div>
      <div class="task-workspace">
        <article class="panel"><header class="panel-heading"><div><span class="kicker">Task State</span><h2>状态</h2></div><span id="task-status" class="status-pill">—</span></header><dl class="facts" id="task-facts"></dl><p class="objective" id="task-objective">请先选择 Task。</p><div class="section"><h2>明确操作</h2><div class="detail-actions"><button class="danger" id="cancel-task">取消</button><button id="rerun-task">重新执行</button></div><form id="decision-form"><label class="field"><span>提供决定</span><textarea id="decision" placeholder="写入 result.txt: 修订内容"></textarea></label><button type="submit">提交决定</button></form><form id="plan-form"><label class="field"><span>更新计划（JSON）</span><textarea id="plan" placeholder='{"alternatives":[],"selected":0,"tasks":[]}'></textarea></label><button type="submit">提交计划</button></form><p class="notice" id="task-notice" role="status"></p></div></article>
        <article class="panel"><header class="panel-heading"><div><span class="kicker">Evidence Trail</span><h2>Artifact / Evidence / Handoff</h2></div><span class="muted">文件为真源</span></header><div class="section" id="task-evidence"><div class="empty">—</div></div><div class="section"><h2>最后 20 行</h2><pre class="codebox" id="task-output">—</pre></div><div class="section"><h2>最近事件</h2><div id="task-events"><div class="empty">—</div></div></div></article>
        <article class="panel"><header class="panel-heading"><div><span class="kicker">Bound Sessions</span><h2>角色 / Provider / Generation</h2></div><span class="muted">Task 级身份</span></header><div class="section" id="task-sessions"><div class="empty">—</div></div></article>
      </div>
    </section>

    <section id="settings" hidden>
      <div class="page-heading"><h1>设置与资源库</h1><p>TaskSession 创建时冻结实际 SHA 与生效来源；此页只读。</p></div>
      <div class="settings-grid"><article class="panel"><header class="panel-heading"><div><span class="kicker">Effective Values</span><h2>设置</h2></div><span class="muted">来源可追溯</span></header><div id="settings-data"><div class="empty">正在读取…</div></div></article><article class="panel"><header class="panel-heading"><div><span class="kicker">File Sources</span><h2>资源库</h2></div><span class="muted">SHA-256 校验</span></header><div id="library-data"><div class="empty">正在读取…</div></div></article></div>
    </section>
  </main>
  <script>
    const views=[...document.querySelectorAll('main>section')];
    const nav=[...document.querySelectorAll('.tabs button')];
    let projects=[],currentProject=null,currentTask=null;
    const $=selector=>document.querySelector(selector);
    const key=()=>globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random()}`;
    const labels={pending:'待执行',in_progress:'进行中',done:'已完成',needs_decision:'待决定',cancelled:'已取消'};
    const label=value=>labels[value]||value||'—';
    const time=value=>value?new Date(value*1000).toLocaleString('zh-CN',{hour12:false}):'—';
    const clear=node=>{while(node.firstChild)node.firstChild.remove()};
    const node=(tag,className,text)=>{const element=document.createElement(tag);if(className)element.className=className;if(text!==undefined)element.textContent=text;return element};
    const status=value=>{const element=node('span',`status-pill status-${value||''}`,label(value));return element};
    const parse=value=>{try{return JSON.parse(value)}catch{return value}};
    const show=name=>{views.forEach(view=>view.hidden=view.id!==name);nav.forEach(button=>{const active=button.dataset.view===name;button.classList.toggle('active',active);button.setAttribute('aria-current',active?'page':'false')});if(name==='settings')loadSettings()};
    nav.forEach(button=>button.addEventListener('click',()=>show(button.dataset.view)));
    async function api(path,options){const response=await fetch(path,options);const body=await response.json();if(!response.ok)throw new Error(body.error||`HTTP ${response.status}`);return body}
    function fact(term,value){const box=node('div');box.append(node('dt','',term),node('dd','mono',String(value??'—')));return box}
    function eventRows(target,events){clear(target);if(!events?.length){target.append(node('div','empty','还没有事件。'));return}events.forEach(event=>{const row=node('div','timeline-row');row.append(node('strong','',event.kind),node('code','',event.detail_json||'{}'),node('time','',time(event.created_at)));target.append(row)})}
    function renderMetrics(){const statuses=projects.map(item=>item.outcome||item.public_status);$('#metric-projects').textContent=projects.length;$('#metric-active').textContent=statuses.filter(item=>item==='pending'||item==='in_progress').length;$('#metric-done').textContent=statuses.filter(item=>item==='done').length;$('#metric-decision').textContent=statuses.filter(item=>item==='needs_decision').length}
    function renderScope(){const select=$('#project-scope');const selected=currentProject||'';clear(select);const all=node('option','','全部项目');all.value='';select.append(all);projects.forEach(project=>{const option=node('option','',project.project_id);option.value=project.project_id;select.append(option)});select.value=projects.some(item=>item.project_id===selected)?selected:''}
    async function loadHealth(){try{const health=await api('/health');$('#health-status').classList.toggle('ok',health.status==='ok');$('#health-status').textContent=health.status==='ok'?'控制面在线':'控制面异常'}catch{$('#health-status').classList.remove('ok');$('#health-status').textContent='控制面离线'}}
    async function loadProjects(){const box=$('#project-list');try{const data=await api('/api/projects');projects=data.projects||[];renderMetrics();renderScope();$('#project-count').textContent=`${projects.length} 项`;clear(box);if(!projects.length){box.append(node('div','empty','还没有项目。提交第一条指令即可创建。'));return}projects.forEach(project=>{const button=node('button','table-row');const identity=node('span');identity.append(node('strong','',project.project_id),node('small','mono',project.task_id||'等待 Task'));button.append(identity,status(project.outcome||project.public_status),node('span','mono',project.phase||'—'),node('span','',time(project.updated_at)));button.addEventListener('click',()=>openProject(project.project_id));box.append(button)})}catch(error){clear(box);box.append(node('div','empty',error.message))}}
    function projectView(data){const task=data.task;const wrapper=node('div','layout');const summary=node('article','panel');const head=node('header','panel-heading');const heading=node('div');heading.append(node('span','kicker','Current Task'),node('h2','',task?'执行摘要':'暂无 Task'));head.append(heading,status(task?.outcome||task?.public_status));summary.append(head);if(!task){summary.append(node('div','empty','消息尚未生成 Task。'));wrapper.append(summary);return wrapper}const facts=node('dl','facts');facts.append(fact('Task ID',task.id),fact('公开状态',label(task.public_status)),fact('内部阶段',task.phase),fact('Spec Revision',task.spec_revision),fact('Retry',task.retry_count),fact('Outcome',label(task.outcome)));summary.append(facts);const spec=parse(task.spec_json)||{};summary.append(node('p','objective',spec.instruction||spec.content||JSON.stringify(spec)));const actions=node('div','section');const open=node('button','primary','查看 Task 证据');open.addEventListener('click',()=>openTask(task.id));actions.append(open);summary.append(actions);const timeline=node('article','panel');const timelineHead=node('header','panel-heading');const timelineTitle=node('div');timelineTitle.append(node('span','kicker','Latest 20'),node('h2','','事件时间线'));timelineHead.append(timelineTitle,node('span','muted','Monitor 只读'));timeline.append(timelineHead);const rows=node('div','timeline');eventRows(rows,data.events);timeline.append(rows);wrapper.append(summary,timeline);return wrapper}
    async function openProject(id){currentProject=id;$('#project-scope').value=id;show('project');$('#project-title').textContent=id;$('#context-copy').textContent=`项目 ${id}`;const box=$('#project-detail');clear(box);box.append(node('article','panel empty','正在读取…'));try{const data=await api(`/api/projects/${encodeURIComponent(id)}`);clear(box);box.append(projectView(data));currentTask=data.task?.id||null}catch(error){clear(box);box.append(node('article','panel empty',error.message))}}
    function renderArtifacts(data){const box=$('#task-evidence');clear(box);const items=[...(data.artifacts||[]),...(data.handoffs||[]).map(item=>({...item,kind:'handoff'}))];if(!items.length){box.append(node('div','empty','尚未产生 Artifact 或 Evidence。'));return}items.forEach(item=>{const card=node('article','artifact-card');const head=node('header');head.append(node('strong','',item.kind),node('span',item.kind==='evidence'?'status-pill status-done':'status-pill',`rev ${item.revision}`));card.append(head,node('code','',item.path),node('small','mono',`sha256 ${item.sha256}`));box.append(card)})}
    function renderSessions(data){const box=$('#task-sessions');clear(box);if(!data.sessions?.length){box.append(node('div','empty','尚未创建 TaskSession。'));return}data.sessions.forEach(session=>{const usage=(data.model_usage||[]).find(item=>item.task_session_id===session.task_session_id)?.normalized_total||0;const card=node('article','session-card');const head=node('header');head.append(node('strong','',session.role_key),status(session.status==='archived'?'done':session.status));card.append(head,node('small','mono',`${session.provider_key} / ${session.model||'—'}`),node('small','mono',`generation ${session.generation} · normalized ${usage}`));box.append(card)})}
    async function openTask(id){currentTask=id;show('task');$('#task-title').textContent=id;$('#task-notice').textContent='';try{const data=await api(`/api/tasks/${encodeURIComponent(id)}`);if(!data.task)throw new Error('Task 不存在');currentProject=data.project_id;renderScope();$('#context-copy').textContent=`${data.project_id} / ${id}`;const task=data.task;$('#task-status').className=`status-pill status-${task.outcome||task.public_status||''}`;$('#task-status').textContent=label(task.outcome||task.public_status);const facts=$('#task-facts');clear(facts);facts.append(fact('Project',task.project_id),fact('Phase',task.phase),fact('Spec Rev',task.spec_revision),fact('Role',task.role_key),fact('Checker',task.checker_role_key),fact('Retry',task.retry_count));const spec=parse(task.spec_json)||{};$('#task-objective').textContent=spec.instruction||spec.content||JSON.stringify(spec);$('#task-output').textContent=(data.last_output||[]).join('\n')||'无输出';renderArtifacts(data);renderSessions(data);eventRows($('#task-events'),data.events)}catch(error){$('#task-notice').textContent=error.message}}
    async function loadSettings(){try{const data=await api('/api/settings-library');const settings=$('#settings-data');clear(settings);if(!data.settings.length)settings.append(node('div','empty','没有设置。'));data.settings.forEach(item=>{const row=node('div','setting-row');row.append(node('strong','',item.setting_key),node('code','',JSON.stringify(item.value)),node('span','',item.source));settings.append(row)});const library=$('#library-data');clear(library);if(!data.library.length)library.append(node('div','empty','资源库为空。'));data.library.forEach(item=>{const card=node('article','library-card');const head=node('header');head.append(node('h2','',`${item.kind} / ${item.item_key}`),node('small',item.sha256_matches?'ok-text':'warning-text',item.sha256_matches?'SHA 匹配':'SHA 不匹配'));card.append(head,node('code','',item.path),node('small','mono',`revision ${item.revision} · ${item.sha256}`));library.append(card)})}catch(error){const box=$('#settings-data');clear(box);box.append(node('div','empty',error.message))}}
    async function postAction(kind,instruction='',plan){if(!currentTask||!currentProject)throw new Error('请先选择项目和 Task');return api('/api/actions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:currentProject,task_id:currentTask,kind,instruction,plan,idempotency_key:key()})})}
    $('#project-scope').addEventListener('change',event=>event.target.value?openProject(event.target.value):(currentProject=null,show('home')));
    $('#refresh').addEventListener('click',async()=>{const button=$('#refresh');button.disabled=true;button.textContent='刷新中';await Promise.all([loadHealth(),loadProjects()]);if(currentTask&&!$('#task').hidden)await openTask(currentTask);else if(currentProject&&!$('#project').hidden)await openProject(currentProject);button.disabled=false;button.textContent='刷新'});
    $('#message-form').addEventListener('submit',async event=>{event.preventDefault();const notice=$('#home-notice');try{const project=$('#project-id').value;await api('/api/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:project,content:$('#message').value,idempotency_key:key()})});notice.textContent='已进入 SQLite 队列；Cronner 将自动推进。';currentProject=project;await loadProjects()}catch(error){notice.textContent=error.message}});
    $('#search-form').addEventListener('submit',async event=>{event.preventDefault();const box=$('#search-results');clear(box);try{const data=await api(`/api/search?q=${encodeURIComponent($('#search').value)}`);if(!data.results?.length){box.append(node('div','empty','没有匹配结果。'));return}data.results.forEach(item=>{const row=node('div','search-result');row.append(node('span','status-pill',item.kind),node('strong','',item.detail||item.ref),node('code','',`${item.project_id||''} ${item.status||''}`));box.append(row)})}catch(error){box.append(node('div','empty',error.message))}});
    $('#cancel-task').addEventListener('click',async()=>{try{await postAction('cancel');$('#task-notice').textContent='取消已入队。'}catch(error){$('#task-notice').textContent=error.message}});
    $('#rerun-task').addEventListener('click',async()=>{try{await postAction('rerun');$('#task-notice').textContent='重新执行已入队。'}catch(error){$('#task-notice').textContent=error.message}});
    $('#decision-form').addEventListener('submit',async event=>{event.preventDefault();try{await postAction('provide_decision',$('#decision').value);$('#task-notice').textContent='决定已入队。'}catch(error){$('#task-notice').textContent=error.message}});
    $('#plan-form').addEventListener('submit',async event=>{event.preventDefault();try{await postAction('provide_plan','',JSON.parse($('#plan').value));$('#task-notice').textContent='计划已入队。'}catch(error){$('#task-notice').textContent=error.message}});
    loadHealth();loadProjects();
  </script>
</body>
</html>
"""
